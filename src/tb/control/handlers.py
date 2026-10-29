"""Исполнитель команд плоскости управления: проверка подписи/времени/nonce/фраз, обработчики действий. Владелец: WP12.

Класс `Ctl` (без потоков внутри `process`): конвейер приёма одной команды и обработчики. Долгие операции (`day.start` с переключением исполнителя) идут в фоновом
потоке (в тестах `sync=True`: выполняются сразу). Всё время и сон берутся из `clock`/`sleep` (подменяются в тестах). Биржевого кода здесь нет.
"""
import json
import os
import threading
import time
import uuid

from tb import paths
from tb.control import audit, auth, brain_iface, ctlstate, gates, lifecycle, protocol, riskstore, settings_args, settings_ctl
from tb.core import exec_cfg
from tb.platform import services as S

ACK = riskstore.ACK
PROFILE_OF = {"observe": ("paper", True), "paper": ("paper", False), "testnet": ("testnet", False), "live": ("trade", False)}
MAX_INBOX_BYTES = protocol.MAX_COMMAND_BYTES
KEEP_OUTBOX_S = 7 * 86400
LIVE_ACTIONS = ("day.start", "risk.set", "brain.use", "carry.decide", "platform.shutdown", "kill.set", "stop.emergency", "brain.role", "exchange.set")


class Reject(Exception):
    def __init__(self, code, data=None):
        Exception.__init__(self, code)
        self.code, self.data = code, data or {}


def _ident(s):
    return "".join(c for c in str(s) if c.isalnum() or c in "_.-")[:40]


class Ctl(object):
    def __init__(self, root=None, clock=time.time, sleep=time.sleep, environ=None, sync=False, reconf=None):
        self.root = root or paths.root()
        self.clock, self.sleep, self.environ, self.sync = clock, sleep, environ, sync
        self.cfg = ctlstate.load_cfg(self.root)
        self.seen = ctlstate.Seen(self.root)
        self.lock = threading.RLock()
        self.reconf_fn = reconf or self._reconf_files
        self.last = []                       # [{"id","status","code"}] для status.json
        self.banner = None
        self.step = None
        self.busy = None                     # id команды, исполняемой в фоне
        self._thread = None
        self._gate_cache = (0.0, None)

    # ------------------------------------------------------------ вспомогательное
    def now(self):
        return self.clock()

    def emit(self, action, code, src, mode=None, wake=False):
        try:
            from tb.core import bus
            d = {"action": _ident(action), "code": _ident(code), "src": _ident(src)}
            if mode:
                d["mode"] = _ident(mode)
            bus.emit("control_cmd", d, pri=bus.PRI_WAKE if wake else bus.PRI_INFO, root=self.root, now=self.now())
        except Exception:
            pass

    def outbox(self, cid, res):
        try:
            ctlstate.write_json(paths.control_outbox(cid + ".json", root=self.root), res)
        except OSError:
            pass

    def finish(self, cmd, status, code, data=None, extra=None, keep_outbox=False):
        """Результат: outbox + аудит + шина + журнал последних. -> результат."""
        now = self.now()
        action = cmd.get("action") if isinstance(cmd, dict) else None
        cid = cmd.get("id") if isinstance(cmd, dict) else "-"
        res = protocol.result(cid, status, code, data, now)
        if isinstance(cmd, dict) and isinstance(cid, str) and protocol.RE_ID.match(cid) and not keep_outbox:
            self.outbox(cid, res)
        if status != "running":
            args = cmd.get("args") if isinstance(cmd.get("args"), dict) else {}
            audit.append(self.root, cid, cmd.get("src"), cmd.get("sess"), action, protocol.args_digest(action or "-", args),
                         {"done": "ok", "rejected": "rejected", "failed": "failed", "expired": "expired"}.get(status, "info"), code, now=now, **(extra or {}))
        if status in ("done", "rejected", "failed", "expired") and action != "challenge":
            self.last = ([{"id": cid, "action": action, "status": status, "code": code, "ts": round(now, 1)}] + self.last)[:10]
            mode = (cmd.get("args") or {}).get("mode") if isinstance(cmd.get("args"), dict) else None
            self.emit(action or "-", code, cmd.get("src") or "-", mode, wake=(action in LIVE_ACTIONS and mode in (None, "live")) and status == "done")
        return res

    def platform(self):
        return ctlstate.platform_state(self.root)

    def save_platform(self, **kw):
        with self.lock:
            st = ctlstate.platform_state(self.root)
            st.update(kw)
            ctlstate.save_platform_state(st, self.root)
            return st

    # ------------------------------------------------------------ конвейер
    def poll_once(self):
        d = paths.control_inbox(root=self.root)
        try:
            names = sorted(n for n in os.listdir(d) if n.startswith("K-") and n.endswith(".json"))
        except OSError:
            return 0
        for n in names:
            self.process(os.path.join(d, n))
        return len(names)

    def process(self, path):
        cid = os.path.basename(path)[:-5]
        try:
            if os.path.getsize(path) > MAX_INBOX_BYTES:
                raise ValueError("big")
            with open(path, encoding="utf-8") as f:
                cmd = json.load(f)
        except (OSError, ValueError):
            cmd = None
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if cmd is None:
            audit.append(self.root, cid, "-", None, "-", "-", "rejected", "E_ARGS", now=self.now())
            return None
        ok, code = protocol.validate_command(cmd, MAX_INBOX_BYTES)
        if not ok:
            return self.finish(cmd if isinstance(cmd, dict) else {"id": cid}, "rejected", code)
        if not auth.verify_signed(cmd, self.environ):
            return self.finish(cmd, "rejected", "E_MAC")
        now = self.now()
        ttl = float(self.cfg["command_ttl_s"])
        if cmd["ts"] < now - ttl or cmd["ts"] > now + 30:
            return self.finish(cmd, "expired", "E_EXPIRED")
        if self.seen.has_id(cmd["id"]) or os.path.exists(paths.control_outbox(cmd["id"] + ".json", root=self.root)):
            return self.finish(cmd, "rejected", "E_DUP", keep_outbox=True)
        self.seen.add_id(cmd["id"])
        _ok, _msg, args = protocol.validate_args(cmd["action"], cmd.get("args"))
        cmd = dict(cmd, args=args)
        try:
            return self.dispatch(cmd)
        except Reject as r:
            return self.finish(cmd, "rejected", r.code, r.data)
        except Exception:
            return self.finish(cmd, "failed", "E_INTERNAL")

    # ------------------------------------------------------------ требования к действию
    def requirements(self, action, args):
        """-> {"danger","phrases","gates","warnings","blocked"}: что требуется для исполнения (и показывает challenge)."""
        now, st = self.now(), self.platform()
        req = {"danger": False, "phrases": [], "gates": [], "warnings": [], "blocked": False}
        if action == "day.start":
            ev = gates.evaluate(self.root, args, now, self.cfg)
            req["gates"], req["warnings"], req["blocked"] = ev["gates"], ev["warnings"], not ev["ok"]
            if args["mode"] == "live":
                req["danger"] = True
                lvl = riskstore.current(self.root)["level"]
                req["phrases"] = ["ТОРГУЮ LIVE %s УРОВЕНЬ %s" % (ctlstate.utc_day(now), lvl)] + ([ACK] if ev["warnings"] else [])
        elif action == "risk.set":
            cur = riskstore.current(self.root)
            if args["level"] > cur["level"]:
                req["danger"] = True
                ws = riskstore.warnings_for(args["level"], self.root)
                req["warnings"] = [{"id": w["id"], "code": w["code"], "msg": w["msg"], "numbers": w["numbers"]} for w in ws]
                # без набора фраз: риски владелец принимает одним нажатием под списком последствий (accepted = все id), защита: сессия, nonce, пауза
                req["phrases"] = []
            else:
                req["phrases"] = []
        elif action in ("stop.emergency", "kill.set"):
            req["danger"] = bool(args.get("flatten"))
        elif action == "carry.decide":
            req["danger"] = args["decision"] == "carry" and st.get("mode") == "live"
        elif action in ("brain.use", "brain.rollback"):
            c = brain_iface.classify(self.root, args, now, action.split(".")[1])
            if c is not None:       # tb.brain.swap (WP11-C2): уровень смены, фразы по 07b п. 6.4, мягкие предупреждения, жёсткие условия
                req["gates"], req["warnings"], req["blocked"], req["danger"] = c["gates"], c["warnings"], c["blocked"], bool(c["danger"])
                req["phrases"] = list(c["phrases"]) + ([ACK] if c["warnings"] else [])
            elif action == "brain.use":
                bs = brain_iface.status(self.root)
                if st.get("mode") == "live" or args.get("team") in (None, bs.get("live_team")):
                    req["danger"] = True
                    req["phrases"] = ["МЕНЯЮ LIVE-КОМАНДУ %s" % (args.get("team") or bs.get("live_team") or "-")]
        elif action in settings_args.ACTIONS:       # провайдеры LLM, профили, роли, биржи (WP-X)
            req = settings_ctl.requirements(self, action, args) or req
        elif action == "platform.shutdown":
            req["danger"] = True
            req["phrases"] = ["ВЫКЛЮЧАЮ ПЛАТФОРМУ"]
        return req

    def check_confirm(self, cmd, req):
        """nonce (одноразовый, 2..60 с) и фразы -> Reject(E_NONCE|E_PHRASE). Nonce сгорает только после успешной проверки фраз: опечатку можно поправить."""
        n = cmd.get("nonce")
        if req["danger"]:
            if not n:
                raise Reject("E_NONCE")
            ok, why = auth.check_nonce(n, cmd.get("sess"), protocol.args_digest(cmd["action"], cmd["args"]), self.now(), float(self.cfg["nonce_ttl_s"]),
                                       float(self.cfg["min_delay_s"]), self.environ)
            if not ok:
                raise Reject("E_NONCE", {"why": why})
            if self.seen.is_used(n):
                raise Reject("E_NONCE", {"why": "reuse"})
        if req["phrases"] and cmd.get("phrase_hash") != protocol.phrase_hash(req["phrases"]):
            raise Reject("E_PHRASE")
        if req["danger"]:
            self.seen.use_nonce(n)

    # ------------------------------------------------------------ диспетчер
    def dispatch(self, cmd):
        a, args = cmd["action"], cmd["args"]
        if a == "challenge":
            return self.h_challenge(cmd)
        if a == "platform.shutdown" and not self.cfg.get("allow_shutdown"):
            raise Reject("E_DISABLED")
        handler = {"day.start": self.h_day_start, "day.stop": self.h_day_stop, "stop.emergency": self.h_kill, "kill.set": self.h_kill, "mode.set": self.h_mode_set,
                   "risk.set": self.h_risk_set, "carry.decide": self.h_pass, "report.ack": self.h_pass, "day.resume": self.h_resume, "notify.test": self.h_pass, "notify.set": self.h_notify_set,
                   "brain.use": self.h_brain, "brain.rollback": self.h_brain, "brain.reload": self.h_brain, "intel.refresh": self.h_intel,
                   "platform.shutdown": self.h_shutdown}.get(a)
        if handler is None and a in settings_args.ACTIONS:
            handler = self.h_settings
        if handler is None:
            raise Reject("E_UNKNOWN")
        return handler(cmd)

    def h_challenge(self, cmd):
        inner, iargs = cmd["args"]["action"], cmd["args"]["args"]
        req = self.requirements(inner, iargs)
        data = {"action": inner, "danger": req["danger"], "phrases": req["phrases"], "gates": req["gates"], "warnings": req["warnings"], "blocked": req["blocked"],
                "expires_in": int(self.cfg["nonce_ttl_s"]), "min_delay": int(self.cfg["min_delay_s"]), "nonce": None}
        if inner == "day.start" and iargs.get("mode") != "live" and not req["blocked"]:
            pv = lifecycle.preflight_preview(self.root, dict(iargs), self.now())
            data["pre_amber"] = (pv or {}).get("amber") or []         # жёлтые предстартовые проверки: окно «Старт» просит принять каждую (allow_amber + accepted)
        if inner == "day.start" and self.platform()["state"] != "stopped":
            return self.finish(cmd, "rejected", "E_STATE", data)
        if req["blocked"]:
            return self.finish(cmd, "rejected", "E_GATE", data)
        if inner == "risk.set" and not riskstore.level_exists(iargs["level"], self.root):
            return self.finish(cmd, "rejected", "E_ARGS", data)
        if req["danger"]:
            data["nonce"] = auth.make_nonce(cmd.get("sess"), protocol.args_digest(inner, iargs), self.now(), self.environ)
            if data["nonce"] is None:
                return self.finish(cmd, "failed", "E_INTERNAL", data)
        return self.finish(cmd, "done", "OK", data)

    # ------------------------------------------------------------ day.start / day.stop
    def h_day_start(self, cmd):
        args = cmd["args"]
        st = self.platform()
        if st["state"] != "stopped":
            raise Reject("E_STATE", {"state": st["state"]})
        if exec_cfg.kill_state(self.root) is not None:
            raise Reject("E_GATE", {"gates": [gates.h1(gates.collect(self.root, self.now(), self.cfg, args))]})
        req = self.requirements("day.start", args)
        if req["blocked"]:
            raise Reject("E_GATE", {"gates": req["gates"]})
        warn_ids = sorted(w["id"] for w in req["warnings"])
        if warn_ids and not args["accepted"]:
            raise Reject("E_WARN", {"warnings": req["warnings"]})
        if args["mode"] == "live" and sorted(args["accepted"]) != warn_ids:
            raise Reject("E_WARN_CHANGED", {"warnings": req["warnings"]})
        self.check_confirm(cmd, req)
        self.save_platform(state="starting", since=round(self.now(), 1), mode=args["mode"], session=args["session"], step={"name": "switch", "i": 1, "n": 4})
        self.busy = cmd["id"]
        self.finish(cmd, "running", "OK", {"state": "starting"})
        if self.sync:
            return self._run_start(cmd, req)
        self._thread = threading.Thread(target=self._run_start, args=(cmd, req), daemon=True)
        self._thread.start()
        return None

    def _exec_profile(self):
        h = gates.common.exec_health(root=self.root, now=self.now())
        d = h.get("data") or {}
        return (d.get("profile"), bool(d.get("observe"))) if h.get("present") else (None, None)

    def _exec_positions(self):
        snap = gates.common.read_json(paths.exec_snapshot_file(root=self.root))
        return len((snap or {}).get("positions") or []) if isinstance(snap, dict) else 0

    def _reconf_files(self, profile, observe, timeout):
        rid = uuid.uuid4().hex[:8]
        try:
            os.remove(S.reconf_ack_file(self.root))
        except OSError:
            pass
        S.write_json(S.reconf_req_file(self.root), {"id": rid, "profile": profile, "observe": bool(observe), "ts": round(self.now(), 3)})
        end = self.now() + timeout
        while self.now() < end:
            ack = S.read_json(S.reconf_ack_file(self.root))
            if isinstance(ack, dict) and ack.get("id") == rid:
                return bool(ack.get("ok")), str(ack.get("code") or "")
            self.sleep(0.3)
        return False, "timeout"

    def _wait_exec(self, profile, observe, timeout):
        end = self.now() + timeout
        while True:
            h = gates.common.exec_health(root=self.root, now=self.now())
            d = h.get("data") or {}
            if h.get("alive") and d.get("profile") == profile and bool(d.get("observe")) == bool(observe):
                return True
            if self.now() >= end:
                return False
            self.sleep(0.5)

    def _rollback_exec(self, prev):
        if prev[0] and prev != self._exec_profile():
            self.reconf_fn(prev[0], bool(prev[1]), float(self.cfg["start_wait_s"]))

    def _fail_start(self, cmd, code, data, prev=None):
        if prev:
            self._rollback_exec(prev)
        self.save_platform(state="stopped", since=round(self.now(), 1), mode="observe", step=None, trading=False)
        self.busy = None
        return self.finish(cmd, "rejected" if code in ("E_GATE", "E_STATE", "E_AMBER") else "failed", code, data)

    def _run_start(self, cmd, req):
        args = cmd["args"]
        mode = args["mode"]
        want = PROFILE_OF[mode]
        prev = self._exec_profile()
        wait = float(self.cfg["start_wait_s"])
        try:
            if prev != want:
                if prev[0] == "trade" and want[0] != "trade" and self._exec_positions():
                    return self._fail_start(cmd, "E_STATE", {"why": "open_positions"})
                ok, why = self.reconf_fn(want[0], want[1], wait)
                if not ok or not self._wait_exec(want[0], want[1], wait):
                    return self._fail_start(cmd, "E_STATE", {"why": "reconf", "detail": why}, prev)
            self.save_platform(step={"name": "recheck", "i": 2, "n": 4})
            if mode == "live":
                f2 = gates.collect(self.root, self.now(), self.cfg, args)
                bad = [g(f2) for g in gates.HARD]
                bad = [g for g in bad if not g["ok"]]
                if bad:
                    return self._fail_start(cmd, "E_GATE", {"gates": bad, "phase": "after_switch"}, prev)
                warn2 = sorted(w["id"] for w in gates.soft(f2))
                if warn2 != sorted(args["accepted"]):
                    return self._fail_start(cmd, "E_WARN_CHANGED", {"warnings": gates.soft(f2)}, prev)
            self.save_platform(step={"name": "day", "i": 3, "n": 4})
            waivers = [{"id": w["id"], "code": w["code"], "numbers": w["numbers"]} for w in req["warnings"] if w["id"] in args["accepted"]] if mode == "live" else []
            agent = gates.brain_iface.status(self.root).get("agent") or brain_iface.DEFAULT_AGENT
            r = lifecycle.start_day(self.root, dict(args, waivers=waivers), self.now(), agent)
            if not r.get("ok"):
                return self._fail_start(cmd, r.get("code") or "E_INTERNAL", r.get("data"), prev)
            day_id = (r.get("data") or {}).get("day_id")
            if mode == "live":
                ctlstate.note_live_start(self.root, self.now())
            self.save_platform(state="running", since=round(self.now(), 1), mode=mode, session=args["session"], day_id=day_id, team=args.get("team"), agent=agent,
                               trading=mode != "observe", live_waivers=waivers, step=None)
            self.banner = None
            self.busy = None
            return self.finish(cmd, "done", "OK", {"day_id": day_id, "mode": mode, "waivers": len(waivers)}, {"warnings_accepted": waivers, "mode": mode})
        except Reject as e:
            return self._fail_start(cmd, e.code, e.data, prev)
        except Exception:
            return self._fail_start(cmd, "E_INTERNAL", {}, prev)

    def h_day_stop(self, cmd):
        st = self.platform()
        if st["state"] != "running":
            raise Reject("E_STATE", {"state": st["state"]})
        self.save_platform(state="closing", since=round(self.now(), 1), trading=False)
        lifecycle.set_trading(self.root, False, st.get("day_id"), st.get("mode"), st.get("agent"))
        r = lifecycle.stop_day(self.root, cmd["args"], self.now())
        if r.get("ok") and (r.get("data") or {}).get("state") == "closing":      # WP14: закрытие ждёт ответ владельца/исполнителя; дальше ведёт tick
            return self.finish(cmd, "done", "OK", r.get("data"))
        if st.get("mode") == "live":
            ctlstate.note_live_stop(self.root, self.now())
        self.save_platform(state="stopped", since=round(self.now(), 1), mode="observe", day_id=None, step=None, live_waivers=[])
        return self.finish(cmd, "done" if r.get("ok") else "failed", "OK" if r.get("ok") else (r.get("code") or "E_INTERNAL"), r.get("data"))

    # ------------------------------------------------------------ остальные действия
    def h_kill(self, cmd):
        args = cmd["args"]
        req = self.requirements(cmd["action"], args)
        if not (args.get("applied") and exec_cfg.kill_state(self.root) is not None):     # KILL уже на диске (создал дашборд): подтверждение не нужно
            self.check_confirm(cmd, req)
        self.apply_kill(args.get("flatten"), args.get("reason") or "emergency")
        return self.finish(cmd, "done", "OK", {"flatten": bool(args.get("flatten"))})

    def apply_kill(self, flatten, reason):
        """KILL на диске (если его создал не дашборд) и переход платформы в halted."""
        k = exec_cfg.kill_state(self.root)
        if k is None or (flatten and not k.get("flatten")):
            exec_cfg.set_kill(bool(flatten), reason, self.root)
        st = self.platform()
        if st["state"] in ("running", "starting", "closing"):
            if st.get("mode") == "live":
                ctlstate.note_live_stop(self.root, self.now())
            lifecycle.set_trading(self.root, False, st.get("day_id"), st.get("mode"), st.get("agent"))
            lifecycle.passthrough("emergency_summary", self.root, {"reason": "emergency"}, self.now())
            self.save_platform(state="halted", since=round(self.now(), 1), trading=False, step=None)
        self.banner = {"level": "alarm", "text": "аварийная остановка: KILL активен%s" % (" (flatten)" if flatten else ""), "day_report": None}

    def h_mode_set(self, cmd):
        target = cmd["args"]["mode"]
        st = self.platform()
        cur = st.get("mode") or "observe"
        if st["state"] != "running":
            raise Reject("E_STATE", {"state": st["state"]})
        if protocol.SAFER_ORDER[target] >= protocol.SAFER_ORDER.get(cur, 0):
            raise Reject("E_STATE", {"why": "only_safer", "current": cur})
        want = PROFILE_OF[target]
        if target == "observe":
            lifecycle.set_trading(self.root, False, st.get("day_id"), "observe", st.get("agent"))
            if not self._exec_positions():
                self.reconf_fn(want[0], True, float(self.cfg["start_wait_s"]))
        else:
            if self._exec_positions():
                raise Reject("E_STATE", {"why": "open_positions"})
            ok, why = self.reconf_fn(want[0], want[1], float(self.cfg["start_wait_s"]))
            if not ok or not self._wait_exec(want[0], want[1], float(self.cfg["start_wait_s"])):
                raise Reject("E_STATE", {"why": "reconf"})
            lifecycle.set_trading(self.root, True, st.get("day_id"), target, st.get("agent"))
        if cur == "live":
            ctlstate.note_live_stop(self.root, self.now())
        self.save_platform(mode=target, trading=target != "observe", live_waivers=[] if cur == "live" else st.get("live_waivers"))
        return self.finish(cmd, "done", "OK", {"mode": target})

    def h_risk_set(self, cmd):
        args = cmd["args"]
        if not riskstore.level_exists(args["level"], self.root):
            raise Reject("E_ARGS", {"why": "no_such_level"})
        req = self.requirements("risk.set", args)
        warn_ids = sorted(w["id"] for w in req["warnings"])
        if req["danger"] and sorted(args["accepted"]) != warn_ids:
            raise Reject("E_WARN" if not args["accepted"] else "E_WARN_CHANGED", {"warnings": req["warnings"]})
        self.check_confirm(cmd, req)
        r = riskstore.set_level(args["level"], args["accepted"], riskstore.phrase(args["level"], self.root), ACK if args["level"] >= 4 else None,
                                self.now(), self.root)
        if not r.get("ok"):
            errs = [str(e)[:140] for e in (r.get("errors") or [])][:3]
            code = "E_COOLDOWN" if any("кулдаун" in e for e in errs) else "E_STATE"
            raise Reject(code, {"errors": errs})
        return self.finish(cmd, "done", "OK", {"level": r.get("level"), "prev": r.get("prev")}, {"warnings_accepted": [{"id": i} for i in args["accepted"]]})

    def h_pass(self, cmd):
        name = {"carry.decide": "carry_decide", "report.ack": "report_ack", "notify.test": "notify_test"}[cmd["action"]]
        req = self.requirements(cmd["action"], cmd["args"])
        self.check_confirm(cmd, req)
        r = lifecycle.passthrough(name, self.root, cmd["args"], self.now())
        return self.finish(cmd, "done" if r.get("ok") else "rejected", "OK" if r.get("ok") else (r.get("code") or "E_UNAVAILABLE"), r.get("data"))

    def h_resume(self, cmd):
        """«Возобновить день» после перезапуска платформы: торговый флаг снова включается (WP14 api.resume_day). Для live не поддерживается: стоп и новый старт с фразой."""
        st = self.platform()
        if st["state"] != "running":
            raise Reject("E_STATE", {"state": st["state"]})
        if st.get("mode") == "live":
            raise Reject("E_STATE", {"why": "live: остановите день и запустите заново"})
        r = lifecycle.passthrough("resume_day", self.root, cmd["args"], self.now())
        if r.get("ok") and st.get("mode") != "observe":
            self.save_platform(trading=True)
        return self.finish(cmd, "done" if r.get("ok") else "rejected", "OK" if r.get("ok") else (r.get("code") or "E_UNAVAILABLE"), r.get("data"))

    def h_notify_set(self, cmd):
        """Настройки уведомлений ОС: достаточно сессии владельца (опасным не считается), аудит с перечнем изменений в note."""
        r = lifecycle.passthrough("notify_set", self.root, dict(cmd["args"], _by=cmd.get("src") or "ctl"), self.now())
        ok = bool(r.get("ok"))
        note = "; ".join((r.get("data") or {}).get("changes") or [])[:160] if ok else None
        return self.finish(cmd, "done" if ok else "rejected", "OK" if ok else (r.get("code") or "E_UNAVAILABLE"), r.get("data"), {"note": note} if note else None)

    def h_brain(self, cmd):
        req = self.requirements(cmd["action"], cmd["args"])
        if not brain_iface.available():
            return self.finish(cmd, "rejected", "E_UNAVAILABLE", {})
        if req["blocked"]:
            raise Reject("E_GATE", {"gates": req["gates"]})
        warn_ids = sorted(w["id"] for w in req["warnings"])
        acc = sorted(set(cmd["args"].get("accepted") or []))
        if warn_ids and not acc:
            raise Reject("E_WARN", {"warnings": req["warnings"]})
        if warn_ids and acc != warn_ids:
            raise Reject("E_WARN_CHANGED", {"warnings": req["warnings"]})
        self.check_confirm(cmd, req)
        r = brain_iface.swap_action(self.root, cmd["action"].split(".")[1], cmd["args"], self.now(), phrase_ok=bool(req["phrases"]))
        return self.finish(cmd, "done" if r.get("ok") else "rejected", "OK" if r.get("ok") else (r.get("code") or "E_INTERNAL"), r.get("data"))

    def h_settings(self, cmd):
        """llm.provider | brain.profile | brain.role | exchange.set: общий конвейер (гейты, предупреждения, nonce и фразы) и исполнение в settings_ctl."""
        req = self.requirements(cmd["action"], cmd["args"])
        if req["blocked"]:
            raise Reject("E_GATE", {"gates": req["gates"]})
        warn_ids = sorted(w["id"] for w in req["warnings"])
        acc = sorted(set(cmd["args"].get("accepted") or []))
        if warn_ids and not acc:
            raise Reject("E_WARN", {"warnings": req["warnings"]})
        if warn_ids and acc != warn_ids:
            raise Reject("E_WARN_CHANGED", {"warnings": req["warnings"]})
        self.check_confirm(cmd, req)
        return settings_ctl.handle(self, cmd, phrase_ok=bool(req["phrases"]))

    def h_intel(self, cmd):
        src = cmd["args"].get("source", "all")
        try:
            ctlstate.write_json(paths.runtime("intel", "refresh.req", root=self.root), {"source": src, "ts": round(self.now(), 1)})
        except OSError:
            raise Reject("E_INTERNAL")
        return self.finish(cmd, "done", "OK", {"queued": src})

    def h_shutdown(self, cmd):
        req = self.requirements("platform.shutdown", cmd["args"])
        self.check_confirm(cmd, req)
        S.write_json(S.stop_req_file(self.root), {"ts": round(self.now(), 1)})
        return self.finish(cmd, "done", "OK", {})

    # ------------------------------------------------------------ фоновые проверки
    def watch(self):
        """Раз в цикл: KILL -> halted; снятие KILL -> stopped; авто-понижение live при нарушении H1-H6."""
        now = self.now()
        st = self.platform()
        k = exec_cfg.kill_state(self.root)
        if k is not None and st["state"] in ("running", "starting", "closing") and not self.busy:
            self.apply_kill(k.get("flatten"), k.get("reason") or "KILL")
        if k is None and st["state"] == "halted":
            self.save_platform(state="stopped", since=round(now, 1), mode="observe", day_id=None)
            self.banner = {"level": "info", "text": "KILL снят владельцем: платформа остановлена, новый день запускается кнопкой «Старт»", "day_report": None}
        r = lifecycle.tick(self.root, now)
        if r and not self.busy:
            self.sync_day(r)
        st = self.platform()
        if st["state"] == "running" and st.get("mode") == "live" and st.get("trading"):
            if now - getattr(self, "_last_mon", 0.0) >= float(self.cfg["auto_downgrade_s"]):
                self._last_mon = now
                bad = gates.monitor(self.root, now, self.cfg)
                if bad:
                    self.downgrade(bad)

    def sync_day(self, r):
        """Состояние платформы за днём WP14 (возобновлённый старт, автоостановка, закрытие, завершённое владельцем): день.json главный."""
        st = self.platform()
        s, ds = st["state"], r.get("state")
        now = round(self.now(), 1)
        if s == "starting" and ds == "running":
            self.save_platform(state="running", since=now, mode=r.get("mode"), day_id=r.get("day_id"), agent=r.get("agent"), trading=r.get("mode") != "observe", step=None)
        elif s == "starting" and ds == "stopped":
            self.save_platform(state="stopped", since=now, mode="observe", step=None, trading=False)
        elif s == "running" and ds == "closing":
            self.save_platform(state="closing", since=now, trading=False)
        elif ds == "stopped" and (s == "closing" or (s == "running" and r.get("day_id") and r["day_id"] == st.get("day_id"))):      # закрытие кончилось (в т.ч. автоостановка за один tick)
            if st.get("mode") == "live":
                ctlstate.note_live_stop(self.root, self.now())
            self.save_platform(state="stopped", since=now, mode="observe", day_id=None, step=None, live_waivers=[], trading=False)

    def downgrade(self, bad):
        st = self.platform()
        lifecycle.set_trading(self.root, False, st.get("day_id"), "observe", st.get("agent"))
        self.save_platform(mode="observe", trading=False)
        ctlstate.note_live_stop(self.root, self.now())
        why = "; ".join("%s: %s" % (g["id"], g["msg"]) for g in bad)[:300]
        self.banner = {"level": "alarm", "text": "live понижен до observe: %s" % why, "day_report": None}
        audit.append(self.root, "-", "ctl", None, "auto.downgrade", protocol.sha(why), "ok", "AUTO_DOWNGRADE", now=self.now(), note=why[:200])
        self.emit("auto.downgrade", "E_GATE", "ctl", "observe", wake=True)
        if any(g["id"] == "H5" for g in bad):
            try:
                from tb.core import bus
                bus.emit("limit_hit", {"which": "daily_loss"}, pri=bus.PRI_WAKE, root=self.root, now=self.now())
            except Exception:
                pass

    # ------------------------------------------------------------ статус
    def status(self):
        now = self.now()
        st = self.platform()
        if now - self._gate_cache[0] >= 2.0 or self._gate_cache[1] is None:
            try:
                ev = gates.evaluate(self.root, {"mode": "live", "session": st.get("session") or "intraday"}, now, self.cfg)
                checks = [{"id": g["id"], "name": g["name"], "sev": "red", "ok": g["ok"], "msg": g["msg"]} for g in ev["gates"]]
                checks += [{"id": w["id"], "name": w["code"], "sev": "amber", "ok": False, "msg": w["msg"]} for w in ev["warnings"]]
                self._gate_cache = (now, checks)
            except Exception:
                self._gate_cache = (now, [{"id": "X", "name": "проверки", "sev": "amber", "ok": False, "msg": "не удалось вычислить"}])
        checks = self._gate_cache[1]
        k = exec_cfg.kill_state(self.root)
        bs = brain_iface.status(self.root)
        rl = riskstore.current(self.root)
        health = "red" if (k is not None or any(c["sev"] == "red" and not c["ok"] for c in checks if c["id"] in ("H1", "H3"))) else (
            "amber" if any(not c["ok"] for c in checks) else "green")
        if st["state"] != "running":
            health = "red" if k is not None else "green" if st["state"] == "stopped" else health
        return {"schema": "tb.ctlstatus/1", "ts": round(now, 3), "ctl": {"alive": True, "pid": os.getpid()},
                "platform": {"state": st["state"], "since": st.get("since"), "mode": st.get("mode"), "observe": st.get("mode") == "observe", "session": st.get("session"),
                             "health": health, "day_id": st.get("day_id"), "trading": bool(st.get("trading"))},
                "step": st.get("step"), "checks": checks, "kill": {"active": k is not None, "flatten": bool((k or {}).get("flatten")), "reason": str((k or {}).get("reason") or "")[:80]} if k is not None else None,
                "team": {"live_team": bs.get("live_team"), "agent": bs.get("agent"), "profiles": bs.get("profiles") or {}, "available": brain_iface.available()},
                "risk": {"level": rl["level"], "name": rl["name"], "source": rl["source"], "levels": riskstore.levels_list(self.root)},
                "pending": [{"id": self.busy, "action": "day.start"}] if self.busy else [], "last": list(self.last), "banner": self.banner or lifecycle.banner(self.root),
                "config": {"allow_shutdown": bool(self.cfg.get("allow_shutdown")), "nonce_ttl_s": self.cfg["nonce_ttl_s"], "min_delay_s": self.cfg["min_delay_s"],
                           "min_type_s": self.cfg["min_type_s"]}}

    def write_status(self):
        ctlstate.write_json(paths.control_status_file(self.root), self.status())

    def cleanup_outbox(self):
        d = paths.control_outbox(root=self.root)
        now = self.now()
        try:
            for n in os.listdir(d):
                p = os.path.join(d, n)
                if now - os.path.getmtime(p) > KEEP_OUTBOX_S:
                    os.remove(p)
        except OSError:
            pass
