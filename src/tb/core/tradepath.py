"""Путь цены внутри сделки (MAE/MFE) без новых запросов: трекер живёт в цикле сканера hotscan и пользуется ценами, которые он и так получил.

На каждом скане PathTracker.update(tickers, now) берёт открытые live-сделки Bybit из журнала (state/trades.jsonl читается кэшем по mtime+размеру)
и обновляет runtime/trade_paths.json (атомарно, fsutil.replace; после рестарта сканера состояние подхватывается из файла):
  {"ts_epoch", "max_gap_s", "trades": {ID: {"sym","side","entry","sl","tp","ts_open","min","max","t_min","t_max","n","last","last_ts","prev_ts","covered_s"}}}
min/max стартуют с цены входа; t_* = epoch достижения; n = число замеров; covered_s = сумма интервалов между соседними замерами, не длиннее max_gap_s
(интервал дольше = сканер не работал, это дыра в пути: экстремум мог прийти внутри неё).
Цена = lastPrice раза в ~30 с, поэтому MAE/MFE занижены относительно настоящих (быстрый прокол между замерами не виден). Стоп и тейк на бирже тоже по LastPrice.

При закрытии journal.cmd_close вызывает finalize() и пишет в событие close поле path (а не файл-состояние): сделки, закрытые до внедрения, поля не имеют.
  path = {"mae_pct","mfe_pct" (от цены входа, с учётом стороны, оба >= 0: MAE худшее движение против, MFE лучшее за), "t_mae_s","t_mfe_s" (секунды от входа),
          "mae_r","mfe_r" (в долях дистанции до стопа, 1.0 = дошла до стопа; комиссии не входят), "mfe_to_tp" (доля пути до тейка, 1.0 = дошла до тейка),
          "n" (замеров), "path_coverage" (0..1: доля времени сделки, покрытая замерами), "src": "hotscan"}
Цена выхода тоже входит в экстремумы (она реальная). Гонка закрытия: замер между исполнением стопа и записью close попадает в путь, это доли секунды-30 с.
"""
import calendar
import json
import os
import time

from tb import fsutil

MAX_GAP_S = 120.0       # интервал между замерами длиннее считается дырой (3-4 скана по 30 с)
STATE_NAME = "trade_paths.json"


def parse_ts(s):
    try:
        return float(calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def load_state(path):
    """-> dict {id: запись}; нет файла/мусор -> {}."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        t = d.get("trades") if isinstance(d, dict) else None
        return {k: v for k, v in t.items() if isinstance(v, dict) and _num(v.get("entry")) and _num(v.get("ts_open"))} if isinstance(t, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def compute(st, side, exit_px=None, ts_end=None, sl=None, tp=None, max_gap=MAX_GAP_S):
    """Запись состояния -> поля path (см. модуль). exit_px/ts_end - момент закрытия (или None для живой сделки: конец = последний замер)."""
    entry = _num(st.get("entry"))
    lo, hi = _num(st.get("min")), _num(st.get("max"))
    t_lo, t_hi = _num(st.get("t_min")), _num(st.get("t_max"))
    t0 = _num(st.get("ts_open"))
    if not entry or lo is None or hi is None or t0 is None:
        return None
    px = _num(exit_px)
    if px and ts_end is not None:
        if px < lo:
            lo, t_lo = px, ts_end
        if px > hi:
            hi, t_hi = px, ts_end
    sign = 1.0 if side == "long" else -1.0
    if sign > 0:
        mae, mfe, t_mae, t_mfe = (entry - lo) / entry, (hi - entry) / entry, t_lo, t_hi
    else:
        mae, mfe, t_mae, t_mfe = (hi - entry) / entry, (entry - lo) / entry, t_hi, t_lo
    mae, mfe = max(mae, 0.0), max(mfe, 0.0)
    end = ts_end if ts_end is not None else (_num(st.get("last_ts")) or t0)
    covered = _num(st.get("covered_s")) or 0.0
    prev = _num(st.get("prev_ts")) or t0
    tail = end - prev
    if 0 < tail <= max_gap:
        covered += tail
    total = end - t0
    cov = 1.0 if total <= 0 else min(1.0, covered / total)
    if not st.get("n"):
        cov = 0.0
    out = {"mae_pct": round(mae * 100.0, 4), "mfe_pct": round(mfe * 100.0, 4),
           "t_mae_s": int(max(0.0, (t_mae or t0) - t0)), "t_mfe_s": int(max(0.0, (t_mfe or t0) - t0))}
    slv, tpv = _num(sl if sl is not None else st.get("sl")), _num(tp if tp is not None else st.get("tp"))
    if slv:
        d = abs(entry - slv) / entry
        if d > 0:
            out["mae_r"], out["mfe_r"] = round(mae / d, 3), round(mfe / d, 3)
    if tpv:
        d = abs(tpv - entry) / entry
        if d > 0:
            out["mfe_to_tp"] = round(mfe / d, 3)
    out.update({"n": int(st.get("n") or 0), "path_coverage": round(cov, 3), "src": "hotscan"})
    return out


def finalize(trade, exit_px, ts_close_epoch, state_path):
    """Для journal.cmd_close: path закрываемой сделки из файла-состояния или None (трекер её не видел)."""
    try:
        st = load_state(state_path).get(trade.get("id"))
        if not st:
            return None
        return compute(st, trade.get("side"), exit_px, ts_close_epoch, trade.get("sl"), trade.get("tp"))
    except Exception:
        return None


class PathTracker:
    def __init__(self, journal_path, state_path, mode="live", venue="bybit", max_gap=MAX_GAP_S, quote="USDT"):
        self.journal_path, self.state_path, self.mode, self.venue = journal_path, state_path, mode, venue
        self.max_gap, self.quote = max_gap, quote
        self.state = load_state(state_path)
        self._jstamp, self._open, self._closed = None, {}, set()

    # --- журнал: открытые сделки (кэш по mtime+размеру)
    def open_trades(self):
        try:
            stt = os.stat(self.journal_path)
            stamp = (stt.st_mtime, stt.st_size)
        except OSError:
            self._jstamp, self._open, self._closed = None, {}, set()
            return {}
        if stamp == self._jstamp:
            return self._open
        opened, closed = {}, set()
        try:
            with open(self.journal_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue            # оборванная/битая строка (идёт запись)
                    if not isinstance(r, dict) or not r.get("id"):
                        continue
                    if r.get("ev") == "open":
                        opened[r["id"]] = r
                        closed.discard(r["id"])
                    elif r.get("ev") == "close" and r["id"] in opened:
                        closed.add(r["id"])
        except OSError:
            return self._open
        self._jstamp, self._closed = stamp, closed
        self._open = {i: r for i, r in opened.items() if i not in closed and r.get("mode") == self.mode and r.get("venue") == self.venue
                      and r.get("side") in ("long", "short") and _num(r.get("entry")) and parse_ts(r.get("ts_open")) is not None}
        return self._open

    def _save(self, now):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp.%d" % os.getpid()
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"ts_epoch": now, "max_gap_s": self.max_gap, "trades": self.state}, f, ensure_ascii=False, separators=(",", ":"))
            fsutil.replace(tmp, self.state_path)
        except OSError:
            pass

    def update(self, tickers, now):
        """tickers: {SOLUSDT: {"price": ...}} как в hotscan. -> число обновлённых сделок. Не бросает при мусоре в журнале/состоянии."""
        trades = self.open_trades()
        dirty = False
        for tid in [i for i in self.state if i in self._closed]:     # закрытые в журнале: путь уже ушёл в событие close
            del self.state[tid]
            dirty = True
        n = 0
        for tid, r in trades.items():
            sym = str(r.get("sym") or "").upper()
            tk = tickers.get(sym if sym.endswith(self.quote) else sym + self.quote)
            px = _num(tk.get("price")) if isinstance(tk, dict) else None
            t0 = parse_ts(r.get("ts_open"))
            if not px or px <= 0 or now < t0:
                continue
            st = self.state.get(tid)
            if st is None or _num(st.get("entry")) != _num(r["entry"]):
                e = _num(r["entry"])
                st = self.state[tid] = {"sym": sym, "side": r["side"], "entry": e, "sl": _num(r.get("sl")), "tp": _num(r.get("tp")), "ts_open": t0,
                                        "min": e, "max": e, "t_min": t0, "t_max": t0, "n": 0, "last": None, "last_ts": None, "prev_ts": t0, "covered_s": 0.0}
            dt = now - st["prev_ts"]
            if dt > 0 and dt <= self.max_gap:
                st["covered_s"] = round(st["covered_s"] + dt, 3)
            if dt > 0:
                st["prev_ts"] = now
            if px < st["min"]:
                st["min"], st["t_min"] = px, now
            if px > st["max"]:
                st["max"], st["t_max"] = px, now
            st["n"] += 1
            st["last"], st["last_ts"] = px, now
            n += 1
        if n or dirty:
            self._save(now)
        return n
