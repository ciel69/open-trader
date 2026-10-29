"""Общие помощники тестов платформы (WP11-B): временный корень, скрипты-пустышки, быстрые настройки супервизора. Тесты не пишут в реальный state/."""
import os
import shutil
import sys
import tempfile
import time

import _bootstrap  # noqa: F401
from tb.platform import services as S

PY = sys.executable

SLEEP = "import time\nwhile True:\n    time.sleep(0.05)\n"
CRASH = "import sys\nsys.exit(1)\n"
CODE2 = "import sys\nsys.exit(2)\n"
ENVDUMP = ("import json, os, sys, time\n"
           "json.dump(dict(os.environ), open(os.path.join(os.environ['TB_ROOT'], 'env_' + os.environ['TB_PLATFORM_SERVICE'] + '.json'), 'w'))\n"
           "while True:\n    time.sleep(0.05)\n")


def mk_root(test):
    d = tempfile.mkdtemp(prefix="tbplat_")
    test.addCleanup(shutil.rmtree, d, True)
    return d


def script(root, name, body):
    p = os.path.join(root, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return [PY, p]


def fast_cfg(**sv):
    cfg = S.load_cfg(None)
    cfg["supervisor"].update({"poll_s": 0.02, "status_s": 0.05, "stop_grace_s": 3.0, "exec_stop_grace_s": 3.0, "fatal_within_s": 30.0,
                              "retry_after_giveup_s": 0})
    cfg["supervisor"]["restart"] = {"base_s": 0.03, "factor": 2.0, "max_s": 0.2, "max_tries": 3}
    cfg["supervisor"].update(sv)
    for n in cfg["svc"]:
        cfg["svc"][n]["hb"] = "none"
    return cfg


def wait_for(pred, timeout=8.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""
