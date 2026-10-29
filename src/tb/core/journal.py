"""Журнал сделок для разбора: тезис, контекст рынка, новости, выход, PnL, R-кратность.

  journal.py open  --sym ETH --side short --entry 2312.95 --lev 3 --margin 9.95 --sl 2452.49 --tp 2036.03 \
                   --thesis "..." [--news "..."] [--regime range] [--tags a,b] [--mode live|paper] [--id X]
  journal.py close --id X --exit 2300 --reason tp|sl|manual|timeout [--funding 0.0004] [--note "..."] [--err TAG[,TAG]] [--rule-followed yes|no|partial] [--lesson "..."]
  journal.py post ID --err TAG[,TAG] --rule-followed yes|no|partial --lesson "..."     разбор закрытой сделки позже (событие post, журнал не правится)
  journal.py list | review [--mode live|paper]
  journal.py analyze [--mode live|paper|testnet|all] [--min-bucket 10] [--json]        разбор ошибок по закрытым сделкам (таблица, сводка, теги, MAE/MFE, ctx-бакеты)
Файл state/trades.jsonl (только дописывание). Комиссия taker 0.09% на сторону считается от номинала.
PnL: sign * (exit/entry - 1) * notional - 2 * fee * notional - funding. R = PnL / риск до стопа (по sl).
Данные для разбора ошибок (все поля необязательны, старые записи без них читаются как раньше):
  open.ctx        снимок рынка на входе (core/tradectx.py: цена, диапазоны и доходность 5м/15м, оборот x к норме, 24ч, спред, funding, ранг в hot.json, режим, возраст источников);
  open.ctx_place  то же на момент ПОСТАНОВКИ ждущего лимита (ctx = на момент заполнения);
  close.path      MAE/MFE внутри сделки (core/tradepath.py, считает сканер hotscan), path_coverage = доля времени, покрытая замерами;
  close/post: err (теги из tradectx.ERR_TAGS), rule_followed (yes|no|partial), lesson. Событие post дописывается позже и ЛОЖИТСЯ ПОВЕРХ закрытой записи при чтении.
v2 (план исполнителя, всё необязательно и обратно-совместимо):
  open.plan_id/plan_rev/exp/var/setup/hyp/plan_ctx + теги plan:ID, exp:X, var:X, setup:X (добавляются автоматически, без дублей);
  journal.py add    --id X --px P --qty Q [--fee F]            добор части позиции (событие add): entry = средневзвешенная, notional/margin растут, risk_usdt (R0) НЕ меняется;
  journal.py part   --id X --px P --qty Q [--fee F] [--pnl G]  частичное закрытие (событие part): G = результат по цене без комиссий (по умолчанию считается от текущего entry), fee отдельно;
  journal.py adjust --id X [--sl S] [--tp T] [--by who]        смена стопа/тейка (событие adjust): текущие sl/tp, исходный sl сохраняется в sl0;
  close: pnl_override (итоговый PnL, уже за вычетом комиссий) и fees_usdt (итог комиссий) для сделок с частями; без override PnL собирается из частей + остатка по a.exit.
  Состояние после add/part (поля сделки при чтении): qty (остаток), entry0, notional0, adds, parts, realized_parts (по цене), fees_parts.
  Старые читатели пропускают незнакомые события; R всегда относится к исходному риску risk_usdt.
  journal.py analyze --by exp|var|setup|plan_id|sym|side|stype|venue|reason|regime|tag   сводка по группам (тег-префикс exp:/var:/setup: подхватывается).
"""
import argparse
import json
import math
import os
import sys
import time

from tb import paths
from tb.core import tradectx
from tb.core import tradepath

PATH = paths.state("trades.jsonl")
FEE = 0.0009


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append(rec):
    with open(PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _load():
    trades, order = {}, []
    if not os.path.exists(PATH):
        return trades, order
    with open(PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r["ev"] in ("add", "part", "adjust"):          # v2: события частей позиции; на закрытую или неизвестную сделку не действуют
                t = trades.get(r.get("id"))
                if t is not None and not t.get("ts_close"):
                    _apply_part_event(t, r)
            elif r["ev"] == "open":
                trades[r["id"]] = dict(r)
                order.append(r["id"])
            elif r["ev"] == "close" and r["id"] in trades:
                trades[r["id"]].update(r)
            elif r["ev"] == "post" and r["id"] in trades:      # разбор позже: только поля разбора, остальное не трогаем
                t = trades[r["id"]]
                for k in ("err", "rule_followed", "lesson"):
                    if r.get(k):
                        t[k] = r[k]
                t["ts_post"] = r.get("ts_post")
    return trades, order


def _init_parts(t):
    """Первое событие add/part: заводит поля состояния частей (у сделок без событий их нет, они читаются как раньше)."""
    if "qty" in t:
        return
    t["qty"] = t["notional"] / t["entry"]
    t["entry0"], t["notional0"] = t["entry"], t["notional"]
    t["adds"], t["parts"] = 0, 0
    t["realized_parts"], t["fees_parts"] = 0.0, 0.0


def _apply_part_event(t, r):
    ev = r["ev"]
    if ev == "adjust":
        if r.get("sl") is not None:
            t.setdefault("sl0", t.get("sl"))
            t["sl"] = r["sl"]
        if r.get("tp") is not None:
            t.setdefault("tp0", t.get("tp"))
            t["tp"] = r["tp"]
        t["adjusts"] = t.get("adjusts", 0) + 1
        return
    q, px = r.get("qty"), r.get("px")
    if not isinstance(q, (int, float)) or not isinstance(px, (int, float)) or q <= 0 or px <= 0:
        return
    _init_parts(t)
    fee = r.get("fee")
    if fee is None:
        fee = FEE * q * px
    sign = 1 if t["side"] == "long" else -1
    if ev == "add":
        q_new = t["qty"] + q
        t["entry"] = (t["entry"] * t["qty"] + px * q) / q_new
        t["margin"] = t["margin"] + (q * px / t["lev"] if t.get("lev") else 0.0)
        t["qty"], t["adds"] = q_new, t["adds"] + 1
        t["notional"] = round(q_new * t["entry"], 4)
    else:
        q = min(q, t["qty"])
        pnl = r["pnl"] if isinstance(r.get("pnl"), (int, float)) else sign * (px - t["entry"]) * q
        q_old = t["qty"]
        t["qty"] = q_old - q if q_old - q > 1e-12 else 0.0
        t["margin"] = t["margin"] * (t["qty"] / q_old)
        t["notional"] = round(t["qty"] * t["entry"], 4)
        t["realized_parts"] += pnl
        t["parts"] += 1
        t["ts_part"] = r.get("ts")
    t["fees_parts"] += fee


def cmd_open(a):
    tid = a.id or "%s-%s-%s" % (a.sym, a.side, time.strftime("%m%d-%H%M", time.gmtime()))
    notional = a.margin * a.lev
    risk = abs(a.entry - a.sl) / a.entry * notional if a.sl else None
    rec = {"ev": "open", "id": tid, "ts_open": getattr(a, "ts_open", None) or _now(), "sym": a.sym, "side": a.side, "entry": a.entry,
             "lev": a.lev, "margin": a.margin, "notional": round(notional, 2), "sl": a.sl, "tp": a.tp,
             "risk_usdt": round(risk, 3) if risk else None, "thesis": a.thesis, "news": a.news,
             "regime": a.regime, "tags": plan_tags([t for t in (a.tags or "").split(",") if t], a), "mode": a.mode,
             "fee_open": getattr(a, "fee_open", None), "session": getattr(a, "session", None), "stype": getattr(a, "stype", None), "venue": getattr(a, "venue", "platform")}
    for k in ("idea", "pending_id", "plan_id", "plan_rev", "exp", "var", "setup", "hyp", "plan_ctx"):   # связь с идеей, ждущим лимитом и планом v2, только если заданы
        if getattr(a, k, None):
            rec[k] = getattr(a, k)
    ctx = getattr(a, "ctx", None)      # снимок рынка на входе; сбой чтения файлов даёт {} и не мешает записи сделки
    if ctx is None:
        ctx = tradectx.snapshot(a.sym, ts_open_epoch=tradepath.parse_ts(rec["ts_open"]))
    rec["ctx"] = ctx
    if getattr(a, "ctx_place", None):
        rec["ctx_place"] = a.ctx_place
    _append(rec)
    print("opened", tid)


def plan_tags(tags, a):
    """К тегам добавляет plan:ID, exp:X, var:X, setup:X из полей открытия (если заданы и ещё не перечислены)."""
    out = list(tags)
    for key, pref in (("plan_id", "plan"), ("exp", "exp"), ("var", "var"), ("setup", "setup")):
        v = getattr(a, key, None)
        if v:
            tag = "%s:%s" % (pref, v)
            if tag not in out:
                out.append(tag)
    return out


def _open_trade(a):
    trades, _ = _load()
    t = trades.get(a.id)
    if not t or t.get("ts_close"):
        sys.exit("нет открытой записи %s" % a.id)
    return t


def cmd_add(a):
    """Добор части позиции (пирамидинг по плану): событие add. Риск R0 сделки не меняется."""
    t = _open_trade(a)
    if not (a.px > 0 and a.qty > 0):
        sys.exit("add: px и qty должны быть > 0")
    rec = {"ev": "add", "id": a.id, "ts": getattr(a, "ts", None) or _now(), "px": a.px, "qty": a.qty, "fee": getattr(a, "fee", None)}
    if rec["fee"] is None:
        rec["fee"] = round(FEE * a.px * a.qty, 6)
    _append(rec)
    print("add %s px=%s qty=%s" % (a.id, a.px, a.qty))


def cmd_part(a):
    """Частичное закрытие: событие part (pnl по цене от текущего средней entry, комиссия отдельно)."""
    t = _open_trade(a)
    left = t.get("qty", t["notional"] / t["entry"])
    if not (a.px > 0 and 0 < a.qty <= left * (1 + 1e-9)):
        sys.exit("part: qty должен быть в (0, %.8g]" % left)
    sign = 1 if t["side"] == "long" else -1
    q = min(a.qty, left)
    pnl = getattr(a, "pnl", None)
    if pnl is None:
        pnl = sign * (a.px - t["entry"]) * q
    fee = getattr(a, "fee", None)
    if fee is None:
        fee = FEE * a.px * q
    _append({"ev": "part", "id": a.id, "ts": getattr(a, "ts", None) or _now(), "px": a.px, "qty": q, "fee": round(fee, 6), "pnl": round(pnl, 6)})
    print("part %s px=%s qty=%s pnl=%.4f" % (a.id, a.px, q, pnl))


def cmd_adjust(a):
    """Смена стопа/тейка по ходу позиции: событие adjust (стоп здесь не проверяется, это делает guard/исполнитель)."""
    _open_trade(a)
    if getattr(a, "sl", None) is None and getattr(a, "tp", None) is None:
        sys.exit("adjust: задай --sl и/или --tp")
    _append({"ev": "adjust", "id": a.id, "ts": getattr(a, "ts", None) or _now(), "sl": getattr(a, "sl", None), "tp": getattr(a, "tp", None),
             "by": getattr(a, "by", None)})
    print("adjust %s" % a.id)


def _post_fields(a):
    """Поля разбора из аргументов (err - список или строка, rule_followed, lesson); только заданные."""
    out = {}
    err = getattr(a, "err", None)
    if isinstance(err, str):
        err = tradectx.parse_err(err)
    if err:
        out["err"] = list(err)
    rf = getattr(a, "rule_followed", None)
    if rf:
        if rf not in tradectx.RULE_VALUES:
            raise ValueError("--rule-followed: %s" % "|".join(tradectx.RULE_VALUES))
        out["rule_followed"] = rf
    if getattr(a, "lesson", None):
        out["lesson"] = a.lesson
    return out


def cmd_post(a):
    """Разбор закрытой сделки позже: событие post (журнал только дописывается). Повторный post дополняет/заменяет заданные поля."""
    trades, _ = _load()
    t = trades.get(a.id)
    if not t:
        sys.exit("нет записи %s" % a.id)
    if not t.get("ts_close"):
        sys.exit("сделка %s ещё открыта: разбор пишется после закрытия" % a.id)
    try:
        f = _post_fields(a)
    except ValueError as e:
        sys.exit(str(e))
    if not f:
        sys.exit("нечего писать: задай --err, --rule-followed или --lesson")
    _append(dict({"ev": "post", "id": a.id, "ts_post": _now()}, **f))
    print("post %s %s" % (a.id, json.dumps(f, ensure_ascii=False)))


def cmd_close(a):
    trades, _ = _load()
    t = trades.get(a.id)
    if not t or t.get("ts_close"):
        sys.exit("нет открытой записи %s" % a.id)
    sign = 1 if t["side"] == "long" else -1
    notional = t["notional"]
    fee = getattr(a, 'fee', None) or FEE
    fees = 2 * fee * notional
    if "qty" in t:                     # были add/part: комиссии = вход исходной части + факт по событиям + выход остатка
        fees = fee * t["notional0"] + t["fees_parts"] + fee * t["qty"] * a.exit
    if getattr(a, 'fees_usdt', None) is not None:
        fees = a.fees_usdt  # фактические комиссии биржи, если известны
    if "qty" in t:
        pnl = t["realized_parts"] + sign * (a.exit - t["entry"]) * t["qty"] - fees - (a.funding or 0.0)
    else:
        pnl = sign * (a.exit / t["entry"] - 1.0) * notional - fees - (a.funding or 0.0)
    if getattr(a, "pnl_override", None) is not None:   # итог по частям посчитан исполнителем (net, после комиссий)
        pnl = a.pnl_override
    r = (pnl / t["risk_usdt"]) if t.get("risk_usdt") else None
    rec = {"ev": "close", "id": a.id, "ts_close": _now(), "exit": a.exit, "reason": a.reason,
           "pnl_usdt": round(pnl, 4), "fees_usdt": round(fees, 4), "funding_usdt": a.funding or 0.0,
           "r": round(r, 2) if r is not None else None, "note": a.note}
    path = getattr(a, "path", None)
    if path is None:                   # путь цены от сканера (runtime/trade_paths.json); нет данных -> поля нет
        path = tradepath.finalize(t, a.exit, tradepath.parse_ts(rec["ts_close"]), paths.runtime(tradepath.STATE_NAME))
    if path:
        rec["path"] = path
    rec.update(_post_fields(a))
    _append(rec)
    print("closed %s pnl=%.4f R=%s" % (a.id, pnl, "n/a" if r is None else "%.2f" % r))


def _summ(rows):
    n = len(rows)
    w = [x for x in rows if x["pnl_usdt"] > 0]
    pnl = sum(x["pnl_usdt"] for x in rows)
    rs = [x["r"] for x in rows if x.get("r") is not None]
    gl = -sum(x["pnl_usdt"] for x in rows if x["pnl_usdt"] <= 0)
    pf = "inf" if gl <= 0 and w else ("n/a" if not rows else "%.2f" % (sum(x["pnl_usdt"] for x in w) / gl))
    ravg = sum(rs) / len(rs) if rs else None
    se = (math.sqrt(sum((r - ravg) ** 2 for r in rs) / (len(rs) - 1)) / math.sqrt(len(rs))) if len(rs) > 1 else None
    return "n=%d win=%.0f%% pnl=%+.3f USDT fees=%.3f PF=%s avgR=%s%s" % (
        n, 100.0 * len(w) / n if n else 0, pnl, sum(x["fees_usdt"] for x in rows), pf,
        "%.2f" % ravg if rs else "n/a", " (±%.2f ст.ош.)" % se if se is not None else "")


def cmd_review(a):
    trades, order = _load()
    rows = [trades[i] for i in order if trades[i].get("ts_close") and (not a.mode or trades[i]["mode"] == a.mode)]
    openl = [trades[i] for i in order if not trades[i].get("ts_close") and (not a.mode or trades[i]["mode"] == a.mode)]
    print("Закрытых: %d, открытых: %d" % (len(rows), len(openl)))
    if rows:
        print("ВСЕГО:", _summ(rows))
        for key in ("sym", "side", "mode", "stype", "venue", "regime", "reason", "exp", "var", "setup"):
            if key in ("exp", "var", "setup") and not any(x.get(key) for x in rows):
                continue
            groups = {}
            for x in rows:
                groups.setdefault(str(x.get(key)), []).append(x)
            for g, v in sorted(groups.items()):
                print("  %s=%s: %s" % (key, g, _summ(v)))
        tg = {}
        for x in rows:
            for tag in x.get("tags", []):
                tg.setdefault(tag, []).append(x)
        for g, v in sorted(tg.items()):
            print("  tag=%s: %s" % (g, _summ(v)))
        eg = {}
        for x in rows:
            for tag in x.get("err") or []:
                eg.setdefault(tag, []).append(x)
        for g, v in sorted(eg.items()):
            print("  err=%s: %s" % (g, _summ(v)))
        print("Худшие сделки:")
        for x in sorted(rows, key=lambda x: x["pnl_usdt"])[:3]:
            print("  %s %+.3f | %s | %s" % (x["id"], x["pnl_usdt"], x.get("reason"), (x.get("thesis") or "")[:80]))
    for x in openl:
        print("ОТКРЫТА %s %s %s вход %s lev %s sl %s tp %s [%s]" % (
            x["id"], x["sym"], x["side"], x["entry"], x["lev"], x["sl"], x["tp"], x["mode"]))


# ---------------------------------------------------------------- разбор ошибок (analyze)
MIN_BUCKET = 10
RANGE_BUCKETS = (("<0.5%", None, 0.5), ("0.5-1%", 0.5, 1.0), ("1-2%", 1.0, 2.0), (">=2%", 2.0, None))      # ctx.range_5m_pct
SPREAD_BUCKETS = (("<2bps", None, 2.0), ("2-5bps", 2.0, 5.0), (">=5bps", 5.0, None))                         # ctx.spread_bps


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _stats(rows):
    """Метрики набора закрытых сделок (словарь для analyze)."""
    pnl = [x["pnl_usdt"] for x in rows]
    wins = [p for p in pnl if p > 0]
    gl = -sum(p for p in pnl if p <= 0)
    rs = [x["r"] for x in rows if x.get("r") is not None]
    fees = sum(x.get("fees_usdt") or 0.0 for x in rows)
    abs_pnl = sum(abs(p) for p in pnl)
    return {"n": len(rows), "wins": len(wins), "winrate": len(wins) / len(rows) if rows else None, "pnl": sum(pnl), "avg_r": _avg(rs),
            "pf": (sum(wins) / gl if gl > 0 else ("inf" if wins else None)), "fees": fees, "fees_share_abs_pnl": fees / abs_pnl if abs_pnl > 0 else None}


def _bucket(rows, key, edges, min_n):
    out = []
    for name, lo, hi in edges:
        sub = [x for x in rows if isinstance((x.get("ctx") or {}).get(key), (int, float))
               and (lo is None or x["ctx"][key] >= lo) and (hi is None or x["ctx"][key] < hi)]
        row = {"bucket": name, "n": len(sub)}
        if len(sub) >= min_n:
            st = _stats(sub)
            row.update({"winrate": st["winrate"], "avg_r": st["avg_r"], "pnl": st["pnl"]})
        else:
            row["note"] = "мало данных"
        out.append(row)
    return out


BY_KEYS = ("exp", "var", "setup", "plan_id", "hyp", "sym", "side", "stype", "venue", "reason", "regime", "mode", "tag", "blackout")


def _group_value(x, by):
    if by == "tag":
        return None
    if by == "blackout":       # вход в макро-окне (paper, blackout_mode=warn) отдельно от обычных сделок
        return "blackout" if "blackout" in (x.get("tags") or []) else "обычные"
    v = x.get(by)
    if v in (None, ""):
        for tg in x.get("tags") or []:      # exp/var/setup/plan_id могут быть только в тегах
            pref = {"plan_id": "plan"}.get(by, by) + ":"
            if isinstance(tg, str) and tg.startswith(pref):
                return tg[len(pref):]
    return v


def analyze_by(rows, by, min_bucket=MIN_BUCKET):
    """Сводка закрытых сделок по ключу by (BY_KEYS): [{value, n, ...метрики}]; при n < min_bucket только счётчик и note."""
    if by not in BY_KEYS:
        raise ValueError("--by: %s" % "|".join(BY_KEYS))
    groups = {}
    for x in rows:
        if by == "tag":
            vals = [str(t) for t in (x.get("tags") or [])] or ["нет"]
        else:
            v = _group_value(x, by)
            vals = [str(v) if v not in (None, "") else "нет"]
        for v in vals:
            groups.setdefault(v, []).append(x)
    out = []
    for v, sub in sorted(groups.items()):
        row = {"value": v, "n": len(sub)}
        st = _stats(sub)
        if len(sub) >= min_bucket:
            row.update({"winrate": st["winrate"], "avg_r": st["avg_r"], "pnl": st["pnl"], "pf": st["pf"], "fees": st["fees"]})
        else:
            row.update({"note": "мало данных", "pnl": st["pnl"], "avg_r": st["avg_r"]})
        out.append(row)
    return out


def analyze(rows, min_bucket=MIN_BUCKET, by=None):
    """rows: закрытые сделки (dict из _load) -> структура отчёта (см. format_analyze). by: ключ группировки (BY_KEYS), результат в out["by"]."""
    paths_ = [x for x in rows if isinstance(x.get("path"), dict)]
    out = {"n": len(rows), "summary": _stats(rows) if rows else None, "min_bucket": min_bucket}
    out["trades"] = [{"id": x["id"], "sym": x.get("sym"), "side": x.get("side"), "r": x.get("r"), "reason": x.get("reason"), "fees": x.get("fees_usdt"),
                      "mae_r": (x.get("path") or {}).get("mae_r"), "mfe_r": (x.get("path") or {}).get("mfe_r"),
                      "mae_pct": (x.get("path") or {}).get("mae_pct"), "mfe_pct": (x.get("path") or {}).get("mfe_pct"),
                      "coverage": (x.get("path") or {}).get("path_coverage"), "err": x.get("err") or [], "rule_followed": x.get("rule_followed"),
                      "lesson": x.get("lesson")} for x in rows]
    tags = {}
    for x in rows:
        for t in x.get("err") or []:
            tags[t] = tags.get(t, 0) + 1
    out["err_tags"] = tags
    out["untagged"] = sum(1 for x in rows if not x.get("err"))
    rf = {}
    for x in rows:
        k = x.get("rule_followed") or "нет данных"
        rf[k] = rf.get(k, 0) + 1
    out["rule_followed"] = rf
    mm = {}
    for label, sub in (("win", [x for x in paths_ if x["pnl_usdt"] > 0]), ("loss", [x for x in paths_ if x["pnl_usdt"] <= 0])):
        mm[label] = {"n": len(sub), "mae_r": _avg([x["path"].get("mae_r") for x in sub]), "mfe_r": _avg([x["path"].get("mfe_r") for x in sub]),
                     "mae_pct": _avg([x["path"].get("mae_pct") for x in sub]), "mfe_pct": _avg([x["path"].get("mfe_pct") for x in sub])}
    out["mae_mfe"] = mm
    out["with_path"], out["with_ctx"] = len(paths_), sum(1 for x in rows if x.get("ctx"))
    out["ctx_range_5m"] = _bucket(rows, "range_5m_pct", RANGE_BUCKETS, min_bucket)
    out["ctx_spread"] = _bucket(rows, "spread_bps", SPREAD_BUCKETS, min_bucket)
    if by:
        out["by"] = {"key": by, "groups": analyze_by(rows, by, min_bucket)}
    return out


def _f(x, spec="%.2f", dash="-"):
    return dash if x is None else spec % x


def format_analyze(rep):
    L = []
    n = rep["n"]
    L.append("ВНИМАНИЕ: выборка мала (n=%d). Пока n<30 это разбор отдельных сделок, а не статистика; бакеты ctx считаются только при n>=%d." % (n, rep["min_bucket"]))
    if not n:
        L.append("Закрытых сделок нет.")
        return "\n".join(L)
    hdr = "%-26s %-8s %-5s %6s %-7s %7s %6s %6s %-24s %-8s" % ("id", "монета", "сторона", "R", "выход", "комис.", "MAE_R", "MFE_R", "ошибки", "правило")
    L += [hdr, "-" * len(hdr)]
    for t in rep["trades"]:
        L.append("%-26s %-8s %-5s %6s %-7s %7s %6s %6s %-24s %-8s" % (
            t["id"][:26], str(t["sym"])[:8], t["side"], _f(t["r"]), str(t["reason"])[:7], _f(t["fees"], "%.3f"), _f(t["mae_r"]), _f(t["mfe_r"]),
            ",".join(t["err"])[:24] or "-", t["rule_followed"] or "-"))
    s = rep["summary"]
    pf = s["pf"] if isinstance(s["pf"], str) else _f(s["pf"])
    L.append("")
    L.append("СВОДКА: n=%d винрейт=%s средний R=%s PF=%s PnL=%+.3f USDT" % (n, "%.0f%%" % (100 * s["winrate"]), _f(s["avg_r"]), pf, s["pnl"]))
    L.append("Комиссии: %.3f USDT, доля от суммы |PnL|: %s" % (s["fees"], "%.0f%%" % (100 * s["fees_share_abs_pnl"]) if s["fees_share_abs_pnl"] is not None else "-"))
    L.append("Теги ошибок (сделок с разбором %d из %d): %s" % (n - rep["untagged"], n, ", ".join("%s=%d" % kv for kv in sorted(rep["err_tags"].items(), key=lambda kv: -kv[1])) or "нет"))
    L.append("rule_followed: %s" % ", ".join("%s=%d" % kv for kv in sorted(rep["rule_followed"].items())))
    L.append("Путь цены есть у %d из %d сделок (остальные закрыты до внедрения или сканер не работал), ctx входа у %d." % (rep["with_path"], n, rep["with_ctx"]))
    for label, name in (("win", "выигрышные"), ("loss", "проигрышные")):
        m = rep["mae_mfe"][label]
        L.append("  %s (n=%d): MAE %s R / %s%%, MFE %s R / %s%%" % (name, m["n"], _f(m["mae_r"]), _f(m["mae_pct"]), _f(m["mfe_r"]), _f(m["mfe_pct"])))
    for key, title in (("ctx_range_5m", "ctx: диапазон 5м на входе"), ("ctx_spread", "ctx: спред на входе")):
        L.append(title + ":")
        for b in rep[key]:
            if "note" in b:
                L.append("  %-8s n=%d: мало данных (нужно n>=%d)" % (b["bucket"], b["n"], rep["min_bucket"]))
            else:
                L.append("  %-8s n=%d винрейт=%.0f%% средний R=%s PnL=%+.3f" % (b["bucket"], b["n"], 100 * b["winrate"], _f(b["avg_r"]), b["pnl"]))
    if rep.get("by"):
        L.append("по %s (при n<%d только сумма и средний R, не статистика):" % (rep["by"]["key"], rep["min_bucket"]))
        for g in rep["by"]["groups"]:
            wr = "%.0f%%" % (100 * g["winrate"]) if g.get("winrate") is not None else "-"
            L.append("  %-24s n=%d винрейт=%s средний R=%s PnL=%+.3f%s" % (g["value"][:24], g["n"], wr, _f(g["avg_r"]), g["pnl"], "  (мало данных)" if "note" in g else ""))
    return "\n".join(L)


def cmd_analyze(a):
    trades, order = _load()
    mode = getattr(a, "mode", None) or "live"
    rows = [trades[i] for i in order if trades[i].get("ts_close") and (mode == "all" or trades[i].get("mode") == mode) and trades[i].get("pnl_usdt") is not None]
    rep = analyze(rows, getattr(a, "min_bucket", MIN_BUCKET), by=getattr(a, "by", None))
    print(json.dumps(rep, ensure_ascii=False, indent=1) if getattr(a, "json", False) else format_analyze(rep))


def _err_arg(s):
    try:
        return tradectx.parse_err(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def add_post_args(p):
    """--err/--rule-followed/--lesson для close и post (общая часть; используется и в venues/bybit.py)."""
    p.add_argument("--err", type=_err_arg, default=None, help="теги ошибок через запятую: " + ", ".join(tradectx.ERR_TAGS))
    p.add_argument("--rule-followed", choices=list(tradectx.RULE_VALUES), default=None, help="соблюдены ли правила: yes|no|partial")
    p.add_argument("--lesson", default=None, help="короткий вывод одной фразой")


def main(argv):
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    o = sp.add_parser("open")
    o.add_argument("--sym", required=True)
    o.add_argument("--side", required=True, choices=["long", "short"])
    o.add_argument("--entry", type=float, required=True)
    o.add_argument("--lev", type=float, required=True)
    o.add_argument("--margin", type=float, required=True)
    o.add_argument("--sl", type=float)
    o.add_argument("--tp", type=float)
    o.add_argument("--thesis", required=True)
    o.add_argument("--news", default="")
    o.add_argument("--regime", default="unknown")
    o.add_argument("--tags", default="")
    o.add_argument("--mode", choices=["live", "paper"], default="live")
    o.add_argument("--id")
    o.add_argument("--session")
    o.add_argument("--stype")
    o.add_argument("--venue", default="platform")
    c = sp.add_parser("close")
    c.add_argument("--id", required=True)
    c.add_argument("--exit", type=float, required=True)
    c.add_argument("--reason", required=True)
    c.add_argument("--funding", type=float)
    c.add_argument("--fee", type=float, help="комиссия на сторону (доля), по умолчанию 0.0009")
    c.add_argument("--note", default="")
    add_post_args(c)
    po = sp.add_parser("post", help="разбор закрытой сделки позже (теги ошибок, соблюдение правил, урок)")
    po.add_argument("id")
    add_post_args(po)
    an = sp.add_parser("analyze", help="разбор ошибок: таблица сделок, сводка, теги, MAE/MFE, бакеты ctx")
    an.add_argument("--mode", choices=["live", "paper", "testnet", "all"], default="live")
    an.add_argument("--min-bucket", type=int, default=MIN_BUCKET)
    an.add_argument("--json", action="store_true")
    an.add_argument("--by", choices=list(BY_KEYS), default=None, help="сводка по группам: exp|var|setup|plan_id|...")
    ad = sp.add_parser("add", help="добор части позиции плана (событие add)")
    ad.add_argument("--id", required=True)
    ad.add_argument("--px", type=float, required=True)
    ad.add_argument("--qty", type=float, required=True)
    ad.add_argument("--fee", type=float, help="комиссия в USDT")
    pt = sp.add_parser("part", help="частичное закрытие (событие part)")
    pt.add_argument("--id", required=True)
    pt.add_argument("--px", type=float, required=True)
    pt.add_argument("--qty", type=float, required=True)
    pt.add_argument("--fee", type=float, help="комиссия в USDT")
    pt.add_argument("--pnl", type=float, help="результат части по цене без комиссий; по умолчанию от текущей средней")
    aj = sp.add_parser("adjust", help="смена стопа/тейка (событие adjust)")
    aj.add_argument("--id", required=True)
    aj.add_argument("--sl", type=float)
    aj.add_argument("--tp", type=float)
    aj.add_argument("--by", default=None)
    sp.add_parser("list")
    r = sp.add_parser("review")
    r.add_argument("--mode", choices=["live", "paper", "testnet"])
    a = ap.parse_args(argv)
    {"open": cmd_open, "close": cmd_close, "post": cmd_post, "analyze": cmd_analyze, "add": cmd_add, "part": cmd_part, "adjust": cmd_adjust, "list": cmd_review, "review": cmd_review}[a.cmd](
        a if a.cmd != "list" else argparse.Namespace(mode=None))


if __name__ == "__main__":
    main(sys.argv[1:])
