"""Адаптер Anthropic Messages API: tool use, prompt caching (cache_control на системном блоке), thinking (WP11-C1).

- POST {base_url}/v1/messages, заголовки x-api-key и anthropic-version (version_header);
- кэш: cache=explicit -> cache_control{type:ephemeral} на последнем статическом блоке (system; кэшируется и tools перед ним);
- usage: input_tokens (не из кэша), cache_read_input_tokens (попадания), cache_creation_input_tokens (запись), output_tokens;
- структурный вывод (response_format={"schema":...}): служебный инструмент `respond` с tool_choice на него; его input возвращается как text (JSON);
  при включённом thinking принудительный tool_choice недопустим -> tool_choice auto и подсказка в системном блоке;
- thinking: budget_tokens из thinking_map (по умолчанию low 1024 / medium 4096 / high 8192); max_tokens увеличивается на budget; temperature не шлётся;
  блоки thinking возвращаются в следующих запросах как есть (opaque).
"""
import json

from tb.llm.adapters.base import Adapter, json_tool_args, strict_compatible
from tb.llm.types import LLMError, Resp, ToolCall, Usage

STOP = {"end_turn": "stop", "stop_sequence": "stop", "pause_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length", "refusal": "filtered"}
DEFAULT_BUDGET = {"low": 1024, "medium": 4096, "high": 8192}
RESPOND = "respond"


class AnthropicAdapter(Adapter):
    kind = "anthropic"

    def auth_headers(self, secret):
        return {"x-api-key": secret.reveal(), "anthropic-version": self.provider.version_header or "2023-06-01"}

    def probe_request(self):
        return self.provider.base_url + "/v1/models", {}

    def _thinking(self, req):
        if self.model.thinking != "param" or req.thinking in (None, "off"):
            return None
        tm = self.model.thinking_map or {}
        b = tm.get(req.thinking)
        if isinstance(b, dict):
            b = (b.get("thinking") or {}).get("budget_tokens")
        b = b if isinstance(b, int) and b >= 1024 else DEFAULT_BUDGET.get(req.thinking, 1024)
        return b

    def _messages(self, req):
        out = []

        def push(role, blocks):
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)      # подряд идущие сообщения одной роли склеиваются (tool_result идут одним user)
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in req.messages:
            r = m.get("role")
            if r == "assistant":
                op = m.get("opaque")
                if isinstance(op, dict) and op.get("kind") == "anthropic" and isinstance(op.get("blocks"), list):
                    blocks = list(op["blocks"])
                else:
                    blocks = []
                    if m.get("content"):
                        blocks.append({"type": "text", "text": m["content"]})
                    for c in m.get("tool_calls") or []:
                        blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c.get("args") or {}})
                if blocks:
                    push("assistant", blocks)
            elif r == "tool":
                push("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m.get("content") or ""}])
            else:
                push("user", [{"type": "text", "text": m.get("content") or ""}])
        return out

    def build(self, req):
        if req.tools and not self.model.tools:
            raise LLMError("config", "модель не поддерживает инструменты (флаг tools)")
        budget = self._thinking(req)
        tools = []
        for t in req.tools:
            o = {"name": t["name"], "description": t.get("description") or "", "input_schema": t["schema"]}
            if self.model.strict and strict_compatible(t["schema"]):
                o["strict"] = True
            tools.append(o)
        rf = req.response_format
        system = req.system or ""
        tool_choice = None
        tc = req.tool_choice
        if isinstance(tc, dict) and tc.get("name"):
            tool_choice = {"type": "tool", "name": tc["name"]}
        elif tc == "required":
            tool_choice = {"type": "any"}
        elif tc in ("auto", "none"):
            tool_choice = {"type": tc}
        if isinstance(rf, dict) and rf.get("schema") and not req.tools:
            tools = [{"name": RESPOND, "description": "Верни итоговый ответ в этом формате.", "input_schema": rf["schema"]}]
            tool_choice = {"type": "tool", "name": RESPOND} if budget is None else {"type": "auto"}
            if budget is not None:
                system = (system + "\n\nИтог верни вызовом инструмента %s." % RESPOND).strip()
        elif rf == "json":
            system = (system + "\n\nОтвет: строго один JSON-объект без пояснений.").strip()
        body = {"model": self.model.name, "max_tokens": int(req.max_tokens) + (budget or 0), "messages": self._messages(req)}
        if system:
            blk = {"type": "text", "text": system}
            if self.model.cache == "explicit":
                blk["cache_control"] = {"type": "ephemeral"}
            body["system"] = [blk]
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        if budget is not None:
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        elif req.temperature is not None:
            body["temperature"] = req.temperature
        body.update(self.model.extra_body)
        return self.provider.base_url + "/v1/messages", {}, body

    def parse(self, data, req):
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise LLMError("bad_body", "нет content")
        text, calls, reasoning = [], [], []
        for b in blocks:
            t = b.get("type")
            if t == "text":
                text.append(b.get("text") or "")
            elif t == "tool_use":
                inp = b.get("input")
                if b.get("name") == RESPOND and isinstance(req.response_format, dict):
                    text.append(json.dumps(inp if isinstance(inp, dict) else {}, ensure_ascii=False, sort_keys=True))
                    continue
                raw = json.dumps(inp if inp is not None else {}, ensure_ascii=False)
                args, err = json_tool_args(raw)
                calls.append(ToolCall(str(b.get("id") or ""), str(b.get("name") or ""), args, raw, err))
            elif t == "thinking":
                reasoning.append(b.get("thinking") or "")
        u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        usage = Usage(miss=int(u.get("input_tokens") or 0), hit=int(u.get("cache_read_input_tokens") or 0),
                      write=int(u.get("cache_creation_input_tokens") or 0), out=int(u.get("output_tokens") or 0))
        finish = STOP.get(data.get("stop_reason"), "other")
        if calls and finish in ("other", "stop"):
            finish = "tool_calls"
        has_think = any(b.get("type") in ("thinking", "redacted_thinking") for b in blocks)
        # блоки для возврата провайдеру (thinking с подписью обязательны при следующем запросе с tool_use)
        opaque = {"kind": "anthropic", "blocks": [b for b in blocks if not (b.get("type") == "tool_use" and b.get("name") == RESPOND)]} if has_think else None
        return Resp("".join(text), calls, usage, finish, raw_id=data.get("id"), reasoning="\n".join(reasoning) or None, opaque=opaque)
