"""Строгая stdlib-проверка JSON-ответа модели по схеме и повтор-подсказка (WP11-C1; 07b п. 5.2).

extract_json(text) -> (obj|None, код|None): первый сбалансированный {...} (<= 8 КБ), дубликаты ключей и NaN/Infinity запрещены.
validate(schema, value) -> список кодов `ТИП:путь` (BAD_TYPE, BAD_ENUM, BAD_CONST, MISSING, EXTRA_KEY, TOO_SHORT, TOO_LONG, BAD_PATTERN, RANGE,
TOO_FEW, TOO_MANY, NOT_FINITE, NO_MATCH). Поддержано: type (в т. ч. список), enum, const, minLength/maxLength, pattern, minimum/maximum,
properties/required/additionalProperties (по умолчанию false), items, minItems/maxItems, anyOf. Целое != bool.
ask_json(call, req, schema, retries=2) -> JsonResult: до `retries` повторов с подсказкой `КОДЫ: ...` (без цитат ответа), затем ok=False («нет действия»).
"""
import json
import math
import re

from tb.llm.types import Usage, assistant, user

MAX_JSON_BYTES = 8192
MAX_ERRORS = 8
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?|\n?\s*```\s*$")


def _pairs(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("DUP_KEY")
        d[k] = v
    return d


def _no_const(_c):
    raise ValueError("NOT_FINITE")


def strict_loads(s):
    return json.loads(s, object_pairs_hook=_pairs, parse_constant=_no_const)


def _balanced_end(text, start):
    """Индекс закрывающей скобки первого объекта или -1 (обрыв)."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def extract_json(text, max_bytes=MAX_JSON_BYTES):
    """-> (obj, None) или (None, код): NO_JSON, TRUNCATED, TOO_BIG, DUP_KEY, NOT_FINITE, BAD_JSON."""
    if not isinstance(text, str) or not text.strip():
        return None, "EMPTY"
    t = _FENCE.sub("", text.strip()) if text.strip().startswith("```") else text
    s = t.find("{")
    if s < 0:
        return None, "NO_JSON"
    e = _balanced_end(t, s)
    if e < 0:
        return None, "TRUNCATED"
    frag = t[s:e + 1]
    if len(frag.encode("utf-8")) > max_bytes:
        return None, "TOO_BIG"
    try:
        obj = strict_loads(frag)
    except ValueError as ex:
        m = str(ex)
        return None, m if m in ("DUP_KEY", "NOT_FINITE") else "BAD_JSON"
    return (obj, None) if isinstance(obj, dict) else (None, "NO_JSON")


def _type_ok(t, v):
    if t == "string":
        return isinstance(v, str)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "boolean":
        return isinstance(v, bool)
    if t == "object":
        return isinstance(v, dict)
    if t == "array":
        return isinstance(v, list)
    if t == "null":
        return v is None
    return False


def validate(schema, value, path="$", errs=None):
    errs = [] if errs is None else errs

    def bad(code):
        if len(errs) < MAX_ERRORS:
            errs.append("%s:%s" % (code, path))

    if len(errs) >= MAX_ERRORS or not isinstance(schema, dict):
        return errs
    if "anyOf" in schema:
        for sub in schema["anyOf"]:
            if not validate(sub, value, path, []):
                break
        else:
            bad("NO_MATCH")
        return errs
    t = schema.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_type_ok(x, value) for x in ts):
            bad("BAD_TYPE")
            return errs
    if isinstance(value, float) and not math.isfinite(value):
        bad("NOT_FINITE")
        return errs
    if "enum" in schema and value not in schema["enum"]:
        bad("BAD_ENUM")
    if "const" in schema and value != schema["const"]:
        bad("BAD_CONST")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            bad("TOO_SHORT")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            bad("TOO_LONG")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            bad("BAD_PATTERN")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            bad("RANGE")
        if "maximum" in schema and value > schema["maximum"]:
            bad("RANGE")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            bad("TOO_FEW")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            bad("TOO_MANY")
        if isinstance(schema.get("items"), dict):
            for i, x in enumerate(value):
                validate(schema["items"], x, "%s[%d]" % (path, i), errs)
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for k in schema.get("required") or []:
            if k not in value:
                errs.append("MISSING:%s.%s" % (path, k)) if len(errs) < MAX_ERRORS else None
        for k, v in value.items():
            if k in props:
                validate(props[k], v, "%s.%s" % (path, k), errs)
            elif schema.get("additionalProperties", False) is False:
                errs.append("EXTRA_KEY:%s.%s" % (path, str(k)[:30])) if len(errs) < MAX_ERRORS else None
            elif isinstance(schema.get("additionalProperties"), dict):
                validate(schema["additionalProperties"], v, "%s.%s" % (path, k), errs)
    return errs


def check_text(text, schema, max_bytes=MAX_JSON_BYTES):
    """Текст ответа -> (obj|None, [коды])."""
    obj, code = extract_json(text, max_bytes)
    if code:
        return None, [code]
    errs = validate(schema, obj)
    return (obj, []) if not errs else (None, errs)


def hint(codes):
    """Подсказка для повторного запроса: только коды ошибок, без цитат ответа."""
    return "Ответ не принят. КОДЫ: %s. Верни только один JSON-объект по схеме, без текста до и после." % "; ".join(codes[:MAX_ERRORS])


class JsonResult(object):
    def __init__(self, ok, obj, errors, resp, attempts, usage):
        self.ok, self.obj, self.errors, self.resp, self.attempts, self.usage = ok, obj, errors, resp, attempts, usage

    @property
    def action(self):
        return "ok" if self.ok else "none"


def ask_json(call, req, schema, retries=2):
    """Вызов модели до валидного JSON. call(req) -> Resp (LLMClient.complete или фейк). Ошибки LLMError пробрасываются.
    Невалидный ответ: до `retries` повторов с подсказкой; затем JsonResult(ok=False) = «нет действия»."""
    usage, errors, resp = Usage(), [], None
    msgs = list(req.messages)
    for attempt in range(retries + 1):
        r = req.copy(messages=msgs)
        resp = call(r)
        usage = usage.add(resp.usage)
        text = resp.text
        obj, errors = check_text(text, schema)
        if resp.finish == "length" and errors and errors[0] != "TRUNCATED":
            errors = ["TRUNCATED"] + errors
        if not errors:
            return JsonResult(True, obj, [], resp, attempt + 1, usage)
        note = getattr(call, "note_format_error", None)
        if note:
            note(resp, errors)
        msgs = msgs + [assistant(text[:400] if text else "(пусто)"), user(hint(errors))]
    return JsonResult(False, None, errors, resp, retries + 1, usage)
