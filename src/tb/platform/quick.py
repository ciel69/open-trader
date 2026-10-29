"""Простой запуск платформы: `./tb start | stop | restart` (и `status` как есть). Владелец: WP11-B.

`start` = полная платформа одной командой: профиль из config/local/platform.json (иначе paper), все сервисы по умолчанию ВКЛЮЧАЯ brain, фоновый режим.
brain не краснит платформу: нет условий (подписчик api выключен, нет ключа провайдера, профиль/модель не настроены) -> сервис не запускается,
печатается одна строка «мозг выключен: причина (как включить: ...)». Профиль trade через `start` не включается (только прежний явный путь `up --profile trade --i-understand-live`).
`stop` = мягкая остановка (как `down`), `restart` = stop + start. Живая платформа: `start` пишет «уже запущена» (код 0). Замороженный (Ctrl+Z, состояние T)
супервизор/сервис размораживается SIGCONT (см. freeze.py). Ключи не читаются: проверяется только наличие файла и поля.
Коды: 0 ок, 1 сбой, 2 использование/отказ (trade).
"""
import argparse
import contextlib
import io
import os
import sys
import time

from tb import paths
from tb.core.filelock import pid_alive
from tb.platform import freeze
from tb.platform import services as S
from tb.platform import status as ST
from tb.platform import supervisor as SV

STOP_WAIT_S = 90.0 if sys.platform.startswith("win") else 30.0      # ожидание штатной остановки супервизора (exec гасится до 20 с; на Windows сервисы останавливаются по очереди дольше)
TERM_WAIT_S = 10.0          # ожидание после SIGTERM


# ---- условия мозга ----
def _has_key(key_file, key_env, environ=None):
    """Есть ли ключ провайдера: файл в каталоге ключей с полем key_env либо переменная окружения. Значение не сохраняется и не возвращается."""
    from tb.llm import keys
    env = os.environ if environ is None else environ
    if not key_file and not key_env:
        return True
    if key_file and keys._RE_FILE.match(key_file):
        for d in keys.key_dirs(env):
            try:
                with open(os.path.join(d, key_file), encoding="utf-8-sig") as f:
                    kv = keys._parse_env(f.read())
            except OSError:
                continue
            if (kv.get(key_env) if key_env else next(iter(kv.values()), None)):
                return True
    return bool(key_env and env.get(key_env))


def brain_readiness(root=None, environ=None):
    """-> (ok, причина, как_включить). Чтение конфигов и наличия ключа; сети нет, значения ключей не читаются."""
    root = root or paths.root()
    from tb.platform import dispatcher
    subs, _ = dispatcher.load_subscribers(root)
    if not any(s.get("id") == "api" and s.get("enabled") and s.get("delivery") == "api_agent" for s in subs):
        return False, "подписчик api выключен", "./tb brain enable (пишет config/local/subscriptions.json)"
    from tb.brain import conf as bconf
    c = bconf.load(root)
    team = (c.teams.get(c.live_team) or {}) if c.loaded else {}
    if not c.loaded or not c.live_team or not team.get("roles"):
        return False, "профиль/модель мозга не настроены", "config/brain.json (trading.live_team и роли команды), см. docs/BRAIN.md"
    try:
        from tb.llm import providers
        reg = providers.load(root)
    except Exception:
        return False, "реестр провайдеров не читается", "config/providers.json, проверка: ./tb llm check"
    why = None
    for ch in team["roles"].values():
        for pid in [ch["primary"]] + list(ch.get("fallback") or []):
            prof = c.profiles.get(pid)
            prov = reg.providers.get(prof.provider) if prof else None
            if prov is None or prov.error:
                why = why or ("провайдер %s неизвестен или с ошибкой" % (prof.provider if prof else pid), "config/providers.json, проверка: ./tb llm check")
            elif not prov.enabled:
                why = why or ("провайдер %s выключен" % prov.id, 'config/local/providers.json: {"providers":{"%s":{"enabled":true}}}' % prov.id)
            elif not _has_key(prov.key_file, prov.key_env, environ):
                why = why or ("нет ключа провайдера %s (файл %s)" % (prov.id, prov.key_file or "-"), "./tb setup (шаг ключа) или положить файл в каталог ключей")
            else:
                return True, "", ""
    why = why or ("нет профилей", "config/brain.json")
    return False, why[0], why[1]


def brain_line(reason, howto):
    return "мозг выключен: %s (как включить: %s)" % (reason, howto)


# ---- план запуска ----
def plan(root, profile_arg=None, services_arg=None, environ=None):
    """-> (profile, [сервисы], строка_про_мозг|None, cfg). Профиль trade -> ConfigError. Brain из набора по умолчанию отсеивается при отсутствии условий."""
    cfg = S.load_cfg(root)
    profile = profile_arg or cfg.get("profile") or "paper"
    if profile not in S.PROFILES:
        raise S.ConfigError("профиль: %s" % "|".join(S.PROFILES))
    if profile == "trade":
        raise S.ConfigError("`start` не включает профиль trade (реальные деньги). Нужны прежние явные шаги: ./tb up --profile trade --i-understand-live --detach "
                            "(после проверок docs/RUNBOOK.md и docs/TRADING.md). Без реальных денег: ./tb start --profile paper")
    note = None
    if services_arg:
        selected = S.parse_services(services_arg)
    else:
        selected = S.parse_services(",".join(S.default_services(profile, cfg)))
        if "brain" in selected:
            ok, reason, howto = brain_readiness(root, environ)
            if not ok:
                selected = [n for n in selected if n != "brain"]
                note = brain_line(reason, howto)
    return profile, selected, note, cfg


def filter_default_brain(selected, root, environ=None):
    """Для `up` без --services: тот же отсев brain. -> (selected, строка|None)."""
    if "brain" not in selected:
        return selected, None
    ok, reason, howto = brain_readiness(root, environ)
    if ok:
        return selected, None
    return [n for n in selected if n != "brain"], brain_line(reason, howto)


def next_steps(root, cfg, profile, environ=None):
    """2-3 следующих шага по состоянию (токен дашборда, ключ подписи)."""
    from tb.control import auth
    st = auth.status(environ)
    steps = []
    if not st["key"] or not st["token"]:
        steps.append("не хватает ключа/токена входа: выполни ./tb setup (один раз), затем ./tb start")
    link = S.link_of("dash", cfg)
    steps.append("открой %s (для paper вход без токена; токен нужен только для опасных действий: ./tb control token new)" % link)
    steps.append("нажми «Старт» в дашборде (профиль %s); состояние: ./tb status, остановка: ./tb stop" % profile)
    return steps


def summary(root, cfg, profile, selected, note, head, out=print, environ=None):
    out(head)
    out("профиль: %s; сервисы: %s" % (profile, ", ".join(selected)))
    if note:
        out(note)
    out("дашборд: %s" % (S.link_of("dash", cfg) or "(сервис dash не в наборе)"))
    for i, s in enumerate(next_steps(root, cfg, profile, environ), 1):
        out("  %d. %s" % (i, s))


# ---- остановка ----
def stop_platform(root, timeout=STOP_WAIT_S, out=print, state_fn=None, cont_fn=None, cmd_fn=None, term_fn=None, sleep=time.sleep):
    """Мягкая остановка: разморозка, stop.req, ожидание, при неудаче SIGTERM и ясная инструкция. -> 0 остановлена / 1 не удалось."""
    thawed, left = freeze.thaw(root, out=out, state_fn=state_fn, cont_fn=cont_fn, cmd_fn=cmd_fn, sleep=sleep)
    if left:
        out(freeze.manual_hint(left))
        return 1
    sup = ST.supervisor_info(root)
    pos = 0
    try:
        pos = ST.build_status(root)["exec"].get("positions") or 0
    except Exception:
        pass
    if sup.get("running"):
        pid = sup["pid"]
        S.write_json(S.stop_req_file(root), {"ts": time.time()})
        if not _wait_gone(pid, timeout, sleep):
            out("супервизор не остановился за %d с: отправляю SIGTERM" % timeout)
            if not (term_fn or freeze.term)(pid):
                out("сигнал не отправлен: pid %s не опознан как процесс проекта (python -m tb.…)" % pid)
            if not _wait_gone(pid, TERM_WAIT_S, sleep):
                out("Не удалось остановить супервизор (pid %s). Вручную: `kill -CONT %s; kill -TERM %s` (Windows: завершить процесс в Диспетчере задач), затем ./tb start." % (pid, pid, pid))
                return 1
    n = _soft_orphans(root, out, state_fn, cont_fn, cmd_fn, term_fn, sleep)
    if n is None:
        return 1
    if not pid_alive(int(sup.get("pid") or 0)):
        try:
            os.remove(S.lock_file(root))
        except OSError:
            pass
    out(("платформа остановлена" if sup.get("running") else "платформа не запущена") + ("; зачищено сирот: %d" % n if n else ""))
    if sup.get("running") and pos:
        out("НАПОМИНАНИЕ: открытых позиций %s. Исполнитель остановлен, позиции защищены биржевым стопом, но не ведутся: проверь вручную." % pos)
    return 0


def _wait_gone(pid, timeout, sleep=time.sleep):
    end = time.time() + timeout
    while time.time() < end:
        if not pid_alive(pid):
            return True
        sleep(0.3)
    return not pid_alive(pid)


def _soft_orphans(root, out, state_fn, cont_fn, cmd_fn, term_fn, sleep):
    """Остатки сервисов после остановки супервизора: только CONT/TERM своим процессам. -> число или None (не удалось)."""
    killed, stuck = 0, []
    for name, pid in freeze.platform_pids(root):
        if name == "supervisor":
            continue
        if not freeze.is_ours(pid, cmd_fn):
            out("pid %s (%s) жив, но не похож на наш сервис: не трогаю" % (pid, name))
            continue
        out("зачистка сироты %s (pid %s)" % (name, pid))
        freeze.thaw(root, out=lambda m: None, state_fn=state_fn, cont_fn=cont_fn, cmd_fn=cmd_fn, sleep=sleep, wait_s=1.0)
        (term_fn or freeze.term)(pid)
        if _wait_gone(pid, TERM_WAIT_S, sleep):
            killed += 1
        else:
            stuck.append(pid)
    for fn in ([n for n in os.listdir(S.pdir(root)) if n.endswith(".pid")] if os.path.isdir(S.pdir(root)) else []):
        d = S.read_json(os.path.join(S.pdir(root), fn))
        if not (isinstance(d, dict) and d.get("pid") and pid_alive(int(d["pid"]))):
            try:
                os.remove(os.path.join(S.pdir(root), fn))
            except OSError:
                pass
    if stuck:
        out("Не завершились сервисы (pid %s). Вручную: `kill -TERM %s`, затем ./tb start." % (" ".join(map(str, stuck)), " ".join(map(str, stuck))))
        return None
    return killed


# ---- порт дашборда ----
def _port_busy(port, host="127.0.0.1"):
    """True, если на host:port уже кто-то слушает (проба соединением; ничего не отправляется)."""
    import socket
    try:
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.settimeout(0.5)
            return s.connect_ex((host, int(port))) == 0
    except (OSError, ValueError):
        return False


def set_dash_port(root, port):
    """Сохраняет порт дашборда в config/local/platform.json (svc.dash.port; личный слой, поставляемый файл не меняется)."""
    from tb.core import localcfg

    def fn(cur):
        cur.setdefault("svc", {}).setdefault("dash", {})["port"] = int(port)
        return cur
    localcfg.update_local("platform", fn, root)


# ---- команды ----
def _parser(prog, with_start=True):
    ap = argparse.ArgumentParser(prog=prog)
    if with_start:
        ap.add_argument("--profile", choices=S.PROFILES, default=None, help="paper (по умолчанию) или testnet; trade через start не включается")
        ap.add_argument("--services", default=None, help="для продвинутых: свой список сервисов через запятую")
        ap.add_argument("--port", type=int, default=None, help="порт дашборда (1024-65535); сохраняется в config/local/platform.json")
    ap.add_argument("--timeout", type=float, default=STOP_WAIT_S, help="ожидание остановки, с")
    ap.add_argument("--root", default=None)
    return ap


def _parse(ap, argv):
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return None
    if a.root:
        os.environ["TB_ROOT"] = os.path.abspath(a.root)
    return a


def start_platform(root, profile_arg=None, services_arg=None, out=print, err=None, environ=None, state_fn=None, cont_fn=None, cmd_fn=None, sleep=time.sleep,
                   port=None, port_busy_fn=None):
    err = err or (lambda m: print(m, file=sys.stderr))
    if port is not None:
        if not 1024 <= int(port) <= 65535:
            err("порт дашборда: число 1024-65535")
            return 2
    try:
        profile, selected, note, cfg = plan(root, profile_arg, services_arg, environ)
    except S.ConfigError as e:
        err(str(e))
        return 2
    thawed, left = freeze.thaw(root, out=out, state_fn=state_fn, cont_fn=cont_fn, cmd_fn=cmd_fn, sleep=sleep)
    if left:
        err("Платформа заморожена и не размораживается. " + freeze.manual_hint(left))
        return 1
    sup = ST.supervisor_info(root)
    if sup.get("running"):
        prof = sup.get("profile") or profile
        svc = sup.get("services") or selected
        summary(root, cfg, prof, svc, None, "платформа уже запущена (pid %s)%s" % (sup["pid"], "; разморожена" if thawed else ""), out, environ)
        return 0
    if port is not None:
        try:
            set_dash_port(root, port)
        except (ValueError, OSError) as e:
            err("порт не сохранён: %s" % e)
            return 2
        cfg = S.load_cfg(root)
        out("порт дашборда %d сохранён в config/local/platform.json" % int(port))
    if "dash" in selected:
        dport = int(((cfg.get("svc") or {}).get("dash") or {}).get("port") or 8765)
        if (port_busy_fn or _port_busy)(dport):
            err("порт дашборда %d занят другой программой, платформа не запущена. Запустите с другим портом: ./tb start --port %d "
                "(порт запомнится) или впишите {\"svc\": {\"dash\": {\"port\": %d}}} в config/local/platform.json." % (dport, dport + 10, dport + 10))
            return 1
    try:       # первый запуск: ключ подписи и токен управления создаются сами (существующие не трогаются); токен печатается один раз
        from tb.control import cli as control_cli
        control_cli.ensure_auth(out)
    except Exception:
        pass
    buf = io.StringIO()
    argv = ["--profile", profile, "--services", ",".join(selected)]
    with contextlib.redirect_stdout(buf):
        rc = SV._detach(argv, root)
    if rc != 0:
        err("не удалось запустить платформу (код %s). Подробности: ./tb logs platform" % rc)
        return rc or 1
    summary(root, cfg, profile, selected, note, "платформа запущена в фоне (pid %s)" % ST.supervisor_info(root).get("pid"), out, environ)
    return 0


def main_start(argv):
    a = _parse(_parser("tb start"), argv)
    if a is None:
        return 2
    return start_platform(paths.root(), a.profile, a.services, port=a.port)


def main_stop(argv):
    a = _parse(_parser("tb stop", with_start=False), argv)
    if a is None:
        return 2
    return stop_platform(paths.root(), a.timeout)


def main_restart(argv):
    a = _parse(_parser("tb restart"), argv)
    if a is None:
        return 2
    root = paths.root()
    try:
        plan(root, a.profile, a.services)         # отказ (trade) до остановки: рабочую платформу не трогаем
    except S.ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    cur = ST.supervisor_info(root)
    if cur.get("running") and cur.get("profile") == "trade":
        print("запущена платформа trade (реальные деньги): restart её не перезапускает. Остановка: ./tb stop; запуск: ./tb up --profile trade --i-understand-live --detach", file=sys.stderr)
        return 2
    rc = stop_platform(root, a.timeout)
    if rc != 0:
        return rc
    return start_platform(root, a.profile, a.services, port=a.port)
