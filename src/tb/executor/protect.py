"""Защиты исполнителя: паузы и частота входов как ЧИСТЫЕ функции от истории закрытых сделок журнала (02 п. 2.9). Владелец: WP5.

Без собственного состояния: всё считается из списка сделок (формат state/trades.jsonl, как возвращает journal._load), поэтому паузы
переживают перезапуск исполнителя. Время в секундах epoch. Защиты только СУЖАЮТ то, что разрешает guard (он остаётся главным).
Сделки mode=paper в расчёт не входят (бумажные не влияют на реальные паузы).
"""
import calendar
import time

HOUR = 3600.0
DAY = 86400.0


def parse_ts(s):
    """'2026-10-28T10:15:02Z' -> epoch или None."""
    try:
        return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def real_trades(trades):
    """Сделки не бумажные (live/testnet) -> список в исходном порядке."""
    return [t for t in trades or [] if isinstance(t, dict) and t.get("mode") != "paper"]


def closed_sorted(trades):
    """Закрытые сделки по возрастанию времени закрытия: [(ts_close, trade)]."""
    out = []
    for t in real_trades(trades):
        tc = parse_ts(t.get("ts_close"))
        if tc is not None:
            out.append((tc, t))
    out.sort(key=lambda x: x[0])
    return out


def symbol_cooldown(trades, sym, now, minutes):
    """Эпоха конца паузы по монете после стопа (reason == 'sl') или None, если паузы нет.

    >>> symbol_cooldown([{"sym": "SOL", "reason": "sl", "ts_close": "2026-10-28T10:00:00Z"}], "SOL", 1793181600.0 + 600, 30) is not None
    True
    """
    last = None
    for tc, t in closed_sorted(trades):
        if t.get("sym") == sym and t.get("reason") == "sl":
            last = tc
    if last is None:
        return None
    until = last + float(minutes) * 60.0
    return until if until > now else None


def stop_streak(trades):
    """-> (длина серии стопов подряд среди последних закрытых, эпоха последнего стопа серии или None)."""
    n, last = 0, None
    for tc, t in reversed(closed_sorted(trades)):
        if t.get("reason") != "sl":
            break
        n += 1
        if last is None:
            last = tc
    return n, last


def global_pause(trades, now, n, minutes):
    """-> {"active":bool,"until":epoch|None,"streak":int,"confirm_needed":bool}.

    Серия из n стопов подряд (пока её не разорвала любая другая закрытая сделка): `active` в течение minutes после последнего стопа;
    после паузы серия всё ещё считается (confirm_needed=True: вход только со свечным подтверждением), пока не закроется несстоповая сделка."""
    streak, last = stop_streak(trades)
    out = {"active": False, "until": None, "streak": streak, "confirm_needed": False}
    if not n or streak < n or last is None:
        return out
    until = last + float(minutes) * 60.0
    out["until"] = until
    if until > now:
        out["active"] = True
    else:
        out["confirm_needed"] = True
    return out


def entries_counts(trades, now):
    """-> (открытий за последний час, открытий за последние 24 ч): по ts_open не бумажных сделок.

    >>> entries_counts([], 0.0)
    (0, 0)
    """
    h = d = 0
    for t in real_trades(trades):
        to = parse_ts(t.get("ts_open"))
        if to is None:
            continue
        age = now - to
        if 0 <= age < HOUR:
            h += 1
        if 0 <= age < DAY:
            d += 1
    return h, d


def entries_today(trades, now):
    """Открытий с 00:00 UTC текущих суток (лимит max_entries_per_day считается по календарным суткам)."""
    day0 = (int(now) // 86400) * 86400
    n = 0
    for t in real_trades(trades):
        to = parse_ts(t.get("ts_open"))
        if to is not None and to >= day0:
            n += 1
    return n


def anomaly(trades, now, factor=3.0, min_today=6):
    """True, если за последние 24 ч открыто в `factor` раз больше сделок, чем в среднем в сутки за предыдущие 7 суток (идея TradeTrap).
    Нужно не менее min_today сделок за сутки, чтобы шум малых чисел не срабатывал."""
    day = 0
    prev = 0
    for t in real_trades(trades):
        to = parse_ts(t.get("ts_open"))
        if to is None:
            continue
        age = now - to
        if 0 <= age < DAY:
            day += 1
        elif DAY <= age < 8 * DAY:
            prev += 1
    if day < min_today:
        return False
    avg = prev / 7.0
    return day > factor * max(avg, 1.0)


def cooldowns(trades, now, prot):
    """-> {"symbols": {SYM: until}, "global": until|0} в формате ctx.cooldowns для plan_schema.validate_context.
    prot = exec_limits.protections."""
    out = {"symbols": {}, "global": 0}
    minutes = float(prot.get("symbol_cooldown_after_stop_min", 30))
    syms = set(t.get("sym") for t in real_trades(trades) if t.get("reason") == "sl" and t.get("sym"))
    for s in syms:
        u = symbol_cooldown(trades, s, now, minutes)
        if u:
            out["symbols"][s] = u
    p = prot.get("pause_after_consecutive_stops") or {}
    gp = global_pause(trades, now, int(p.get("n", 2)), float(p.get("minutes", 60)))
    if gp["active"]:
        out["global"] = gp["until"]
    return out


def check_entry(trades, sym, now, prot, has_confirm, size_mult=1.0, no_confirm_max=0.5):
    """Все защиты входа -> (ok, code, detail). code из: COOLDOWN, LIMIT_HOUR, LIMIT_DAY, ANOMALY, NO_CONFIRM."""
    u = symbol_cooldown(trades, sym, now, float(prot.get("symbol_cooldown_after_stop_min", 30)))
    if u:
        return False, "COOLDOWN", "пауза по %s ещё %.0f мин после стопа" % (sym, (u - now) / 60.0)
    p = prot.get("pause_after_consecutive_stops") or {}
    gp = global_pause(trades, now, int(p.get("n", 2)), float(p.get("minutes", 60)))
    if gp["active"]:
        return False, "COOLDOWN", "общая пауза после %d стопов подряд ещё %.0f мин" % (gp["streak"], (gp["until"] - now) / 60.0)
    if gp["confirm_needed"] and p.get("require_confirm", True) and not has_confirm:
        return False, "COOLDOWN", "после серии стопов вход только со свечным подтверждением"
    if not has_confirm and size_mult > float(no_confirm_max) + 1e-9:
        return False, "NO_CONFIRM", "вход без подтверждения только с size_mult <= %s" % no_confirm_max
    h, _ = entries_counts(trades, now)
    if h >= int(prot.get("max_entries_per_hour", 4)):
        return False, "LIMIT_HOUR", "входов за час %d >= %s" % (h, prot.get("max_entries_per_hour", 4))
    d = entries_today(trades, now)
    if d >= int(prot.get("max_entries_per_day", 12)):
        return False, "LIMIT_DAY", "входов за сутки %d >= %s" % (d, prot.get("max_entries_per_day", 12))
    if anomaly(trades, now):
        return False, "ANOMALY", "число сделок за сутки втрое выше среднего за неделю"
    return True, None, None
