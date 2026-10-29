"""Цикл вызовов инструментов шлюза (WP11-C1).

run(call, req, run_tool, ...) -> LoopResult. call(req) -> Resp (LLMClient или фейк); run_tool(name, args) -> dict (шлюз: gateway_runner()).
Защиты: лимит раундов (на последнем раунде tool_choice=none, чтобы получить итог), детектор повторов одинаковых вызовов (одинаковое имя+аргументы
исполняется не более `repeat_limit` раз; сверх лимита вызов не исполняется, модель получает REPEAT; `loop_hits` >= `loop_abort` -> status "loop"),
пустой ответ (одна подсказка, затем "empty"), усечённый ответ/аргументы (finish=length: подсказка, затем "truncated"), неизвестный инструмент
(не исполняется), проверка итогового JSON по схеме с подсказкой (format_retries), параллельные вызовы исполняются последовательно, на КАЖДЫЙ tool_call
есть ответ. `check(obj) -> [коды]` (необязательно) добавляет смысловую проверку к схеме: коды уходят в подсказку повтора. reasoning_content/thinking-блоки сохраняются в истории (Resp.message) и уходят провайдеру адаптером.
status: ok | empty | truncated | loop | max_rounds | format | cancelled. LLMError из call пробрасывается (решает fallback раннер).
"""
import json

from tb.llm import schema as schema_mod
from tb.llm.types import Usage, tool_result, user

MAX_RESULT_CHARS = 12000


class LoopResult(object):
    def __init__(self):
        self.status, self.text, self.obj, self.errors = "ok", "", None, []
        self.messages, self.rounds, self.calls, self.usage, self.resp = [], 0, [], Usage(), None
        self.loop_hits = 0

    @property
    def ok(self):
        return self.status == "ok"


def _canon(args):
    try:
        return json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    except (TypeError, ValueError):
        return repr(args)


def _result_text(res, limit):
    try:
        s = json.dumps(res, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        s = json.dumps({"ok": False, "error": "RESULT_NOT_JSON"})
    if len(s) > limit:
        s = s[:limit] + '…[обрезано, всего %d симв.]' % len(s)
    return s


def run(call, req, run_tool, max_rounds=8, repeat_limit=2, loop_abort=2, schema=None, format_retries=2, empty_retries=1, truncated_retries=1,
        max_result_chars=MAX_RESULT_CHARS, should_stop=None, check=None):
    res = LoopResult()
    msgs = list(req.messages)
    names = {t["name"] for t in req.tools}
    seen = {}
    fmt = empty = trunc = 0
    for rnd in range(1, max_rounds + 1):
        if should_stop and should_stop():
            res.status = "cancelled"
            break
        last = rnd == max_rounds
        r = req.copy(messages=msgs)
        r.meta = dict(req.meta, round=rnd)
        if last and names:
            r.tool_choice = "none"
        resp = call(r)
        res.rounds, res.resp = rnd, resp
        res.usage = res.usage.add(resp.usage)
        msgs.append(resp.message)
        if resp.tool_calls and not (last and names):
            bad = 0
            for tc in resp.tool_calls:
                if tc.error:                     # усечённые/некорректные аргументы
                    bad += 1
                    out = {"ok": False, "error": "BAD_ARGS", "msg": "аргументы не JSON (возможно, ответ усечён): повтори вызов короче"}
                elif tc.name not in names:
                    out = {"ok": False, "error": "UNKNOWN_TOOL", "msg": "инструмент недоступен"}
                else:
                    key = (tc.name, _canon(tc.args))
                    seen[key] = seen.get(key, 0) + 1
                    if seen[key] > repeat_limit:
                        res.loop_hits += 1
                        out = {"ok": False, "error": "REPEAT", "msg": "тот же вызов уже выполнен, повтор не нужен: используй полученный результат или дай итог"}
                    else:
                        try:
                            out = run_tool(tc.name, tc.args)
                        except Exception as e:      # текст исключения наружу (в модель) не отдаём
                            out = {"ok": False, "error": "TOOL_FAILED", "msg": e.__class__.__name__}
                res.calls.append({"name": tc.name, "ok": isinstance(out, dict) and out.get("ok") is True, "round": rnd})
                msgs.append(tool_result(tc.id, tc.name, _result_text(out, max_result_chars)))
            if res.loop_hits >= loop_abort:
                res.status = "loop"
                break
            if bad and resp.finish == "length":
                trunc += 1
                if trunc > truncated_retries:
                    res.status = "truncated"
                    break
            continue
        # ---- ответ без вызовов (или лимит раундов исчерпан) ----
        if resp.tool_calls:                      # на последнем раунде модель всё равно просит инструменты
            res.status = "max_rounds"
            break
        if not resp.text.strip():
            empty += 1
            if empty > empty_retries:
                res.status = "empty"
                break
            msgs.append(user("Ответ пуст. Дай итоговый ответ в требуемом формате."))
            continue
        if schema is not None:
            obj, errs = schema_mod.check_text(resp.text, schema)
            if resp.finish == "length" and errs and errs[0] != "TRUNCATED":
                errs = ["TRUNCATED"] + errs
            if not errs and check is not None:         # смысловая проверка роли (WP11-C3): те же коды и повтор, что для ошибок схемы
                errs = list(check(obj) or [])
            if errs:
                note = getattr(call, "note_format_error", None)
                if note:
                    note(resp, errs)
                fmt += 1
                res.errors = errs
                if fmt > format_retries:
                    res.status = "format"
                    res.text = resp.text
                    break
                msgs.append(user(schema_mod.hint(errs)))
                continue
            res.obj, res.errors = obj, []
        elif resp.finish == "length":
            res.status = "truncated"
            res.text = resp.text
            break
        res.status, res.text = "ok", resp.text
        break
    else:
        res.status = "max_rounds"
    res.messages = msgs
    return res


# ---------------------------------------------------------------- связка со шлюзом (WP11-A)
def gateway_tools(agent_id, names=None, root=None):
    """Нейтральные описания инструментов, доступных агенту в шлюзе (names — сужение ролью: инструменты вне списка модели не показываются)."""
    from tb.gateway import rpc
    out = []
    for t in rpc.tools_for(agent_id, root=root):
        if names is None or t.name in names:
            out.append({"name": t.name, "description": t.desc, "schema": t.input_schema()})
    return out


def gateway_runner(agent_id, root=None, cancel=None):
    """-> run_tool(name, args) -> dict: вызов инструмента шлюза от имени агента (права, лимиты и аудит шлюза действуют)."""
    from tb.gateway import rpc

    def run_tool(name, args):
        return rpc.call(agent_id, name, args, root=root, cancel=cancel)
    return run_tool
