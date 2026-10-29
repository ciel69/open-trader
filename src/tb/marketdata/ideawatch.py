"""Будильник по идеям агента: hotscan на каждом скане сверяет цены с уровнями активных идей. Только чтение тикеров, которые сканер уже получил (лишних запросов нет).

Берутся идеи state/ideas.jsonl со статусом idea/watching и заданным level (без level идея только заметка и игнорируется; дешёвое чтение, перечитывается только при смене mtime/size файла).
События (в stdout, будят агента вне зависимости от --wake-score и без общего лимита max-per-hour; свой антиспам на идею), в runtime/hot_events.jsonl с тем же type:
  idea_near         цена вошла в зону уровня: |цена/level-1| <= zone_pct (0.3%, --idea-zone-pct). Раз на идею до выхода из зоны (выход = дальше 1.5*zone_pct) и не чаще cooldown (1800 с).
  idea_level        цена пересекла level с той стороны, откуда ждали. Сторона подхода определяется по цене записи идеи: цена записи выше level -> ждём откат ВНИЗ (цена <= level),
                    ниже level -> ждём подъём (цена >= level). Нет цены записи (или она равна level): по side (long вниз, short вверх, watch вниз); если цены нет и это watch,
                    берётся первая цена сканера. Один раз на идею (запоминается в runtime/idea_watch.json, переживает рестарт сканера).
  idea_invalidated  цена пересекла stop (сторона stop относительно level) БЕЗ касания level: событие и идея сразу помечается invalidated.
  idea_tp           цена достигла tp (сторона tp относительно level), а идея всё ещё не взята: «ушла без нас», идея помечается expired с note.
Срок жизни: ttl_h идеи (поле ttl_h записи, `ideas add --ttl`, по умолчанию 24 ч, 0 = без срока) от её ts; по истечении идея молча (без события) получает expired. Проверяется и у идей без level.
Пометка статуса: ideas.mark_if_active (свежее чтение файла, одна строка одним write; взятую/уже помеченную агентом идею не трогает).
Формат строки stdout: {"type","wake":true,"id":"I7","sym":"DOOD","side","level","price","dist_pct":(цена/level-1)*100 со знаком,"stop","tp","zone_pct","idea_note","ts","env"}.
События НЕ идут в трекер исходов (outcomes.py): статистика пропущенных сканера (kind=event) их не видит; идея сама трекается как kind=idea с момента записи.
"""
import json
import os

from tb.core import ideas

ZONE_PCT = 0.3
COOLDOWN_S = 1800.0
EXIT_K = 1.5                    # гистерезис зоны: выход из неё только при |dist| > EXIT_K*zone
NOTE_MAX = 80


class IdeaWatcher:
    def __init__(self, ideas_path, state_path, zone_pct=ZONE_PCT, cooldown=COOLDOWN_S, default_ttl_h=ideas.TTL_H_DEFAULT):
        self.path, self.state_path = ideas_path, state_path
        self.zone, self.cooldown, self.default_ttl_h = float(zone_pct), float(cooldown), float(default_ttl_h)
        self._stamp, self._ideas = None, []
        self.st = self._load_state()

    # --- состояние
    def _load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
            return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp.%d" % os.getpid()
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.st, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # --- чтение идей (кэш по mtime/size)
    def _active(self):
        try:
            s = os.stat(self.path)
            stamp = (s.st_mtime, s.st_size)
        except OSError:
            self._stamp, self._ideas = None, []
            return self._ideas
        if stamp != self._stamp:
            self._stamp = stamp
            self._ideas = [i for i in ideas.load_ideas(path=self.path) if (i.get("status") or "idea") in ideas.ACTIVE]
        return self._ideas

    # --- проверка
    def _event(self, kind, i, price, dist, level):
        return {"type": kind, "wake": True, "id": i["id"], "sym": i["sym"], "side": i.get("side"), "level": level, "price": price,
                "dist_pct": round(dist, 3), "stop": ideas.num(i.get("stop")), "tp": ideas.num(i.get("tp")), "zone_pct": self.zone,
                "idea_note": (i.get("note") or "")[:NOTE_MAX]}

    def check(self, tickers, now):
        """tickers: {SYMUSDT: {"price"}} из ответа сканера. -> список событий (dict без ts/env); побочно ставит статусы expired/invalidated."""
        out, dirty = [], False
        alive = set()
        for i in list(self._active()):
            iid = i["id"]
            ttl = ideas.num(i.get("ttl_h"))
            ttl = self.default_ttl_h if ttl is None else ttl
            ts0 = ideas.num(i.get("ts_epoch"))
            if ttl > 0 and ts0 is not None and now >= ts0 + ttl * 3600.0:
                ideas.mark_if_active(iid, "expired", "ttl %g ч истёк" % ttl, path=self.path, now=now)
                continue
            level = ideas.num(i.get("level"))
            t = tickers.get((i.get("sym") or "") + "USDT")
            price = ideas.num((t or {}).get("price"))
            if not level or level <= 0 or not price:
                continue
            alive.add(iid)
            st = self.st.setdefault(iid, {})
            if "s0" not in st:
                p0 = ideas.num(i.get("price"))
                ref = p0 if p0 and p0 != level else price
                st["s0"] = (1 if ref > level else -1) if ref != level else (-1 if i.get("side") == "short" else 1)
                dirty = True
            dist = 100.0 * (price / level - 1.0)
            stop, tp = ideas.num(i.get("stop")), ideas.num(i.get("tp"))
            if stop and stop != level and not st.get("hit") and (price - stop) * (1 if stop > level else -1) >= 0:
                if ideas.mark_if_active(iid, "invalidated", "цена %g пересекла stop %g без касания уровня %g" % (price, stop, level), path=self.path, now=now):
                    out.append(self._event("idea_invalidated", i, price, dist, level))
                self.st.pop(iid, None)
                alive.discard(iid)
                dirty = True
                continue
            if tp and tp != level and (price - tp) * (1 if tp > level else -1) >= 0:
                if ideas.mark_if_active(iid, "expired", "tp %g достигнут без входа (цена %g)" % (tp, price), path=self.path, now=now):
                    out.append(self._event("idea_tp", i, price, dist, level))
                self.st.pop(iid, None)
                alive.discard(iid)
                dirty = True
                continue
            if st.get("hit"):
                continue
            if (price - level) * st["s0"] <= 0:
                st["hit"], st["in_zone"] = True, True
                out.append(self._event("idea_level", i, price, dist, level))
                dirty = True
                continue
            if abs(dist) <= self.zone:
                if not st.get("in_zone"):
                    st["in_zone"] = True
                    dirty = True
                    if now - st.get("near_ts", -1e18) >= self.cooldown:
                        st["near_ts"] = now
                        out.append(self._event("idea_near", i, price, dist, level))
            elif st.get("in_zone") and abs(dist) > self.zone * EXIT_K:
                st["in_zone"] = False
                dirty = True
        for k in [k for k in self.st if k not in alive]:          # идея ушла из активных (взята, помечена, ttl)
            del self.st[k]
            dirty = True
        if dirty:
            self._save_state()
        return out
