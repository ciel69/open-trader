"""Общая часть адаптеров: HTTP-запрос через transport, разбор статусов, чтение ключа, пробный запрос.

Подкласс реализует build(req) -> (url, headers_extra, body_dict), parse(data, req) -> Resp, auth_headers(secret), probe_url(), probe_parse().
Ключ живёт только в локальных переменных complete()/probe(); в исключения и объекты не сохраняется.
"""
import json
import time

from tb.llm import keys
from tb.llm.transport import Transport
from tb.llm.types import LLMError, Req, Resp, user


def strict_compatible(schema):
    """Схема годится для strict: у каждого объекта additionalProperties=false и все свойства в required."""
    if not isinstance(schema, dict):
        return False
    if schema.get("type") == "object":
        props = schema.get("properties") or {}
        if schema.get("additionalProperties") is not False or sorted(schema.get("required") or []) != sorted(props):
            return False
        if not all(strict_compatible(v) for v in props.values()):
            return False
    if schema.get("type") == "array" and "items" in schema and not strict_compatible(schema["items"]):
        return False
    return True


def json_tool_args(raw):
    """Строка аргументов инструмента -> (dict, ошибка|None). Усечённый/некорректный JSON -> ({}, 'bad_json')."""
    if isinstance(raw, dict):
        return raw, None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return {}, "bad_json"
    return (v, None) if isinstance(v, dict) else ({}, "not_object")


class Adapter(object):
    kind = "base"

    def __init__(self, provider, model, transport=None, environ=None):
        self.provider, self.model = provider, model
        self.transport = transport or Transport()
        self.environ = environ

    # ---- переопределяется ----
    def build(self, req):
        raise NotImplementedError

    def parse(self, data, req):
        raise NotImplementedError

    def auth_headers(self, secret):
        return {}

    def probe_request(self):
        """-> (url, headers_extra) для GET списка моделей."""
        return self.provider.base_url + "/models", {}

    def probe_names(self, data):
        return [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]

    # ---- общее ----
    def _secret(self):
        return keys.load(self.provider.key_file, self.provider.key_env, self.environ)

    def _headers(self, secret, extra):
        h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "tb-llm/1"}
        h.update(extra or {})
        h.update(self.auth_headers(secret))
        return h

    def _error(self, status, body, secret, retries):
        msg = ""
        try:
            d = json.loads(body.decode("utf-8", "replace"))
            e = d.get("error") if isinstance(d, dict) else None
            msg = e.get("message") if isinstance(e, dict) else (e if isinstance(e, str) else "")
        except (ValueError, AttributeError):
            pass
        msg = keys.redact(msg or "", secret, 200)
        code = {400: "bad_request", 401: "auth", 403: "auth", 402: "billing", 404: "bad_request", 422: "invalid_params", 429: "rate"}.get(status)
        if code is None:
            code = "server" if status >= 500 else "bad_request"
        return LLMError(code, "HTTP %d%s" % (status, ": " + msg if msg else ""), status=status, retries=retries)

    def complete(self, req):
        secret = self._secret()
        url, extra, body = self.build(req)
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        t0 = time.time()
        try:
            rep = self.transport.request("POST", url, self._headers(secret, extra), raw, self.provider.timeouts, self.provider.retry, total_s=req.timeout_s)
        except LLMError:
            raise
        if rep.status < 200 or rep.status >= 300:
            raise self._error(rep.status, rep.body, secret, rep.retries)
        text = rep.body.decode("utf-8", "replace").strip()
        if not text:
            raise LLMError("empty", "пустое тело ответа", status=rep.status, retries=rep.retries)
        try:
            data = json.loads(text)
        except ValueError:
            raise LLMError("bad_body", "тело ответа не JSON", status=rep.status, retries=rep.retries)
        if not isinstance(data, dict):
            raise LLMError("bad_body", "тело ответа не объект", status=rep.status, retries=rep.retries)
        try:
            resp = self.parse(data, req)
        except LLMError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            raise LLMError("bad_body", "неожиданная структура ответа", status=rep.status, retries=rep.retries)
        resp.latency_ms = int((time.time() - t0) * 1000)
        resp.retries = rep.retries
        return resp

    def probe(self, ping=True):
        """Проверка на живом ключе: GET списка моделей (есть ли имя из конфига) и, если ping, минимальный запрос. -> dict без секретов."""
        secret = self._secret()
        url, extra = self.probe_request()
        out = {"model_name_in_list": None, "models_count": None, "ping": None}
        hdrs = self._headers(secret, extra)
        hdrs.pop("Content-Type", None)
        rep = self.transport.request("GET", url, hdrs, None, self.provider.timeouts, {"max": 0})
        if 200 <= rep.status < 300:
            try:
                names = self.probe_names(json.loads(rep.body.decode("utf-8", "replace")))
                out["models_count"] = len(names)
                out["model_name_in_list"] = self.model.name in names
            except (ValueError, AttributeError, TypeError):
                out["models_count"] = None
        else:
            out["models_status"] = self._error(rep.status, rep.body, secret, 0).code
        if ping:
            r = self.complete(Req(system="Ответь одним JSON-объектом.", messages=[user('Верни {"ok":true}')], max_tokens=64, temperature=0,
                                  response_format="json" if self.model.json_mode else None))
            out["ping"] = {"finish": r.finish, "in": r.usage.inp, "in_hit": r.usage.hit, "out": r.usage.out, "reasoning": r.usage.reasoning,
                           "lat_ms": r.latency_ms, "text_len": len(r.text)}
        return out
