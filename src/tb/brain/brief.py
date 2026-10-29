"""Досье `./tb brief [--scope start|event|plan:ID]`: числа и белые списки полей, без текстов третьих лиц (03 п. 3.4). Владелец: WP6.

Чистый читатель локальных файлов: без сети, без ключа, биржу не трогает. Источники: runtime/{exec_health,exec_snapshot,levels,hot,market,context,prices}.json,
runtime/structure.json (блок `struct`: режим, ATR, границы суток/недели, ближайшие уровни; пишет marketdata/structure.py), state/{plans,ideas.jsonl,trades.jsonl,sessions.jsonl,portfolio.json,pending_orders.json,bus}, config/{risk_profile,brief_checklist}.json.
Любой блок, который не удалось собрать, опускается (имя попадает в `missing`), остальные работают.

Защита от инъекций: каждый блок строится явным списком полей; символы проверяются regex; строки допускаются только как идентификаторы или значения
перечислений; текстов внешних источников (новости, названия токенов, описания, `idea.note`, `hot.reasons`, `session.thesis`) в выводе нет.
Единственный свободный текст: `own_thesis` (собственный тезис плана, <= 160 символов, без управляющих символов) и чек-лист владельца.

Вызов сохраняет досье в `state/briefs/B-*.json` (хранение 14 дней), решение ссылается на `brief_id`; `--no-save` отключает запись.
`tb.dash.model` импортируется как зафиксированный технический долг (03 п. 3.4; извлечение в core/riskroom.py после v2).
"""
import argparse
import json
import math
import os
import re
import sys
import time

from tb import paths
from tb.brain import common
from tb.core import blackout
from tb.core import bus
from tb.core import exec_cfg
from tb.core import ideas as ideas_core
from tb.core import portfolio
from tb.core import plan_schema

DEFAULT_BUDGET = 2500
EVENT_BUDGET = 900
CHARS_PER_TOKEN = 3.5
BRIEF_KEEP_DAYS = 14
BRIEF_MAX_BYTES = 12 * 1024
THESIS_LEN = 160
CUT_RESERVE = 8
CHECKLIST_MAX = 12
CHECKLIST_TEXT_LEN = 200
SCOPES = ("start", "event")
RE_PLAN_SCOPE = re.compile(r"^plan:(PL-\d{4}-[A-Z0-9]{2,20}-\d{2,3})$")
SCAN_FRESH_S = 150.0
FEED_FRESH_S = 30.0
HOT_STALE_S = 180.0
ACTIVE_PLAN = ("draft", "armed")
OPEN_PLAN = ("entered", "managing")
# причины сканера приходят русским текстом из hotscan: отдаём только коды по префиксу, остальное отбрасывается
_REASON_CODES = (("диапазон 5м", "range_5m"), ("диапазон 15м", "range_15m"), ("оборот", "turnover_surge"), ("24ч", "move_24h"))
_HOT_NUMS = ("score", "ret_5m_pct", "range_5m_pct", "turn_surge_5m", "spread_bps", "funding_pct", "lot_usdt")
_LEVEL_NUMS = ("px", "atr_5m", "vol_med_5m")
_LEVEL_ALIAS = {"px": "price", "atr_5m": "atr_5m_14", "vol_med_5m": "vol_med_5m_20"}        # так ключи называет logic/levels.key_levels (исполнитель пишет их в levels.json)
STRUCT_FRESH_S = 300.0
STRUCT_BUDGET = 600           # токенов на блок struct при scope=start (цель WP «данные мозга»: не больше +600)
STRUCT_EVENT_BUDGET = 260
STRUCT_MAX_SYMS = 8
_STRUCT_NUMS = ("a5_pct", "a1h_pct", "r1h", "r4h", "r24h", "vx", "te", "dpos", "dx", "tx", "t24_musd")
_STRUCT_REG = ("trend_up", "trend_down", "range", "storm", "unknown")
_STRUCT_TAGS = ("wh", "wl", "pdh", "pdl", "pdc", "dh", "dl", "s1h", "s5m")
_STRUCT_BASE = ("BTC", "ETH", "SOL")
_SYM_RE = re.compile(r"^[A-Z0-9]{2,20}$")


def _model():
    from tb.dash import model       # импорт ленивый: сбой дашборда не ломает остальные команды мозга
    return model


# ------------------------------------------------------------------ мелкие помощники
def _p(x):
    """Цена с значащими цифрами как dash.model.fmt_price (число, не строка)."""
    v = common.num(x)
    if v is None:
        return None
    try:
        return float(_model().fmt_price(v))
    except (ValueError, TypeError):
        return v


def _r(x, n=2):
    v = common.num(x)
    return None if v is None else round(v, n)


def _sym(v):
    return v if isinstance(v, str) and common.RE_SYMBOL.match(v) else None


def _drop_none(d):
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}


def tokens(obj):
    """Оценка токенов по числу символов / 3.5 (05 WP6)."""
    return int(math.ceil(len(common.dumps(obj)) / CHARS_PER_TOKEN))


class _Ctx(object):
    """Один раз прочитанные источники для всех блоков."""

    def __init__(self, root, now):
        m = _model()
        self.root, self.now = root, now
        self.today = time.strftime("%Y-%m-%d", time.gmtime(now))
        self.trades, self.twarn = m.load_trades(root)
        prof = m.read_json(paths.config("risk_profile.json", root=root))
        self.prof = prof if isinstance(prof, dict) else {}
        p = m.read_json(paths.state("portfolio.json", root=root))
        self.port = p if isinstance(p, dict) and isinstance(p.get("venues"), dict) else {"venues": {}, "hwm": 0.0}
        self.mkt = m.market_info(root, now)
        self.sp = m.scan_prices(root, now)
        sessions = m.load_sessions(root)
        self.active = next((s for s in reversed(sessions) if not s.get("stopped")), None)
        self.plans, self.plans_bad = common.load_plans(root)
        self.health = common.exec_health(root=root, now=now)
        self._price = {}

    def price(self, sym):
        if sym not in self._price:
            self._price[sym] = _model().resolve_price(self.mkt, self.sp, sym, None)
        return self._price[sym]


# ------------------------------------------------------------------ блоки
def _b_system(c):
    h = c.health
    d = h["data"]
    ex = {"alive": h["alive"], "age_s": h["age_s"], "present": h["present"]}
    for k in ("env", "mode", "hold", "ws"):
        ex[k] = common.ident(d.get(k))
    if isinstance(d.get("observe"), bool):
        ex["observe"] = d["observe"]
    ra = common.num(d.get("reconcile_age_s"))
    ex["reconcile_age_s"] = None if ra is None else round(ra, 1)
    scan_age = c.sp["scan"]["age_s"]
    st = bus.stats(root=c.root, now=c.now)
    sysd = {"exec": _drop_none(ex), "kill": os.path.exists(paths.kill_file(root=c.root)),
            "scanner": {"alive": bool(scan_age is not None and scan_age <= SCAN_FRESH_S), "age_s": _r(scan_age, 0)},
            "feed": {"alive": bool(c.mkt["ok"] and c.mkt["age_s"] is not None and c.mkt["age_s"] <= FEED_FRESH_S), "age_s": _r(c.mkt["age_s"], 0)},
            "unacked": st["unacked_wake"]}
    warn_min = 10
    try:
        with open(paths.exec_limits_file(root=c.root), encoding="utf-8") as f:
            warn_min = float(json.load(f)["events"]["unacked_warn_min"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    old = st["oldest_unacked_age_s"]
    if st["unacked_wake"] and old is not None and old > warn_min * 60:
        sysd["unacked_old_min"] = int(old // 60)
    sysd["scanner"] = _drop_none(sysd["scanner"])
    sysd["feed"] = _drop_none(sysd["feed"])
    return sysd


def _b_account(c):
    live = [t for t in c.trades if t.get("mode") == "live"]
    closed_today = [t for t in live if t["status"] == "closed" and str(t.get("ts_close") or "").startswith(c.today)]
    streak = 0
    for t in sorted(closed_today, key=lambda x: str(x.get("ts_close") or ""), reverse=True):
        if t.get("reason") == "sl":
            streak += 1
        else:
            break
    venues = c.port.get("venues") or {}
    out = {"equity": _r(portfolio.total(c.port)), "free": _r(sum(common.num(v.get("free"), 0.0) for v in venues.values() if isinstance(v, dict))),
           "dd_pct": _r(100.0 * portfolio.drawdown(c.port), 1),
           "day_pnl": _r(sum(t.get("pnl_usdt") or 0.0 for t in closed_today)),
           "stops_today": sum(1 for t in closed_today if t.get("reason") == "sl"), "stop_streak": streak,
           "day_trades": sum(1 for t in live if str(t.get("ts_open") or "").startswith(c.today))}
    try:
        out["dd_mult"] = _r(portfolio.dd_mult(c.port, c.prof), 2)
    except (KeyError, TypeError, ValueError):
        pass
    return _drop_none(out)


def _b_limits(c):
    """Эффективные лимиты (risklevels.limits_summary: уровень, потолки, agents.json): только числа из белого списка. WP11-C3: роли читают limits/risk_level."""
    from tb.core import risklevels
    eq = portfolio.total(c.port)
    s = risklevels.limits_summary(c.root, equity=eq if eq and eq > 0 else None)
    if not s.get("config"):
        return None
    n_open = len(c.brief_positions) if hasattr(c, "brief_positions") else 0
    slots = max(0, int(s["max_open_positions"]) - n_open)
    daily_left = None
    if s.get("daily_loss_usdt") is not None:
        lost = -sum(min(0.0, t.get("pnl_usdt") or 0.0) for t in c.trades if t.get("status") == "closed" and str(t.get("ts_close") or "").startswith(c.today))
        daily_left = round(max(0.0, s["daily_loss_usdt"] - lost), 4)
    return _drop_none({"risk_per_trade_usdt": s.get("risk_per_trade_usdt"), "max_risk_usdt": s.get("risk_per_trade_usdt"), "daily_loss_usdt": s.get("daily_loss_usdt"),
                       "daily_left_usdt": daily_left, "max_open_positions": s["max_open_positions"], "slots": slots, "max_new_plans": slots,
                       "total_open_risk_usdt": s.get("total_open_risk_usdt"), "size_mult": _r(s["size_mult_cap"], 2)})


def _b_risk_level(c):
    from tb.core import risklevels
    s = risklevels.limits_summary(c.root)
    if not s.get("config"):
        return None
    cr, py, nc = s["carry"], s["pyramid"], s["no_confirm"]
    return _drop_none({"level": s["level"], "name": common.ident(s.get("name")), "carry": {"policy": common.ident(cr.get("policy")), "max_positions": cr.get("max_positions")},
                       "pyramid": {"allowed": bool(py.get("allowed")), "max_adds": py.get("max_adds")},
                       "no_confirm": {"allowed": bool(nc.get("allowed")), "size_mult_max": nc.get("size_mult_max")}, "setups_aggr": common.ident(s.get("setups_aggr"))})


def _b_economics(c):
    """Стоимость работы мозга и результат дня (UTC): llm_usd_day = расход на токены, pnl_<mode>_day = результат закрытых сделок дня,
    net_<mode>_day = pnl минус расход. Мозг должен выходить в net >= 0; это не повод ослаблять стоп, RR и лимиты. Только числа."""
    from tb.llm import usage as usage_mod
    day0 = c.now - (c.now % 86400.0)
    usd = usage_mod.window_totals(usage_mod.read_rows(c.root, since=day0))["usd"]
    pnl = {}
    for t in c.trades:
        if t.get("status") == "closed" and t.get("mode") in ("paper", "live") and str(t.get("ts_close") or "").startswith(c.today):
            pnl[t["mode"]] = pnl.get(t["mode"], 0.0) + (t.get("pnl_usdt") or 0.0)
    out = {"llm_usd_day": _r(usd, 4)}
    for m, v in pnl.items():
        out["pnl_%s_day" % m], out["net_%s_day" % m] = _r(v, 4), _r(v - usd, 4)
    return out


EXPERIMENTS_TOP = 5


def _b_experiments(c):
    """Гипотезы (тег hyp сделки): confirmed = на бумаге n >= MIN_N, средний R > 0 и p ниже alpha с поправкой Бонферрони (их можно повторять приоритетно);
    open = ещё копят выборку или не подтверждены. По каждой paper n, avg_r, доля прибыльных и, если были, live n / avg_r. Нет гипотез -> None."""
    from tb.core import expstats
    by = {}
    for r in expstats.group_stats(expstats.load_trades("all", c.root, with_rules=False), by="hyp"):
        by.setdefault(r["key"], {})[r["mode"]] = r
    conf, opn = [], []
    for hyp, m in by.items():
        p, h = m.get("paper"), common.ident(hyp)
        if not p or not h:
            continue
        row = {"hyp": h, "n": p["n"], "avg_r": _r(p["avg_r"], 2), "win": _r(p["win_rate"], 2)}
        if m.get("live"):
            row["live_n"], row["live_avg_r"] = m["live"]["n"], _r(m["live"]["avg_r"], 2)
        (conf if p["below_alpha"] and (p["avg_r"] or 0) > 0 else opn).append(_drop_none(row))
    if not (conf or opn):
        return None
    conf.sort(key=lambda x: -(x.get("avg_r") or 0))
    opn.sort(key=lambda x: -x["n"])
    return {"min_n": expstats.MIN_N, "confirmed": conf[:EXPERIMENTS_TOP], "open": opn[:EXPERIMENTS_TOP]}


_DAY_SETUPS = ("level_reclaim", "level_reject", "sweep_reject", "breakout_retest", "range_fade", "trend_pullback", "none")   # как validate.SETUPS (проверяет тест)
DAY_LIMITS_MAX_AGE_S = 36 * 3600.0     # план дня старше полутора суток не действует (файл остаётся от прошлого дня)


def _b_day(c):
    """Блок `day`: рамка плана дня (lifecycle S6 -> state/control/day_limits.json): допустимые сетапы, монеты избегать, список монет, потолки.
    Только идентификаторы и числа из белого списка. Нет файла / устарел -> None (блок не выводится, проверки validate по нему пропускаются)."""
    d = _model().read_json(paths.control_dir("day_limits.json", root=c.root))
    if not isinstance(d, dict):
        return None
    ts = common.num(d.get("ts_epoch"))
    if ts is not None and c.now - ts > DAY_LIMITS_MAX_AGE_S:
        return None
    setups = [x for x in (d.get("allowed_setups") or []) if isinstance(x, str) and x in _DAY_SETUPS] if isinstance(d.get("allowed_setups"), list) else []
    avoid = [x for x in (d.get("avoid") or []) if _sym(x)] if isinstance(d.get("avoid"), list) else []
    syms = [x for x in (d.get("symbols") or []) if _sym(x)] if isinstance(d.get("symbols"), list) else []
    mnp = d.get("max_new_plans")
    return _drop_none({"id": common.ident(d.get("day_id")), "setups": setups, "avoid": avoid or None, "symbols": syms or None,
                       "max_new_plans": mnp if isinstance(mnp, int) and not isinstance(mnp, bool) else None,
                       "max_horizon": d.get("max_horizon") if d.get("max_horizon") in _HORIZONS else None,
                       "max_risk_usdt": _r(common.num(d.get("max_risk_usdt"))), "size_mult": _r(common.num(d.get("size_mult")), 2)})


PAUSE_SYM_MIN = 30          # пауза по монете после стопа (common п. 6)
PAUSE_ALL_MIN = 60          # общая пауза после двух стопов подряд


def _b_pause(c, syms):
    """Паузы после стопа (любой режим, paper тоже): syms {монета: минут осталось} по монетам события, all (минут до конца общей паузы), streak (стопов подряд, последние закрытые сделки).
    Только числа; нет пауз -> None (блок не выводится)."""
    closed = sorted((t for t in c.trades if t.get("status") == "closed"), key=lambda t: str(t.get("ts_close") or ""))
    left = {}
    for t in closed:
        ts = common.parse_ts(t.get("ts_close"))
        if ts is None or t.get("reason") != "sl":
            continue
        sym = _sym(t.get("sym"))
        mn = PAUSE_SYM_MIN - (c.now - ts) / 60.0
        if sym in syms and mn > 0:
            left[sym] = max(left.get(sym, 0), int(math.ceil(mn)))
    streak, last_ts = 0, None
    for t in reversed(closed):
        if t.get("reason") != "sl":
            break
        streak += 1
        last_ts = last_ts or common.parse_ts(t.get("ts_close"))
    allp = None
    if streak >= 2 and last_ts is not None and PAUSE_ALL_MIN - (c.now - last_ts) / 60.0 > 0:
        allp = int(math.ceil(PAUSE_ALL_MIN - (c.now - last_ts) / 60.0))
    root = getattr(c, "root", None)
    ms = common.read_json(paths.runtime("min_size.json", root=root)) if root else None          # монеты, на которых исполнитель отказал по MIN_SIZE (минут осталось помнить)
    small = {}
    for sym, ts in (ms.items() if isinstance(ms, dict) else []):
        mn = exec_cfg.MIN_SIZE_TTL_S / 60.0 - (c.now - ts) / 60.0 if isinstance(ts, (int, float)) else 0
        if _sym(sym) in syms and mn > 0:
            small[_sym(sym)] = int(math.ceil(mn))
    return _drop_none({"syms": left or None, "all": allp, "streak": streak if streak else None, "min_size": small or None})


def _b_flash(c, sym):
    """Быстрые новости по монете события за последние 30 минут (intel/flash.py): только класс, подсказка направления, важность, возраст и изменение цены с момента новости.
    Текста заголовка здесь нет: это данные, не инструкции. Нет новостей -> None."""
    if not sym or not getattr(c, "root", None):
        return None
    from tb.core import newsflash
    rows = newsflash.recent_for(c.root, sym, c.now)
    return rows or None


def _b_flow(c, sym):
    """Ордерфлоу монеты события (поток flowwatch): перекос агрессии CVD за 5 минут, перекос стакана, устойчивые стены, разрыв с Binance, прогрев ленты. Только числа. Нет данных -> None."""
    if not sym or not getattr(c, "root", None):
        return None
    from tb.core import flowdoc
    return flowdoc.flow_for(c.root, sym, c.now)


def _b_playbook(c, b, sym):
    """Карточки сетапов (brain/playbook.py) по сигналам события и режиму монеты: 1-2 карточки с условием входа, стопом, целью, инвалидацией и тем, чего избегать. Нет карточек -> None."""
    if not sym or not getattr(c, "root", None):
        return None
    from tb.brain import playbook
    cards, _errs = playbook.load(c.root)
    if not cards:
        return None
    kinds = set()
    for e in (b.get("recent") or []):
        if isinstance(e, dict) and e.get("sym") == sym and e.get("type") == "scanner_signal" and isinstance(e.get("d"), dict) and e["d"].get("kind"):
            kinds.add(e["d"]["kind"])
    for f in (b.get("flash") or []):
        if isinstance(f, dict) and f.get("cls"):
            kinds.add("news:%s" % f["cls"])
    st = ((b.get("struct") or {}).get("s") or {}).get(sym) if isinstance(b.get("struct"), dict) else None
    reg = st.get("reg") if isinstance(st, dict) else None
    rows = playbook.match(cards, kinds, reg, playbook.exp_index(b.get("experiments")))
    return rows or None


def _b_paper_history(c):
    closed = [t for t in c.trades if t.get("mode") == "paper" and t.get("status") == "closed"]
    days = set(str(t.get("ts_close") or "")[:10] for t in closed)
    days.discard("")
    return {"closed": len(closed), "days": len(days)}


def _b_session(c):
    if not c.active:
        return None
    v = _model().session_view(c.active, c.trades, c.prof, c.now)
    types = c.prof.get("session_types") if isinstance(c.prof.get("session_types"), dict) else {}
    out = {"id": common.ident(v["id"]), "type": common.ident(v["type"]),
           "min_left": None if v["left_s"] is None else int(v["left_s"] // 60), "budget": _r(v["budget_usdt"]),
           "realized": _r(v["realized"]), "open_risk": _r(v["open_risk"]), "trades": v["trades"], "max_trades": v["max_trades"],
           "cost_rt_pct": _cost_rt_pct(c)}
    if v["type"] == _DAY:
        # сессия-день: горизонт выбирает агент в каждом плане; пороги каждого горизонта (те же, что у guard) для выбора и смысловой проверки entry/1
        out["horizons"] = {t: _drop_none({"max_stop_pct": _r(common.num(cfg.get("max_stop_pct"))), "min_rr": _r(common.num(cfg.get("min_rr"))),
                                          "max_leverage": _r(common.num(cfg.get("max_leverage")), 1)})
                           for t in _HORIZONS for cfg in [types.get(t)] if isinstance(cfg, dict)}
    else:
        # сессия одного горизонта (CLI): пороги типа сессии для смысловой проверки entry/1 (validate.entry_context)
        cfg = types.get(v["type"]) if isinstance(types.get(v["type"]), dict) else {}
        out["max_stop_pct"], out["min_rr"] = _r(common.num(cfg.get("max_stop_pct"))), _r(common.num(cfg.get("min_rr")))
    return _drop_none(out)


_DAY = "day"
_HORIZONS = ("scalp", "intraday", "swing", "position")


def _cost_rt_pct(c):
    """Издержки круга (вход + выход по рынку) в % номинала по площадке активной сессии: 2 x (fee + slip) из risk_profile.costs, как guard.round_trip_cost_pct."""
    venue = (c.active or {}).get("venue") if isinstance(c.active, dict) else None
    costs = c.prof.get("costs") if isinstance(c.prof.get("costs"), dict) else {}
    cc = costs.get(venue) if isinstance(costs.get(venue), dict) else None
    if cc is None:
        fee = common.num(c.prof.get("taker_fee_pct"))
        cc = {"fee": fee, "slip": 0.0005} if fee is not None else None
    if cc is None or common.num(cc.get("fee")) is None:
        return None
    return _r(2.0 * (cc["fee"] + (common.num(cc.get("slip")) or 0.0)) * 100.0)


def _b_room(c):
    if not c.prof.get("session_types"):
        return {"allowed": None, "why": "no_profile"}
    prof = c.prof
    try:
        from tb.core import risklevels
        prof = risklevels.effective(c.prof, c.root)      # как на дашборде: лимиты выбранного уровня риска, а не базового профиля
    except Exception:
        prof = c.prof
    try:
        r = _model().risk_room(c.root, c.trades, prof, c.port, c.active, c.now)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
        return {"allowed": None, "why": e.__class__.__name__}
    rows = {x["id"]: x for x in r.get("rows") or []}
    nr = r.get("next_risk") or {}
    return _drop_none({"allowed": bool(r.get("allowed")), "reasons": [common.clean_text(x, 90) for x in (r.get("reasons") or [])[:4]],
                       "next_risk_usdt": _r(nr.get("usdt")), "limited_by": common.clean_text(nr.get("limited_by") or "", 30) or None,
                       "slots_free": None if (rows.get("slots") or {}).get("room") is None else int(rows["slots"]["room"]), "daily_room": _r((rows.get("daily") or {}).get("room")),
                       "session_room": _r((rows.get("session") or {}).get("room"))})


def _r_of(side_sign, px, avg, unit):
    if None in (px, avg) or not unit:
        return None
    return side_sign * (px - avg) / unit


def _need(spec, rid, r_now):
    """Для правила с условием pnl_r: «pnl_r>=1.0 (now 0.54)»; иначе None. Строится из чисел и перечислений."""
    for rule in spec.get("manage") or []:
        if not isinstance(rule, dict) or rule.get("id") != rid:
            continue
        w = rule.get("when")
        if isinstance(w, dict) and w.get("kind") == "pnl_r":
            for op, sym in (("gte", ">="), ("gt", ">"), ("lte", "<="), ("lt", "<")):
                if common.num(w.get(op)) is not None:
                    now_s = "" if r_now is None else " (now %.2f)" % r_now
                    return "pnl_r%s%g%s" % (sym, common.num(w[op]), now_s)
    return None


def _plan_position(c, it, snap):
    spec, rt = it["spec"], it["rt"]
    pos = rt.get("pos") if isinstance(rt.get("pos"), dict) else {}
    sym = _sym(spec.get("symbol"))
    if not sym:
        return None
    sign = 1 if spec.get("side") == "long" else -1
    avg, unit = common.num(pos.get("avg")), common.num(pos.get("r_unit"))
    px = c.price(sym)["price"]
    r_now = _r_of(sign, px, avg, unit)
    ent = common.parse_ts((rt.get("ts") or {}).get("entered"))
    targets = []
    for tid, t in sorted((rt.get("targets") or {}).items()) if isinstance(rt.get("targets"), dict) else []:
        if isinstance(t, dict) and common.ident(tid):
            targets.append(_drop_none({"id": tid, "px": _p(t.get("price")), "state": common.ident(t.get("state"))}))
    rules = []
    for rid, st in sorted((rt.get("rules") or {}).items()) if isinstance(rt.get("rules"), dict) else []:
        if isinstance(st, dict) and common.ident(rid):
            rules.append(_drop_none({"id": rid, "state": common.ident(st.get("state")), "need": _need(spec, rid, r_now)}))
    return _drop_none({
        "plan": it["id"], "sym": sym, "side": common.ident(spec.get("side")), "mode": common.ident(spec.get("mode")), "size": pos.get("qty"),
        "avg": _p(avg), "px": _p(px), "sl": _p(pos.get("cur_stop")), "liq": _p((snap.get(sym) or {}).get("liq")),
        "r": _r(r_now), "mae_r": _r(_r_of(sign, common.num(pos.get("worst_px")), avg, unit)),
        "mfe_r": _r(_r_of(sign, common.num(pos.get("best_px")), avg, unit)), "age_min": None if ent is None else int((c.now - ent) // 60),
        "targets": targets, "rules": rules})


def _snapshot_liq(c):
    """exec_snapshot.json -> {SYM: {"liq": цена}} (формат допускает positions списком или словарём; лишнее игнорируется)."""
    d = common.read_json(paths.exec_snapshot_file(root=c.root))
    pos = d.get("positions") if isinstance(d, dict) else None
    out = {}
    rows = pos.values() if isinstance(pos, dict) else pos if isinstance(pos, list) else []
    for r in rows:
        if isinstance(r, dict):
            sym = _sym(r.get("sym") or r.get("symbol"))
            if sym:
                out[sym] = {"liq": r.get("liq") if r.get("liq") is not None else r.get("liq_price")}
    return out


def _b_positions(c):
    m = _model()
    snap = _snapshot_liq(c)
    rows, linked = [], set()
    for it in c.plans:
        if common.plan_status(it) in OPEN_PLAN:
            jid = (it["rt"].get("pos") or {}).get("journal_id") if isinstance(it["rt"].get("pos"), dict) else None
            if jid:
                linked.add(jid)
            r = _plan_position(c, it, snap)
            if r:
                rows.append(r)
    opened = [t for t in c.trades if t["status"] == "open" and t["id"] not in linked and not t.get("plan_id")]
    paths_live = m.path_live(c.root, opened)
    for t in opened:
        sym = _sym(t.get("sym"))
        if not sym:
            continue
        pr = c.price(sym)
        calc = m.live_calc(dict(t), pr["price"], None, common.num(c.prof.get("maintenance_margin_pct"), 1.0), stale=pr["stale"])
        pth = paths_live.get(t["id"]) or {}
        age = None if not t.get("ts_open_epoch") else int((c.now - t["ts_open_epoch"]) // 60)
        size = (t["notional"] / t["entry"]) if t.get("notional") and t.get("entry") else None
        rows.append(_drop_none({
            "trade": common.ident(t["id"]), "sym": sym, "side": t.get("side"), "mode": common.ident(t.get("mode")), "venue": common.ident(t.get("venue")),
            "size": None if size is None else float("%.4g" % size), "avg": _p(t.get("entry")), "px": _p(pr["price"]), "sl": _p(t.get("sl")), "tp": _p(t.get("tp")),
            "liq": _p(calc.get("liq_est")), "r": _r(calc.get("r_now")), "pnl": _r(calc.get("pnl")), "mae_r": _r(pth.get("mae_r")), "mfe_r": _r(pth.get("mfe_r")),
            "age_min": age, "risk_usdt": _r(t.get("risk_usdt")), "stale": True if pr["stale"] else None}))
    return rows


def _levels_of(when):
    """Все числовые level/low/high в условии (для расстояния до триггера)."""
    out = []
    if isinstance(when, dict):
        for k in ("level", "low", "high"):
            v = common.num(when.get(k))
            if v is not None:
                out.append(v)
        for k in ("all", "any", "steps"):
            for x in when.get(k) or []:
                out.extend(_levels_of(x))
        if isinstance(when.get("not"), dict):
            out.extend(_levels_of(when["not"]))
    return out


def _describe_when(when, depth=0):
    """Короткое описание условия входа только из перечислений и чисел (kind, tf, dir, level)."""
    if not isinstance(when, dict) or depth > 3:
        return "?"
    for key, joiner in (("all", "+"), ("any", "|")):
        if isinstance(when.get(key), list):
            return joiner.join(_describe_when(x, depth + 1) for x in when[key][:3])
    if isinstance(when.get("not"), dict):
        return "not " + _describe_when(when["not"], depth + 1)
    kind = when.get("kind")
    if kind == "seq":
        return "seq(" + ">".join(_describe_when(x, depth + 1) for x in (when.get("steps") or [])[:3]) + ")"
    if kind not in plan_schema.COND_KINDS:
        return "?"
    parts = [kind]
    tf = when.get("tf")
    if tf in plan_schema.TIMEFRAMES:
        parts.append(tf)
    d = when.get("dir")
    if isinstance(d, str) and d in ("above", "below", "short", "long", "up", "down", "any"):
        parts.append(d)
    lv = common.num(when.get("level"))
    if lv is not None:
        parts.append("%g" % _p(lv))
    return " ".join(parts)


def _b_plans(c):
    rows = []
    for it in c.plans:
        st = common.plan_status(it)
        if st not in ACTIVE_PLAN:
            continue
        spec, rt = it["spec"], it["rt"]
        sym = _sym(spec.get("symbol"))
        if not sym:
            continue
        entry = spec.get("entry") if isinstance(spec.get("entry"), dict) else {}
        px = c.price(sym)["price"]
        lv = _levels_of(entry.get("when"))
        how = entry.get("how") if isinstance(entry.get("how"), dict) else {}
        if not lv and common.num(how.get("price")) is not None:
            lv = [common.num(how["price"])]
        dist = min((abs(px - x) / px * 100.0 for x in lv), default=None) if px and lv else None
        armed = common.parse_ts((rt.get("ts") or {}).get("armed")) or common.parse_ts(spec.get("armed_ts"))
        valid = entry.get("valid") if isinstance(entry.get("valid"), dict) else {}
        life = spec.get("lifecycle") if isinstance(spec.get("lifecycle"), dict) else {}
        ttl = common.num(valid.get("ttl_min"))
        if ttl is None:
            ttl = common.num(life.get("ttl_min"))
        left = None if armed is None or ttl is None else int((armed + ttl * 60 - c.now) // 60)
        tags = spec.get("tags") if isinstance(spec.get("tags"), dict) else {}
        rows.append(_drop_none({"id": it["id"], "sym": sym, "side": common.ident(spec.get("side")), "status": st, "rev": spec.get("rev"),
                                "mode": common.ident(spec.get("mode")), "entry": _describe_when(entry.get("when")) if entry.get("when") else common.ident(how.get("type")),
                                "dist_pct": _r(dist), "ttl_left_min": left, "exp": common.ident(tags.get("experiment")),
                                "hold": common.ident(rt.get("hold")) if rt.get("hold") not in (None, False) else None,
                                "own_thesis": common.clean_text(spec.get("thesis"), THESIS_LEN) or None}))
    rows.sort(key=lambda x: (x.get("dist_pct") is None, x.get("dist_pct") or 0.0))
    return rows


def _b_pending(c):
    """Ждущие лимитные входы прежней системы (state/pending_orders.json): числа и enum."""
    rows = _model().pending_info(c.root, "live", "all", c.mkt, c.sp, c.now)
    out = []
    for r in rows[:6]:
        sym = _sym(r.get("sym"))
        if sym:
            out.append(_drop_none({"sym": sym, "side": r.get("side"), "price": _p(r.get("price")), "dist_pct": _r(r.get("dist_pct")), "sl": _p(r.get("sl")),
                                   "tp": _p(r.get("tp")), "risk_usdt": _r(r.get("risk_usdt")), "left_min": None if r.get("left_s") is None else int(r["left_s"] // 60)}))
    return out


def _utc_session(now):
    h = time.gmtime(now).tm_hour
    return "asia" if h < 7 else "europe" if h < 13 else "us" if h < 21 else "off"


def _b_market(c):
    syms = c.mkt.get("symbols") or {}
    out = {}
    for key, name in (("btc", "BTC"), ("eth", "ETH")):
        s = syms.get(name)
        if isinstance(s, dict):
            book = s.get("book") if isinstance(s.get("book"), dict) else {}
            f5, f15, rv = common.num(s.get("ret_5m")), common.num(s.get("ret_15m")), common.num(s.get("rv_5m"))
            fh = common.num(s.get("funding_hourly"))
            out[key] = _drop_none({"px": _p(s.get("price")), "ret_5m_pct": None if f5 is None else round(f5 * 100, 3),
                                   "ret_15m_pct": None if f15 is None else round(f15 * 100, 3), "rv_5m_pct": None if rv is None else round(rv * 100, 3),
                                   "spread_bps": _r(book.get("spread_bps"), 2), "funding_8h_pct": None if fh is None else round(fh * 8 * 100, 4),
                                   "regime": common.ident(s.get("regime"))})
            try:                    # агрессия и перекос стакана по BTC/ETH из потока flowwatch (единственный источник для агента: цифры фида market.json в бриф не идут)
                from tb.core import flowdoc
                fl = flowdoc.flow_for(c.root, name, c.now) if getattr(c, "root", None) else None
            except Exception:
                fl = None
            if fl:
                out[key].update(_drop_none({"cvd_5m_pct": fl.get("cvd_5m_pct"), "imb_pct": fl.get("imb_pct")}))
            st = ((_struct_doc(c) or {}).get("symbols") or {}).get(name)
            if isinstance(st, dict) and out[key].get("regime") in (None, "unknown") and st.get("reg") in _STRUCT_REG and st["reg"] != "unknown":
                out[key]["regime"], out[key]["regime_src"] = st["reg"], "1h"          # фид ещё не прогрет (EMA из тиков): режим по свечам 1ч из structure.json
    ctx = common.read_json(paths.runtime("context.json", root=c.root))
    block = None
    if isinstance(ctx, dict):
        out["risk_off"] = bool(ctx.get("risk_off"))
        ts = common.num(ctx.get("ts_epoch"))
        if ts is not None and c.now - ts > 600:
            out["context_stale_s"] = int(c.now - ts)
        mac = (ctx.get("sources") or {}).get("macro") if isinstance(ctx.get("sources"), dict) else None
        if isinstance(mac, dict) and mac.get("ok"):
            nxt = mac.get("next_event") if isinstance(mac.get("next_event"), dict) else {}
            mn = common.num(mac.get("minutes_to_next"))
            name = common.ident(nxt.get("name"), "macro")          # имя из календаря владельца: только если это идентификатор
            if mac.get("active"):
                block = {"event": name, "in_min": 0}
            elif mn is not None and mn <= 360:
                block = {"event": name, "in_min": int(mn)}
    out["utc_session"] = _utc_session(c.now)
    if block:
        out["blackout"] = block
    return out

_FNG_CLS = ("extreme_fear", "fear", "neutral", "greed", "extreme_greed")
_EV_TYPES = ("listing", "delisting", "maintenance", "hack", "regulation", "macro", "etf", "unlock", "partnership", "price_move", "rumor", "other")
_CAL_TYPES = ("FOMC", "CPI", "NFP", "PCE", "GDP", "OTHER")
INTEL_STALE_S = 900


def intel_block(root, now, syms=()):
    """Блок `intel` из runtime/intel/facts.json (WP13a). Явный белый список полей и проверка видов: числа, enum, символы; строки третьих лиц сюда не приходят
    (facts.json их не содержит), текста новостей нет. Нет файла/схемы -> None. Устаревшие события (age_min > ttl_min) исключаются, блок помечается stale_s."""
    f = common.read_json(paths.runtime("intel", "facts.json", root=root))
    if not isinstance(f, dict) or f.get("schema") != "tb.facts/1":
        return None
    ts = common.num(f.get("ts"))
    out = {}
    if ts is None or now - ts > INTEL_STALE_S:
        out["stale_s"] = None if ts is None else int(now - ts)
    sent = f.get("sentiment") if isinstance(f.get("sentiment"), dict) else {}
    sn = {}
    fng = sent.get("fng") if isinstance(sent.get("fng"), dict) else {}
    if common.num(fng.get("v")) is not None:
        sn["fng"] = _drop_none({"v": int(common.num(fng["v"])), "cls": fng.get("cls") if fng.get("cls") in _FNG_CLS else None, "prev": None if common.num(fng.get("prev")) is None else int(common.num(fng["prev"]))})
    dv = {k: _r(v, 1) for k, v in (sent.get("dvol") or {}).items() if _SYM_RE.match(str(k)) and common.num(v) is not None} if isinstance(sent.get("dvol"), dict) else {}
    if dv:
        sn["dvol"] = dv
    dom = sent.get("btc_dom") if isinstance(sent.get("btc_dom"), dict) else {}
    if common.num(dom.get("v")) is not None:
        sn["dom"] = _drop_none({"v": _r(dom["v"], 1), "chg": _r(dom.get("chg_24h"), 2)})
    if sn:
        out["sentiment"] = sn
    want = [x for x in list(dict.fromkeys(["BTC", "ETH", "SOL"] + [x for x in syms if isinstance(x, str)])) if _SYM_RE.match(x)][:8]
    der = f.get("deriv") if isinstance(f.get("deriv"), dict) else {}
    dd = {}
    for sym in want:
        r = der.get(sym)
        if isinstance(r, dict):
            row = _drop_none({"fund_pct": common.num(r.get("funding_pct")), "oi24_pct": _r(r.get("oi_chg_24h_pct"), 1), "ls": _r(r.get("ls_ratio"), 2)})
            if row:
                dd[sym] = row
    if dd:
        out["deriv"] = dd
    cal = f.get("calendar") if isinstance(f.get("calendar"), dict) else {}
    nxt = []
    for e in (cal.get("next") or [])[:3] if isinstance(cal.get("next"), list) else []:
        if isinstance(e, dict) and e.get("type") in _CAL_TYPES and common.num(e.get("in_min")) is not None:
            nxt.append({"type": e["type"], "in_min": int(common.num(e["in_min"])), "imp": int(common.num(e.get("impact"), 2)), "v": e.get("v") is True})
    if nxt:
        out["calendar"] = {"next": nxt}
    um = f.get("us_market") if isinstance(f.get("us_market"), dict) else {}
    if isinstance(um.get("open"), bool):
        out["us_open"] = um["open"]
    ex = f.get("exchange") if isinstance(f.get("exchange"), dict) else {}
    exo = _drop_none({"status": ex.get("status") if ex.get("status") in ("ok", "maintenance", "unknown") else None,
                      "maint_in_min": None if common.num(ex.get("maint_in_min")) is None else int(common.num(ex["maint_in_min"])),
                      "delist": [x for x in (ex.get("delist_syms") or []) if isinstance(x, str) and _SYM_RE.match(x)][:5] or None,
                      "new_listing": [x for x in (ex.get("new_listing_syms") or []) if isinstance(x, str) and _SYM_RE.match(x)][:5] or None})
    if exo and (exo.get("status") != "ok" or len(exo) > 1):
        out["exchange"] = exo
    evs = []
    for e in sorted([e for e in (f.get("events") or []) if isinstance(e, dict)], key=lambda e: -(common.num(e.get("imp"), 0))):
        age, ttl, imp = common.num(e.get("age_min")), common.num(e.get("ttl_min")), common.num(e.get("imp"), 0)
        if imp >= 2 and age is not None and ttl is not None and age <= ttl and e.get("type") in _EV_TYPES:
            evs.append(_drop_none({"type": e["type"], "syms": [x for x in (e.get("syms") or []) if isinstance(x, str) and _SYM_RE.match(x)][:3] or None, "imp": int(imp),
                                   "dir": e.get("dir") if e.get("dir") in ("up", "down", "neutral", "unknown") else None, "age_min": int(age)}))
    if evs:
        out["events"] = evs[:6]
    deg = [x for x in (f.get("degraded") or []) if common.ident(x)][:6] if isinstance(f.get("degraded"), list) else []
    if deg:
        out["degraded"] = deg
    bo = cal.get("blackout") if isinstance(cal.get("blackout"), dict) else None
    if bo and bo.get("name") in _CAL_TYPES and common.num(bo.get("in_min")) is not None:
        out["blackout"] = {"event": bo["name"], "in_min": int(common.num(bo["in_min"])), "v": bo.get("v") is True}
    return out or None


_ND_TYPES = _EV_TYPES + ("upgrade", "outage")
_ND_STATUS = ("ok", "idle", "off", "limit", "paused", "llm_down")
_ND_REF = re.compile(r"^N-[0-9a-f]{8}\Z")


def news_digest_block(root, now):
    """Блок `news_digest` из runtime/intel/digest.json (WP13b, карантинный читатель). Только идентификаторы, enum и числа по явному белому списку;
    текста заголовков в файле нет и сюда он не попадёт. Нет файла/схемы -> None. Событие живёт, пока age_min <= ttl_min; imp>=2, топ-5; stale = устарел дайджест или читатель недоступен."""
    d = common.read_json(paths.runtime("intel", "digest.json", root=root))
    if not isinstance(d, dict) or d.get("schema") != "tb.digest/1":
        return None
    ts = common.num(d.get("ts"))
    out = {}
    if ts is None or now - ts > INTEL_STALE_S or d.get("stale") is True:
        out["stale"] = True
    if d.get("status") in _ND_STATUS and d["status"] not in ("ok", "idle"):
        out["reader"] = d["status"]
    evs, susp = [], 0
    for ref, e in (d.get("items") or {}).items() if isinstance(d.get("items"), dict) else []:
        if not isinstance(e, dict) or not isinstance(ref, str) or not _ND_REF.match(ref):
            continue
        its, ttl, imp = common.num(e.get("its")), common.num(e.get("ttl_min")), common.num(e.get("imp"), 0)
        if its is None or now - its > 21600:
            continue
        if e.get("st") == "susp":
            susp += 1
        elif e.get("st") == "ok" and ttl is not None and imp >= 2 and (now - its) / 60.0 <= ttl and e.get("type") in _ND_TYPES:
            evs.append(_drop_none({"ref": ref, "type": e["type"], "syms": [x for x in (e.get("syms") or []) if isinstance(x, str) and _SYM_RE.match(x)][:3] or None,
                                   "imp": int(imp), "dir": e.get("dir") if e.get("dir") in ("up", "down", "neutral", "unknown") else None,
                                   "age_min": max(0, int((now - its) // 60)), "conf": _r(e.get("conf"), 2)}))
    evs.sort(key=lambda x: (-x["imp"], x["age_min"]))
    if evs:
        out["events"] = evs[:5]
    if susp:
        out["susp_n"] = susp
    return out or None


def _merge_blackout(b):
    """market.blackout = ближайшее из контекста и из intel (объединение окон); intel.blackout остаётся внутри блока intel как есть."""
    ib = (b.get("intel") or {}).get("blackout")
    if not ib or ib.get("in_min", 0) > 360:
        return
    mk = b.get("market")
    if mk is None:
        mk = b["market"] = {}
    elif not isinstance(mk, dict):
        return
    cur = mk.get("blackout")
    if not cur or ib["in_min"] < cur.get("in_min", 1e9):
        mk["blackout"] = {"event": ib["event"], "in_min": max(0, ib["in_min"])}


def _blackout_mode(b, c):
    """market.blackout.mode = warn|block: правило окна новостей, чтобы модель знала режим. warn только при режиме исполнителя paper и paper.blackout_mode=warn
    (tb.core.blackout.policy); live/testnet/нет данных = block."""
    mk = b.get("market")
    bo = mk.get("blackout") if isinstance(mk, dict) else None
    if isinstance(bo, dict):
        try:
            bo["mode"] = blackout.policy(c.root, common.ident(c.health["data"].get("mode")))["mode"]
        except Exception:
            bo["mode"] = "block"


def _reason_codes(reasons):
    codes = []
    for r in reasons if isinstance(reasons, list) else []:
        for pref, code in _REASON_CODES:
            if isinstance(r, str) and r.startswith(pref) and code not in codes:
                codes.append(code)
    return codes


def _b_hot(c, limit=6):
    h = _model().read_json(paths.runtime("hot.json", root=c.root))
    if not isinstance(h, dict):
        return None
    rows = []
    for r in h.get("top") or []:
        if not isinstance(r, dict):
            continue
        sym = _sym(r.get("sym"))
        if not sym:
            continue
        row = {"sym": sym, "px": _p(r.get("price")), "reasons": _reason_codes(r.get("reasons")), "tradable": r.get("tradable") if isinstance(r.get("tradable"), bool) else None}
        for k in _HOT_NUMS:
            row[k] = _r(r.get(k), 3)
        rows.append(_drop_none(row))
        if len(rows) >= limit:
            break
    ts = common.num(h.get("ts_epoch")) or common.parse_ts(h.get("ts"))
    age = None if ts is None else c.now - ts
    return {"stale_s": int(age) if age is not None and age > HOT_STALE_S else None, "top": rows}


def _struct_doc(c):
    """runtime/structure.json (marketdata/structure.py), один раз на досье. -> dict с ключом symbols | None."""
    if not hasattr(c, "_struct"):
        d = common.read_json(paths.runtime("structure.json", root=c.root))
        ok = isinstance(d, dict) and d.get("schema") == "tb.structure/1" and isinstance(d.get("symbols"), dict) and common.num(d.get("ts_epoch")) is not None
        c._struct = d if ok else None
    return c._struct


def _num_list(v, n):
    if not isinstance(v, list):
        return None
    out = [_p(x) for x in v[:n]]
    return out if out and all(x is not None for x in out) else None


def _struct_levels(rows):
    out = []
    for r in rows[:2] if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("t") in _STRUCT_TAGS and common.num(r.get("px")) is not None:
            out.append([_p(r["px"]), _r(r.get("d_pct"), 2), _r(r.get("d_atr"), 1), r["t"]])
    return out


def _struct_row(c, sym, s, hot_rows):
    """Одна монета: режим, ATR, возвраты, границы суток/недели, уровни [px, расстояние %, расстояние в ATR(1ч), тег], оборот и волатильность к норме, спред/funding."""
    row = {"px": _p(s.get("px")), "reg": s.get("reg") if s.get("reg") in _STRUCT_REG else None}
    for k in _STRUCT_NUMS:
        row[k] = _r(s.get(k), 3)
    row["day"] = _num_list(s.get("day"), 2)
    row["pd"] = _num_list(s.get("pd"), 3)
    row["wk"] = _num_list(s.get("wk"), 2)
    row["res"], row["sup"] = _struct_levels(s.get("res")), _struct_levels(s.get("sup"))
    m = (c.mkt.get("symbols") or {}).get(sym) if isinstance(c.mkt.get("symbols"), dict) else None
    sp = fu = None
    if isinstance(m, dict):
        book = m.get("book") if isinstance(m.get("book"), dict) else {}
        sp = _r(book.get("spread_bps"), 2)
        fh = common.num(m.get("funding_hourly"))
        fu = None if fh is None else round(fh * 800, 4)
    h = hot_rows.get(sym)
    if h:
        sp = sp if sp is not None else _r(h.get("spread_bps"), 2)
        fu = fu if fu is not None else _r(h.get("funding_pct"), 4)
    row["sp"], row["fu"] = sp, fu
    return _drop_none(row)


def _b_struct(c, wanted, limit=STRUCT_MAX_SYMS, budget=STRUCT_BUDGET, base_first=True, pin=()):
    """Блок `struct` (данные для плана дня): по каждой монете режим reg (trend_up|trend_down|range|storm), ATR 5м/1ч в % цены (a5_pct, a1h_pct), возвраты (r1h, r4h, r24h, %),
    te = (EMA20-EMA50)/ATR(1ч) по 1ч, vx = волатильность 5м к суточной норме, tx = оборот часа к норме, dx = диапазон дня к медианному дневному, dpos = положение в диапазоне дня 0..1,
    day [hi, lo], pd [hi, lo, close] (вчера), wk [hi, lo] (неделя с понедельника UTC), res/sup = до 2 ближайших уровней [цена, расстояние %, расстояние в ATR(1ч), тег wh/wl/pdh/pdl/pdc/dh/dl/s1h/s5m],
    sp = спред bps, fu = funding 8ч %, t24_musd = оборот 24ч млн USDT. Только числа и enum из белого списка. Нет файла/схемы -> KeyError (имя попадёт в `missing`).
    Устаревший файл (> 5 мин) отдаётся с stale_s. Подрезка под budget токенов: меньше монет, потом второй уровень, потом вторичные числа.
    pin: символы, которые обязаны остаться в пределах limit, если есть в файле (для события: BTC для режима рынка)."""
    d = _struct_doc(c)
    if d is None:
        raise KeyError("struct")
    body = d["symbols"]
    hot = _model().read_json(paths.runtime("hot.json", root=c.root))
    hot_rows = {r.get("sym"): r for r in (hot.get("top") if isinstance(hot, dict) and isinstance(hot.get("top"), list) else []) if isinstance(r, dict)}
    order = [x for x in dict.fromkeys((list(_STRUCT_BASE) if base_first else []) + list(wanted) + list(_STRUCT_BASE) + list(body)) if _sym(x) and isinstance(body.get(x), dict)][:limit]
    for sym in pin:
        if sym not in order and isinstance(body.get(sym), dict) and _sym(sym):
            order = order[:max(0, limit - 1)] + [sym]
    rows = {}
    for sym in order:
        r = _struct_row(c, sym, body[sym], hot_rows)
        if r:
            rows[sym] = r
    out = {"s": rows}
    age = c.now - common.num(d.get("ts_epoch"))
    if age > STRUCT_FRESH_S:
        out["stale_s"] = int(age)
    for step in (lambda: len(rows) > 4 and rows.pop(list(rows)[-1]), lambda: any(v.update({k: v[k][:1]}) or True for v in rows.values() for k in ("res", "sup") if len(v.get(k, ())) > 1),
                 lambda: any(v.pop(k, None) is not None for v in rows.values() for k in ("tx", "dx", "r1h", "t24_musd", "pd"))):
        while tokens(out) > budget:
            if not step():
                break
    return out if rows else None


def _struct_missing(c, syms, root, now):
    """Монеты события, по которым в структуре нет записи: явный флаг вместо молчания (модель отвечает skip no_levels, а не выдумывает уровни) + запрос потоку структуры подтянуть их."""
    d = _struct_doc(c)
    body = d["symbols"] if d else {}
    miss = [x for x in syms if not isinstance(body.get(x), dict)]
    if miss:
        try:
            from tb.marketdata import structure
            structure.request(root, miss, now)
        except Exception:
            pass
    return miss or None


def _b_levels(c, wanted):
    d = common.read_json(paths.levels_file(root=c.root))
    if not isinstance(d, dict):
        return None
    body = d.get("symbols") if isinstance(d.get("symbols"), dict) else d
    ts = common.num(d.get("ts_epoch")) or common.parse_ts(d.get("ts"))
    stale = None if ts is None or c.now - ts <= 60 else int(c.now - ts)
    out = {}
    for sym in wanted:
        s = body.get(sym)
        if not isinstance(s, dict):
            continue
        row = {k: _p(s.get(k, s.get(_LEVEL_ALIAS[k]))) if k == "px" else _r(s.get(k, s.get(_LEVEL_ALIAS[k])), 6) for k in _LEVEL_NUMS}
        row["atr_1h"] = _r(s.get("atr_1h_14"), 6)
        for k in ("day", "prev_day", "week"):
            v = s.get(k)
            if isinstance(v, dict):
                row[k] = _drop_none({"hi": _p(v.get("hi")), "lo": _p(v.get("lo"))})
        sw = s.get("swing_5m")
        if isinstance(sw, list):
            row["swing_5m"] = [_p(x) for x in sw[:6] if common.num(x) is not None]
        sw = s.get("swing_1h")
        if isinstance(sw, list):
            row["swing_1h"] = [_p(x) for x in sw[:4] if common.num(x) is not None]
        if stale is not None or h_exec_dead(c):
            row["stale_s"] = stale if stale is not None else int(c.health["age_s"] or 0)
        out[sym] = _drop_none(row)
    return out


def h_exec_dead(c):
    return not c.health["alive"]


def _b_ideas(c):
    items = []
    book = ideas_core.price_book(c.root, c.now)
    allr = ideas_core.load_ideas(c.root)
    by_status = {}
    for i in allr:
        k = i.get("status") or "idea"
        by_status[k] = by_status.get(k, 0) + 1
    for i in reversed(allr):
        if (i.get("status") or "idea") not in ideas_core.ACTIVE:
            continue
        sym = _sym(i.get("sym"))
        iid = i.get("id") if isinstance(i.get("id"), str) and common.RE_IDEA_ID.match(i["id"]) else None
        if not sym or not iid:
            continue
        cur = book.get(sym)
        lvl = common.num(i.get("level"))
        dist = 100.0 * (cur[0] / lvl - 1.0) if cur and lvl else None
        ts = common.num(i.get("ts_epoch"))
        items.append(_drop_none({"id": iid, "sym": sym, "side": i.get("side") if i.get("side") in ("long", "short", "watch") else None,
                                 "level": _p(lvl), "stop": _p(i.get("stop")), "tp": _p(i.get("tp")), "dist_pct": _r(dist),
                                 "status": i.get("status") or "idea", "age_h": None if ts is None else round((c.now - ts) / 3600.0, 1)}))
    items = items[:8]
    n_closed = sum(v for k, v in by_status.items() if k not in ideas_core.ACTIVE)
    stats = {"total": len(allr), "n_closed": n_closed, "by_status": by_status}
    if n_closed < ideas_core.SMALL_N:
        stats["note"] = "n<%d" % ideas_core.SMALL_N
    return {"active": items, "stats": stats}


def _ideas_of(block, syms):
    """Активные идеи только по монетам события (до 3): блок `ideas` досье события | None."""
    act = [x for x in (block or {}).get("active", []) if x.get("sym") in syms][:3]
    return {"active": act} if act else None


def _b_recent(c, n, after=None, seqs=None):
    """Последние n событий после `after` (или хвост шины). seqs: события задания мозга, которые ОБЯЗАНЫ попасть в блок, даже если после них на шине уже
    больше n событий (иначе wake-событие вытесняется очередью и entry_context теряет цену, спред и диапазон 5м события)."""
    must = sorted({int(x) for x in (seqs or ()) if isinstance(x, int) and not isinstance(x, bool)})
    if after is not None or must:
        lo = after if after is not None else must[0] - 1
        if must:
            lo = min(lo, must[0] - 1)
        allv = bus.read(after_seq=lo, limit=0, root=c.root)
        if must:
            keep = [e for e in allv if e["seq"] in must]
            rest = [e for e in allv if e["seq"] not in must and (after is None or e["seq"] > after)]
            evs = sorted(keep + rest[-max(0, n - len(keep)):], key=lambda e: e["seq"])[-max(n, len(keep)):]
        else:
            evs = allv[-n:]
    else:
        evs = bus.read_tail(n, root=c.root)
    out = []
    for e in evs:
        ce = bus.clean_out(e)
        if ce:
            ts = bus.parse_iso(ce.get("ts"))
            ce.pop("ts", None)
            if ts is not None:
                ce["age_s"] = int(max(0, c.now - ts))
            out.append(ce)
    return out


def _b_lessons(c):
    last = [t for t in c.trades if t["status"] == "closed" and t.get("mode") == "live"][-4:]
    out = []
    if last:
        br = sum(1 for t in last if t.get("rule_followed") == "no")
        nc = sum(1 for t in last if "no-confirm" in (t.get("err") or []))
        if br:
            out.append("rule-break: %d из %d последних сделок" % (br, len(last)))
        if nc:
            out.append("no-confirm: %d из %d последних сделок" % (nc, len(last)))
    return out


def memory_block(c, scope, plans, involved=(), consumer=None, scout_sym=None, scout_feats=None):
    """Блок `memory` (WP15b; 08 п. 3.6): похожие прошлые случаи и уроки-гипотезы, <= 450 токенов, только числа/enum/идентификаторы. Считает tb.memory.blocks.
    scope=start: статистика за 14 дней + топ-3 урока; scope=event: только для плана armed/draft, о котором событие (по открытой позиции памяти нет).
    paper/testnet сразу, live только при config/memory.json: show_in_live=true; выключено/нет данных -> None."""
    from tb.memory import blocks
    mode = common.ident(c.health["data"].get("mode"))
    spec = None
    if scope == "event":
        cand = [p for p in plans if p.get("status") in ACTIVE_PLAN and (not involved or p.get("id") in involved)]
        it = next((x for x in c.plans if cand and x["id"] == cand[0]["id"]), None)
        if it is None:                       # плана нет: поиск входа по монете события (entry_scout), позиций по-прежнему без памяти
            return blocks.brief_block(c.root, c.now, scope, mode=mode, consumer=consumer, scout_sym=scout_sym, scout_feats=scout_feats) if scout_sym else None
        spec, mode = it["spec"], common.ident(it["spec"].get("mode")) or mode
    return blocks.brief_block(c.root, c.now, scope, spec=spec, mode=mode, consumer=consumer)


def _b_checklist(c):
    d = common.read_json(paths.brief_checklist_file(root=c.root))
    items = d.get("items") if isinstance(d, dict) else None
    out = []
    for it in (items if isinstance(items, list) else [])[:CHECKLIST_MAX]:
        if isinstance(it, dict) and common.ident(it.get("id")):
            out.append({"id": it["id"], "text": common.clean_text(it.get("text"), CHECKLIST_TEXT_LEN)})
    return out


def _prev_brief(root):
    d = paths.briefs(root=root)
    try:
        names = sorted(n for n in os.listdir(d) if n.startswith("B-") and n.endswith(".json"))
    except OSError:
        return None
    for n in reversed(names):
        b = common.read_json(os.path.join(d, n))
        if isinstance(b, dict) and common.parse_ts(b.get("ts")):
            return common.parse_ts(b["ts"])
    return None


def _b_since_last(c):
    since = _prev_brief(c.root)
    if since is None:
        since = c.now - 86400.0
    rows = [t for t in c.trades if t["status"] == "closed" and (t.get("ts_close_epoch") or 0) > since and t.get("mode") == "live"]
    wake = bus.read(after_seq=0, limit=0, wake_only=True, root=c.root)
    n_wake = sum(1 for e in wake if (bus.parse_iso(e.get("ts")) or 0) > since)
    return _drop_none({"since": common.iso(since), "closed": len(rows), "pnl": _r(sum(t.get("pnl_usdt") or 0.0 for t in rows)),
                       "rule_breaks": sum(1 for t in rows if t.get("rule_followed") == "no"), "wake_events": n_wake})


# ------------------------------------------------------------------ план целиком (scope=plan:ID)
_clean_struct = common.clean_struct


def _b_plan_full(c, plan_id):
    for it in c.plans:
        if it["id"] == plan_id:
            spec = dict(it["spec"])
            thesis = spec.pop("thesis", None)
            rt = it["rt"]
            body = {"id": plan_id, "status": common.plan_status(it), "spec": _clean_struct(spec),
                    "rt": _clean_struct({k: rt.get(k) for k in ("status", "hold", "entry", "pos", "targets", "rules", "realized", "close") if k in rt})}
            body["own_thesis"] = common.clean_text(thesis, THESIS_LEN) or None
            return _drop_none(body)
    return None


# ------------------------------------------------------------------ сборка и усечение
# порядок отбрасывания при нехватке бюджета: (описание, функция(brief)); ядро (system, account, session, room, positions, plans) не отбрасывается
def _trim_recent(n):
    def f(b):
        if isinstance(b.get("recent"), list) and len(b["recent"]) > n:
            b["recent"] = b["recent"][-n:]
            return True
        return False
    return f


def _trim_hot(n):
    def f(b):
        h = b.get("hot")
        if isinstance(h, dict) and len(h.get("top") or []) > n:
            h["top"] = h["top"][:n]
            return True
        return False
    return f


def _drop(key):
    def f(b):
        return b.pop(key, None) is not None
    return f


def _checklist_ids(b):
    cl = b.get("checklist")
    if isinstance(cl, list) and any("text" in x for x in cl):
        b["checklist"] = [{"id": x["id"]} for x in cl]
        return True
    return False


def _trim_ideas(n):
    def f(b):
        i = b.get("ideas")
        if isinstance(i, dict) and len(i.get("active") or []) > n:
            i["active"] = i["active"][:n]
            return True
        return False
    return f


def _trim_levels(b):
    lv = b.get("levels")
    if not isinstance(lv, dict) or not lv:
        return False
    for row in lv.values():
        row.pop("swing_5m", None)
        row.pop("swing_1h", None)
    pos_syms = {x.get("sym") for x in (b.get("positions") or [])}
    b["levels"] = {k: v for k, v in lv.items() if k in pos_syms}
    if not b["levels"]:
        b.pop("levels")
    return True


def _trim_struct(b):
    st = b.get("struct")
    if isinstance(st, dict) and len(st.get("s") or {}) > 3:
        st["s"] = dict(list(st["s"].items())[:3])
        return True
    return False


TRIM_STEPS = (
    ("lessons", _drop("lessons")), ("missing", _drop("missing")), ("memory", _drop("memory")), ("playbook", _drop("playbook")), ("flow", _drop("flow")), ("since_last", _drop("since_last")), ("paper_history", _drop("paper_history")), ("checklist_text", _checklist_ids), ("recent>5", _trim_recent(5)),
    ("hot>3", _trim_hot(3)), ("ideas>4", _trim_ideas(4)), ("struct>3", _trim_struct), ("levels", _trim_levels), ("recent>3", _trim_recent(3)), ("pending", _drop("pending")),
    ("hot", _drop("hot")), ("ideas", _drop("ideas")), ("experiments", _drop("experiments")), ("struct", _drop("struct")), ("news_digest", _drop("news_digest")), ("intel", _drop("intel")), ("recent>1", _trim_recent(1)), ("checklist", _drop("checklist")), ("market", _drop("market")),
    ("recent", _drop("recent")),
)


def fit(brief, budget):
    """Усекает досье до бюджета токенов (приблизительно) в порядке приоритета; помечает trunc и перечисляет снятое в `cut`."""
    cut = []
    for name, fn in TRIM_STEPS:
        if tokens(brief) + (CUT_RESERVE + 3 * len(cut) if cut else 0) <= budget:       # запас под ключи trunc/cut, которые добавятся после усечения
            break
        if fn(brief):
            cut.append(name)
    if cut:
        brief["trunc"] = True
        brief["cut"] = cut
    return brief


def _safe(name, fn, missing):
    try:
        return fn()
    except (KeyError, TypeError, ValueError, ZeroDivisionError, AttributeError, OSError, NotImplementedError, ImportError, TimeoutError):
        missing.append(name)
        return None


def next_brief_id(root, now):
    stamp = time.strftime("B-%Y%m%d-%H%M", time.gmtime(now))
    try:
        n = sum(1 for x in os.listdir(paths.briefs(root=root)) if x.startswith(stamp))
    except OSError:
        n = 0
    return "%s-%02d" % (stamp, n + 1)


def build(scope="start", since=None, budget_tokens=None, root=None, now=None, consumer=bus.DEFAULT_CONSUMER, seqs=None, sym=None):
    """-> dict досье. scope: start | event | plan:ID. since: seq шины (для event: события строго после него; по умолчанию acked потребителя).
    seqs/sym (scope=event): события и монета задания мозга: они всегда попадают в `recent`, монета задания идёт первой в `struct`/`struct_missing`/`levels`
    (без них монеты берутся из хвоста шины, где wake-событие может быть вытеснено, а монета выбрана не та)."""
    now = time.time() if now is None else now
    scope_plan = RE_PLAN_SCOPE.match(scope or "")
    if scope not in SCOPES and not scope_plan:
        raise ValueError("scope: start | event | plan:PL-MMDD-SYM-NN")
    own_budget = budget_tokens is None
    if budget_tokens is None:
        budget_tokens = EVENT_BUDGET if scope == "event" else DEFAULT_BUDGET
    missing = []
    c = _Ctx(root, now)
    b = {"brief_id": next_brief_id(root, now), "ts": common.iso(now), "scope": scope, "trunc": False}
    b["system"] = _safe("system", lambda: _b_system(c), missing)
    b["account"] = _safe("account", lambda: _b_account(c), missing)
    b["session"] = _safe("session", lambda: _b_session(c), missing)
    b["room"] = _safe("room", lambda: _b_room(c), missing)
    b["risk_level"] = _safe("risk_level", lambda: _b_risk_level(c), missing)
    if scope in SCOPES:
        b["economics"] = _safe("economics", lambda: _b_economics(c), missing)
        b["experiments"] = _safe("experiments", lambda: _b_experiments(c), missing)
    b["paper_history"] = _safe("paper_history", lambda: _b_paper_history(c), missing)
    positions = _safe("positions", lambda: _b_positions(c), missing) or []
    plans = _safe("plans", lambda: _b_plans(c), missing) or []
    b["positions"], b["plans"] = positions, plans
    c.brief_positions = positions
    b["limits"] = _safe("limits", lambda: _b_limits(c), missing)
    wanted = [x["sym"] for x in positions] + [x["sym"] for x in plans]
    if scope == "start":
        b["pending"] = _safe("pending", lambda: _b_pending(c), missing)
        b["market"] = _safe("market", lambda: _b_market(c), missing)
        b["hot"] = _safe("hot", lambda: _b_hot(c), missing)
        b["ideas"] = _safe("ideas", lambda: _b_ideas(c), missing)
        wanted += [x["sym"] for x in (b["ideas"] or {}).get("active", [])[:2]] + [x["sym"] for x in ((b["hot"] or {}).get("top") or [])[:2]]
        b["levels"] = _safe("levels", lambda: _b_levels(c, list(dict.fromkeys(wanted))[:6]), missing)
        b["struct"] = _safe("struct", lambda: _b_struct(c, list(dict.fromkeys(x["sym"] for x in positions + plans) ) + [x["sym"] for x in (b["hot"] or {}).get("top", [])]), missing)
        b["intel"] = _safe("intel", lambda: intel_block(root, now, wanted), missing)
        b["news_digest"] = _safe("news_digest", lambda: news_digest_block(root, now), missing)
        _merge_blackout(b)
        b["recent"] = _safe("recent", lambda: _b_recent(c, 8), missing)
        b["since_last"] = _safe("since_last", lambda: _b_since_last(c), missing)
        b["lessons"] = _safe("lessons", lambda: _b_lessons(c), missing)
        b["memory"] = _safe("memory", lambda: memory_block(c, "start", b["plans"], consumer=consumer), missing)
        b["checklist"] = _safe("checklist", lambda: _b_checklist(c), missing)
    elif scope == "event":
        after = since if since is not None else bus.cursor_get(consumer, root=root)["acked"]
        job_seqs = {int(x) for x in (seqs or ()) if isinstance(x, int) and not isinstance(x, bool)}
        b["recent"] = _safe("recent", lambda: _b_recent(c, 8, after=after, seqs=job_seqs), missing)
        involved = {e.get("plan") for e in (b["recent"] or []) if e.get("plan")}
        syms = {e.get("sym") for e in (b["recent"] or []) if e.get("sym")}
        # монеты по приоритету: монета задания, монеты событий задания (новые первыми), затем остальные из recent (новые первыми); НЕ по алфавиту
        job_sym = _sym(sym)
        if job_sym:
            syms.add(job_sym)
        prio = ([job_sym] if job_sym else []) + [e["sym"] for e in reversed(b["recent"] or []) if e.get("seq") in job_seqs and _sym(e.get("sym"))] \
            + [e["sym"] for e in reversed(b["recent"] or []) if _sym(e.get("sym"))]
        prio = list(dict.fromkeys(prio))
        if involved:
            b["positions"] = [x for x in positions if x.get("plan") in involved] or positions
            b["plans"] = [x for x in plans if x.get("id") in involved] or plans
        b["plans"] = b["plans"][:4]          # ближайшие триггеры (список уже отсортирован по расстоянию)
        if syms:          # сигнал сканера: планы по монетам события не теряются в срезе (роль entry_scout не должна дублировать вход)
            have = {x.get("id") for x in b["plans"]}
            b["plans"] += [x for x in plans if x.get("sym") in syms and x.get("id") not in have][:3]
        b["pause"] = _safe("pause", lambda: _b_pause(c, syms), missing) if syms else None
        b["flash"] = _safe("flash", lambda: _b_flash(c, job_sym), missing) if job_sym else None
        b["flow"] = _safe("flow", lambda: _b_flow(c, job_sym), missing) if job_sym else None
        sig = next((e for e in reversed(b["recent"] or []) if e.get("type") in ("scanner_signal", "news_flash") and job_sym and e.get("sym") == job_sym), None)
        sd = (sig or {}).get("d") if isinstance((sig or {}).get("d"), dict) else {}
        sfeats = {k: float(sd[k]) for k in ("range_5m_pct", "ret_5m_pct") if isinstance(sd.get(k), (int, float)) and not isinstance(sd.get(k), bool)}
        b["memory"] = _safe("memory", lambda: memory_block(c, "event", b["plans"], involved, consumer, job_sym if sig is not None else None, sfeats), missing)
        wanted = prio[:2] + [x["sym"] for x in b["positions"]] + [x["sym"] for x in b["plans"]] + prio
        b["levels"] = _safe("levels", lambda: _b_levels(c, list(dict.fromkeys(wanted))[:4]), missing)
        ev_syms = prio[:2]
        b["day"] = _safe("day", lambda: _b_day(c), missing)
        b["ideas"] = _safe("ideas", lambda: _ideas_of(_b_ideas(c), syms), missing)      # idea_level: уровень, стоп и цель идеи по монете события
        b["struct"] = _safe("struct", lambda: _b_struct(c, ev_syms + wanted, limit=3, budget=STRUCT_EVENT_BUDGET, base_first=False, pin=("BTC",)), missing)
        b["struct_missing"] = _struct_missing(c, ev_syms, root, now)
        b["market"] = _safe("market", lambda: {k: v for k, v in _b_market(c).items() if k in ("risk_off", "blackout")}, missing)
        mk = b["market"] if isinstance(b["market"], dict) else {}
        _merge_blackout({"intel": _safe("intel", lambda: intel_block(root, now), missing), "market": mk})
        b["market"] = mk or None
        b["playbook"] = _safe("playbook", lambda: _b_playbook(c, b, job_sym), missing) if (job_sym and not involved) else None      # только поиск входа: у событий планов карточки не нужны
    else:
        pid = scope_plan.group(1)
        full = _safe("plan", lambda: _b_plan_full(c, pid), missing)
        if full is None and "plan" not in missing:
            missing.append("plan_not_found")
        b["plan"] = full
        sym = _sym(((full or {}).get("spec") or {}).get("symbol")) if isinstance((full or {}).get("spec"), dict) else None
        b["positions"] = [x for x in positions if x.get("plan") == pid]
        b["plans"] = [x for x in plans if x.get("id") == pid]
        b["levels"] = _safe("levels", lambda: _b_levels(c, [sym] if sym else []), missing) if sym else None
        b["recent"] = _safe("recent", lambda: [e for e in _b_recent(c, 40) if e.get("plan") == pid][-8:], missing)
    _blackout_mode(b, c)
    if c.plans_bad:
        b["plans_bad"] = c.plans_bad
    if missing:
        b["missing"] = missing
    b = {k: v for k, v in b.items() if k in ("brief_id", "ts", "scope", "trunc") or (v is not None and v != [] and v != {})}
    if own_budget and b.get("memory"):
        budget_tokens += tokens(b["memory"])          # блок памяти имеет собственный лимит (<= 450) и не вытесняет остальное досье из бюджета по умолчанию
    if own_budget and b.get("struct"):
        budget_tokens += tokens(b["struct"])          # struct (структура рынка) тоже со своим лимитом (STRUCT_BUDGET / STRUCT_EVENT_BUDGET): не вытесняет остальное досье
    return fit(b, budget_tokens)


def save(brief, root=None, now=None):
    """Сохраняет досье в state/briefs/B-*.json (сжатый JSON, <= 12 КБ) и удаляет старше 14 дней. Ошибки записи не фатальны -> путь или None."""
    now = time.time() if now is None else now
    d = paths.briefs(root=root)
    text = common.dumps(brief)
    if len(text.encode("utf-8")) > BRIEF_MAX_BYTES:
        slim = {k: v for k, v in brief.items() if k in ("brief_id", "ts", "scope", "system", "account", "room", "positions", "plans", "session")}
        slim["trunc"] = True
        text = common.dumps(slim)
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, brief["brief_id"] + ".json")
        tmp = path + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        from tb import fsutil
        fsutil.replace(tmp, path)
        for n in os.listdir(d):
            p = os.path.join(d, n)
            if n.startswith("B-") and n.endswith(".json") and now - os.path.getmtime(p) > BRIEF_KEEP_DAYS * 86400.0:
                os.remove(p)
        return path
    except OSError:
        return None


def render_text(b):
    """Человекочитаемый вид (`--text`): блок на строку, числа как в JSON."""
    lines = ["%s  %s  scope=%s%s" % (b.get("brief_id"), b.get("ts"), b.get("scope"), "  (усечено: %s)" % ",".join(b.get("cut", [])) if b.get("trunc") else "")]
    for k, v in b.items():
        if k in ("brief_id", "ts", "scope", "trunc", "cut"):
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            lines.append("%s:" % k)
            lines.extend("  " + common.dumps(x) for x in v)
        else:
            lines.append("%s: %s" % (k, common.dumps(v)))
    return "\n".join(lines)


def main(argv=None):
    common.setup_stdout()
    p = argparse.ArgumentParser(prog="tb brief")
    p.add_argument("--scope", default="start")
    p.add_argument("--since", type=int, default=None)
    p.add_argument("--budget-tokens", type=int, default=None)
    p.add_argument("--consumer", default=bus.DEFAULT_CONSUMER)
    p.add_argument("--text", action="store_true")
    p.add_argument("--no-save", action="store_true", help="не сохранять досье в state/briefs/")
    try:
        a = p.parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as e:
        return common.EXIT_USAGE if e.code else common.EXIT_OK
    try:
        if a.budget_tokens is not None and a.budget_tokens < 200:
            raise ValueError("--budget-tokens >= 200")
        b = build(a.scope, since=a.since, budget_tokens=a.budget_tokens, consumer=a.consumer)
    except ValueError as e:
        return common.fail(str(e))
    if not a.no_save:
        save(b)
    if a.text:
        print(render_text(b))
    else:
        common.out(b)
    return common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
