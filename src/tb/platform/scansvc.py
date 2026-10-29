"""Сканер как сервис платформы: `python -m tb.platform.scansvc run` (запускает супервизор). Владелец: WP11-B.

Оборачивает существующие hotscan (горячие монеты), ideawatch (будильник по идеям), outcomes и tradepath БЕЗ изменений их поведения и порогов
(пороги берутся из config/platform.json, раздел "scan", и совпадают с умолчаниями hotscan). Вместо печати в stdout для Monitor события уходят в
шину `core/bus` типом `scanner_signal` (kind: range_5m, turnover_surge, hot_enter, idea_near, idea_level, idea_invalidated, idea_tp, idea_expired):
  - источник событий тот же файл runtime/hot_events.jsonl, что пишет hotscan (хвост с конца файла, без истории при старте);
  - белый список полей d: kind, px, score, dir, range_5m_pct, ret_5m_pct, turn_surge_5m, spread_bps, funding_pct, idea, wake (reasons/note/idea_note
    и любой текст сканера НЕ попадают в шину; финальную очистку делает bus.sanitize);
  - дедупликация: (kind, symbol, idea) не чаще окна dedup_s (по умолчанию 300 с; idea_near 1800 с; idea_level/invalidated/tp/expired сутки);
    окна сеются из хвоста шины при старте, поэтому перезапуск сервиса не даёт дублей;
  - idea_expired: идея снята по ttl (ideawatch делает это молча); сервис замечает смену статуса в state/ideas.jsonl и публикует событие;
  - patrol: плановый обход монет дня на высоких уровнях риска (класс Patrol), чтобы агент искал входы сам, а не только по всплеску.
Heartbeat: runtime/platform/scan.hb.json каждые hb_s секунд. Старый режим `./tb bybit read scan --watch` (Monitor, stdout) не затронут. Запуск моста исполнителя
(executor/scanbridge) при этом выключается переменной TB_SCAN_BRIDGE=0 (её ставит супервизор), иначе события дублировались бы.
Публичные данные: ключ не нужен; окружение чистит супервизор (ни ключей Bybit, ни прокси).
"""
import argparse
import json
import os
import re
import signal
import sys
import time

from tb import paths
from tb.core import bus
from tb.platform import services as S

_SYM = re.compile(r"^[A-Z0-9]{2,20}$")
_IDEA = re.compile(r"^I\d+$")
NUM_KEYS = (("price", "px"), ("score", "score"), ("range_5m_pct", "range_5m_pct"), ("ret_5m_pct", "ret_5m_pct"), ("turn_surge_5m", "turn_surge_5m"),
            ("spread_bps", "spread_bps"), ("funding_pct", "funding_pct"), ("oi_chg_15m_pct", "oi_chg_15m_pct"), ("oi_chg_1h_pct", "oi_chg_1h_pct"), ("liq_usd", "liq_usd"))
MAX_DEDUP_KEYS = 4000


class _Stop(BaseException):
    pass


class _Null(object):
    def write(self, s):
        return len(s)

    def flush(self):
        pass


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


class Publisher(object):
    """Хвост hot_events.jsonl -> шина с белым списком и дедупликацией."""

    def __init__(self, root, scfg, events_path=None, clock=time.time, emit=None):
        self.root, self.cfg, self.clock = root, scfg, clock
        self.path = events_path or paths.runtime("hot_events.jsonl", root=root)
        self.emit = emit or (lambda t, d, sym: bus.emit(t, d, sym=sym, root=root))
        self.dedup = {}
        self.published = 0
        self.dropped_dup = 0
        self.last_publish = None
        self.last_error = None
        try:
            self.pos = os.path.getsize(self.path)
        except OSError:
            self.pos = 0
        self._seed()

    def _window(self, kind):
        w = self.cfg.get("dedup_s") or {}
        return float(w.get(kind, w.get("default", 300.0)))

    def _seed(self):
        """Окна дедупликации из хвоста шины: перезапуск сервиса не повторяет только что отправленное."""
        try:
            for e in bus.read_tail(200, root=self.root):
                if e.get("type") != "scanner_signal" or not e.get("sym"):
                    continue
                d = e.get("d") or {}
                ts = bus.parse_iso(e.get("ts"))
                if ts is not None and d.get("kind"):
                    self.dedup[(d["kind"], e["sym"], d.get("idea") or "")] = ts
        except Exception:
            pass

    def convert(self, r):
        """Строка сканера -> (d, sym) или None."""
        if not isinstance(r, dict) or r.get("type") not in bus.SCANNER_KINDS:
            return None
        sym = r.get("sym")
        if not isinstance(sym, str) or not _SYM.match(sym):
            return None
        kind = r["type"]
        wake = bool(r.get("wake"))
        if not wake and not self.cfg.get("publish_info", True):
            return None
        d = {"kind": kind}
        for src, dst in NUM_KEYS:
            if _isnum(r.get(src)):
                d[dst] = r[src]
        direction = r.get("direction") or r.get("side")
        if direction in ("up", "down", "long", "short"):
            d["dir"] = direction
        if r.get("liq_side") in ("long", "short"):
            d["liq_side"] = r["liq_side"]
        iid = r.get("idea") if isinstance(r.get("idea"), str) else (r.get("id") if kind.startswith("idea_") else None)
        if isinstance(iid, str) and _IDEA.match(iid):
            d["idea"] = iid
        d["wake"] = wake
        return d, sym

    def publish(self, d, sym, now=None):
        """-> True, если событие ушло в шину (не дубль)."""
        now = self.clock() if now is None else now
        key = (d["kind"], sym, d.get("idea") or "")
        last = self.dedup.get(key)
        if last is not None and now - last < self._window(d["kind"]):
            self.dropped_dup += 1
            return False
        try:
            self.emit("scanner_signal", d, sym)
        except Exception as e:
            self.last_error = "bus: %s" % type(e).__name__
            return False
        self.dedup[key] = now
        if len(self.dedup) > MAX_DEDUP_KEYS:
            for k in sorted(self.dedup, key=self.dedup.get)[:MAX_DEDUP_KEYS // 4]:
                del self.dedup[k]
        self.published += 1
        self.last_publish = now
        self._want_structure(d, sym, now)
        return True

    def _want_structure(self, d, sym, now):
        """Сигнал по монете: просим поток структуры (marketdata/structure.py) подтянуть свечи вне очереди, чтобы к приходу мозга уровни уже были."""
        kind = d.get("kind") or ""
        if kind.startswith("idea_") and kind not in ("idea_near", "idea_level"):
            return
        if not (self.cfg.get("structure") or {}).get("enabled", True):
            return
        try:
            from tb.marketdata import structure
            structure.request(self.root, [sym], now)
        except Exception:
            pass

    def poll(self):
        """Новые строки файла -> шина. -> число отправленных событий."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return 0
        if size < self.pos:                       # файл усечён/ротирован: читаем сначала
            self.pos = 0
        if size == self.pos:
            return 0
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            data = f.read()
        nl = data.rfind(b"\n")
        if nl < 0:
            return 0
        self.pos += nl + 1
        n = 0
        for ln in data[:nl].split(b"\n"):
            try:
                r = json.loads(ln.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("type") == "scan_error":
                self.last_error = "scan_error"
            elif isinstance(r, dict) and r.get("type") == "scan_recovered":
                self.last_error = None
            c = self.convert(r)
            if c is not None and self.publish(c[0], c[1]):
                n += 1
        return n


class ExpiryWatcher(object):
    """idea_expired: идея, активная раньше, стала expired по ttl (ideawatch ставит статус молча)."""

    def __init__(self, ideas_path, publisher):
        self.path, self.pub = ideas_path, publisher
        self.stamp = None
        self.known = set()
        self.check(initial=True)

    def check(self, initial=False):
        from tb.core import ideas
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            self.stamp = None
            return 0
        if stamp == self.stamp:
            return 0
        self.stamp = stamp
        n = 0
        cur = ideas.load_ideas(path=self.path)
        for i in cur:
            iid = i.get("id")
            if (i.get("status") or "idea") in ideas.ACTIVE:
                self.known.add(iid)
            elif i.get("status") == "expired" and iid in self.known:
                self.known.discard(iid)
                notes = i.get("mark_notes") or []
                if not initial and notes and str(notes[-1]).startswith("ttl") and _SYM.match(str(i.get("sym") or "")) and _IDEA.match(str(iid)):
                    if self.pub.publish({"kind": "idea_expired", "idea": iid, "wake": True}, i["sym"]):
                        n += 1
        return n


PATROL_BASE = ("BTC", "ETH", "SOL")


def _risk_level(root):
    from tb.core import risklevels
    try:
        return int(risklevels.current_level(root).get("level") or 3)
    except Exception:
        return 3


class Patrol(object):
    """Плановый обход рынка. Без него агент реагирует только на события сканера и идей и в тишине просто «ждёт». На высоких уровнях риска
    (`min_level`, по умолчанию 4) сканер сам, без всплеска, будит entry_scout по очереди на каждую монету дня: day.symbols (иначе BTC/ETH/SOL)
    плюс до `hot_n` горячих; монеты из day.avoid пропускаются. Период `every_s` на уровне 5, вдвое реже на уровне 4.
    Идёт только пока день работает в paper (не observe): entry_scout в live не работает по замыслу. Событие несёт только kind и wake."""

    def __init__(self, root, pcfg, publisher, clock=time.time, level_fn=None):
        self.root, self.cfg, self.pub, self.clock = root, pcfg or {}, publisher, clock
        self.level_fn = level_fn or _risk_level
        self.last = clock()                     # первый обход через период после старта, а не сразу
        self.i = 0
        self.published = 0

    def _read(self, *p):
        d = S.read_json(paths.state("control", *p, root=self.root))
        return d if isinstance(d, dict) else {}

    def active(self):
        """-> уровень риска, если обход разрешён, иначе None."""
        if not self.cfg.get("enabled", True):
            return None
        lvl = self.level_fn(self.root)
        if lvl < int(self.cfg.get("min_level", 4)):
            return None
        plat = self._read("platform.json")            # trading.json не смотрим: для paper флаг не нужен (шлюз пропускает paper всегда)
        if plat.get("state") != "running" or plat.get("mode") != "paper" or plat.get("trading") is False:
            return None
        return lvl

    def symbols(self):
        day = self._read("day_limits.json")
        avoid = set(x for x in (day.get("avoid") or []) if isinstance(x, str))
        base = [x for x in (day.get("symbols") or []) if isinstance(x, str) and _SYM.match(x)] or list(PATROL_BASE)
        out = [x for x in base if x not in avoid]
        hot = S.read_json(paths.runtime("hot.json", root=self.root))
        n = 0
        for r in (hot.get("top") if isinstance(hot, dict) and isinstance(hot.get("top"), list) else []):
            if n >= int(self.cfg.get("hot_n", 2)):
                break
            s = r.get("sym") if isinstance(r, dict) else None
            if isinstance(s, str) and _SYM.match(s) and r.get("tradable") is not False and s not in avoid and s not in out:
                out.append(s)
                n += 1
        return out

    def tick(self):
        """-> 1, если событие обхода отправлено."""
        now = self.clock()
        every = float(self.cfg.get("every_s", 300.0))
        if now - self.last < every:
            return 0
        lvl = self.active()
        if lvl is None:
            return 0
        if lvl < 5:
            if now - self.last < 2 * every:
                return 0
        syms = self.symbols()
        if not syms:
            return 0
        self.last = now
        sym = syms[self.i % len(syms)]
        self.i += 1
        if self.pub.publish({"kind": "patrol", "wake": True}, sym, now):
            self.published += 1
            return 1
        return 0


def params_of(scfg):
    keys = ("top", "min_turnover", "max_spread_bps", "max_lot", "range_pct", "surge_min", "cooldown", "max_per_hour", "wake_score", "outcome_min",
            "idea_zone_pct", "interval")
    p = {k: scfg[k] for k in keys if k in scfg}
    p["interval"] = max(float(p.get("interval", 30.0)), 5.0)
    p["max_seconds"] = float(scfg.get("max_seconds") or 0.0)
    return p


def make_client(scfg):
    from tb.venues import bybit
    return bybit.Client(scfg.get("env") or "mainnet")


def run(root, cfg, client=None, clock=time.time, sleep=time.sleep, stop=lambda: False, hs_kwargs=None, ideas_path=None, events_path=None, emit=None, structure=False):
    """Основной цикл сервиса. -> код выхода. hs_kwargs: пути hotscan для тестов (hot_path, log_path, prices_path, state_dir)."""
    from tb.core import ideas
    from tb.marketdata import hotscan
    scfg = cfg.get("scan") or {}
    client = client or make_client(scfg)
    hs_kwargs = dict(hs_kwargs or {})
    pub = Publisher(root, scfg, events_path or hs_kwargs.get("log_path"), clock, emit)
    exp = ExpiryWatcher(ideas_path or ((os.path.join(os.path.dirname(hs_kwargs["log_path"]), "ideas.jsonl")) if hs_kwargs.get("log_path") else ideas.ideas_path(root)), pub)
    patrol = Patrol(root, scfg.get("patrol"), pub, clock)
    hb_s = float(scfg.get("hb_s") or 5.0)
    state = {"hb": -1e18, "scans": 0}

    def beat(force=False):
        now = clock()
        if force or now - state["hb"] >= hb_s:
            state["hb"] = now
            S.write_hb("scan", {"published": pub.published, "dropped_dup": pub.dropped_dup, "last_publish_ts": pub.last_publish, "last_error": pub.last_error,
                                "interval": scfg.get("interval")}, root=root, now=now)

    def tick():
        pub.poll()
        exp.check()
        patrol.tick()
        beat()
        if stop():
            raise _Stop()

    def sleeper(s):
        end = clock() + s
        tick()
        while clock() < end:
            sleep(max(0.0, min(1.0, end - clock())))
            tick()

    beat(True)
    th = _start_structure(root, scfg, stop) if structure else None
    lth = _start_liq(root, scfg, stop) if structure else None          # только в боевом запуске сервиса (как и структура), в тестах цикла сеть не трогаем
    try:
        code = hotscan.run(client, params_of(scfg), True, out=_Null(), clock=clock, sleep=sleeper, **hs_kwargs)
    except _Stop:
        code = 0
    pub.poll()
    beat(True)
    return code


def _start_structure(root, scfg, stop):
    """Поток структуры рынка (marketdata/structure.py): свечи 5м/1ч по публичному REST -> runtime/structure.json для досье мозга. Свой клиент (не делит сокет с hotscan);
    ошибки гасятся внутри, выключается scan.structure.enabled=false. Поток демон: остановка процесса его не ждёт."""
    scfg = scfg.get("structure") or {}
    if not scfg.get("enabled", True):
        return None
    import threading
    from tb.marketdata import structure as st
    try:
        cl = make_client(dict(scfg, env=scfg.get("env") or "mainnet"))
    except Exception:
        return None
    th = threading.Thread(target=st.run_loop, args=(cl, root, scfg), kwargs={"stop": stop}, name="structure", daemon=True)
    th.start()
    return th


def _start_liq(root, scfg, stop):
    """Поток ликвидаций (marketdata/liqwatch.py): публичный WebSocket Bybit -> runtime/liq.json и события liq_cascade в hot_events.jsonl. Выключается scan.liq.enabled=false;
    свой сокет, ошибки гасятся внутри, поток демон."""
    lcfg = dict(scfg.get("liq") or {})
    if not lcfg.get("enabled", True):
        return None
    import threading
    from tb.marketdata import liqwatch
    lcfg.setdefault("env", scfg.get("env") or "mainnet")
    th = threading.Thread(target=liqwatch.run_loop, args=(root, lcfg), kwargs={"stop": stop, "log": lambda m: print(m, flush=True)}, name="liqwatch", daemon=True)
    th.start()
    return th


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="tb.platform.scansvc")
    ap.add_argument("cmd", choices=("run",))
    ap.add_argument("--max-seconds", type=float, default=0.0, dest="max_seconds")
    ap.add_argument("--root", default=None)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if a.root:
        os.environ["TB_ROOT"] = os.path.abspath(a.root)
    from tb import launcher
    launcher.setup_stdio()
    launcher.disable_system_proxies()          # urllib на macOS/Windows берёт прокси из системных настроек; для публичных данных биржи они не нужны
    root = paths.root()
    cfg = S.load_cfg(root)
    if a.max_seconds:
        cfg["scan"]["max_seconds"] = a.max_seconds
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
    print("scansvc: интервал %ss, wake_score %s, env %s" % (cfg["scan"].get("interval"), cfg["scan"].get("wake_score"), cfg["scan"].get("env")), flush=True)
    return run(root, cfg, stop=lambda: flag["stop"], structure=True)


if __name__ == "__main__":
    sys.exit(main())
