"""Единый статус платформы (`./tb status`, дашборд `/api/platform`, MCP get_status). Владелец: WP11-B.

Контракт для читателей: `build_status(root) -> dict` (чистое чтение файлов, без сети и блокировок) либо файл `runtime/platform/status.json`
(пишет супервизор раз в status_s; у файла только блок сервисов, а живые поля — exec, kill, шина, сканер, непрочитанное — считает build_status).
Цвет: green | yellow | red | off (платформа не запущена). Красный: сервис сдался, исполнитель упал/не отвечает при открытых позициях.
"""
import time

from tb import paths
from tb.core import bus, exec_cfg
from tb.core.filelock import pid_alive
from tb.platform import services as S

STALE_FILE_S = 15.0           # status.json старше этого = супервизор завис/упал (при живом pid жёлтый)
SCAN_STALE_X = 4.0            # возраст hot.json > 4 интервалов скана = сканер не обновляется


def supervisor_info(root=None):
    """-> {"running":bool,"pid":int,"started":float|None,...данные lock}."""
    d = S.read_json(S.lock_file(root))
    if not isinstance(d, dict):
        return {"running": False, "pid": 0}
    pid = int(d.get("pid") or 0)
    out = dict(d)
    out["pid"] = pid
    out["running"] = bool(pid and pid_alive(pid))
    return out


def _exec_block(root, now):
    from tb.brain import common
    h = common.exec_health(root=root, now=now)
    d = h.get("data") or {}
    pos = d.get("positions")
    return {"alive": bool(h.get("alive")), "present": bool(h.get("present")), "age_s": h.get("age_s"), "mode": d.get("mode"), "env": d.get("env"),
            "observe": d.get("observe"), "positions": pos if isinstance(pos, int) and not isinstance(pos, bool) else None, "hold": d.get("hold"),
            "plans": d.get("plans")}


def _bus_block(root, now):
    try:
        tail = bus.read_tail(1, root=root)
        last = tail[-1] if tail else None
        ts = bus.parse_iso(last.get("ts")) if last else None
        return {"last_seq": bus.last_seq(root=root), "age_s": None if ts is None else round(max(0.0, now - ts), 1)}
    except Exception:
        return {"last_seq": None, "age_s": None}


def _scan_block(root, now):
    hot = S.read_json(paths.runtime("hot.json", root=root))
    ts = hot.get("ts_epoch") if isinstance(hot, dict) else None
    return {"age_s": round(max(0.0, now - ts), 1) if isinstance(ts, (int, float)) else None}


def build_status(root=None, now=None):
    now = time.time() if now is None else now
    cfg = S.load_cfg(root)
    sup = supervisor_info(root)
    fresh = S.read_json(S.status_file(root))
    fresh = fresh if isinstance(fresh, dict) else {}
    file_age = None
    if isinstance(fresh.get("ts_epoch"), (int, float)):
        file_age = max(0.0, now - fresh["ts_epoch"])
    running = bool(sup.get("running"))
    svcs = {}
    for n, v in (fresh.get("services") or {}).items():
        if not isinstance(v, dict):
            continue
        v = dict(v)
        if running and v.get("state") == "running" and isinstance(v.get("started_ts"), (int, float)):
            v["uptime_s"] = round(max(0.0, now - v["started_ts"]), 1)
        if not running:
            v["state"] = "stopped" if v.get("state") in ("running", "starting", "backoff") else v.get("state")
            v["pid"] = None
        svcs[n] = v
    ex = _exec_block(root, now)
    kill = exec_cfg.kill_state(root)
    bus_b, scan_b = _bus_block(root, now), _scan_block(root, now)
    unread = S.read_json(S.unread_file(root))
    unread = unread if isinstance(unread, dict) else {}
    profile = sup.get("profile") if running else None
    reasons, color = [], "green"

    def worse(c):
        order = ("green", "yellow", "red")
        return c if order.index(c) > order.index(color) else color

    if not running:
        color = "off"
        reasons.append("платформа не запущена (./tb start)")
    else:
        if file_age is not None and file_age > STALE_FILE_S:
            color = worse("yellow")
            reasons.append("status.json не обновляется %d с: супервизор завис?" % file_age)
        for n, v in svcs.items():
            st = v.get("state")
            if st == "failed":
                color = worse("red")
                reasons.append("сервис %s не поднимается (%s)" % (n, v.get("last_error") or "сбой"))
            elif st in ("backoff", "starting"):
                color = worse("yellow")
                reasons.append("сервис %s перезапускается (подряд сбоев: %s)" % (n, v.get("tries")))
            elif st == "unavailable":
                color = worse("yellow")
                reasons.append("сервис %s недоступен: %s" % (n, v.get("last_error") or "нет модуля"))
            elif st == "running" and v.get("hb_ok") is False:
                color = worse("yellow")
                reasons.append("сервис %s: heartbeat устарел" % n)
        bsv = svcs.get("brain") or {}
        if bsv.get("state") == "running":           # WP11-C2: API-мозг сообщает здоровье в heartbeat (red = «только защита», yellow = запасной профиль/бюджет/ошибки конфигурации)
            _a, bhb = S.read_hb("brain", root, now)
            if bhb.get("protect_only"):
                color = worse("red")
                reasons.append("мозг в режиме «только защита»: %s (новых входов нет, позиции ведёт исполнитель)" % (str(bhb.get("reason") or "-")[:80]))
            elif bhb.get("health") == "yellow":
                color = worse("yellow")
                reasons.append("мозг работает в ограниченном режиме (запасной профиль, бюджет или ошибки конфигурации): ./tb brain status")
        sc = (svcs.get("scan") or {})
        if sc.get("state") == "running" and scan_b["age_s"] is not None:
            lim = SCAN_STALE_X * float((cfg.get("scan") or {}).get("interval") or 30.0)
            if scan_b["age_s"] > max(lim, 120.0):
                color = worse("yellow")
                reasons.append("сканер не обновляет hot.json %d с" % scan_b["age_s"])
    exec_expected = "exec" in svcs or (running and "exec" in (sup.get("services") or []))
    if exec_expected and not ex["alive"] and running:
        if (svcs.get("exec") or {}).get("state") == "running" and (svcs.get("exec") or {}).get("uptime_s", 0) < 60:
            pass                                          # исполнитель только стартует
        else:
            color = worse("yellow")
            reasons.append("исполнитель не отвечает (heartbeat)")
    pos = ex.get("positions") or (fresh.get("positions_seen") or 0)
    if exec_expected and not ex["alive"] and pos:
        color = worse("red")
        reasons.append("ИСПОЛНИТЕЛЬ НЕ ЖИВ при открытых позициях (%s): они защищены биржевым стопом, но не ведутся; проверь вручную" % pos)
    if kill:
        color = worse("yellow") if color != "off" else color
        reasons.append("KILL активен (flatten=%s): новых входов нет" % bool(kill.get("flatten")))
    total = 0
    for u in unread.values():
        if isinstance(u, dict) and isinstance(u.get("unread"), int):
            total += u["unread"]
    links = {}
    for n in svcs:
        lk = S.link_of(n, cfg)
        if lk:
            links[n] = lk
    return {"ts_epoch": round(now, 3), "color": color, "reasons": reasons, "running": running,
            "supervisor": {"pid": sup.get("pid") or None, "started": sup.get("started"), "uptime_s": round(now - sup["started"], 1) if running and isinstance(sup.get("started"), (int, float)) else None,
                           "profile": profile, "observe": sup.get("observe") if running else None, "services": sup.get("services") if running else None},
            "services": svcs, "exec": ex, "kill": kill is not None, "kill_info": kill, "bus": bus_b, "scanner": scan_b,
            "unread": unread, "unread_total": total, "links": links}


def format_line(st):
    """Короткая строка для статусной строки Claude Code: `TB paper G exec:ok scan:ok chat:3`."""
    mark = {"green": "OK", "yellow": "WARN", "red": "RED", "off": "OFF"}.get(st.get("color"), "?")
    prof = (st.get("supervisor") or {}).get("profile") or "-"
    parts = ["TB", prof, mark]
    for n, v in (st.get("services") or {}).items():
        parts.append("%s:%s" % (n, {"running": "ok", "failed": "DOWN", "backoff": "retry", "starting": "start", "unavailable": "n/a", "stopped": "off"}.get(v.get("state"), "?")))
    for sid, u in (st.get("unread") or {}).items():
        if isinstance(u, dict) and u.get("unread"):
            parts.append("%s:%d%s" % (sid, u["unread"], "!" if u.get("wake_unread") else ""))
    if st.get("kill"):
        parts.append("KILL")
    return " ".join(parts)


def format_text(st):
    c = {"green": "ЗЕЛЁНЫЙ", "yellow": "ЖЁЛТЫЙ", "red": "КРАСНЫЙ", "off": "ВЫКЛЮЧЕНА"}.get(st["color"], st["color"])
    sup = st["supervisor"]
    out = ["платформа: %s" % c]
    if st["running"]:
        out.append("профиль %s%s, супервизор pid %s, аптайм %s" % (sup.get("profile"), " (observe)" if sup.get("observe") else "", sup.get("pid"), _dur(sup.get("uptime_s"))))
    for r in st["reasons"]:
        out.append("  ! " + r)
    out.append("%-9s %-11s %-7s %-8s %-9s %-4s %s" % ("сервис", "состояние", "pid", "аптайм", "heartbeat", "рст", "последняя ошибка"))
    for n, v in st["services"].items():
        hb = "-" if v.get("hb_age_s") is None else ("%ds%s" % (v["hb_age_s"], "" if v.get("hb_ok") is not False else " !"))
        out.append("%-9s %-11s %-7s %-8s %-9s %-4s %s" % (n, v.get("state"), v.get("pid") or "-", _dur(v.get("uptime_s")), hb, v.get("restarts", 0), v.get("last_error") or ""))
    ex = st["exec"]
    out.append("исполнитель: %s, режим %s, позиций %s, hold %s, KILL %s" % ("жив" if ex["alive"] else "НЕ ЖИВ", ex.get("mode"), ex.get("positions"), ex.get("hold"), "да" if st["kill"] else "нет"))
    out.append("шина: последний seq %s, возраст %s с; сканер: hot.json %s с; непрочитано: %s" % (st["bus"]["last_seq"], st["bus"]["age_s"], st["scanner"]["age_s"], st["unread_total"]))
    for n, u in st["links"].items():
        out.append("ссылка %s: %s" % (n, u))
    return "\n".join(out)


def _dur(s):
    if not isinstance(s, (int, float)):
        return "-"
    s = int(s)
    if s < 120:
        return "%ds" % s
    if s < 7200:
        return "%dm" % (s // 60)
    return "%dh%02dm" % (s // 3600, s % 3600 // 60)
