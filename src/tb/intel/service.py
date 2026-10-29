"""Сервис intel: `python -m tb.intel.service run [--max-seconds N]` (запускает супервизор: `./tb up --services exec,scan,dispatch,dash,intel`). Владелец: WP13.

Цикл (tick_s): источники, у которых подошёл срок, загружаются пулом <= workers потоков (fetch.Fetcher: таймаут, ETag, backoff, вежливость к доменам),
текстовые элементы очищаются и пишутся в items.jsonl (дедуп), числа остаются в кэше; раз в cal_s пересчитываются события календаря и
`runtime/intel/blackouts.json`, в шину уходят предупреждения `macro_blackout_soon`; раз в publish_s (или при изменении) пишется `facts.json`.
Heartbeat: `runtime/platform/intel.hb.json` каждые hb_s. Сеть: только через fetch.http_get_text. Ни один источник не роняет сервис: сбой = пометка
degraded в facts. Ключей не требует; ничего не торгует и не читает ключи.
"""
import argparse
import concurrent.futures
import json
import os
import signal
import sys
import threading
import time

from tb import paths
from tb.core import filelock
from tb.intel import calendar as ical
from tb.intel import fetch as ifetch
from tb.intel import flash as iflash
from tb.intel import items as iitems
from tb.intel import reader as ireader
from tb.intel import sources as src_mod
from tb.intel import store


class Service(object):
    def __init__(self, root, cfg=None, transport=None, clock=time.time, emit=None, inline=False, log=None, reader_call=None):
        self.root = root
        self.cfg = cfg if cfg is not None else store.load_cfg(root)
        self.clock = clock
        self.fetcher = ifetch.Fetcher(root, self.cfg, transport, clock)
        self.items = iitems.ItemStore(root)
        self.emit = emit or self._emit_bus
        self.inline = inline or int(self.cfg.get("workers", 4)) <= 1
        self.log = log or (lambda m: print(m, flush=True))
        self.symbols = []
        self.sources = []
        self._t = {"symbols": -1e18, "cal": -1e18, "publish": -1e18, "hb": -1e18, "prune": -1e18}
        self._last_sig = None
        self._err = {}                         # id -> последний код ошибки (лог только при смене)
        self.stats = {"fetched": 0, "failed": 0, "deferred": 0, "items_new": 0, "items_dup": 0, "published": 0, "alerts": 0}
        self._pool = None if self.inline else concurrent.futures.ThreadPoolExecutor(max_workers=int(self.cfg.get("workers", 4)))
        self._fired = ical.load_fired(root)
        self._events = []
        self.reader = ireader.Reader(root, self.cfg, call=reader_call, clock=clock, log=self.log) if ireader.reader_cfg(self.cfg)["enabled"] else None
        self._reader_thread = None
        self.flash = iflash.Flash(root, self.cfg, clock, self._emit_flash, self.log)         # быстрый канал: правила по заголовкам без LLM -> news_flash

    # ---- карантинный читатель (WP13b): LLM-вызов не должен задерживать загрузку источников, поэтому отдельный поток, один за раз
    def reader_step(self, now):
        if self.reader is None:
            return
        if self.inline:
            self.reader.step(now)
            return
        if self._reader_thread is not None and self._reader_thread.is_alive():
            return
        self._reader_thread = threading.Thread(target=self._reader_run, args=(now,), daemon=True)
        self._reader_thread.start()

    def _reader_run(self, now):
        try:
            self.reader.step(now)
        except Exception as e:  # noqa: BLE001
            self.log("intel: сбой читателя (%s)" % type(e).__name__)

    def close(self):
        if self._pool is not None:
            self._pool.shutdown(wait=False)

    # ---- шина ----
    def _emit_bus(self, type_, d, now, sym=None):
        from tb.core import bus
        return bus.emit(type_, d, sym=sym, root=self.root, now=now)

    def _emit_flash(self, type_, d, now, sym):
        return self.emit(type_, d, now, sym) if self.emit is self._emit_bus else self.emit(type_, d, now)

    # ---- источники ----
    def refresh_symbols(self, now, force=False):
        if force or now - self._t["symbols"] >= float(self.cfg.get("symbols_s", 120.0)):
            self._t["symbols"] = now
            self.symbols = store.symbols_now(self.root, self.cfg)
            self.sources = src_mod.expand(self.cfg, self.symbols)

    def _one(self, src, now, force):
        ctx = {"symbols": self.symbols, "symbol": src.get("symbol")}
        return self.fetcher.fetch(src, ctx, now, force=force)

    def fetch_due(self, now, force=False, only=None):
        sel = [s for s in self.sources if s.get("enabled", True) and (not only or s["id"] in only or s.get("base") in only)]
        if not force:
            sel = [s for s in sel if self.fetcher.due(s, now)]
        if not sel:
            return []
        if self._pool is None:
            outs = [self._one(s, now, force) for s in sel]
        else:
            futs = [self._pool.submit(self._one, s, now, force) for s in sel]
            outs = [f.result() for f in futs]
        updated = []
        for s, o in zip(sel, outs):
            self._after(s, o, now)
            if o.ok:
                updated.append(s["id"])
        return updated

    def _after(self, src, o, now):
        if o.deferred:
            self.stats["deferred"] += 1
            return
        if o.ok:
            self.stats["fetched"] += 1
            if self._err.pop(src["id"], None):
                self.log("intel: %s восстановлен" % src["id"])
            if o.items:
                self.add_items(src, o.items, now)
        else:
            self.stats["failed"] += 1
            if self._err.get(src["id"]) != o.error:
                self._err[src["id"]] = o.error
                self.log("intel: %s недоступен (%s)" % (src["id"], o.error))

    def add_items(self, src, raw_items, now):
        made = []
        for r in raw_items:
            if not isinstance(r, dict):
                continue
            it = iitems.make_item(src["group"], src["group"], r.get("title"), r.get("snip"), r.get("url"), r.get("ts"), now)
            if it and it["ts"] >= now - self.items.retention_days * 86400:
                made.append(it)
        try:
            res = self.items.add_many(made, now)
        except (OSError, TimeoutError) as e:
            self.log("intel: items не записаны (%s)" % type(e).__name__)
            return
        self.stats["items_new"] += res["new"]
        self.stats["items_dup"] += res["dup"]
        if res["new"]:
            try:
                self.stats["flashes"] = self.stats.get("flashes", 0) + len(self.flash.process(list(getattr(self.items, "last_fresh", [])), now))
            except Exception as e:  # noqa: BLE001 - быстрый канал не должен ронять загрузку источников
                self.log("intel: сбой быстрого канала (%s)" % type(e).__name__)

    # ---- календарь ----
    def calendar_step(self, now, force=False):
        if not force and now - self._t["cal"] < float(self.cfg.get("cal_s", 60.0)):
            return
        self._t["cal"] = now
        ccfg = self.cfg.get("calendar") or {}
        imp = {s["id"]: bool(s.get("verified")) for s in self.sources if s["id"].startswith("cal_") and s.get("enabled", True)}
        events = ical.all_events(self.root, ccfg, imp)
        self._events = events
        doc = ical.build_blackouts(self.root, ccfg, events, now)
        path = ical.blackouts_path(self.root)
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            old = {}
        if old.get("windows") != doc["windows"] or not old:
            filelock.atomic_write_json(path, doc, indent=None)
        pend, self._fired = ical.due_alerts([e for e in events if e["v"] or ccfg.get("alert_unverified", True)], now, ccfg.get("warn_min") or [60], self._fired)
        for e, thr in pend:
            try:
                self.emit("macro_blackout_soon", {"event": e["id"], "kind": e["type"], "in_min": max(0, int((e["ts"] - now) // 60)), "impact": e["impact"],
                                                   "verified": e["v"]}, now)
                self.stats["alerts"] += 1
            except (ValueError, OSError, TimeoutError) as ex:
                self.log("intel: событие macro_blackout_soon не записано (%s)" % type(ex).__name__)
        if pend:
            ical.save_fired(self.root, self._fired)

    # ---- публикация ----
    def publish(self, now, changed=False, force=False):
        gap = now - self._t["publish"]
        if not force and gap < float(self.cfg.get("publish_s", 60.0)) and not (changed and gap >= 5.0):
            return False
        facts = store.build_facts(self.root, self.cfg, now, self.sources, self.symbols)
        sig = store.facts_signature(facts)
        try:
            store.write_facts(self.root, facts)
        except ValueError as e:
            self.log("intel: %s" % e)
            return False
        self._t["publish"] = now
        self._last_sig = sig
        self.stats["published"] += 1
        return True

    def heartbeat(self, now, force=False):
        if force or now - self._t["hb"] >= float(self.cfg.get("hb_s", 5.0)):
            self._t["hb"] = now
            from tb.platform import services as S
            S.write_hb("intel", {"sources": len(self.sources), "fetched": self.stats["fetched"], "failed": self.stats["failed"], "items_new": self.stats["items_new"],
                                 "alerts": self.stats["alerts"], "req_hour": self.fetcher.requests_last_hour(now)}, root=self.root, now=now)

    def tick(self, now=None, force=False, only=None):
        now = self.clock() if now is None else now
        self.refresh_symbols(now)
        try:
            updated = self.fetch_due(now, force, only)
        except Exception as e:  # noqa: BLE001
            self.log("intel: сбой цикла загрузки (%s)" % type(e).__name__)
            updated = []
        try:
            self.calendar_step(now, force)
        except Exception as e:  # noqa: BLE001
            self.log("intel: сбой календаря (%s)" % type(e).__name__)
        try:
            self.publish(now, changed=bool(updated), force=force)
        except Exception as e:  # noqa: BLE001
            self.log("intel: сбой публикации (%s)" % type(e).__name__)
        try:
            self.reader_step(now)
        except Exception as e:  # noqa: BLE001
            self.log("intel: сбой читателя (%s)" % type(e).__name__)
        try:
            self.flash.macro_step(self._events, now)      # реакция BTC на макро-релиз из календаря
        except Exception as e:  # noqa: BLE001
            self.log("intel: сбой макро-реакции (%s)" % type(e).__name__)
        try:
            self.flash.track(now)                         # исходы новостей через 1/5/15 минут
        except Exception as e:  # noqa: BLE001
            self.log("intel: сбой учёта исходов новостей (%s)" % type(e).__name__)
        if now - self._t["prune"] >= 3600:
            self._t["prune"] = now
            try:
                self.items.prune(now)
            except (OSError, TimeoutError):
                pass
        self.heartbeat(now)
        return updated


def run(root, cfg=None, transport=None, clock=time.time, sleep=time.sleep, stop=lambda: False, emit=None, max_seconds=0.0, inline=False, log=None):
    svc = Service(root, cfg, transport, clock, emit, inline, log)
    t_end = clock() + max_seconds if max_seconds else None
    svc.heartbeat(clock(), force=True)
    try:
        while not stop():
            svc.tick()
            if t_end is not None and clock() >= t_end:
                break
            sleep(float(svc.cfg.get("tick_s", 2.0)))
    finally:
        svc.heartbeat(clock(), force=True)
        svc.close()
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="tb.intel.service")
    ap.add_argument("cmd", choices=("run",))
    ap.add_argument("--max-seconds", type=float, default=0.0, dest="max_seconds")
    ap.add_argument("--root", default=None)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if a.root:
        os.environ["TB_ROOT"] = os.path.abspath(a.root)
    for st in (sys.stdout, sys.stderr):                      # UTF-8 на консоли (Windows), launcher не нужен: ключей здесь нет
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    root = paths.root()
    cfg = store.load_cfg(root)
    flag = {"stop": False}

    def _h(*_):
        flag["stop"] = True

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _h)
            except (ValueError, OSError):
                pass
    print("intel: старт, источников %d, публикация каждые %ss%s" % (len(src_mod.expand(cfg, store.symbols_now(root, cfg))), cfg.get("publish_s"),
                                                                   "; " + cfg["_warn"] if cfg.get("_warn") else ""), flush=True)
    return run(root, cfg, stop=lambda: flag["stop"], max_seconds=a.max_seconds)


if __name__ == "__main__":
    sys.exit(main())
