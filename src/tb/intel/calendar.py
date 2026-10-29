"""Календарь событий, blackout и расписание рынка США (07 п. 3.2). Владелец: WP13. Без сети и без LLM.

Источники событий (приоритет сверху вниз):
  1. `config/macro_events.json` (владелец; `[{"id","name","type","ts_utc"|"ts_iso","impact"}]`) -> verified:true;
  2. импорт с официальных страниц (BLS ICS, страница FOMC): `runtime/intel/cache/cal_*.json` -> verified = флаг источника (до `intel verify` false);
  3. КАНДИДАТЫ из `config/intel.json: calendar.candidates` (даты по памяти автора, НЕ проверены) -> verified:false.
Непроверенные события всегда помечены `v:false` и в facts, и в blackouts; по умолчанию они ТОЖЕ дают окна blackout (ложное окно лишь запрещает торговлю,
пропущенное опасно), отключается `calendar.unverified_blackout=false`. Выдавать кандидатов за факт нельзя: ПРОВЕРИТЬ ПО ОФИЦИАЛЬНОМУ КАЛЕНДАРЮ.
Окна: [ts - before, ts + after] минут по типу (по умолчанию FOMC 30/90). Ручные окна `config/watch_config.json: blackouts` читаются и объединяются, файл не меняется.
Выход `runtime/intel/blackouts.json`: {"schema":"tb.blackouts/1","ts":..,"windows":[{"from":ISO,"to":ISO,"name":"FOMC","src":"calendar|watch","v":bool,"event":id?}]}.
"""
import calendar as _cal
import json
import re
import time

from tb import paths

KINDS = ("FOMC", "CPI", "NFP", "PCE", "GDP", "OTHER")
DEFAULT_WINDOWS = {"FOMC": [30, 90], "CPI": [30, 60], "NFP": [30, 60], "PCE": [15, 45], "GDP": [15, 45], "OTHER": [15, 45]}
RE_IDENT = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
RE_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
SRC_RANK = {"owner": 0, "import": 1, "candidate": 2}


# ---------- время США (Восточное, без zoneinfo: на Windows нет tzdata) ----------
def _nth_sunday(year, month, n):
    first = _cal.weekday(year, month, 1)                  # 0 = пн
    day = 1 + (6 - first) % 7 + 7 * (n - 1)
    return day


def dst_bounds(year):
    """-> (начало EDT, конец EDT) в UTC-epoch: 2-е воскресенье марта 2:00 EST (07:00 UTC) .. 1-е воскресенье ноября 2:00 EDT (06:00 UTC)."""
    start = _cal.timegm((year, 3, _nth_sunday(year, 3, 2), 7, 0, 0))
    end = _cal.timegm((year, 11, _nth_sunday(year, 11, 1), 6, 0, 0))
    return start, end


def et_offset_h(ts):
    """Смещение Восточного времени США относительно UTC в часах (-4 летом, -5 зимой) на момент UTC ts."""
    y = time.gmtime(ts).tm_year
    s, e = dst_bounds(y)
    return -4 if s <= ts < e else -5


def et_to_utc(y, mo, d, h, mi=0):
    """Местное время США (ET) -> epoch UTC."""
    local = _cal.timegm((y, mo, d, h, mi, 0))
    for off in (-4, -5):
        ts = local - off * 3600
        if et_offset_h(ts) == off:
            return ts
    return local + 5 * 3600


def et_date(ts):
    """-> (y, m, d, weekday, hour, minute) местного времени ET на момент UTC ts."""
    t = time.gmtime(ts + et_offset_h(ts) * 3600)
    return t.tm_year, t.tm_mon, t.tm_mday, t.tm_wday, t.tm_hour, t.tm_min


def _ds(y, m, d):
    return "%04d-%02d-%02d" % (y, m, d)


def us_market(cfg, now):
    """Расписание торгов США (NYSE) по локальным данным: {"open","next_open_ts","next_close_ts"?,"holiday","half_day"}.
    Сессия 9:30-16:00 ET (13:00 в половинные дни); праздники и половинные дни из cfg["us_holidays"/"us_half_days"] (редактирует владелец)."""
    hol = set(cfg.get("us_holidays") or [])
    half = set(cfg.get("us_half_days") or [])

    def trading(y, m, d):
        wd = _cal.weekday(y, m, d)
        return wd < 5 and _ds(y, m, d) not in hol

    y, m, d, wd, hh, mm = et_date(now)
    today = _ds(y, m, d)
    is_hol = today in hol
    is_half = today in half
    out = {"open": False, "next_open_ts": None, "holiday": today if is_hol else None, "half_day": bool(is_half and trading(y, m, d))}
    if trading(y, m, d):
        o = et_to_utc(y, m, d, 9, 30)
        c = et_to_utc(y, m, d, 13 if is_half else 16, 0)
        if o <= now < c:
            out["open"] = True
            out["next_close_ts"] = int(c)
        if now < o:
            out["next_open_ts"] = int(o)
            return out
    base = _cal.timegm((y, m, d, 12, 0, 0))
    for i in range(1, 15):
        t = time.gmtime(base + i * 86400)
        if trading(t.tm_year, t.tm_mon, t.tm_mday):
            out["next_open_ts"] = int(et_to_utc(t.tm_year, t.tm_mon, t.tm_mday, 9, 30))
            break
    return out


# ---------- события ----------
def _num(v):
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def parse_ts(v):
    """epoch (число), 'YYYY-MM-DDTHH:MM:SS[Z]' или None."""
    n = _num(v)
    if n is not None:
        return n
    if isinstance(v, str):
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?Z?$", v.strip())
        if m:
            a = [int(x or 0) for x in m.groups()]
            try:
                return float(_cal.timegm((a[0], a[1], a[2], a[3], a[4], a[5], 0, 0, 0)))
            except (ValueError, OverflowError):
                return None
    return None


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def impact_of(v, default=2):
    if isinstance(v, str):
        return {"high": 3, "medium": 2, "med": 2, "low": 1}.get(v.strip().lower(), default)
    n = _num(v)
    return int(min(3, max(1, n))) if n is not None else default


def make_event(kind, ts, impact, src, verified, eid=None, name=None):
    kind = kind if kind in KINDS else "OTHER"
    d = time.gmtime(ts)
    eid = eid if isinstance(eid, str) and RE_IDENT.match(eid) else "%s-%04d-%02d-%02d" % (kind.lower(), d.tm_year, d.tm_mon, d.tm_mday)
    return {"id": eid, "type": kind, "ts": int(ts), "impact": int(impact), "src": src, "v": bool(verified)}


def owner_events(root):
    """config/macro_events.json (формат владельца; также ts_iso/impact high из context.py). Битый файл -> []."""
    try:
        with open(paths.config("macro_events.json", root=root), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        ts = parse_ts(e.get("ts_utc", e.get("ts_iso")))
        if ts is None:
            continue
        kind = str(e.get("type") or "").upper()
        out.append(make_event(kind, ts, impact_of(e.get("impact"), 3), "owner", True, e.get("id")))
    return out


def candidate_events(ccfg):
    """Кандидаты по памяти автора: {"type","date":"YYYY-MM-DD","et":"HH:MM","impact"} -> события с v:false."""
    out = []
    for c in (ccfg or {}).get("candidates") or []:
        if not isinstance(c, dict):
            continue
        m = RE_DATE.match(str(c.get("date", "")))
        t = re.match(r"^(\d{1,2}):(\d{2})$", str(c.get("et", "")))
        if not m or not t:
            continue
        y, mo, d = (int(x) for x in m.groups())
        try:
            if not (1 <= mo <= 12 and 1 <= d <= _cal.monthrange(y, mo)[1] and int(t.group(1)) < 24 and int(t.group(2)) < 60):
                continue
        except (ValueError, OverflowError):
            continue
        try:
            ts = et_to_utc(y, mo, d, int(t.group(1)), int(t.group(2)))
        except (ValueError, OverflowError):
            continue
        out.append(make_event(str(c.get("type") or "OTHER").upper(), ts, impact_of(c.get("impact"), 3), "candidate", False))
    return out


def import_events(root, ids_verified):
    """События, разобранные из ответов официальных страниц (кэш cal_*): ids_verified = {source_id: verified}."""
    out = []
    for sid, ver in ids_verified.items():
        try:
            with open(paths.runtime("intel", "cache", sid + ".json", root=root), encoding="utf-8") as f:
                data = (json.load(f) or {}).get("data") or {}
        except (OSError, ValueError):
            continue
        for e in data.get("events") or []:
            ts, kind = _num(e.get("ts")), str(e.get("type") or "OTHER").upper()
            if ts is not None:
                out.append(make_event(kind, ts, impact_of(e.get("impact"), 3 if kind in ("FOMC", "CPI", "NFP") else 2), "import", ver))
    return out


def merge_events(*lists):
    """Объединение: одно событие (тот же тип в пределах 36 ч) оставляется от более приоритетного источника."""
    allv = sorted((e for l in lists for e in l), key=lambda e: (SRC_RANK.get(e["src"], 9), e["ts"]))
    out = []
    for e in allv:
        if any(o["type"] == e["type"] and abs(o["ts"] - e["ts"]) < 36 * 3600 for o in out):
            continue
        out.append(e)
    out.sort(key=lambda e: e["ts"])
    return out


def all_events(root, ccfg, import_ids=None):
    return merge_events(owner_events(root), import_events(root, import_ids or {}), candidate_events(ccfg))


def next_events(events, now, horizon_days=14, limit=6, grace_min=0):
    """Ближайшие события: ts в [now - grace_min, now + horizon]. -> [{"id","type","ts","in_min","impact","v"}]."""
    out = []
    for e in events:
        if now - grace_min * 60 <= e["ts"] <= now + horizon_days * 86400:
            out.append({"id": e["id"], "type": e["type"], "ts": e["ts"], "in_min": int((e["ts"] - now) // 60), "impact": e["impact"], "v": e["v"]})
    return out[:limit]


# ---------- окна blackout ----------
def window_minutes(ccfg, kind):
    w = ((ccfg or {}).get("windows") or {}).get(kind) or DEFAULT_WINDOWS.get(kind) or DEFAULT_WINDOWS["OTHER"]
    try:
        return max(0.0, float(w[0])), max(0.0, float(w[1]))
    except (TypeError, ValueError, IndexError):
        return tuple(DEFAULT_WINDOWS["OTHER"])


def event_windows(events, ccfg):
    unver = (ccfg or {}).get("unverified_blackout", True)
    min_imp = int((ccfg or {}).get("blackout_min_impact", 2))
    out = []
    for e in events:
        if (not e["v"] and not unver) or e["impact"] < min_imp:
            continue
        b, a = window_minutes(ccfg, e["type"])
        out.append({"from": iso(e["ts"] - b * 60), "to": iso(e["ts"] + a * 60), "name": e["type"], "src": "calendar", "v": e["v"], "event": e["id"]})
    return out


def ident_name(s, default="manual"):
    """Имя ручного окна -> безопасный идентификатор (первое слово из латиницы/цифр)."""
    m = re.match(r"^\s*([A-Za-z0-9_.-]{1,40})", s if isinstance(s, str) else "")
    return m.group(1) if m else default


def manual_windows(root):
    """watch_config.json: blackouts как ручные окна. Окно с пометкой «НЕ ПРОВЕРЕНА» в имени считается v:false."""
    try:
        with open(paths.config("watch_config.json", root=root), encoding="utf-8") as f:
            raw = (json.load(f) or {}).get("blackouts") or []
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for b in raw if isinstance(raw, list) else []:
        if not isinstance(b, dict):
            continue
        a, z = parse_ts(b.get("from")), parse_ts(b.get("to"))
        if a is None or z is None or z <= a:
            continue
        nm = str(b.get("name") or "")
        out.append({"from": iso(a), "to": iso(z), "name": ident_name(nm), "src": "watch", "v": "НЕ ПРОВЕР" not in nm.upper()})
    return out


def union_windows(manual, generated):
    """Объединение ручных и сгенерированных окон (без слияния пересечений: имена сохраняются), сортировка по началу."""
    return sorted(list(manual) + list(generated), key=lambda w: (w["from"], w["to"]))


def blackout_state(windows, now):
    """Текущее/ближайшее окно: {"name","from","to","in_min","v"}; in_min = минут до начала (<= 0: внутри окна). Окна далее 24 ч не учитываются."""
    best = None
    for w in windows:
        a, z = parse_ts(w["from"]), parse_ts(w["to"])
        if a is None or z is None or now > z:
            continue
        if a - now > 24 * 3600:
            continue
        if best is None or a < best[0]:
            best = (a, z, w)
        if a <= now <= z:
            best = (a, z, w)
            break
    if not best:
        return None
    a, z, w = best
    return {"name": w["name"], "from": int(a), "to": int(z), "in_min": int((a - now) // 60) if a > now else -int((now - a) // 60), "v": bool(w.get("v", True))}


def build_blackouts(root, ccfg, events, now):
    doc = {"schema": "tb.blackouts/1", "ts": int(now), "windows": union_windows(manual_windows(root), event_windows(events, ccfg))}
    return doc


def blackouts_path(root=None):
    return paths.runtime("intel", "blackouts.json", root=root)


# ---------- предупреждения в шину ----------
def due_alerts(events, now, warn_min, fired):
    """События, до которых осталось <= порога из warn_min и для которых этот порог ещё не отправлялся.
    -> (список (event, порог), новый fired = {event_id: [порог,...]}). Отправляется только самый узкий достигнутый порог; прошедшие события не шлём."""
    warn = sorted(int(x) for x in warn_min)
    new = {k: list(v) for k, v in fired.items()}
    out = []
    for e in events:
        if e["impact"] < 2:
            continue
        mins = (e["ts"] - now) / 60.0
        if mins < 0:
            continue
        reached = [w for w in warn if mins <= w]
        if not reached:
            continue
        thr = reached[0]
        if thr in new.get(e["id"], []):
            continue
        new.setdefault(e["id"], []).extend(w for w in reached if w not in new.get(e["id"], []))
        out.append((e, thr))
    live = {e["id"] for e in events}
    new = {k: v for k, v in new.items() if k in live}
    return out, new


def load_fired(root):
    try:
        with open(paths.runtime("intel", "alerts.json", root=root), encoding="utf-8") as f:
            d = json.load(f)
        return {k: [int(x) for x in v] for k, v in d.items() if isinstance(v, list)} if isinstance(d, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_fired(root, fired):
    from tb.core import filelock
    filelock.atomic_write_json(paths.runtime("intel", "alerts.json", root=root), fired, indent=None)
