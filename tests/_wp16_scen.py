"""WP16: набор сценариев для теста эквивалентности guard ДО и ПОСЛЕ подключения уровней риска.
`run_all(root)` строит во временном корне состояние (журнал, портфель, сессии) и возвращает {имя: вывод guard}; золотой файл
tests/fixtures_wp16/guard_golden.json записан со СТАРОГО guard (до правок WP16): tests/gen_wp16_golden.py (запуск вручную, только в корне без risklevels)."""
import argparse
import json
import os
import random
import shutil
import tempfile
import time

TODAY_FMT = "%Y-%m-%d"
VENUE = "bybit"


def _patch_paths(d):
    from tb.core import journal, learning, pending, portfolio, session
    old = (journal.PATH, portfolio.PATH, session.PATH, learning.PATH, pending.PATH)
    journal.PATH = os.path.join(d, "trades.jsonl")
    portfolio.PATH = os.path.join(d, "portfolio.json")
    session.PATH = os.path.join(d, "sessions.jsonl")
    learning.PATH = os.path.join(d, "learning.json")
    pending.PATH = os.path.join(d, "pending_orders.json")
    return old


def _restore(old):
    from tb.core import journal, learning, pending, portfolio, session
    journal.PATH, portfolio.PATH, session.PATH, learning.PATH, pending.PATH = old


def make_root(with_levels, ROOT):
    """Временный корень с config/risk_profile.json, exec_limits.json (и risk_levels.json, если with_levels) и пустым state/."""
    root = tempfile.mkdtemp(prefix="tbwp16_")
    os.makedirs(os.path.join(root, "config"))
    os.makedirs(os.path.join(root, "state"))
    names = ["risk_profile.json", "exec_limits.json"] + (["risk_levels.json"] if with_levels else [])
    for n in names:
        shutil.copy(os.path.join(ROOT, "config", n), os.path.join(root, "config", n))
    return root


def _opens(today):
    T = today + "T01:00:00Z"
    return {
        "none": [],
        "eth_long": [("open", dict(id="o1", sym="ETH", side="long", mode="live", risk_usdt=0.5, notional=40, entry=1, ts_open=T))],
        "btc_eth": [("open", dict(id="o1", sym="BTC", side="long", mode="live", risk_usdt=0.4, notional=40, entry=1, ts_open=T)),
                    ("open", dict(id="o2", sym="ETH", side="long", mode="live", risk_usdt=0.4, notional=40, entry=1, ts_open=T))],
        "sol_short": [("open", dict(id="o1", sym="SOL", side="short", mode="live", risk_usdt=0.8, notional=40, entry=1, ts_open=T))],
        "loss_2_5": [("open", dict(id="c1", sym="DOGE", side="long", mode="live", risk_usdt=0.5, notional=40, entry=1, ts_open=T)),
                     ("close", dict(id="c1", ts_close=today + "T02:00:00Z", pnl_usdt=-2.5, reason="manual"))],
        "streak3": sum(([("open", dict(id="s%d" % i, sym="S%d" % i, side="long", mode="live", risk_usdt=0.3, notional=20, entry=1, ts_open=T)),
                         ("close", dict(id="s%d" % i, ts_close=today + "T0%d:00:00Z" % (3 + i), pnl_usdt=-0.3, reason="sl"))] for i in range(3)), []),
    }


PORTS = {"flat30": (30.0, 30.0), "dd7": (28.0, 30.0), "dd17": (25.0, 30.0), "halt": (23.0, 30.0), "eq60": (60.5, 60.5), "eq10": (10.0, 10.0)}
SESS = (None, "scalp", "intraday", "swing", "position")


def _setup(d, today, port, opens, stype):
    from tb.core import journal
    eq, hwm = PORTS[port]
    with open(os.path.join(d, "portfolio.json"), "w", encoding="utf-8") as f:
        json.dump({"venues": {VENUE: {"equity": eq, "free": round(eq * 0.8, 2), "ts": "2026-10-28T00:00:00Z"}}, "hwm": hwm}, f)
    for p in ("trades.jsonl", "sessions.jsonl", "learning.json", "pending_orders.json"):
        try:
            os.remove(os.path.join(d, p))
        except OSError:
            pass
    for ev, r in _opens(today)[opens]:
        journal._append(dict(r, ev=ev))
    if stype:
        t = time.time()
        with open(os.path.join(d, "sessions.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"ev": "start", "id": "%s-x" % stype, "type": stype, "venue": VENUE, "thesis": "t", "ts": "x", "start": t,
                                "expires": t + 36000, "budget_usdt": round(0.04 * eq, 4), "equity_at_start": eq}) + "\n")


def _norm(o):
    return json.loads(json.dumps(o, sort_keys=True))


STOPS = {None: [0.4, 0.7, 1.5, 2.0, 2.9], "scalp": [0.3, 0.5, 0.7, 1.5], "intraday": [0.5, 1.0, 2.0, 2.9, 4.0], "swing": [1.0, 3.0, 8.0, 14.0],
         "position": [2.0, 10.0, 20.0, 30.0]}
RRS = [1.1, 1.6, 2.2, 3.3, 5.0]


def grid(n=260, seed=16):
    rnd = random.Random(seed)
    combos = []
    for _ in range(n):
        stype = rnd.choice(SESS)
        port = rnd.choice(["flat30", "flat30", "flat30", "eq60", "eq60", "dd7", "dd17", "halt", "eq10"])
        opens = rnd.choice(["none", "none", "none", "eth_long", "btc_eth", "sol_short", "loss_2_5", "streak3"])
        combos.append(dict(port=port, opens=opens, stype=stype, sym=rnd.choice(["BTC", "ETH", "SOL", "XYZ", "DOGE"]),
                           side=rnd.choice(["long", "short"]), stop=rnd.choice(STOPS[stype]), rr=rnd.choice(RRS), score=rnd.choice([0.0, 0.3, 0.7, 1.0]),
                           regime=rnd.choice(["range", "range", "range", "trend", "storm"]), entry=rnd.choice([100.0, 2300.0, 0.25]),
                           mql=rnd.choice([30, 50, 125]), paper=rnd.random() < 0.15, ex=rnd.choice(["taker", "maker", "limit"]), min_quote=rnd.choice([1.0, 5.0])))
    return combos


def _args(c):
    e = c["entry"]
    sgn = 1 if c["side"] == "long" else -1
    sl = round(e * (1 - sgn * c["stop"] / 100.0), 6)
    tp = round(e * (1 + sgn * c["stop"] * c["rr"] / 100.0), 6)
    return argparse.Namespace(sym=c["sym"], side=c["side"], entry=e, sl=sl, tp=tp, equity=None, free=None, no_session=(c["stype"] is None),
                              score=c["score"], regime=c["regime"], min_quote=c["min_quote"], contract_max_lev=c["mql"], profile=None, paper=c["paper"], exec=c["ex"])


def add_cases():
    base = dict(sym="BTC", side="long", px=105.0, stop=101.0, qty_open=1.0, avg_open=100.0, plan_risk0=0.5, risk_add_cap=None, score=0.7,
                contract_max_lev=50, exec="taker", venue=VENUE, no_session=True, pyramid_enabled=True, realized_net=0.0)
    return [dict(base), dict(base, px=108.0, stop=103.0), dict(base, pyramid_enabled=False), dict(base, px=100.3, stop=100.2), dict(base, side="short", px=95.0, stop=99.0),
            dict(base, plan_risk0=0.2, contract_max_lev=5), dict(base, exec="maker", free=9.0, qty_step=0.01, min_qty=0.01)]


def run_all(root, n=260):
    """-> {имя: вывод}. TB_ROOT = root; пути состояния подменены на временную папку внутри root."""
    from tb.core import guard
    today = time.strftime(TODAY_FMT, time.gmtime())
    d = os.path.join(root, "state")
    old_env = os.environ.get("TB_ROOT")
    os.environ["TB_ROOT"] = root
    old = _patch_paths(d)
    out = {}
    try:
        for i, c in enumerate(grid(n)):
            _setup(d, today, c["port"], c["opens"], c["stype"])
            a = _args(c)
            r = guard.evaluate(a)
            ctx = r.get("ctx") or {}
            if ctx.get("session"):
                ctx["session"] = "S"
            out["g%03d" % i] = _norm(r)
        for i, k in enumerate(add_cases()):
            _setup(d, today, "flat30", "none", None)
            out["a%02d" % i] = _norm(guard.evaluate_add(argparse.Namespace(**k)))
    finally:
        _restore(old)
        if old_env is None:
            os.environ.pop("TB_ROOT", None)
        else:
            os.environ["TB_ROOT"] = old_env
    return out
