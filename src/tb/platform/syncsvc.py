"""Автосинхронизация капитала: `python -m tb.platform.syncsvc run|once|status|confirm` (run запускает супервизор через `./tb sync run`). Владелец: WP11-B.

Раз в interval_min минут читает баланс ТОЛЬКО читающим ключом `read` (тот же клиент и вызов, что `./tb bybit read balance`; ключ в окружение ЭТОГО
процесса грузит launcher, супервизор и другие сервисы его не видят) и обновляет в state/portfolio.json equity/free/ts площадки (venue, по умолчанию bybit).
Защиты:
  - ошибка чтения, пустой/нулевой баланс, нет ключа: портфель НЕ меняется, статус «устарело» (runtime/platform/sync.state.json, его читает дашборд);
  - расхождение с прежней записью больше max_dev_frac (10%, как equity_sync в 02_executor): не обновлять, событие reconcile_alert equity_mismatch
    и ожидание подтверждения владельца: `./tb sync confirm` (пополнение или вывод: `./tb portfolio deposit`);
  - высшая отметка (hwm) только вверх и только по подтверждённому росту: суммарный капитал выше hwm в hwm_confirm_reads чтениях подряд
    (берётся наименьшее из них); разовый всплеск отметку не поднимает; снижается hwm только вручную;
  - ордеров, перевода средств и торговых ключей здесь нет; сеть: только публичный/читающий вызов баланса.
Heartbeat: runtime/platform/sync.hb.json на каждом цикле (в том числе при деградации: без ключа сервис живёт и сообщает no_key).
"""
import argparse
import json
import math
import os
import signal
import sys
import time

from tb import paths
from tb.platform import services as S

STATE_NAME = "sync.state.json"


def state_file(root=None):
    return os.path.join(S.pdir(root), STATE_NAME)


def read_state(root=None):
    d = S.read_json(state_file(root))
    return d if isinstance(d, dict) else {}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def key_present():
    """Есть ли читающий ключ в окружении этого процесса (значения не читаются и не печатаются)."""
    try:
        from tb.venues import bybit
        c = bybit.make_client()
        return bool(c.key and c.secret)
    except Exception:  # noqa: BLE001
        return False


def fetch_balance(client=None):
    """-> (equity, free). Тот же расчёт, что `./tb bybit read balance` (UNIFIED). Любая ошибка/пустой ответ -> исключение (портфель не трогаем)."""
    from tb.venues import bybit
    c = client if client is not None else bybit.make_client()
    w = bybit.wallet_all(c).get("UNIFIED") or {}
    if "error" in w:
        raise RuntimeError("баланс UNIFIED: " + str(w["error"])[:80])
    eq, free = w.get("equity"), w.get("free")
    if not _num(eq) or not _num(free) or eq <= 0 or free < 0:
        raise RuntimeError("баланс пуст или нечисловой")
    return float(eq), float(free)


def apply_reading(p, venue, eq, free, now, st, cfg):
    """Чистая логика защит. p = portfolio dict (меняется на месте), st = состояние сервиса (меняется). -> (action, подробности):
    action: "set" (портфель обновлён) | "mismatch" (ждём подтверждения; p не менялся)."""
    dev_lim = float(cfg.get("max_dev_frac", 0.10))
    need = max(1, int(cfg.get("hwm_confirm_reads", 2)))
    v = (p.get("venues") or {}).get(venue)
    old = float(v.get("equity") or 0.0) if isinstance(v, dict) else 0.0
    dev = abs(eq - old) / old if old > 0 else 0.0
    if old > 0 and dev > dev_lim:
        new = not (isinstance(st.get("pending"), dict) and abs(st["pending"].get("equity", 0) - eq) / max(eq, 1e-9) < 0.01)
        st["pending"] = {"equity": round(eq, 4), "free": round(free, 4), "ts": round(now, 1), "old_equity": round(old, 4), "dev": round(dev, 4)}
        return "mismatch", {"new": new, "dev": dev, "old": old}
    p.setdefault("venues", {})[venue] = {"equity": round(eq, 4), "free": round(free, 4), "ts": _iso(now)}
    st.pop("pending", None)
    tot = sum(float(x.get("equity") or 0.0) for x in p["venues"].values() if isinstance(x, dict))
    hwm = float(p.get("hwm") or 0.0)
    if tot > hwm:
        cand = st.get("hwm_cand")
        st["hwm_cand"] = round(min(float(cand), tot), 4) if _num(cand) else round(tot, 4)
        st["hwm_reads"] = int(st.get("hwm_reads") or 0) + 1
        if st["hwm_reads"] >= need:
            p["hwm"] = max(hwm, st["hwm_cand"])
            st["hwm_cand"], st["hwm_reads"] = None, 0
    else:
        st["hwm_cand"], st["hwm_reads"] = None, 0
    p.setdefault("hwm", hwm)
    return "set", {"dev": dev}


def _emit_mismatch(root, now):
    try:
        from tb.core import bus
        bus.emit("reconcile_alert", {"kind": "equity_mismatch", "sym": "BTC"}, root=root, now=now)
    except Exception:  # noqa: BLE001 — шина недоступна: статус всё равно виден в sync.state.json и дашборде
        pass


def step(root, cfg, now, fetch=fetch_balance, has_key=True, emit=_emit_mismatch, load=None, save=None):
    """Один цикл. fetch() -> (equity, free). -> новое состояние (записано в sync.state.json). Сеть и портфель инъектируются (тесты без сети)."""
    from tb.core import portfolio
    load, save = load or portfolio.load, save or portfolio.save
    st = read_state(root)
    venue = cfg.get("venue") or "bybit"
    st.update({"venue": venue, "checked_ts": round(now, 1)})
    if not has_key:
        st.update({"status": "no_key", "reason": "нет читающего ключа read (./tb setup): капитал не синхронизируется"})
    else:
        try:
            eq, free = fetch()
        except Exception as e:  # noqa: BLE001 — любая ошибка = прежние значения и пометка «устарело»
            st.update({"status": "error", "reason": "%s: %s" % (type(e).__name__, str(e)[:100])})
        else:
            p = load()
            act, info = apply_reading(p, venue, eq, free, now, st, cfg)
            if act == "set":
                save(p)
                st.update({"status": "ok", "reason": "", "last_ok_ts": round(now, 1), "equity": round(eq, 4), "free": round(free, 4)})
            else:
                st.update({"status": "mismatch", "reason": "equity биржи %.2f vs портфель %.2f (расхождение %.0f%% > %.0f%%): подтверди ./tb sync confirm"
                           % (eq, info["old"], info["dev"] * 100, float(cfg.get("max_dev_frac", 0.10)) * 100)})
                if info["new"]:
                    emit(root, now)
    last = st.get("last_ok_ts")
    st["stale"] = not (_num(last) and now - last <= float(cfg.get("stale_min", 15.0)) * 60.0)
    S.write_json(state_file(root), st)
    return st


def confirm(root, now=None, load=None, save=None):
    """Владелец подтверждает ожидающее значение (пополнение/вывод/большой PnL). hwm поднимается, если капитал выше (владелец подтвердил рост); вниз не трогается."""
    from tb.core import portfolio
    load, save = load or portfolio.load, save or portfolio.save
    now = time.time() if now is None else now
    st = read_state(root)
    pend = st.get("pending")
    if not isinstance(pend, dict):
        return False, "нечего подтверждать"
    p = load()
    venue = st.get("venue") or "bybit"
    p.setdefault("venues", {})[venue] = {"equity": float(pend["equity"]), "free": float(pend["free"]), "ts": _iso(now)}
    tot = sum(float(x.get("equity") or 0.0) for x in p["venues"].values() if isinstance(x, dict))
    p["hwm"] = max(float(p.get("hwm") or 0.0), tot)
    save(p)
    st.pop("pending", None)
    st.update({"status": "ok", "reason": "", "last_ok_ts": round(now, 1), "equity": pend["equity"], "free": pend["free"], "stale": False, "hwm_cand": None, "hwm_reads": 0})
    S.write_json(state_file(root), st)
    return True, "капитал %.2f принят (высшая отметка %.2f)" % (pend["equity"], p["hwm"])


def run(root, stop=lambda: False, max_seconds=0.0, clock=time.time, sleep=time.sleep, fetch=fetch_balance, has_key=None, emit=_emit_mismatch, log=None):
    log = log or (lambda m: print(m, flush=True))
    cfg = (S.load_cfg(root).get("sync") or {})
    if has_key is None:
        has_key = key_present()
    period = max(30.0, float(cfg.get("interval_min", 5.0)) * 60.0)
    t_end = clock() + max_seconds if max_seconds else None
    last = None
    while not stop():
        now = clock()
        try:
            st = step(root, cfg, now, fetch, has_key, emit)
        except Exception as e:  # noqa: BLE001 — сервис не падает
            st = {"status": "error", "reason": type(e).__name__}
        if st.get("status") != last:
            log("sync: %s%s" % (st.get("status"), " (%s)" % st.get("reason") if st.get("reason") else ""))
            last = st.get("status")
        S.write_hb("sync", {"status": st.get("status"), "stale": st.get("stale"), "last_ok_ts": st.get("last_ok_ts")}, root=root, now=clock())
        if t_end is not None and clock() >= t_end:
            break
        end = clock() + period
        while not stop() and clock() < end:         # короткие паузы: heartbeat и остановка не ждут весь период
            S.write_hb("sync", {"status": st.get("status"), "stale": st.get("stale"), "last_ok_ts": st.get("last_ok_ts")}, root=root, now=clock())
            sleep(min(30.0, max(0.0, end - clock())))
            if t_end is not None and clock() >= t_end:
                return 0
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tb.platform.syncsvc")
    ap.add_argument("cmd", choices=("run", "once", "status", "confirm"))
    ap.add_argument("--max-seconds", type=float, default=0.0, dest="max_seconds")
    ap.add_argument("--root", default=None)
    try:
        a = ap.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit:
        return 2
    if a.root:
        os.environ["TB_ROOT"] = os.path.abspath(a.root)
    root = paths.root()
    if a.cmd == "status":
        print(json.dumps(read_state(root), ensure_ascii=False, indent=1))
        return 0
    if a.cmd == "confirm":
        ok, msg = confirm(root)
        print(msg)
        return 0 if ok else 1
    if a.cmd == "once":
        cfg = S.load_cfg(root).get("sync") or {}
        st = step(root, cfg, time.time(), has_key=key_present())
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return 0 if st.get("status") == "ok" else 1
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
    print("sync: старт", flush=True)
    return run(root, stop=lambda: flag["stop"], max_seconds=a.max_seconds)


if __name__ == "__main__":
    sys.exit(main())
