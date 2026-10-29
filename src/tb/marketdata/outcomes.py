"""Авто-статистика пропущенных сигналов: форвард-доходность от цены события сканера (и от цены записи идеи агента). Только чтение, без сети.

hotscan уже опрашивает ВСЕ USDT-тикеры раз в interval секунд; трекер берёт из этого же ответа цену монеты через +15 и +60 минут (по умолчанию; можно +240)
после события и дописывает строку в state/hot_outcomes.jsonl. Лишних запросов нет. Что трекается:
  - каждое эмитированное событие сканера (range_5m, turnover_surge, hot_enter), в т. ч. тихие (score ниже --wake-score): kind="event" (в дашборде статус auto_skipped);
  - каждая идея агента из state/ideas.jsonl (./tb ideas add): kind="idea", цена записи из идеи (нет цены: первая цена сканера, если идея не старше 2 минут).
Строка исхода: {"ts","ts_epoch","id","kind","type","sym","dir","h","p0","p1","ret_pct","dir_pct","cost_pct","late_s","ts0_epoch","range_5m_pct","turn_surge_5m","score","side","tag"}.
  ret_pct  = (p1/p0-1)*100 без знака направления; dir_pct = ret_pct * (+1 для up/long, -1 для down/short); для watch/без направления dir_pct=null.
  Продолжение (follow) = dir_pct, откат (fade) = -dir_pct; издержки круга cost_pct (taker вход+выход с проскальзыванием, config/risk_profile.json:costs.bybit)
  вычитаются в статистике (net = dir_pct - cost_pct). Вход по цене события оптимистичен: реальный вход позже и хуже.
Устойчивость к рестарту: очередь незакрытых горизонтов лежит в runtime/hot_pending.json (атомарно). После перезапуска доезжают те горизонты, до срока
которых ещё есть замер; опоздавший больше grace = max(3*interval, 120 с) горизонт отбрасывается без записи (честнее, чем подставлять чужую цену).
"""
import json
import os

from tb import fsutil
from tb.core import ideas

DEFAULT_HORIZONS = (15, 60)
SEEN_CAP = 500
IDEA_PRICE_MAX_AGE_S = 120.0


class OutcomeTracker:
    def __init__(self, out_path, pending_path, ideas_path=None, horizons=DEFAULT_HORIZONS, interval=30.0, cost_pct=0.24, env=None):
        self.out_path, self.pending_path, self.ideas_path = out_path, pending_path, ideas_path
        self.horizons = sorted({int(h) for h in horizons if int(h) > 0}) or list(DEFAULT_HORIZONS)
        self.grace = max(3.0 * interval, 120.0)
        self.cost_pct = cost_pct
        self.env = env
        self.items, self.seen = {}, []
        self._stamp = None
        self._load()

    # --- очередь
    def _load(self):
        try:
            with open(self.pending_path, encoding="utf-8") as f:
                d = json.load(f)
            items = d.get("items") or {}
            self.items = {k: v for k, v in items.items() if isinstance(v, dict) and v.get("sym") and v.get("p0") and v.get("h_left")}
            self.seen = [x for x in (d.get("seen_ideas") or []) if isinstance(x, str)][-SEEN_CAP:]
        except (OSError, ValueError, AttributeError):
            self.items, self.seen = {}, []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.pending_path), exist_ok=True)
            tmp = self.pending_path + ".tmp.%d" % os.getpid()
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"items": self.items, "seen_ideas": self.seen[-SEEN_CAP:]}, f, ensure_ascii=False)
            fsutil.replace(tmp, self.pending_path)
        except OSError:
            pass

    # --- вход
    def add_event(self, ev, now):
        """ev: событие hotscan с id, sym, price, direction."""
        if not ev.get("id") or not ev.get("price") or not ev.get("sym"):
            return
        self.items[ev["id"]] = {"id": ev["id"], "kind": "event", "type": ev.get("type"), "sym": ev["sym"], "ts0": now, "p0": ev["price"],
                                "dir": ev.get("direction"), "h_left": list(self.horizons), "range_5m_pct": ev.get("range_5m_pct"),
                                "turn_surge_5m": ev.get("turn_surge_5m"), "score": ev.get("score")}
        self._save()

    def _ingest_ideas(self, tickers, now):
        if not self.ideas_path:
            return False
        try:
            st = os.stat(self.ideas_path)
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            return False
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        changed = False
        for r in ideas.read_rows(self.ideas_path):
            iid = r.get("id")
            if r.get("ev") != "idea" or not iid or iid in self.seen:
                continue
            self.seen.append(iid)
            changed = True
            sym, ts0 = r.get("sym"), ideas.num(r.get("ts_epoch"))
            p0 = ideas.num(r.get("price"))
            if p0 is None and ts0 is not None and now - ts0 <= IDEA_PRICE_MAX_AGE_S:
                p0 = (tickers.get((sym or "") + "USDT") or {}).get("price")
            if not sym or not p0 or ts0 is None:
                continue
            side = r.get("side")
            self.items["idea:" + iid] = {"id": iid, "kind": "idea", "type": "idea", "sym": sym, "ts0": ts0, "p0": p0,
                                         "dir": {"long": "up", "short": "down"}.get(side), "side": side, "tag": r.get("tag"), "h_left": list(self.horizons)}
        return changed

    # --- замеры
    def update(self, tickers, now):
        """tickers: {SYMUSDT: {"price": ..}} из того же ответа, что и сканер. Дописывает созревшие горизонты."""
        changed = self._ingest_ideas(tickers, now)
        done = []
        for key, it in self.items.items():
            left = []
            for h in it["h_left"]:
                due = it["ts0"] + h * 60.0
                if now < due:
                    left.append(h)
                    continue
                px = (tickers.get(it["sym"] + "USDT") or {}).get("price")
                late = now - due
                if late > self.grace:
                    changed = True                # пропущено (перерыв сканера): не подставляем чужую цену
                    continue
                if not px:
                    left.append(h)               # нет цены в этом скане: ждём следующий, пока не истёк grace
                    continue
                changed = True
                self._write(it, h, px, late, now)
            it["h_left"] = left
            if not left:
                done.append(key)
        for k in done:
            del self.items[k]
        if changed or done:
            self._save()

    def _write(self, it, h, px, late, now):
        ret = 100.0 * (px / it["p0"] - 1.0)
        sign = {"up": 1.0, "down": -1.0}.get(it.get("dir"))
        rec = {"ts": ideas.iso(now), "ts_epoch": round(now, 1), "id": it["id"], "kind": it["kind"], "type": it.get("type"), "sym": it["sym"],
               "dir": it.get("dir"), "h": h, "p0": it["p0"], "p1": px, "ret_pct": round(ret, 4),
               "dir_pct": None if sign is None else round(ret * sign, 4), "cost_pct": self.cost_pct, "late_s": round(late, 1), "ts0_epoch": round(it["ts0"], 1)}
        for k in ("range_5m_pct", "turn_surge_5m", "score", "side", "tag"):
            if it.get(k) is not None:
                rec[k] = it[k]
        try:
            ideas.append(self.out_path, rec)
        except OSError:
            pass
