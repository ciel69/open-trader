"""Адаптер формата OpenAI chat-completions (база для DeepSeek и локальных серверов).

Особенности по флагам модели (providers.json): tools, strict (tools[].function.strict, на beta_url), json_mode (response_format json_object),
thinking (карта thinking_map или стандартная), extra_body (произвольные поля тела).
"""
import json

from tb.llm.adapters.base import Adapter, json_tool_args, strict_compatible
from tb.llm.types import LLMError, Resp, ToolCall, Usage

FINISH = {"stop": "stop", "tool_calls": "tool_calls", "function_call": "tool_calls", "length": "length", "content_filter": "filtered"}
DEFAULT_THINKING = {"off": {"thinking": {"type": "disabled"}},
                    "low": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
                    "medium": {"thinking": {"type": "enabled"}, "reasoning_effort": "medium"},
                    "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}}
# У OpenAI-совместимых моделей с рассуждениями (DeepSeek) reasoning_tokens входят в max_tokens: без запаса ответ обрезается (finish=length) ещё в рассуждениях,
# как у роли entry_scout (max_output_tokens 700, thinking low -> 17 из 58 ответов пустые). Запас добавляется к лимиту вывода при включённом thinking,
# как у адаптера Anthropic (budget_tokens), и ограничивается max_out модели.
THINK_BUDGET = {"low": 1024, "medium": 4096, "high": 8192}


class OpenAICompat(Adapter):
    kind = "openai-compatible"
    pass_reasoning_always = False       # DeepSeek: reasoning_content возвращается при запросах с tools

    def auth_headers(self, secret):
        return {"Authorization": "Bearer " + secret.reveal()} if secret is not None else {}

    # ---- сообщения ----
    def _messages(self, req, with_tools):
        msgs = []
        system = req.system or ""
        if req.response_format and self.model.json_mode and "json" not in system.lower():
            system = (system + "\n\nОтвет: строго один JSON-объект (json).").strip()
        if system:
            msgs.append({"role": "system", "content": system})
        for m in req.messages:
            r = m.get("role")
            if r == "assistant":
                o = {"role": "assistant", "content": m.get("content") or ""}
                calls = m.get("tool_calls") or []
                if calls:
                    o["tool_calls"] = [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"], "arguments": c.get("raw_args") or json.dumps(c.get("args") or {}, ensure_ascii=False)}}
                                       for c in calls]
                    if not o["content"]:
                        o["content"] = None
                if m.get("reasoning") and (with_tools or self.pass_reasoning_always):
                    o["reasoning_content"] = m["reasoning"]
                msgs.append(o)
            elif r == "tool":
                msgs.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m.get("content") or ""})
            else:
                msgs.append({"role": "user", "content": m.get("content") or ""})
        return msgs

    def _tools(self, req):
        out, any_strict = [], False
        for t in req.tools:
            fn = {"name": t["name"], "description": t.get("description") or "", "parameters": t["schema"]}
            if self.model.strict and strict_compatible(t["schema"]):
                fn["strict"] = True
                any_strict = True
            out.append({"type": "function", "function": fn})
        return out, any_strict

    def _thinking_fields(self, req):
        if self.model.thinking != "param" or not req.thinking:
            return {}, False
        tm = self.model.thinking_map or DEFAULT_THINKING
        return dict(tm.get(req.thinking) or {}), req.thinking != "off"

    def build(self, req):
        if req.tools and not self.model.tools:
            raise LLMError("config", "модель не поддерживает инструменты (флаг tools)")
        tools, any_strict = self._tools(req) if req.tools else ([], False)
        think, think_on = self._thinking_fields(req)
        mx = int(req.max_tokens)
        if think_on:
            mx += int(THINK_BUDGET.get(req.thinking, 0))
            if self.model.max_out:
                mx = min(mx, int(self.model.max_out))
        body = {"model": self.model.name, "messages": self._messages(req, bool(req.tools)), self.model.max_tokens_field: mx, "stream": False}
        if req.temperature is not None and not think_on and not self.model.no_temperature:
            body["temperature"] = req.temperature
        if tools:
            body["tools"] = tools
            tc = req.tool_choice
            if isinstance(tc, dict) and tc.get("name"):
                body["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
            elif tc in ("auto", "none", "required"):
                body["tool_choice"] = tc
        if req.response_format and self.model.json_mode and not tools:
            body["response_format"] = {"type": "json_object"}
        body.update(think)
        body.update(self.model.extra_body)
        base = self.provider.beta_url if (any_strict and self.provider.beta_url) else self.provider.base_url
        return base + "/chat/completions", {}, body

    # ---- ответ ----
    def parse_usage(self, u):
        if not isinstance(u, dict):
            return Usage()
        pt = int(u.get("prompt_tokens") or 0)
        det = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
        hit = int(det.get("cached_tokens") or 0)
        cdet = u.get("completion_tokens_details") if isinstance(u.get("completion_tokens_details"), dict) else {}
        return Usage(miss=max(0, pt - hit), hit=hit, out=int(u.get("completion_tokens") or 0), reasoning=int(cdet.get("reasoning_tokens") or 0))

    def parse(self, data, req):
        ch = (data.get("choices") or [None])[0]
        if not isinstance(ch, dict):
            raise LLMError("bad_body", "нет choices")
        msg = ch.get("message") if isinstance(ch.get("message"), dict) else {}
        content = msg.get("content")
        text = content if isinstance(content, str) else ""
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            fn = c.get("function") or {}
            raw = fn.get("arguments")
            raw = raw if isinstance(raw, str) else json.dumps(raw or {}, ensure_ascii=False)
            args, err = json_tool_args(raw)
            calls.append(ToolCall(str(c.get("id") or "call_%d" % i), str(fn.get("name") or ""), args, raw, err))
        finish = FINISH.get(ch.get("finish_reason"), "other")
        if calls and finish == "other":
            finish = "tool_calls"
        reasoning = msg.get("reasoning_content")
        return Resp(text, calls, self.parse_usage(data.get("usage")), finish, raw_id=data.get("id"),
                    reasoning=reasoning if isinstance(reasoning, str) and reasoning else None)
