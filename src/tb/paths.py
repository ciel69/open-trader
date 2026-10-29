"""Единственное место, где определяются пути проекта.

Корень = родитель папки src (от расположения файла); переопределяется переменной окружения TB_ROOT.
Все функции принимают необязательный root (для тестов и --root в CLI) и считают путь в момент вызова.

  config/   настройки (risk_profile.json, config.json, feed_config.json, ...)
  state/    состояние и журналы (trades.jsonl, sessions.jsonl, portfolio.json, plans/, decisions.jsonl, bus/, KILL, ...)
  data/     CSV истории и тики
  runtime/  выходы фида и наблюдателей
"""
import os

DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def root():
    return os.path.abspath(os.environ.get("TB_ROOT") or DEFAULT_ROOT)


# Тестовый хук герметичности: переменные TB_STATE_DIR_CONTROL / TB_STATE_DIR_BRAIN подменяют state/control и state/brain ТОЛЬКО для корня проекта по умолчанию
# (корень не задан аргументом/TB_ROOT или равен папке проекта). Временные корни тестов не затрагиваются. Без переменных поведение прежнее (в продакшене не задаются).
_STATE_OVERRIDE = {"control": "TB_STATE_DIR_CONTROL", "brain": "TB_STATE_DIR_BRAIN"}


def _join(sub, root_dir, parts):
    base = root_dir or root()
    if sub == "state" and parts and parts[0] in _STATE_OVERRIDE and os.path.abspath(base) == os.path.abspath(DEFAULT_ROOT):
        over = os.environ.get(_STATE_OVERRIDE[parts[0]])
        if over:
            return os.path.join(os.path.abspath(over), *parts[1:])
    return os.path.join(base, sub, *parts)


def config(*parts, root=None):
    return _join("config", root, parts)


def state(*parts, root=None):
    return _join("state", root, parts)


def data(*parts, root=None):
    return _join("data", root, parts)


def runtime(*parts, root=None):
    return _join("runtime", root, parts)


def plans(*parts, root=None):
    return _join("state", root, ("plans",) + parts)


# ---- v2 (SPEC_V2): план + исполнитель. Пути только вычисляются, каталоги создают модули-владельцы лениво при записи. ----
# state/plans/PL-*/{spec.json,runtime.json,revs.jsonl}, _inbox/, _outbox/, _archive/YYYY-MM/, _counters.json  (WP4, plan_store)
# state/decisions.jsonl (+ decisions.lock)    журнал решений (WP4)
# state/bus/{events.jsonl,.lock,cursors/}     шина событий (WP6)
# state/briefs/B-*.json                       сохранённые досье (WP6)
# state/KILL                                  аварийная остановка (флаг-файл, читается при каждой проверке)
# runtime/exec_{health,snapshot}.json, levels.json, instruments.json, exec.lock   файлы исполнителя (WP5)

def plans_inbox(*parts, root=None):
    return plans("_inbox", *parts, root=root)


def plans_outbox(*parts, root=None):
    return plans("_outbox", *parts, root=root)


def plans_archive(*parts, root=None):
    return plans("_archive", *parts, root=root)


def plans_counters(root=None):
    return plans("_counters.json", root=root)


def decisions_file(root=None):
    return state("decisions.jsonl", root=root)


def decisions_lock(root=None):
    return state("decisions.lock", root=root)


def bus(*parts, root=None):
    return _join("state", root, ("bus",) + parts)


def bus_events(root=None):
    return bus("events.jsonl", root=root)


def bus_cursor(consumer, root=None):
    return bus("cursors", "%s.json" % consumer, root=root)


def briefs(*parts, root=None):
    return _join("state", root, ("briefs",) + parts)


def kill_file(root=None):
    return state("KILL", root=root)


def exec_limits_file(root=None):
    return config("exec_limits.json", root=root)


def brief_checklist_file(root=None):
    return config("brief_checklist.json", root=root)


DEFAULT_VENUE = "bybit"


def venue_suffix(venue=None):
    """Суффикс файлов исполнителя биржи: основная (bybit) и не заданная остаются с прежними именами (ничего не переименовывается), остальные получают `@<id>`."""
    return "" if venue in (None, "", DEFAULT_VENUE) else "@" + str(venue)


def exec_health_file(root=None, venue=None):
    return runtime("exec_health%s.json" % venue_suffix(venue), root=root)


def exec_snapshot_file(root=None, venue=None):
    return runtime("exec_snapshot%s.json" % venue_suffix(venue), root=root)


def exec_lock_file(root=None, venue=None):
    return runtime("exec%s.lock" % venue_suffix(venue), root=root)


def exec_state_file(root=None, venue=None):
    return runtime("exec_state%s.json" % venue_suffix(venue), root=root)


def exec_reconcile_req_file(root=None, venue=None):
    return runtime("exec_reconcile%s.req" % venue_suffix(venue), root=root)


def paper_port_file(root=None, venue=None):
    return state("paper_port%s.json" % venue_suffix(venue), root=root)


def levels_file(root=None, venue=None):
    return runtime("levels%s.json" % venue_suffix(venue), root=root)


def instruments_file(root=None, venue=None):
    return runtime("instruments%s.json" % venue_suffix(venue), root=root)


# ---- плоскость управления (WP12): state/control/{_inbox,_outbox,status.json,audit.jsonl,...} ----
def control_dir(*parts, root=None):
    return _join("state", root, ("control",) + parts)


def control_inbox(*parts, root=None):
    return control_dir("_inbox", *parts, root=root)


def control_outbox(*parts, root=None):
    return control_dir("_outbox", *parts, root=root)


def control_status_file(root=None):
    return control_dir("status.json", root=root)


def control_audit_file(root=None):
    return control_dir("audit.jsonl", root=root)


def control_config_file(root=None):
    return config("control.json", root=root)
