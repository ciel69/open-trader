"""Супервизор платформы: `./tb up | down | status | logs`. Владелец: WP11-B.

`up [--profile paper|testnet|trade] [--observe] [--services exec,scan,dash,dispatch,mcp,brain] [--max-seconds N] [--detach]`
  поднимает сервисы дочерними процессами, пишет pid-файлы и status.json (runtime/platform/), перезапускает упавшие с экспоненциальной паузой
  и потолком попыток, ведёт логи с ротацией (runtime/logs/<сервис>.log), по `down`/Ctrl+C останавливает всех. Вторая копия платформы на одном
  корне отказана блокировкой (код 4). `--profile trade` только с `--i-understand-live` и без файла state/KILL.
  Печать файлов автозапуска (ничего не устанавливается): `--print-launchd`, `--print-windows`.
`down [--timeout N]` останавливает платформу (файл-запрос stop.req, затем сигнал, затем зачистка сирот).
`status [--json|--line]` сводка. `logs [сервис] [-n N] [--follow]`.
Коды: 0 ок, 1 сбой/платформа не запущена (status), 2 использование, 4 вторая копия.
"""
import argparse
import json
import os
import signal
import sys
import time

from tb import paths
from tb.core import exec_cfg
from tb.core.filelock import pid_alive
from tb.platform import logs as L
from tb.platform import procs, reconf
from tb.platform import services as S
from tb.platform import status as ST

EXIT_ALREADY = 4
FATAL_TEXT = {2: "ошибка запуска или не реализовано (код 2)", 4: "второй экземпляр на этом корне (код 4)"}


class Service(object):
    def __init__(self, name, cmd, env):
        self.name, self.cmd, self.env = name, cmd, env
        self.state = "stopped"            # stopped|running|backoff|failed|unavailable
        self.proc = None
        self.reader = None
        self.log = None
        self.started = None               # wall clock старта текущего процесса
        self.tries = 0                    # сбоев подряд (обнуляется после stable_s работы)
        self.restarts = 0                 # всего перезапусков за сессию
        self.next_start = None
        self.failed_ts = None
        self.last_exit = None
        self.last_error = None
        self.last_down_event = None
        self.hb_ok = None
        self.hb_age = None
        self.fatal = False

    def pid(self):
        return self.proc.pid if self.proc is not None and self.proc.poll() is None else None

    def to_dict(self, now):
        run = self.state == "running"
        return {"state": self.state, "pid": self.pid(), "started_ts": self.started if run else None,
                "uptime_s": round(now - self.started, 1) if run and self.started else 0, "restarts": self.restarts, "tries": self.tries,
                "last_exit": self.last_exit, "last_error": self.last_error, "hb_ok": self.hb_ok,
                "hb_age_s": None if self.hb_age is None else round(self.hb_age, 1)}


def acquire_lock(root, info):
    """runtime/platform/supervisor.lock (O_EXCL + pid). Мёртвый владелец снимается. -> (ok, info владельца)."""
    path = S.lock_file(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            cur = S.read_json(path)
            cur = cur if isinstance(cur, dict) else {}
            pid = int(cur.get("pid") or 0)
            if pid and pid_alive(pid):
                return False, cur
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        return True, info
    return False, {}


def release_lock(root):
    try:
        cur = S.read_json(S.lock_file(root)) or {}
        if int(cur.get("pid") or 0) == os.getpid():
            os.remove(S.lock_file(root))
    except OSError:
        pass


class Platform(object):
    def __init__(self, root, profile, observe, selected, cfg, base_env=None, cmds=None, clock=time.time, sleep=time.sleep, emit=None, out=None,
                 spawner=None):
        self.root, self.profile, self.observe = root, profile, observe
        self.selected = list(selected)
        self.cfg = cfg
        sv = cfg["supervisor"]
        self.sv = sv
        self.clock, self.sleep = clock, sleep
        self.out = out
        self.spawner = spawner or procs.spawn
        self.emit = emit or self._emit_bus
        self.stop_flag = False
        self.started = None
        self.plog = L.RotatingLog(S.log_file("platform", root), sv["log"]["max_bytes"], sv["log"]["keep"])
        base_env = dict(os.environ if base_env is None else base_env)
        self.svcs = {}
        for n in self.selected:
            cmd = cmds[n] if cmds and n in cmds else S.service_cmd(n, profile, observe, cfg)
            self.svcs[n] = Service(n, cmd, S.child_env(n, base_env, root, self.selected, profile))
        self._positions = 0
        self._last_status = 0.0

    # ---- вывод и события
    def say(self, msg):
        self.plog.write(msg)
        if self.out is not None:
            try:
                self.out.write(msg + "\n")
                self.out.flush()
            except (OSError, ValueError):
                pass

    def _emit_bus(self, type_, d, **kw):
        from tb.core import bus
        return bus.emit(type_, d, root=self.root, **kw)

    def _down_event(self, svc, code):
        now = self.clock()
        rep = self.sv["down_event_repeat_s"]
        if svc.last_down_event is not None and now - svc.last_down_event < rep:
            return
        svc.last_down_event = now
        d = {"service": svc.name, "tries": svc.tries, "code": code}
        if svc.name == "exec":
            d["positions"] = self._positions
        try:
            self.emit("service_down", d)
        except Exception as e:
            self.say("не удалось записать service_down в шину: %s" % type(e).__name__)

    # ---- жизненный цикл сервисов
    def start(self, svc):
        if svc.cmd is None:
            svc.state = "unavailable"
            svc.last_error = "модуль сервиса не найден (его делает другой пакет работ)"
            self.say("%s: недоступен: %s" % (svc.name, svc.last_error))
            return False
        if svc.log is None:
            svc.log = L.RotatingLog(S.log_file(svc.name, self.root), self.sv["log"]["max_bytes"], self.sv["log"]["keep"])
        try:
            svc.proc = self.spawner(svc.cmd, svc.env, self.root)
        except Exception as e:
            svc.proc = None
            svc.last_error = "не запустился: %s" % type(e).__name__
            self._crashed(svc, None, "spawn_failed")
            return False
        svc.started = self.clock()
        svc.state = "running"
        svc.hb_ok, svc.hb_age, svc.next_start, svc.fatal = None, None, None, False
        if getattr(svc.proc, "stdout", None) is not None:
            svc.reader = procs.LineReader(svc.proc, svc.log)
        try:
            S.write_json(S.pid_file(svc.name, self.root), {"pid": svc.proc.pid, "started": svc.started, "supervisor": os.getpid()})
        except OSError:
            pass
        self.say("%s: запущен pid %s" % (svc.name, svc.proc.pid))
        return True

    def _backoff_delay(self, tries):
        r = self.sv["restart"]
        return min(float(r["max_s"]), float(r["base_s"]) * float(r["factor"]) ** max(0, tries - 1))

    def _crashed(self, svc, rc, code):
        """Процесс умер (или не запустился): решаем, перезапускать ли."""
        now = self.clock()
        uptime = (now - svc.started) if svc.started else 0.0
        if svc.proc is not None:
            svc.last_exit = rc
        if uptime >= self.sv["stable_s"]:
            svc.tries = 0
        svc.proc = None
        self._rm_pid(svc)
        if rc in self.sv["fatal_codes"] and uptime < self.sv["fatal_within_s"]:
            svc.state, svc.fatal, svc.failed_ts = "failed", True, now
            svc.last_error = FATAL_TEXT.get(rc, "код %s" % rc)
            svc.tries += 1
            self.say("%s: %s: не перезапускаю" % (svc.name, svc.last_error))
            self._down_event(svc, "exit_%s" % rc)
            return
        svc.tries += 1
        svc.restarts += 1
        if svc.tries > int(self.sv["restart"]["max_tries"]):
            svc.state, svc.failed_ts = "failed", now
            self.say("%s: сдался после %d сбоев подряд (%s)" % (svc.name, svc.tries, svc.last_error))
            self._down_event(svc, code)
            return
        svc.state = "backoff"
        delay = self._backoff_delay(svc.tries)
        svc.next_start = now + delay
        self.say("%s: упал (%s), перезапуск через %.1f с (попытка %d/%d)" % (svc.name, code, delay, svc.tries, self.sv["restart"]["max_tries"]))

    def _rm_pid(self, svc):
        try:
            os.remove(S.pid_file(svc.name, self.root))
        except OSError:
            pass

    def _check_hb(self, svc, now):
        sc = (self.cfg.get("svc") or {}).get(svc.name) or {}
        kind = sc.get("hb", "none")
        if kind == "none" or svc.started is None:
            svc.hb_ok, svc.hb_age = None, None
            return
        stale = float(sc.get("hb_stale_s") or 60.0)
        grace = float(sc.get("grace_s") or 60.0)
        age, ok_pid = None, True
        if kind == "exec":
            from tb.brain import common
            h = common.exec_health(root=self.root, now=now)
            age = h["age_s"] if h["present"] else None
            if h["present"] and isinstance(h["data"].get("positions"), int) and not isinstance(h["data"].get("positions"), bool):
                self._positions = h["data"]["positions"]
            try:                                      # файл от прошлого запуска не считается heartbeat'ом нового процесса
                if os.stat(paths.exec_health_file(root=self.root)).st_mtime < svc.started - 1.0:
                    age = None
            except OSError:
                age = None
        else:
            age, d = S.read_hb(svc.name, self.root, now)
            if age is not None and d.get("pid") not in (None, svc.pid()):
                age = None
        svc.hb_age = age
        if now - svc.started < grace:
            svc.hb_ok = None if age is None else age <= stale
            return
        svc.hb_ok = age is not None and age <= stale and ok_pid

    def tick(self):
        now = self.clock()
        for svc in self.svcs.values():
            if svc.state == "running":
                rc = svc.proc.poll() if svc.proc is not None else -1
                if rc is not None:
                    self._finish_reader(svc)
                    svc.last_error = "упал, код %s" % rc
                    self._crashed(svc, rc, "exit_%s" % rc)
                    continue
                if svc.tries and now - svc.started >= self.sv["stable_s"]:
                    svc.tries = 0
                self._check_hb(svc, now)
                if svc.hb_ok is False:
                    svc.last_error = "heartbeat устарел (%s с): перезапуск" % (round(svc.hb_age) if svc.hb_age is not None else "нет")
                    self.say("%s: %s" % (svc.name, svc.last_error))
                    procs.terminate(svc.proc, 3.0)
                    self._finish_reader(svc)
                    self._crashed(svc, svc.proc.poll() if svc.proc else None, "hb_stale")
            elif svc.state == "backoff" and svc.next_start is not None and now >= svc.next_start:
                self.start(svc)
            elif svc.state == "failed" and not svc.fatal:
                r = float(self.sv["retry_after_giveup_s"] or 0)
                if r > 0 and svc.failed_ts is not None and now - svc.failed_ts >= r:
                    self.say("%s: повторная попытка после паузы" % svc.name)
                    svc.tries = 0
                    self.start(svc)

    def _finish_reader(self, svc):
        if svc.reader is not None:
            svc.reader.join(1.0)
            svc.reader = None

    # ---- статус
    def write_status(self, final=False):
        now = self.clock()
        d = {"ts_epoch": round(now, 3), "pid": os.getpid(), "profile": self.profile, "observe": self.observe, "running": not final,
             "services": {n: s.to_dict(now) for n, s in self.svcs.items()}, "positions_seen": self._positions}
        try:
            S.write_json(S.status_file(self.root), d)
        except OSError:
            pass
        self._last_status = now

    # ---- запуск и остановка
    def start_all(self):
        self.started = self.clock()
        for n in self.selected:
            self.start(self.svcs[n])
        self.write_status()

    def stop_all(self):
        self.say("остановка сервисов: %s" % ", ".join(S.stop_order(self.selected)))
        for n in S.stop_order(self.selected):
            svc = self.svcs[n]
            if svc.proc is not None and svc.proc.poll() is None:
                grace = self.sv["exec_stop_grace_s"] if n == "exec" else self.sv["stop_grace_s"]
                rc = procs.terminate(svc.proc, grace)
                svc.last_exit = rc
                self.say("%s: остановлен (код %s)" % (n, rc))
            self._finish_reader(svc)
            if svc.log is not None:
                svc.log.close()
            svc.state, svc.proc = "stopped", None
            self._rm_pid(svc)
        self.write_status(final=True)

    def request_stop(self):
        self.stop_flag = True

    def _stop_requested(self):
        if self.stop_flag:
            return True
        p = S.stop_req_file(self.root)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
            self.say("получен запрос остановки (./tb down)")
            return True
        return False

    def run(self, max_seconds=None, handle_signals=True):
        old = {}
        if handle_signals:
            for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                sig = getattr(signal, name, None)
                if sig is None:
                    continue
                try:
                    old[sig] = signal.signal(sig, lambda *_: self.request_stop())
                except (ValueError, OSError):
                    pass
        try:
            self.start_all()
            t0 = self.clock()
            while not self._stop_requested():
                if max_seconds is not None and self.clock() - t0 >= max_seconds:
                    self.say("достигнут --max-seconds")
                    break
                self.tick()
                reconf.handle(self)               # запрос ctl на смену профиля исполнителя (WP12)
                if self.clock() - self._last_status >= self.sv["status_s"]:
                    self.write_status()
                self.sleep(self.sv["poll_s"])
        except KeyboardInterrupt:
            self.say("Ctrl+C")
        finally:
            self.stop_all()
            for sig, h in old.items():
                try:
                    signal.signal(sig, h)
                except (ValueError, OSError):
                    pass
        return 0


# ------------------------------------------------------------------ CLI
def _err(msg):
    print(msg, file=sys.stderr)


def cleanup_orphans(root, out=print):
    """Сироты предыдущей (упавшей) платформы: pid-файлы сервисов с живым процессом нашего проекта. POSIX: проверка командной строки; Windows: не трогаем."""
    pdir = S.pdir(root)
    try:
        names = [n for n in os.listdir(pdir) if n.endswith(".pid") and n != "supervisor.pid"]
    except OSError:
        return 0
    killed = 0
    for fn in names:
        d = S.read_json(os.path.join(pdir, fn))
        pid = int((d or {}).get("pid") or 0) if isinstance(d, dict) else 0
        path = os.path.join(pdir, fn)
        if pid and pid != os.getpid() and pid_alive(pid):
            cmd = procs.cmdline_of(pid)
            if cmd and ("tb.launcher" in cmd or "tb.platform" in cmd):
                out("зачистка сироты %s (pid %s)" % (fn[:-4], pid))
                if procs.kill_pid(pid):
                    killed += 1
            else:
                out("pid %s (%s) жив, но не похож на наш сервис: не трогаю" % (pid, fn[:-4]))
        try:
            os.remove(path)
        except OSError:
            pass
    return killed


def _up_parser():
    ap = argparse.ArgumentParser(prog="tb up", description="запуск платформы (сервисы под супервизором)")
    ap.add_argument("--profile", choices=S.PROFILES, default=None)
    ap.add_argument("--observe", action="store_true", help="исполнитель только наблюдает (ордеров нет)")
    ap.add_argument("--services", default=None, help="через запятую: " + ",".join(S.SERVICE_NAMES))
    ap.add_argument("--i-understand-live", action="store_true", dest="live_ok", help="обязателен для --profile trade")
    ap.add_argument("--max-seconds", type=float, default=None, dest="max_seconds", help="самостоятельная остановка (для проверок)")
    ap.add_argument("--detach", action="store_true", help="уйти в фон (платформа живёт без терминала)")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run", help="только показать, что поднимется")
    ap.add_argument("--print-launchd", action="store_true", dest="print_launchd", help="напечатать plist автозапуска macOS (не устанавливается)")
    ap.add_argument("--print-windows", action="store_true", dest="print_windows", help="напечатать PowerShell-скрипт Планировщика Windows (не запускается)")
    ap.add_argument("--root", default=None, help="корень проекта (по умолчанию TB_ROOT)")
    return ap


def plan_of(a, cfg):
    """-> (profile, [services]) или ConfigError."""
    profile = a.profile or cfg.get("profile") or "paper"
    if profile not in S.PROFILES:
        raise S.ConfigError("профиль: %s" % "|".join(S.PROFILES))
    selected = S.parse_services(a.services) if a.services else S.parse_services(",".join(S.default_services(profile, cfg)))
    return profile, selected


def check_live(profile, live_ok, root):
    """Профиль trade: только с явным флагом и при чистом KILL. -> текст отказа или None."""
    if profile != "trade":
        return None
    if not live_ok:
        return "профиль trade (МАЙНЕТ, реальные деньги) запускается только с --i-understand-live"
    k = exec_cfg.kill_state(root)
    if k is not None:
        return "отказ: state/KILL не чист (flatten=%s, причина: %s). Сними его `./tb exec resume`, когда будешь готов" % (k.get("flatten"), str(k.get("reason"))[:60])
    return None


def describe(profile, observe, selected, cfg, root):
    lines = ["платформа: профиль %s%s, корень %s" % (profile, " (observe: исполнитель без ордеров)" if observe else "", root),
             "сервисы: %s" % ", ".join(selected)]
    if profile == "trade":
        lines.append("ВНИМАНИЕ: профиль trade = реальные деньги на Bybit mainnet")
    for n in selected:
        lk = S.link_of(n, cfg)
        if lk:
            lines.append("  %s: %s" % (n, lk))
    return lines


def _ensure_control_auth():
    """Первый запуск: ключ подписи и токен управления создаются сами (существующие не трогаются); токен печатается один раз."""
    try:
        from tb.control import cli as control_cli
        control_cli.ensure_auth()
    except Exception:
        pass


def main_up(argv):
    ap = _up_parser()
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if a.root:
        os.environ["TB_ROOT"] = os.path.abspath(a.root)
    root = paths.root()
    if a.print_launchd or a.print_windows:
        from tb.platform import autostart
        passthru = [x for x in argv if x not in ("--print-launchd", "--print-windows")]
        print(autostart.launchd_plist(root, passthru) if a.print_launchd else autostart.windows_script(root, passthru))
        return 0
    cfg = S.load_cfg(root)
    try:
        profile, selected = plan_of(a, cfg)
    except S.ConfigError as e:
        _err(str(e))
        return 2
    why = check_live(profile, a.live_ok, root)
    if why:
        _err(why)
        return 2
    brain_note = None
    if not a.services:
        from tb.platform import quick
        selected, brain_note = quick.filter_default_brain(selected, root)       # brain без условий не запускается и не краснит платформу
    for ln in describe(profile, a.observe, selected, cfg, root):
        print(ln)
    if brain_note:
        print(brain_note)
    if cfg.get("_warn"):
        print("предупреждение: " + cfg["_warn"])
    if a.dry_run:
        for n in selected:
            print("  %s: %s" % (n, " ".join(S.service_cmd(n, profile, a.observe, cfg) or ["<недоступен>"])))
        return 0
    _ensure_control_auth()
    if a.detach:
        return _detach(argv, root)
    info = {"pid": os.getpid(), "started": time.time(), "profile": profile, "observe": a.observe, "services": selected, "root": root}
    ok, cur = acquire_lock(root, info)
    if not ok:
        _err("платформа уже запущена (pid %s, профиль %s): вторая копия отказана. Остановить: ./tb down" % (cur.get("pid"), cur.get("profile")))
        from tb.platform import freeze
        if freeze.frozen(root):
            _err("процесс платформы приостановлен (Ctrl+Z?): ./tb start или ./tb stop разморозят его")
        return EXIT_ALREADY
    try:
        try:
            os.remove(S.stop_req_file(root))
        except OSError:
            pass
        cleanup_orphans(root, out=lambda m: print(m))
        pf = Platform(root, profile, a.observe, selected, cfg, out=sys.stdout)
        print("запущено; статус: ./tb status, логи: ./tb logs, остановка: ./tb down или Ctrl+C")
        return pf.run(max_seconds=a.max_seconds)
    finally:
        release_lock(root)


def _detach(argv, root):
    import subprocess
    rest = [x for x in argv if x != "--detach"]
    cmd = [sys.executable, "-m", "tb.launcher", "up"] + rest
    env = S.child_env("platform", os.environ, root)
    env.pop("TB_PLATFORM_SERVICE", None)
    kw = procs.popen_kwargs(detached=True)
    os.makedirs(S.logs_dir(root), exist_ok=True)
    proc = subprocess.Popen(cmd, env=env, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **kw)
    end = time.time() + 12.0
    while time.time() < end:
        sup = ST.supervisor_info(root)
        if sup.get("running") and sup.get("pid") == proc.pid:
            print("платформа в фоне, pid %s. Статус: ./tb status; остановка: ./tb down" % proc.pid)
            return 0
        if proc.poll() is not None:
            _err("фоновый запуск завершился с кодом %s (см. ./tb logs platform или повтори без --detach)" % proc.returncode)
            return proc.returncode or 1
        time.sleep(0.2)
    _err("не дождался запуска платформы за 12 с (см. ./tb logs platform)")
    return 1


def main_down(argv):
    ap = argparse.ArgumentParser(prog="tb down")
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--root", default=None)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if a.root:
        os.environ["TB_ROOT"] = os.path.abspath(a.root)
    root = paths.root()
    from tb.platform import freeze
    _, left = freeze.thaw(root, out=print)            # приостановленный (Ctrl+Z) супервизор не прочтёт stop.req: сначала SIGCONT
    if left:
        _err(freeze.manual_hint(left))
        return 1
    sup = ST.supervisor_info(root)
    pos = 0
    try:
        st = ST.build_status(root)
        pos = st["exec"].get("positions") or 0
    except Exception:
        pass
    if not sup.get("running"):
        n = cleanup_orphans(root, out=print)
        try:
            os.remove(S.lock_file(root))
        except OSError:
            pass
        print("платформа не запущена" + ("; зачищено сирот: %d" % n if n else ""))
        return 0
    pid = sup["pid"]
    S.write_json(S.stop_req_file(root), {"ts": time.time()})
    end = time.time() + a.timeout
    while time.time() < end and pid_alive(pid):
        time.sleep(0.3)
    if pid_alive(pid):
        _err("супервизор не остановился за %d с: прошу завершиться (SIGTERM)" % a.timeout)
        freeze.term(pid)
        end = time.time() + 10.0
        while time.time() < end and pid_alive(pid):
            time.sleep(0.3)
        if pid_alive(pid):
            _err("Не удалось остановить супервизор (pid %s). Вручную: `kill -CONT %s; kill -TERM %s` (Windows: завершить процесс в Диспетчере задач), затем ./tb start." % (pid, pid, pid))
            return 1
    n = cleanup_orphans(root, out=print)
    release_stale = S.lock_file(root)
    if not pid_alive(pid):
        try:
            os.remove(release_stale)
        except OSError:
            pass
    print("платформа остановлена" + ("; зачищено сирот: %d" % n if n else ""))
    if pos:
        print("НАПОМИНАНИЕ: открытых позиций %s. Исполнитель остановлен, позиции защищены биржевым стопом, но не ведутся: проверь вручную." % pos)
    return 0


def main_status(argv):
    ap = argparse.ArgumentParser(prog="tb status")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--line", action="store_true", help="одна короткая строка (для статусной строки Claude Code)")
    ap.add_argument("--root", default=None)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if a.root:
        os.environ["TB_ROOT"] = os.path.abspath(a.root)
    st = ST.build_status(paths.root())
    if a.json:
        print(json.dumps(st, ensure_ascii=False))
    elif a.line:
        print(ST.format_line(st))
    else:
        print(ST.format_text(st))
    return 0 if st["running"] else 1


def main_logs(argv):
    ap = argparse.ArgumentParser(prog="tb logs")
    ap.add_argument("service", nargs="?", default="platform", help="platform|" + "|".join(S.SERVICE_NAMES))
    ap.add_argument("-n", type=int, default=50)
    ap.add_argument("--follow", "-f", action="store_true")
    ap.add_argument("--root", default=None)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if a.root:
        os.environ["TB_ROOT"] = os.path.abspath(a.root)
    if a.service != "platform" and a.service not in S.SERVICE_NAMES:
        _err("неизвестный сервис: %s" % a.service)
        return 2
    path = S.log_file(a.service, paths.root())
    for ln in L.tail(path, a.n):
        print(ln)
    if a.follow:
        try:
            L.follow(path, sys.stdout)
        except KeyboardInterrupt:
            pass
    return 0
