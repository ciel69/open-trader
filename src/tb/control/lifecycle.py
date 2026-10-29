"""Точка интеграции с жизненным циклом дня (WP14) и заглушка. Владелец: WP12; WP14 заменяет заглушку модулем `tb.lifecycle.api`.

Контракт модуля WP14 `tb.lifecycle.api` (все функции берут root и now, возвращают словарь {"ok","code","msg","data"}):
  start_day(root, args, now) -> data {"day_id"}     шаги S1..S9; к моменту вызова ctl уже проверил гейты/фразы и переключил исполнитель
  stop_day(root, args, now)  -> data {"day_id","audit":{...}}   мягкая остановка: чек-лист закрытия C1..C8 и отчёт (аудит закрытия дня делает WP14)
  emergency_summary(root, args, now)   краткий итог аварийной остановки без LLM
  carry_decide(root, args, now), report_ack(root, args, now), notify_test(root, args, now), notify_set(root, args, now)
Заглушка (ниже) делает минимум: выдаёт id дня D-YYYYMMDD-N, пишет упрощённый `state/days/<id>/day.json` (stub=true), включает/выключает торговый флаг
`state/control/trading.json`; аудит закрытия дня НЕ делает (точка `closing_audit` возвращает {"stub": true}).
Торговый флаг: {"day_id","enabled","mode","agent","until_ts"}; шлюз (WP11-A) должен отклонять submit_plan/add при enabled=false (контракт registry.trading_allowed).
"""
import importlib
import importlib.util
import os
import re

from tb import paths
from tb.control import ctlstate


def _api():
    try:
        if importlib.util.find_spec("tb.lifecycle.api") is not None:
            return importlib.import_module("tb.lifecycle.api")
    except (ImportError, ValueError, AttributeError):
        pass
    return None


def real():
    return _api() is not None


def day_dir(root, day_id):
    return paths.state("days", day_id, root=root)


def next_day_id(root, now):
    stamp = ctlstate.utc_day(now).replace("-", "")
    n = 0
    try:
        for name in os.listdir(paths.state("days", root=root)):
            m = re.match(r"^D-%s-(\d+)$" % stamp, name)
            if m:
                n = max(n, int(m.group(1)))
    except OSError:
        pass
    return "D-%s-%d" % (stamp, n + 1)


def set_trading(root, enabled, day_id, mode, agent, until_ts=None):
    ctlstate.write_json(ctlstate.f_trading(root), {"day_id": day_id, "enabled": bool(enabled), "mode": mode, "agent": agent, "until_ts": until_ts})


def trading_flag(root):
    d = ctlstate.read_json(ctlstate.f_trading(root))
    return d if isinstance(d, dict) else {"day_id": None, "enabled": False}


def closing_audit(root, day_id, now):
    """Точка интеграции: аудит закрытия дня (WP14). Заглушка."""
    return {"stub": True, "msg": "аудит закрытия дня появится с WP14"}


def _call(name, root, args, now):
    api = _api()
    fn = getattr(api, name, None) if api is not None else None
    if fn is None:
        return None
    try:
        r = fn(root, args, now)
        return r if isinstance(r, dict) and "ok" in r else {"ok": False, "code": "E_INTERNAL", "msg": "", "data": {}}
    except Exception:
        return {"ok": False, "code": "E_INTERNAL", "msg": "", "data": {}}


def start_day(root, args, now, agent=None):
    r = _call("start_day", root, dict(args, agent=agent) if agent else args, now)
    if r is not None:
        return r
    day_id = next_day_id(root, now)
    d = {"day_id": day_id, "state": "running", "mode": args.get("mode"), "session": args.get("session"), "team": args.get("team"),
         "agent": agent, "started": round(now, 1), "live_waivers": args.get("waivers") or [], "stub": True}
    ctlstate.write_json(os.path.join(day_dir(root, day_id), "day.json"), d)
    set_trading(root, args.get("mode") != "observe", day_id, args.get("mode"), agent, None)
    return {"ok": True, "code": "OK", "msg": "", "data": {"day_id": day_id}}


def preflight_preview(root, args, now):
    """-> {"amber": [...], "red": [...]} предстартовых замечаний (WP14) или None, если слоя нет или он упал."""
    r = _call("preflight_preview", root, args, now)
    return (r.get("data") if r and r.get("ok") else None)


def stop_day(root, args, now):
    r = _call("stop_day", root, args, now)
    if r is not None:
        return r
    st = ctlstate.platform_state(root)
    day_id = st.get("day_id")
    aud = closing_audit(root, day_id, now)
    if day_id:
        p = os.path.join(day_dir(root, day_id), "day.json")
        d = ctlstate.read_json(p) or {}
        d.update({"state": "stopped", "stopped": round(now, 1), "audit": aud})
        ctlstate.write_json(p, d)
    set_trading(root, False, day_id, st.get("mode"), st.get("agent"))
    return {"ok": True, "code": "OK", "msg": "", "data": {"day_id": day_id, "audit": aud}}


def tick(root, now):
    """Цикл ctl: возобновление/автоостановка/продолжение закрытия (WP14). -> {"day_id","state","changed",...} | None (нет WP14 или сбой)."""
    api = _api()
    fn = getattr(api, "tick", None) if api is not None else None
    if fn is None:
        return None
    try:
        return fn(root, now)
    except Exception:
        return None


def banner(root):
    """Баннер дня для status.json (WP14) или None."""
    api = _api()
    fn = getattr(api, "banner", None) if api is not None else None
    if fn is None:
        return None
    try:
        return fn(root)
    except Exception:
        return None


def passthrough(name, root, args, now):
    """carry_decide / report_ack / notify_test / notify_set / emergency_summary: WP14 или E_UNAVAILABLE (для emergency_summary заглушка молчит и успешна)."""
    r = _call(name, root, args, now)
    if r is not None:
        return r
    if name == "emergency_summary":
        return {"ok": True, "code": "OK", "msg": "", "data": {"stub": True}}
    return {"ok": False, "code": "E_UNAVAILABLE", "msg": "", "data": {}}
