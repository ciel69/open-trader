"""Общие помощники тестов плоскости управления (WP12): временный корень и каталог ключей с ПОДДЕЛЬНЫМ ключом, «хорошее» окружение для live, фейковый ctl-цикл.
Реальный state/, ~/.config/traider-bot и биржа не затрагиваются."""
import json
import os
import shutil
import tempfile
import threading
import time

import _bootstrap  # noqa: F401
from tb import paths
from tb.control import auth, client, ctlstate, handlers, lifecycle  # noqa: F401
from tb.core import risklevels

NOW = 1793106000.0          # 2026-10-27T12:20:00Z
PROJECT = _bootstrap.ROOT
SENTINEL_NOTE = "ctl-test"


def rd(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def wr(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class Env(object):
    """root + keys; env TB_KEYS_DIR / TB_ROOT выставляются на время теста."""

    def __init__(self, test, now=NOW, with_token=True, with_key=True, real_lifecycle=False):
        self.test = test
        self.now = now
        if not real_lifecycle:                 # WP12 проверяет ctl на заглушке жизненного цикла; настоящий tb.lifecycle.api (WP14) тестируют test_lifecycle_*
            old_api = lifecycle._api
            lifecycle._api = lambda: None
            test.addCleanup(setattr, lifecycle, "_api", old_api)
        self.root = tempfile.mkdtemp(prefix="tbctl_")
        self.keys = tempfile.mkdtemp(prefix="tbkeys_")
        test.addCleanup(shutil.rmtree, self.root, True)
        test.addCleanup(shutil.rmtree, self.keys, True)
        self._old = {k: os.environ.get(k) for k in ("TB_ROOT", "TB_KEYS_DIR")}
        os.environ["TB_ROOT"], os.environ["TB_KEYS_DIR"] = self.root, self.keys
        test.addCleanup(self._restore)
        for n in ("risk_profile.json", "exec_limits.json", "risk_levels.json", "platform.json"):
            src = os.path.join(PROJECT, "config", n)
            if os.path.exists(src):
                os.makedirs(os.path.join(self.root, "config"), exist_ok=True)
                shutil.copy(src, os.path.join(self.root, "config", n))
        self.token = None
        if with_key:
            auth.init_key()
            if with_token:
                self.token = auth.new_token()

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---- файлы
    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def w(self, rel, obj, raw=False):
        path = self.p(*rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(obj if raw else json.dumps(obj, ensure_ascii=False))

    def r(self, rel):
        with open(self.p(*rel.split("/")), encoding="utf-8") as f:
            return json.load(f)

    def exists(self, rel):
        return os.path.exists(self.p(*rel.split("/")))

    def brain_fixture(self, now=None, live_ok=True, budget_day_usd=2.0, eval_age_days=1):
        """WP11-C2: настоящие config/brain.json (профили ds-pro/ds-flash с live_ok и eval_ts), providers.json (провайдеры включены), роли; active.json нет (действует brain.json)."""
        now = self.now if now is None else now
        prov = json.loads(rd(os.path.join(PROJECT, "config", "providers.json")))
        for pr in prov["providers"].values():
            pr["enabled"] = True
        prov["budget"] = {"day_usd": budget_day_usd, "warn_frac": 0.8}
        self.w("config/providers.json", prov)
        doc = json.loads(rd(os.path.join(PROJECT, "config", "brain.json")))
        for pid in ("ds-pro", "ds-flash", "an-sonnet"):
            doc["profiles"][pid]["live_ok"] = bool(live_ok)
            doc["profiles"][pid]["eval_ts"] = now - eval_age_days * 86400
        self.w("config/brain.json", doc)
        dst = self.p("config", "roles")
        if not os.path.isdir(dst):
            shutil.copytree(os.path.join(PROJECT, "config", "roles"), dst)

    # ---- окружение «всё хорошо для live»
    def good_exec(self, profile="trade", observe=False, now=None, **over):
        now = self.now if now is None else now
        h = {"ts_epoch": now, "pid": 1, "env": "mainnet", "profile": profile, "mode": "live", "observe": observe, "alive": True,
             "ws": {"public": {"up": True, "age_s": 0}, "private": {"up": True, "age_s": 0}}, "rest": {"ok": True, "last_ok_s": 1, "errors_1m": 0},
             "plans": {}, "positions": 0, "kill": False, "hold": None, "hold_reasons": [], "reconcile_age_s": 5.0, "diffs": 0}
        h.update(over)
        self.w("runtime/exec_health.json", h)
        self.w("runtime/exec_snapshot.json", {"ts_epoch": now, "env": "mainnet", "account": {"equity": 30.0, "free": 30.0}, "positions": [], "orders": []})

    def good_live(self, now=None):
        now = self.now if now is None else now
        self.good_exec(now=now)
        self.w("state/portfolio.json", {"venues": {"bybit": {"equity": 30.0, "free": 30.0, "ts": iso(now)}}, "hwm": 30.0})
        self.w("config/agents.json", {"agents": {"api": {"role": "brain-api", "enabled": True, "tools": ["@read"],
                                                         "limits": {"max_risk_usdt": 1.0, "max_risk_pct": 3.0, "symbols": ["*"], "modes": ["paper", "live"],
                                                                    "max_size_mult": 1.0, "allow_add": False}}}})
        self.w("runtime/platform/brain.hb.json", {"ts_epoch": now, "pid": 7, "service": "brain"})
        self.brain_fixture(now)
        self.w("runtime/intel/facts.json", {"schema": "tb.facts/1", "ts": int(now) - 60, "degraded": [], "calendar": {"next": []}})
        rows = []
        for i in range(32):          # 32 paper-сделки за 8 дней, 12 live intraday
            rows.append({"ev": "open", "id": "P%d" % i, "mode": "paper", "stype": "intraday"})
            rows.append({"ev": "close", "id": "P%d" % i, "ts_close": iso(now - (i % 8) * 86400 - 3600), "pnl_usdt": 0.1})
        for i in range(12):
            rows.append({"ev": "open", "id": "L%d" % i, "mode": "live", "stype": "intraday"})
            rows.append({"ev": "close", "id": "L%d" % i, "ts_close": iso(now - 2 * 86400 - i), "pnl_usdt": 0.05})
        self.w("state/trades.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n", raw=True)
        self.w("config/control.json", {"version": 1, "auth": {"mode": "token"}})      # тесты WP12 идут в режиме token (как раньше); open_local: test_control_auth.py
        if risklevels.current_level(self.root)["source"] != "signed":
            r = risklevels.set_level(3, "owner", self.root, phrase=risklevels.confirm_phrase(3, self.root), now=now - 10)
            self.test.assertTrue(r["ok"], r)

    # ---- ctl и команды
    def ctl(self, clock=None, reconf=None, sync=True):
        self.clock_t = [self.now]
        c = handlers.Ctl(self.root, clock=clock or (lambda: self.clock_t[0]), sleep=lambda s: self.advance(s), sync=sync, reconf=reconf)
        return c

    def advance(self, s):
        self.clock_t[0] += s

    def cmd(self, action, args=None, nonce=None, phrases=None, sess="s-abcd1234", src="dash", ts=None, ctl=None):
        ok, msg, a = handlers.protocol.validate_args(action, args or {}) if action != "challenge" else (True, "", args)
        c = client.build(action, a if ok else (args or {}), src, sess, nonce, phrases, now=self.clock_t[0] if ts is None else ts)
        return c

    def send(self, ctl, action, args=None, **kw):
        """Подписать, положить в inbox, прогнать один цикл ctl, вернуть результат из outbox."""
        c = self.cmd(action, args, **kw)
        client.submit(c, self.root)
        ctl.poll_once()
        return client.read_result(c["id"], self.root), c

    def challenge(self, ctl, action, args, sess="s-abcd1234"):
        res, c = self.send(ctl, "challenge", {"action": action, "args": args}, sess=sess)
        return res

    def confirmed(self, ctl, action, args, sess="s-abcd1234", wait=3.0, phrases=None, **kw):
        """challenge -> ждать min_delay -> command с nonce и фразами challenge."""
        ch = self.challenge(ctl, action, args, sess=sess)
        d = ch["data"]
        self.advance(wait)
        a = dict(args)
        if d.get("warnings"):
            a["accepted"] = [w["id"] for w in d["warnings"]]
        ph = phrases if phrases is not None else d.get("phrases") or None
        return self.send(ctl, action, a, nonce=d.get("nonce"), phrases=ph, sess=sess, **kw)[0]

    def audit(self):
        from tb.control import audit
        return audit.tail(self.root, 500)


def pump(ctl, stop, period=0.02):
    """Фоновый цикл ctl для тестов веб-слоя (challenge ждёт ответ)."""
    def run():
        while not stop.is_set():
            try:
                ctl.poll_once()
            except Exception:
                pass
            time.sleep(period)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
