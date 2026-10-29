"""`./tb smoke [--max-seconds N] [--keep] [--root DIR] [--report FILE]`: сквозной прогон paper во ВРЕМЕННОМ корне (WP интеграции).

Что делает: временный TB_ROOT (копия config/, пустой state/, свой каталог ключей с ПОДДЕЛЬНЫМИ ключами) -> сценарный фейковый LLM (smoke_llm: локальный HTTP,
настоящие адаптеры и транспорт) -> платформа `up --profile paper --detach` (exec, scan, dispatch, ctl, dash, brain) на реальных ПУБЛИЧНЫХ данных Bybit ->
старт дня кнопкой (ctl day.start) -> утренний план мозга (задание L-* -> фейк) -> событие сканера -> план агента через шлюз -> вход в paper -> сопровождение мозгом
(событие entry_filled -> position_manager hold) -> закрытие -> остановка дня -> отчёт. Печатает таблицу проверок. В конце платформу гасит и проверяет, что нет
осиротевших процессов; временный корень остаётся для просмотра: `TB_ROOT=<корень> ./tb dash --port 8790`.
Реальные state/ и config/ проекта не затрагиваются, ключи пользователя не читаются, торговли на бирже нет (только paper). Живых LLM-запросов нет.
Сеть: только публичные данные Bybit для сервисов платформы (прокси у них снимает супервизор). Запуск: Bash с отключённой песочницей одной командой `./tb smoke`.
Код возврата 0: все проверки прошли.
"""
import argparse
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from tb import paths

SERVICES = "exec,scan,feed,dispatch,ctl,dash,brain"


class Checks(object):
    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail=""):
        self.rows.append((name, bool(ok), str(detail)[:200]))
        print("  [%s] %s%s" % ("ok " if ok else "СБОЙ", name, (" - " + str(detail)[:160]) if detail else ""), flush=True)
        return ok

    def all_ok(self):
        return all(r[1] for r in self.rows)

    def table(self):
        w = max(len(r[0]) for r in self.rows) if self.rows else 10
        out = ["%-*s  %-6s  %s" % (w, "проверка", "итог", "детали")]
        out += ["%-*s  %-6s  %s" % (w, n, "прошла" if ok else "СБОЙ", d) for n, ok, d in self.rows]
        return "\n".join(out)

    def markdown(self):
        out = ["| проверка | итог | детали |", "|---|---|---|"]
        out += ["| %s | %s | %s |" % (n, "прошла" if ok else "**СБОЙ**", d.replace("|", "/")) for n, ok, d in self.rows]
        return "\n".join(out)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_until(fn, timeout, step=0.5):
    end = time.time() + timeout
    while True:
        try:
            v = fn()
        except Exception:
            v = None
        if v:
            return v
        if time.time() >= end:
            return None
        time.sleep(step)


def rj(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def jl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


# ------------------------------------------------------------------ подготовка корня
def prepare(root, keys, llm_url, dash_port):
    """Копия config/, правки ТОЛЬКО во временном корне: адрес провайдера, подписчик api, сервис brain, порт дашборда. Ключи: подписи, токен, агент api, поддельный DeepSeek."""
    src = paths.root()
    shutil.copytree(os.path.join(src, "config"), os.path.join(root, "config"))
    for d in ("state", "runtime"):
        os.makedirs(os.path.join(root, d), exist_ok=True)

    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(os.path.join(root, "state", "portfolio.json"), "w", encoding="utf-8") as f:
        json.dump({"venues": {"platform": {"equity": 30.0, "free": 30.0, "ts": ts}}, "hwm": 30.0}, f)      # бумажный капитал сценария (как `./tb portfolio set`)

    def edit(name, fn):
        p = os.path.join(root, "config", name)
        d = rj(p)
        fn(d)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)

    def prov(d):
        d["providers"]["deepseek"]["base_url"] = llm_url
        d["providers"]["deepseek"]["beta_url"] = llm_url + "/beta"
        d["providers"]["deepseek"]["timeouts"] = {"connect_s": 5, "read_s": 30, "total_s": 60}
        d["providers"]["deepseek"]["enabled"] = True

    def subs(d):
        for s in d["subscribers"]:
            if s["id"] == "api":
                s["enabled"] = True
                s["rate"] = {"jobs_per_hour": 60, "critical_per_hour": 60, "coalesce_s": 1}

    def plat(d):
        d.setdefault("svc", {}).setdefault("dash", {})["port"] = dash_port
        d["services"]["paper"] = SERVICES.split(",")
        d.setdefault("dispatch", {}).setdefault("api", {}).update({"coalesce_s": 1.0, "settle_s": 1.0})

    edit("providers.json", prov)
    edit("subscriptions.json", subs)
    edit("platform.json", plat)
    os.makedirs(keys, exist_ok=True)
    env = {"TB_KEYS_DIR": keys, "HOME": keys}
    from tb.control import auth
    from tb.gateway import registry
    auth.init_key(env)
    token = auth.new_token(env)
    tok = registry.new_token()
    registry.set_token_hash("api", registry.hash_token(tok), root)
    registry.write_token_file("api", tok, env)
    with open(os.path.join(keys, "deepseek.env"), "w", encoding="utf-8") as f:
        f.write("DEEPSEEK_API_KEY=smoke-fake-key-not-real\n")
    os.chmod(os.path.join(keys, "deepseek.env"), 0o600)
    return token


def child_env(root, keys):
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") and not k.upper().startswith(("BYBIT_", "ANTHROPIC_", "DEEPSEEK_"))}
    env.update({"TB_ROOT": root, "TB_KEYS_DIR": keys, "PYTHONPATH": src_dir(), "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8"})
    return env


def src_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tb(args, env, timeout=120):
    e = dict(env)
    e["PYTHONPATH"] = src_dir() + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") and e["PYTHONPATH"] != src_dir() else "")
    return subprocess.run([sys.executable, "-m", "tb.launcher"] + list(args), env=e, cwd=os.path.dirname(src_dir()), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, timeout=timeout)


def platform_pids(root):
    pids = set()
    for p in glob.glob(os.path.join(root, "runtime", "platform", "*.pid")):
        try:
            with open(p) as f:
                pids.add(int(f.read().strip().split()[0]))
        except (OSError, ValueError, IndexError):
            pass
    st = rj(os.path.join(root, "runtime", "platform", "status.json")) or {}
    for sv in (st.get("services") or {}).values():
        if isinstance(sv, dict) and isinstance(sv.get("pid"), int):
            pids.add(sv["pid"])
    lock = rj(os.path.join(root, "runtime", "platform", "supervisor.lock")) or {}
    if isinstance(lock.get("pid"), int):
        pids.add(lock["pid"])
    return pids


# ------------------------------------------------------------------ прогон
def run(a):
    from tb.control import client
    ck = Checks()
    t_end = time.time() + a.max_seconds
    root = os.path.abspath(a.root) if a.root else tempfile.mkdtemp(prefix="tbsmoke_")
    keys = os.path.join(root, "_keys")
    os.makedirs(root, exist_ok=True)
    from tb import smoke_llm
    llm = smoke_llm.Server().start()
    dash_port = free_port()
    print("временный корень: %s\nдашборд: http://127.0.0.1:%d/ (порт временный)" % (root, dash_port), flush=True)
    token = prepare(root, keys, llm.url, dash_port)
    env = child_env(root, keys)
    os.environ["TB_ROOT"], os.environ["TB_KEYS_DIR"] = root, keys          # подписи команд и шлюз этого процесса смотрят во временный корень
    from tb.core import journal, learning, pending, portfolio, session
    for mod, name in ((portfolio, "portfolio.json"), (journal, "trades.jsonl"), (session, "sessions.jsonl"), (pending, "pending_orders.json"), (learning, "learning.json")):
        mod.PATH = os.path.join(root, "state", name)    # PATH вычислен при импорте: иначе шлюз читал бы капитал и сессию из state/ клона (в чистом клоне их нет -> E_RISK_PCT)
    pids = set()
    up_started = False
    try:
        # -------- платформа
        r = tb(["up", "--profile", "paper", "--services", SERVICES, "--detach"], env, timeout=90)
        up_started = r.returncode == 0
        ck.add("платформа поднята (up --detach)", up_started, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")
        if not up_started:
            return ck, root, keys, token, llm, pids

        def services_ok():
            st = rj(os.path.join(root, "runtime", "platform", "status.json")) or {}
            sv = st.get("services") or {}
            need = SERVICES.split(",")
            return all((sv.get(n) or {}).get("state") == "running" for n in need)
        ck.add("все сервисы running", wait_until(services_ok, 60), SERVICES)
        pids = platform_pids(root)

        def exec_ok():
            h = rj(os.path.join(root, "runtime", "exec_health.json")) or {}
            return h.get("alive") and h.get("mode") == "paper" and (h.get("ws") or {}).get("public", {}).get("up")
        ck.add("исполнитель paper жив, публичный WS Bybit подключён", wait_until(exec_ok, 90), "данные: публичные Bybit")

        def price_ok():
            mk = rj(os.path.join(root, "runtime", "market.json")) or {}
            return bool(mk.get("symbols")) and bool(rj(os.path.join(root, "runtime", "prices.json")))
        ck.add("рыночные данные пошли (feed -> market.json, scan -> prices.json)", wait_until(price_ok, 90))
        ck.add("ctl жив", wait_until(lambda: not client.read_status(root).get("stale"), 30))
        import urllib.request
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:%d/" % dash_port, timeout=5) as resp:
                ck.add("дашборд отвечает", resp.status == 200, "HTTP %s" % resp.status)
        except Exception as e:
            ck.add("дашборд отвечает", False, e.__class__.__name__)

        from tb.brain import brief as brief_mod

        def brief_px():
            mk = brief_mod.build("start", root=root).get("market") or {}
            return [k for k in ("btc", "eth") if isinstance(mk.get(k), dict) and isinstance(mk[k].get("px"), (int, float))]
        ck.add("в досье есть цена BTC/ETH (блок market из фида)", wait_until(brief_px, 90, 2.0), "")

        # -------- старт дня кнопкой
        cmd = client.build("day.start", {"mode": "paper", "session": "intraday", "minutes": 120, "allow_amber": True, "accepted": []}, src="cli", sess="s-abcd1234")
        client.submit(cmd, root)
        res = client.wait_result(cmd["id"], root, timeout=min(240, max(30, t_end - time.time())))
        ck.add("day.start принят (paper)", bool(res) and res.get("status") == "done", (res or {}).get("code"))
        pf = None
        day = None
        if res and res.get("status") != "done":
            ck.add("причина отказа старта", False, json.dumps(res.get("data") or {}, ensure_ascii=False)[:180])
        plat = rj(os.path.join(root, "state", "control", "platform.json")) or {}
        day = plat.get("day_id")
        ck.add("день running, торговый флаг", plat.get("state") == "running" and bool(plat.get("trading")), "%s %s" % (day, plat.get("state")))
        dd = rj(os.path.join(root, "state", "days", str(day), "dayplan.json")) or {}
        ck.add("утренний план от мозга (фейковый LLM через L-задание)", (dd.get("meta") or {}).get("source") == "agent", "source=%s" % (dd.get("meta") or {}).get("source"))
        ck.add("роль morning_analyst вызвана", "morning_analyst" in llm.roles_called(), llm.roles_called())
        pf = rj(os.path.join(root, "state", "days", str(day), "preflight.json")) or {}
        ck.add("preflight без красных", bool(pf) and not any(c.get("sev") == "R" and not c.get("ok") for c in (pf.get("checks") or {}).values()) if isinstance(pf.get("checks"), dict)
               else bool(pf), "")

        # -------- событие сканера
        from tb.core import bus
        bus.emit("scanner_signal", {"kind": "hot_enter", "px": 1.0, "score": 5.0, "dir": "up", "wake": True}, sym="BTC", root=root)
        q = wait_until(lambda: [e for e in jl(os.path.join(root, "state", "bus", "queues", "chat.jsonl"))] or os.path.exists(os.path.join(root, "state", "dispatch")), 15)
        ck.add("событие сканера прошло шину и диспетчер", bool(bus.last_seq(root) > 0), "seq=%s" % bus.last_seq(root))

        # -------- план агента -> вход -> сопровождение
        from tb.gateway import rpc
        gw = rpc.Gateway(root)
        br = gw.call("api", "get_brief", {"scope": "start"})
        lv = br.get("levels") if isinstance(br, dict) else None
        sym, px = None, None
        mk = br.get("market") if isinstance(br, dict) and isinstance(br.get("market"), dict) else {}
        for s in ("BTC", "ETH"):
            m = mk.get(s.lower())
            if isinstance(m, dict) and isinstance(m.get("px"), (int, float)):
                sym, px = s, float(m["px"])
                break
        ck.add("досье start собрано, цена BTC/ETH есть", bool(sym), "%s %s" % (sym, px))
        sym, px = None, None
        pr = (rj(os.path.join(root, "runtime", "prices.json")) or {}).get("prices") or {}
        if isinstance(pr.get("SOL"), (int, float)):              # SOL: минимальный лот BTC/ETH не помещается в риск 30 USDT (MIN_SIZE), берём дешёвый символ
            sym, px = "SOL", float(pr["SOL"])
        pid = None
        if sym:
            plan = {"schema": "tb.plan/1", "symbol": sym, "side": "long", "mode": "paper", "session_type": "intraday", "source": {"kind": "brain"},
                    "thesis": "smoke: сценарный вход по спреду, стоп 1.5 процента, цель 3.5 процента", "tags": {"experiment": "smoke-v1", "variant": "A", "setup": "level_reclaim"},
                    "entry": {"when": {"kind": "spread_bps", "lte": 40}, "how": {"type": "market"}, "ref_price": px,
                              "valid": {"ttl_min": 30, "cancel_above": round(px * 1.05, 6), "cancel_below": round(px * 0.95, 6), "max_chase_bps": 30}},
                    "stop": {"price": round(px * 0.985, 6), "trigger_by": "last"},
                    "targets": [{"id": "t1", "price": round(px * 1.035, 6), "frac": 1.0, "exec": "resting"}],
                    "risk": {"score": 0.5, "size_mult": 0.5, "confirm_waiver": True},
                    "lifecycle": {"ttl_min": 30, "max_hold_min": 60, "flatten_at_session_end": True}}
            sub = gw.call("api", "submit_plan", {"plan": plan, "reason": "smoke: вход по уровню"})
            pid = sub.get("plan_id") if isinstance(sub, dict) else None
            ck.add("план принят шлюзом/исполнителем", isinstance(sub, dict) and sub.get("ok") is not False and bool(pid), json.dumps((sub or {}).get("errors") or {"ok": (sub or {}).get("ok")}, ensure_ascii=False)[:160])

        def entered():
            snap = rj(os.path.join(root, "runtime", "exec_snapshot.json")) or {}
            return [p for p in snap.get("positions") or [] if p.get("sym") == sym] if sym else None
        pos = wait_until(entered, 90) if pid else None
        ck.add("вход в paper (позиция в снимке исполнителя)", bool(pos), "size=%s" % ((pos or [{}])[0].get("size") if pos else None))

        def pm_called():
            return "position_manager" in llm.roles_called()
        ck.add("сопровождение: position_manager вызван на entry_filled", bool(pos) and wait_until(pm_called, 90), llm.roles_called()[-3:])
        dec = jl(os.path.join(root, "state", "decisions.jsonl"))
        ck.add("решение мозга записано в журнал решений", any((d.get("actor") or "").startswith("brain") or d.get("kind") for d in dec) if pos else False, "записей %d" % len(dec))

        # -------- закрытие
        if pid and pos:
            cl = gw.call("api", "close_plan", {"id": pid, "reason": "smoke: закрытие сценария"})
            ck.add("команда закрытия принята", isinstance(cl, dict) and cl.get("ok") is not False, json.dumps((cl or {}).get("errors") or {}, ensure_ascii=False)[:120])

            def closed():
                snap = rj(os.path.join(root, "runtime", "exec_snapshot.json")) or {}
                return not [p for p in snap.get("positions") or [] if p.get("sym") == sym]
            ck.add("позиция закрыта", wait_until(closed, 60))
            trades = jl(os.path.join(root, "state", "trades.jsonl"))
            ck.add("сделка в журнале (open+close, mode=paper)", any(t.get("ev") == "close" for t in trades) and any(t.get("mode") == "paper" for t in trades), "строк %d" % len(trades))
            ck.add("теги agent./model. в журнале", any(any(str(x).startswith("agent.") for x in (t.get("tags") or [])) for t in trades if t.get("ev") == "open"))
            wait_until(lambda: "reviewer" in llm.roles_called(), 40)
            ck.add("reviewer вызван на position_closed", "reviewer" in llm.roles_called(), "")

        # -------- остановка дня и отчёт
        cmd = client.build("day.stop", {"mode": "soft"}, src="cli", sess="s-abcd1234")
        client.submit(cmd, root)
        res = client.wait_result(cmd["id"], root, timeout=120)
        ck.add("day.stop принят", bool(res) and res.get("status") == "done", (res or {}).get("code"))
        ok_stopped = wait_until(lambda: (rj(os.path.join(root, "state", "control", "platform.json")) or {}).get("state") == "stopped", min(240, max(60, t_end - time.time())))
        ck.add("день остановлен", bool(ok_stopped))
        rep = rj(os.path.join(root, "state", "days", str(day), "report.json"))
        ck.add("итоговый отчёт дня (report.json/md)", bool(rep) and os.path.exists(os.path.join(root, "state", "days", str(day), "report.md")), (rep or {}).get("verdict"))
        ck.add("торговый флаг выключен", (rj(os.path.join(root, "state", "control", "trading.json")) or {}).get("enabled") is False)
        errs = [e for e in bus.read(0, limit=500, root=root) if e.get("type") == "error"]
        ck.add("нет ошибок шины BAD_FORMAT/JOB_ERROR от мозга", not [e for e in errs if (e.get("d") or {}).get("code") in ("BAD_FORMAT", "JOB_ERROR", "BRAIN_DOWN")],
               ",".join(sorted(set((e.get("d") or {}).get("code", "?") for e in errs))) or "ошибок нет")
        if a.keep:
            print("--keep: платформа оставлена запущенной. Остановка: TB_ROOT=%s ./tb down; дашборд http://127.0.0.1:%d/" % (root, dash_port))
    finally:
        pass
    return ck, root, keys, token, llm, pids


def shutdown(root, keys, env, pids, ck, llm):
    r = tb(["down"], env, timeout=90)
    ck.add("платформа остановлена (down)", r.returncode == 0, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")
    time.sleep(1.5)
    alive = [p for p in pids if pid_alive(p)]
    if alive:
        time.sleep(3)
        alive = [p for p in pids if pid_alive(p)]
    ck.add("осиротевших процессов нет", not alive, "живы: %s" % alive if alive else "pid проверено: %d" % len(pids))
    llm.stop()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tb smoke")
    ap.add_argument("--max-seconds", type=float, default=600.0, dest="max_seconds")
    ap.add_argument("--keep", action="store_true", help="не гасить платформу в конце (для просмотра дашборда)")
    ap.add_argument("--root", default=None, help="каталог временного корня (по умолчанию новый в TMP)")
    ap.add_argument("--report", default=None, help="записать таблицу проверок в markdown-файл")
    try:
        a = ap.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit:
        return 2
    real = paths.root()
    if a.root and os.path.abspath(a.root) == os.path.abspath(real):
        print("smoke: корень совпадает с реальным, отказ", file=sys.stderr)
        return 2
    root = os.path.abspath(a.root) if a.root else None
    ck = Checks()
    env = None
    pids = set()
    llm = None
    croot = keys = None
    try:
        ck, croot, keys, _token, llm, pids = run(a)
        env = child_env(croot, keys)
    except KeyboardInterrupt:
        ck.add("прервано владельцем", False)
    except Exception as e:                                     # сбой самого сценария = красная строка, а не молчаливый выход
        ck.add("сценарий smoke завершился исключением", False, "%s: %s" % (e.__class__.__name__, str(e)[:120]))
    finally:
        if croot and not a.keep:
            if env is None:
                env = child_env(croot, keys)
            shutdown(croot, keys, env, pids or platform_pids(croot), ck, llm) if llm else None
        elif llm and a.keep:
            pass
    print("\n" + ck.table())
    print("\nвременный корень: %s\nПросмотр: TB_ROOT=%s ./tb dash --port 8790 (чтение файлов корня)" % (croot, croot))
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            f.write(ck.markdown() + "\n")
    return 0 if ck.all_ok() else 1


if __name__ == "__main__":
    sys.exit(main())
