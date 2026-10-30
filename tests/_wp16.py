"""Оснастка тестов WP16: временный TB_ROOT (config/state) и временный TB_KEYS_DIR с ПОДДЕЛЬНЫМ ключом; реальные state/, config/ и ~/.config не используются."""
import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
import _wp16_scen as S
from tb.core import risklevels

ROOT = _bootstrap.ROOT
FAKE_KEY_HEX = "ab" * 32
GOLDEN = os.path.join(ROOT, "tests", "fixtures_wp16", "guard_golden.json")


def jload(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def jdump(obj, p):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def _live_platform():
    """Есть ли живая платформа владельца (heartbeat живого чужого процесса): она сама пишет шину событий, утечку теста от неё не отличить."""
    from tb.core import filelock
    d = os.path.join(ROOT, "runtime", "platform")
    try:
        names = os.listdir(d)
    except OSError:
        return False
    for n in names:
        if n.endswith(".hb.json"):
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    pid = json.load(f).get("pid")
                if isinstance(pid, int) and pid != os.getpid() and filelock.pid_alive(pid):
                    return True
            except (OSError, ValueError, AttributeError):
                pass
    return False


def real_files():
    out = [os.path.join(ROOT, "state", "control", "risk_level.json"), os.path.join(ROOT, "state", "control", "risk_audit.jsonl"),
           os.path.join(ROOT, "config", "risk_levels.json"), os.path.join(ROOT, "config", "risk_profile.json")]
    if not _live_platform():
        out.append(os.path.join(ROOT, "state", "bus", "events.jsonl"))
    return out


def stat_of(p):
    try:
        st = os.stat(p)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


class Base(unittest.TestCase):
    with_levels = True

    def setUp(self):
        self._real = {p: stat_of(p) for p in real_files()}
        self.root = S.make_root(self.with_levels, ROOT)
        self.keys = tempfile.mkdtemp(prefix="tbkeys_")
        self.put_key(FAKE_KEY_HEX)
        self._env = {k: os.environ.get(k) for k in ("TB_ROOT", "TB_KEYS_DIR")}
        os.environ["TB_ROOT"] = self.root
        os.environ["TB_KEYS_DIR"] = self.keys
        self._old = S._patch_paths(os.path.join(self.root, "state"))

    def tearDown(self):
        S._restore(self._old)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.keys, ignore_errors=True)
        self.assertEqual(self._real, {p: stat_of(p) for p in real_files()}, "тест тронул реальные state/config")

    def put_key(self, hexkey):
        with open(os.path.join(self.keys, "control.json"), "w", encoding="utf-8") as f:
            json.dump({"hmac_key": hexkey}, f)

    def drop_key(self):
        os.remove(os.path.join(self.keys, "control.json"))

    def cfg(self):
        with open(os.path.join(self.root, "config", "risk_levels.json"), encoding="utf-8") as f:
            return json.load(f)

    def put_cfg(self, cfg):
        with open(os.path.join(self.root, "config", "risk_levels.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def prof(self):
        with open(os.path.join(self.root, "config", "risk_profile.json"), encoding="utf-8") as f:
            return json.load(f)

    def sign_level(self, level, by="owner", history=None, key=None):
        """Кладёт корректно подписанный файл уровня напрямую (минуя set_level) -> тело."""
        body = {"level": level, "since": "2026-10-28T00:00:00Z", "by": by, "history": history or []}
        body["mac"] = risklevels.sign_state(body, bytes.fromhex(key or FAKE_KEY_HEX))
        p = risklevels.level_file(self.root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(body, f)
        return body

    def phrase(self, n):
        return risklevels.confirm_phrase(n, self.root)

    def raise_to(self, n, now=None):
        req = [w["id"] for w in risklevels.level_warnings(n, self.root) if w["required"]]
        return risklevels.set_level(n, "owner", self.root, accepted=req, phrase=self.phrase(n), ack=risklevels.ACK_PHRASE, now=now)
