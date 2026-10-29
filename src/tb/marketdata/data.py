"""Хранение тиков, ресемплинг в бары, синтетические данные. Только stdlib."""
import calendar
import math
import os
import random
import time
from datetime import datetime

SYNTH_START_EPOCH = 1767225600.0  # 2026-01-01T00:00:00Z
SYNTH_STEP = 60.0


def parse_ts(ts):
    """ISO-8601 ('2026-10-27T06:30:00Z', допускается дробная часть/офсет +00:00) или epoch -> float epoch."""
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip()
    try:
        return float(s)
    except ValueError:
        pass
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1]
    if s.endswith("+00:00"):
        s = s[:-6]
    frac = 0.0
    if "." in s:
        s, f = s.split(".", 1)
        frac = float("0." + f)
    dt = datetime.strptime(s.replace(" ", "T"), "%Y-%m-%dT%H:%M:%S")
    return float(calendar.timegm(dt.timetuple())) + frac


def epoch_to_iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def append_tick(path, ts_iso, price):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("ts_iso,price\n")
        f.write("%s,%s\n" % (ts_iso, repr(float(price))))


def load_ticks(path, max_rows=None):
    """-> [(ts_epoch, price)] по возрастанию времени; битые/нулевые строки пропускаются; max_rows = последние N."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("ts"):
                continue
            parts = line.split(",")
            if len(parts) < 2:
                continue
            try:
                ts = parse_ts(parts[0])
                px = float(parts[1])
            except (ValueError, TypeError):
                continue
            if not (px > 0) or math.isnan(px) or math.isinf(px):
                continue
            out.append((ts, px))
    out.sort(key=lambda t: t[0])
    if max_rows is not None and max_rows > 0 and len(out) > max_rows:
        out = out[-max_rows:]
    return out


def load_candles(path):
    """CSV ts_iso,o,h,l,c,volume -> [(ts_epoch, o, h, l, c, v)] по возрастанию времени; битые строки пропускаются; нет файла -> []."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) < 6 or parts[0].startswith("ts"):
                continue
            try:
                row = (parse_ts(parts[0]),) + tuple(float(x) for x in parts[1:6])
            except (ValueError, TypeError):
                continue
            if row[4] > 0 and row[2] >= row[3] and not any(math.isnan(x) or math.isinf(x) for x in row[1:]):
                out.append(row)
    out.sort(key=lambda r: r[0])
    return out


def to_bars(ticks, bar_seconds, drop_last=False):
    """Тики -> OHLC бары. ts бара = начало бакета (epoch). Пустые бакеты пропускаются.
    drop_last=True отбрасывает последний (возможно незакрытый) бар."""
    bars = []
    cur = None
    for ts, px in ticks:
        b = math.floor(ts / bar_seconds) * bar_seconds
        if cur is None or b != cur["ts"]:
            if cur is not None:
                bars.append(cur)
            cur = {"ts": float(b), "o": px, "h": px, "l": px, "c": px}
        else:
            if px > cur["h"]:
                cur["h"] = px
            if px < cur["l"]:
                cur["l"] = px
            cur["c"] = px
    if cur is not None and not drop_last:
        bars.append(cur)
    return bars


def synthetic_ticks(n, seed, regime="mixed", start_price=2300.0, sigma=0.0007):
    """Минутные тики. regime: trend (дрейф со сменой знака), range (mean-reversion OU),
    mixed (сегменты trend/range/shock + редкие скачки и fat tails). Детерминировано по seed."""
    if regime not in ("trend", "range", "mixed"):
        raise ValueError("unknown regime: %s" % regime)
    rng = random.Random(seed)
    x = math.log(start_price)
    out = []
    seg_left = 0
    mode = "range"
    drift = 0.0
    center = x
    for i in range(n):
        if seg_left <= 0:
            if regime == "mixed":
                mode = rng.choice(["trend", "range", "range", "shock"])
                seg_left = rng.randint(200, 600)
            else:
                mode = regime
                seg_left = rng.randint(400, 900)
            drift = rng.choice([-1.0, 1.0]) * rng.uniform(0.0002, 0.0005)
            center = x
        seg_left -= 1
        eps = rng.gauss(0.0, 1.0)
        if mode == "trend":
            step = drift + sigma * eps
        elif mode == "range":
            step = 0.02 * (center - x) + sigma * eps
        else:  # shock: повышенная волатильность + скачки
            vol = sigma * 2.0
            step = vol * eps
            if rng.random() < 0.01:
                step += rng.choice([-1.0, 1.0]) * rng.uniform(0.005, 0.015)
        if regime == "mixed" and rng.random() < 0.002:
            step += rng.choice([-1.0, 1.0]) * rng.uniform(0.004, 0.012)
        x += step
        out.append((SYNTH_START_EPOCH + i * SYNTH_STEP, math.exp(x)))
    return out
