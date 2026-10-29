"""Смена профиля исполнителя по запросу сервиса `ctl` (WP12): `runtime/platform/reconf.req` -> перезапуск ТОЛЬКО exec -> `reconf.ack`. Владелец: WP12.

Запрос: {"id","profile":"paper|testnet|trade","observe":bool,"ts"}; ответ: {"id","ok","code","profile","observe","ts"}. Коды: OK, SAME, NO_EXEC, BAD_REQUEST, KILL (профиль trade
при активном state/KILL не поднимается), SPAWN. Решение «можно ли менять» принимает ctl (нет позиций и т. п.); супервизор повторяет только проверку KILL для trade.
"""
import os
import time

from tb.core import exec_cfg
from tb.platform import procs
from tb.platform import services as S


def _ack(plat, rid, ok, code):
    d = {"id": rid, "ok": bool(ok), "code": code, "profile": plat.profile, "observe": plat.observe, "ts": round(time.time(), 3)}
    try:
        S.write_json(S.reconf_ack_file(plat.root), d)
    except OSError:
        pass
    plat.say("reconf: %s (%s)" % (code, "profile=%s observe=%s" % (plat.profile, plat.observe)))


def handle(plat):
    """Вызывается из цикла контроля супервизора. -> True, если запрос обработан."""
    path = S.reconf_req_file(plat.root)
    if not os.path.exists(path):
        return False
    req = S.read_json(path)
    try:
        os.remove(path)
    except OSError:
        pass
    rid = req.get("id") if isinstance(req, dict) else None
    if not isinstance(req, dict) or req.get("profile") not in S.PROFILES or not isinstance(req.get("observe"), bool) or not isinstance(rid, str):
        _ack(plat, rid if isinstance(rid, str) else "-", False, "BAD_REQUEST")
        return True
    svc = plat.svcs.get("exec")
    if svc is None:
        _ack(plat, rid, False, "NO_EXEC")
        return True
    profile, observe = req["profile"], req["observe"]
    if profile == plat.profile and observe == plat.observe and svc.state == "running":
        _ack(plat, rid, True, "SAME")
        return True
    if profile == "trade" and exec_cfg.kill_state(plat.root) is not None:
        _ack(plat, rid, False, "KILL")
        return True
    if svc.proc is not None and svc.proc.poll() is None:
        procs.terminate(svc.proc, plat.sv["exec_stop_grace_s"])
    plat._finish_reader(svc)
    plat._rm_pid(svc)
    svc.proc, svc.state, svc.tries, svc.fatal = None, "stopped", 0, False
    plat.profile, plat.observe = profile, observe
    svc.cmd = S.service_cmd("exec", profile, observe, plat.cfg)
    svc.env = S.child_env("exec", dict(os.environ), plat.root, plat.selected, profile)
    ok = plat.start(svc)
    _update_lock(plat)
    plat.write_status()
    _ack(plat, rid, ok, "OK" if ok else "SPAWN")
    return True


def _update_lock(plat):
    cur = S.read_json(S.lock_file(plat.root))
    if isinstance(cur, dict) and int(cur.get("pid") or 0) == os.getpid():
        cur.update({"profile": plat.profile, "observe": plat.observe})
        try:
            S.write_json(S.lock_file(plat.root), cur)
        except OSError:
            pass
