"""Сценарный ФЕЙКОВЫЙ LLM для `./tb smoke`: локальный HTTP-сервер в формате DeepSeek (OpenAI-совместимый), живёт в процессе smoke.

Роль определяется по первой строке системного промпта (`# prompt vN <роль>`); ответ строится из ДАННЫХ запроса (досье, событие, вход аудитора), как отвечала бы
разумная модель: план дня «торгуем осторожно у уровня, который есть в досье», position_manager «hold», day_auditor «ничего не переносим», reviewer «неясно».
Реальных обращений в сеть нет: слушает 127.0.0.1 на свободном порту. Журнал вызовов: `Server.calls` (роль, время).
"""
import http.server
import json
import re
import threading
import time


def tag_json(text, name):
    m = re.search(r"<%s>\s*(.*?)\s*</%s>" % (name, name), text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def role_of(system):
    m = re.search(r"#\s*prompt\s+v\d+\s+([a-z_]+)", system or "")
    return m.group(1) if m else None


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def dayplan_answer(user):
    brief = tag_json(user, "brief") or {}
    caps = tag_json(user, "caps") or {}
    oc = tag_json(user, "owner_choice") or {}
    allowed = tag_json(user, "symbols") or []
    prices = {}
    for k, v in (brief.get("levels") or {}).items() if isinstance(brief.get("levels"), dict) else []:
        if isinstance(v, dict) and _num(v.get("px")):
            prices[k] = float(v["px"])
    mk = brief.get("market") if isinstance(brief.get("market"), dict) else {}
    for key, name in (("btc", "BTC"), ("eth", "ETH")):
        if isinstance(mk.get(key), dict) and _num(mk[key].get("px")):
            prices.setdefault(name, float(mk[key]["px"]))
    sym = next((s for s in ("BTC", "ETH", "SOL") if s in prices and (not allowed or s in allowed)), None) or next((s for s in sorted(prices) if not allowed or s in allowed), None)
    sess = {"type": oc.get("session") or "intraday", "minutes": int(oc.get("minutes") or 360)}
    risk = min(0.3, _num(caps.get("max_risk_usdt")) or 0.3)
    mult = min(0.5, _num(caps.get("size_mult")) or 0.5)
    if sym is None:
        return {"schema": "tb.dayplan/1", "regime": "unclear", "bias": "neutral", "session": sess, "watch": [], "allowed_setups": [], "avoid": [],
                "limits": {"max_new_plans": 0, "max_risk_usdt": 0, "symbols": [], "size_mult": 0}, "carry": {"allow": False, "max_positions": 0}, "carried_review": [],
                "risks": ["no_price_data"], "questions": [], "why": "нет цен в досье | не торгуем | риск не оправдан"}
    px = prices[sym]
    return {"schema": "tb.dayplan/1", "regime": "range", "bias": "neutral", "session": sess,
            "watch": [{"sym": sym, "side": "long", "level": round(px * 0.995, 6), "setup": "level_reclaim", "note": "возврат выше уровня"}],
            "allowed_setups": ["level_reclaim"], "avoid": [], "limits": {"max_new_plans": 2, "max_risk_usdt": round(risk, 4), "symbols": [sym] + [s for s in ("SOL",) if s in allowed and s != sym], "size_mult": mult},
            "carry": {"allow": False, "max_positions": 0}, "carried_review": [], "risks": [], "questions": [], "why": "диапазон | цена у уровня | осторожный размер"}


def pm_answer(user):
    ev = tag_json(user, "event") or {}
    brief = tag_json(user, "brief") or {}
    plan = ev.get("plan")
    rev = 1
    for key in ("plans", "positions"):
        for it in brief.get(key) or []:
            if isinstance(it, dict) and plan in (it.get("id"), it.get("plan")) and isinstance(it.get("rev"), int):
                rev = it["rev"]
    return {"schema": "tb.pm/1", "event_seq": int(ev.get("seq_last") or 0), "plan": plan, "rev": rev, "decision": "hold", "args": {},
            "why": "стоп стоит | позиция в плане | риск ограничен", "conf": 0.6, "next": "event"}


def audit_answer(user):
    inp = tag_json(user, "audit_input") or {}
    return {"schema": "tb.audit/1", "day_id": inp.get("day_id") or "D-0", "carry": [], "changes": [], "owner_questions": [], "narrative": "день завершён, переносов нет"}


def review_answer(user):
    return {"schema": "tb.casereview/1", "verdict": "unclear", "right": [], "improve": ["none"], "alternative": {"action": "none", "detail": "данных мало"},
            "known_at_decision": [], "lesson_hint": None, "conf": 0.3}


def entry_answer(user):
    ev = tag_json(user, "event") or {}
    return {"schema": "tb.entry/1", "event_seq": int(ev.get("seq_last") or 0), "decision": "skip", "skip_code": "no_setup",
            "why": "нет сетапа | сценарный фейковый LLM не входит | риск не берётся", "conf": 0.3}


ANSWERS = {"morning_analyst": dayplan_answer, "position_manager": pm_answer, "day_auditor": audit_answer, "reviewer": review_answer, "entry_scout": entry_answer}


class Server(object):
    def __init__(self, model_names=("deepseek-pro", "deepseek-flash")):
        self.calls, self.models, self._lk = [], list(model_names), threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    return self._send(200, {"data": [{"id": m} for m in outer.models]})
                self._send(404, {"error": {"message": "нет"}})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    req = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8"))
                except ValueError:
                    return self._send(400, {"error": {"message": "не JSON"}})
                msgs = req.get("messages") or []
                system = next((m.get("content") for m in msgs if m.get("role") == "system"), "")
                user = next((m.get("content") for m in msgs if m.get("role") == "user"), "")
                role = role_of(system)
                fn = ANSWERS.get(role)
                with outer._lk:
                    outer.calls.append({"role": role, "t": time.time(), "model": req.get("model")})
                text = json.dumps(fn(user), ensure_ascii=False) if fn else "{}"
                self._send(200, {"id": "smoke-%d" % len(outer.calls), "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": 1200, "completion_tokens": 120, "total_tokens": 1320, "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 300}})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def roles_called(self):
        with self._lk:
            return [c["role"] for c in self.calls]
