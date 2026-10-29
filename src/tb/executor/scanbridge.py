"""Мост сканера: хвост runtime/hot_events.jsonl -> шина (03 п. 3.3 scanner_signal). Владелец: WP5.

Белый список полей: kind (из bus.SCANNER_KINDS), px, score, dir, range_5m_pct, ret_5m_pct, turn_surge_5m, spread_bps, funding_pct, idea, wake.
Любой текст сканера (reasons, note, idea_note) и неизвестные типы отбрасываются: внешний текст в шину не попадает. Позиция в файле хранится в
памяти; при старте читается только новое (с конца файла), чтобы не заливать шину историей.
"""
import json
import os
import re

from tb import paths
from tb.core import bus

_SYM = re.compile(r"^[A-Z0-9]{2,20}$")
_IDEA = re.compile(r"^I\d+$")
NUM_KEYS = (("price", "px"), ("score", "score"), ("range_5m_pct", "range_5m_pct"), ("ret_5m_pct", "ret_5m_pct"), ("turn_surge_5m", "turn_surge_5m"),
            ("spread_bps", "spread_bps"), ("funding_pct", "funding_pct"))


class ScanBridge(object):
    def __init__(self, root=None, clock=None):
        self.root = root
        self.clock = clock
        self.pos = None
        self.sent = 0

    def convert(self, r):
        """Строка сканера -> (type, d, sym) или None."""
        if not isinstance(r, dict) or r.get("type") not in bus.SCANNER_KINDS:
            return None
        sym = r.get("sym")
        if not isinstance(sym, str) or not _SYM.match(sym):
            return None
        d = {"kind": r["type"]}
        for src, dst in NUM_KEYS:
            v = r.get(src)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf"):
                d[dst] = v
        if r.get("direction") in ("up", "down", "long", "short"):
            d["dir"] = r["direction"]
        if isinstance(r.get("idea"), str) and _IDEA.match(r["idea"]):
            d["idea"] = r["idea"]
        d["wake"] = bool(r.get("wake"))
        return "scanner_signal", d, sym

    def poll(self):
        """Новые строки -> шина. -> число отправленных событий.
        TB_SCAN_BRIDGE=0: мост выключен (события в шину кладёт сервис сканера платформы, WP11-B: иначе дубли)."""
        if os.environ.get("TB_SCAN_BRIDGE") == "0":
            return 0
        path = paths.runtime("hot_events.jsonl", root=self.root)
        try:
            size = os.path.getsize(path)
        except OSError:
            return 0
        if self.pos is None or size < self.pos:
            self.pos = size if self.pos is None else 0
            if self.pos == size:
                return 0
        n = 0
        with open(path, "rb") as f:
            f.seek(self.pos)
            data = f.read()
        nl = data.rfind(b"\n")
        if nl < 0:
            return 0
        self.pos += nl + 1
        for ln in data[:nl].split(b"\n"):
            try:
                r = json.loads(ln.decode("utf-8", "replace"))
            except ValueError:
                continue
            c = self.convert(r)
            if c is None:
                continue
            try:
                bus.emit(c[0], c[1], sym=c[2], root=self.root, now=self.clock() if self.clock else None)
                n += 1
            except Exception:
                pass
        self.sent += n
        return n
