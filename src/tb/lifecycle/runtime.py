"""Среда выполнения lifecycle: часы, сон, порты мозга и исполнителя. По умолчанию боевые; тесты подменяют через configure()."""
import time

from tb.lifecycle import cfg as cfgmod
from tb.lifecycle import ports

_RT = {"clock": None, "sleep": time.sleep, "brain": None, "exec": None, "sync": False}


def configure(clock=None, sleep=None, brain=None, execport=None, sync=None):
    if clock is not None:
        _RT["clock"] = clock
    if sleep is not None:
        _RT["sleep"] = sleep
    if brain is not None:
        _RT["brain"] = brain
    if execport is not None:
        _RT["exec"] = execport
    if sync is not None:
        _RT["sync"] = bool(sync)


def reset():
    _RT.update({"clock": None, "sleep": time.sleep, "brain": None, "exec": None, "sync": False})


def sync_mode():
    return bool(_RT["sync"])


class Ctx(object):
    """Контекст одного вызова: корень, настройки, порты, время. now() = заданные часы либо now вызова + прошедшее по monotonic."""

    def __init__(self, root, now):
        self.root = root
        self.cfg = cfgmod.load(root)
        self.brain = _RT["brain"] or ports.FileJobBrain(root)
        self.execp = _RT["exec"] or ports.FileExecPort(root)
        self._t0, self._m0 = float(now), time.monotonic()
        self._clock = _RT["clock"]
        self._sleep = _RT["sleep"]

    def now(self):
        if self._clock is not None:
            return self._clock()
        return self._t0 + (time.monotonic() - self._m0)

    def sleep(self, s):
        self._sleep(s)
