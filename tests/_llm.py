"""Общие помощники тестов слоя llm (WP11-C1): временный корень, поддельные ключи (НЕ настоящие), реестр на фейковом сервере."""
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401
from fakes.fake_llm_http import FakeLLMServer
from tb.llm import providers
from tb.llm.client import LLMClient
from tb.llm.transport import Transport

FAKE_DS_KEY = "sk-FAKEDEEPSEEK-0123456789abcdef"
FAKE_AN_KEY = "sk-ant-FAKEANTHROPIC-0123456789abcdef"
PEAK = {"days": [0, 1, 2, 3, 4], "hours": [[1, 4], [6, 10]]}


def doc(url, **over):
    d = {"version": 1, "budget": {"day_usd": 2.0, "warn_frac": 0.8}, "providers": {
        "ds": {"adapter": "deepseek", "base_url": url, "beta_url": url + "/beta", "key_file": "ds.env", "key_env": "DS_KEY", "enabled": True,
               "timeouts": {"connect_s": 2, "read_s": 2, "total_s": 10}, "retry": {"max": 3, "base_s": 1, "max_s": 8, "on": [429, 500, 503]},
               "limits": {"day_usd": 3.0},
               "models": {"flash": {"name": "m-flash", "tools": True, "strict": False, "json_mode": True, "thinking": "param",
                                    "price": {"in_miss": 0.30, "in_hit": 0.006, "out": 1.20, "offpeak_factor": 0.5, "peak_utc": PEAK}},
                          "pro": {"name": "m-pro", "tools": True, "strict": True, "json_mode": True, "thinking": "param",
                                  "price": {"in_miss": 1.0, "in_hit": 0.1, "out": 4.0}}}},
        "an": {"adapter": "anthropic", "base_url": url, "version_header": "2023-06-01", "key_file": "an.env", "key_env": "AN_KEY", "enabled": True,
               "timeouts": {"connect_s": 2, "read_s": 2, "total_s": 10}, "retry": {"max": 2, "base_s": 1, "max_s": 8, "on": [429, 500, 503]},
               "limits": {},
               "models": {"son": {"name": "m-son", "tools": True, "strict": True, "json_mode": False, "thinking": "param", "cache": "explicit",
                                  "price": {"in": 2.0, "in_hit": 0.2, "cache_write_factor": 1.25, "out": 10.0}}}},
        "loc": {"adapter": "openai-compatible", "base_url": url + "/v1", "key_file": None, "key_env": None, "enabled": True,
                "timeouts": {"connect_s": 2, "read_s": 2, "total_s": 5}, "retry": {"max": 1, "base_s": 1, "max_s": 2, "on": [500, 503]}, "limits": {},
                "models": {"q": {"name": "m-q", "tools": True, "strict": False, "json_mode": True, "thinking": "none", "price": {"in": 0, "out": 0}}}}}}
    d.update(over)
    return d


class LLMBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tbllm_")
        self.keys = os.path.join(self.root, "keys")
        os.makedirs(self.keys)
        with open(os.path.join(self.keys, "ds.env"), "w") as f:
            f.write("DS_KEY=%s\n" % FAKE_DS_KEY)
        with open(os.path.join(self.keys, "an.env"), "w") as f:
            f.write("AN_KEY=%s\n" % FAKE_AN_KEY)
        self.env = {"TB_KEYS_DIR": self.keys}
        self.srv = FakeLLMServer().start()
        self.sleeps = []
        self.tr = Transport(sleep=self.sleeps.append, rand=lambda: 0.5)
        self.reg = providers.parse(doc(self.srv.url))
        self._env_before = dict(os.environ)

    def tearDown(self):
        self.tr.close()
        self.srv.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def client(self, pid="ds", mkey="flash", **kw):
        kw.setdefault("transport", self.tr)
        kw.setdefault("environ", self.env)
        return LLMClient(self.reg, pid, mkey, root=self.root, **kw)
