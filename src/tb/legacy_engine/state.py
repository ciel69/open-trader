"""state.json + journal.jsonl (Agent B). Только stdlib, Python 3.9."""
import datetime
import json
import math
import os
import re
import tempfile
import time
from tb import fsutil

UTC = datetime.timezone.utc


def parse_ts(v):
    """ISO-строка / epoch / datetime -> epoch float (UTC) или None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, datetime.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v.timestamp()
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    s = str(v).strip()
    if not s:
        return None
    try:
        f = float(s)
        return f if math.isfinite(f) else None
    except ValueError:
        pass
    if s[-1] in "zZ":
        s = s[:-1] + "+00:00"
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def iso_utc(epoch):
    return datetime.datetime.fromtimestamp(float(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_str(epoch):
    return datetime.datetime.fromtimestamp(float(epoch), UTC).strftime("%Y-%m-%d")


def default_state():
    return {
        "version": 1,
        "session_active": False,
        "day": None,
        "day_start_equity": None,
        "realized_pnl_today": 0.0,
        "trades_today": 0,
        "last_stop_ts": {},     # symbol -> epoch
        "last_close_ts": {},    # symbol -> epoch (любое исчезновение позиции)
        "halted": False,
        "halt_reason": "",
        "last_tick_ts": None,
        "last_equity": None,
        "positions": {},        # contract_id -> трекинг позиции
        "pending_open": {},     # symbol -> {ts, side, stop, tp, margin}
    }


def _num(v, default=None):
    try:
        if v is None or isinstance(v, bool):
            return default
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _normalize(st):
    d = default_state()
    for k, v in st.items():
        d[k] = v
    for k in ("last_stop_ts", "last_close_ts", "positions", "pending_open"):
        if not isinstance(d.get(k), dict):
            d[k] = {}
    d["session_active"] = d.get("session_active") is True
    d["halted"] = d.get("halted") is True
    d["halt_reason"] = str(d.get("halt_reason") or "")
    try:
        d["trades_today"] = int(d.get("trades_today") or 0)
    except (TypeError, ValueError):
        d["trades_today"] = 0
    d["realized_pnl_today"] = _num(d.get("realized_pnl_today"), 0.0)
    d["day_start_equity"] = _num(d.get("day_start_equity"))
    d["last_equity"] = _num(d.get("last_equity"))
    d["last_tick_ts"] = parse_ts(d.get("last_tick_ts"))
    return d


def load_state(path):
    """Нет файла -> чистое состояние (session_active=False).
    Битый файл -> halted=True (счётчики дня потеряны: не торговать до --start)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return default_state()
    except OSError as e:
        st = default_state()
        st["halted"] = True
        st["halt_reason"] = "state_unreadable: %s" % e
        return st
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("state is not an object")
    except ValueError as e:
        st = default_state()
        st["halted"] = True
        st["halt_reason"] = "state_corrupt: %s" % e
        return st
    return _normalize(obj)


def save_state(path, st):
    """Атомарная запись: tmp в той же папке + fsync + os.replace."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        fsutil.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def journal(path, event):
    """Дописывает строку jsonl; ts добавляется, если его нет."""
    ev = dict(event)
    if not ev.get("ts"):
        ev["ts"] = iso_utc(time.time())
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
    return ev


def read_journal(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if isinstance(o, dict):
                    out.append(o)
    except OSError:
        pass
    return out


def new_day_reset(st, now, equity):
    """Сброс дневных счётчиков (UTC-день от now). cooldown'ы по контрактам сохраняются."""
    ts = parse_ts(now)
    if ts is None:
        ts = time.time()
    st["day"] = day_str(ts)
    st["day_start_equity"] = _num(equity)
    st["realized_pnl_today"] = 0.0
    st["trades_today"] = 0
    st["halted"] = False
    st["halt_reason"] = ""
    return st
