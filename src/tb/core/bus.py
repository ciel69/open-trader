"""Шина событий `state/bus/events.jsonl` (03_brain.md п. 3.3). Владелец: WP6.

Append-only, `seq` монотонный (блокировка `state/bus/.lock`), ротация `events-YYYYMMDD.jsonl` (по размеру и по смене суток), срок хранения
`events.retention_days` (14). Курсоры: `state/bus/cursors/<consumer>.json` = {"delivered":N,"acked":M,"ts":..} (атомарная замена).
Писатели: исполнитель, `exec halt`. Читатели: `./tb events`, `./tb brief`.
Конверт: {"seq":1843,"ts":"2026-10-28T10:15:02Z","type":"milestone","pri":"wake","plan":"PL-...","sym":"SOL","d":{...}}.
Поля `d`: только число / bool / строка из белого списка перечислений / идентификатор `^[A-Za-z0-9_.-]{1,40}$`; свободный текст
третьих лиц отбрасывается при записи (`sanitize`); символы `^[A-Z0-9]{2,20}$`; длина строки события <= 400 байт.

Гарантии:
  - события не теряются: запись одним `write` под блокировкой, `seq` = последний в файле + 1; обрыв записи оставляет «хвост» без `\\n`,
    следующая запись начинается с `\\n`, битая строка при чтении пропускается;
  - доставка at-least-once: курсор `delivered` (что напечатано) и `acked` (что обработал мозг) отдельно; повторная доставка делается читателем;
  - дедупликация на записи: веха плана (`milestone` с тем же `plan` и `name`) не повторяется, `connectivity` (канал+состояние) не чаще раза в 30 с.
Модуль самодостаточен (только `tb.paths` и `tb.fsutil`): не зависит от других пакетов v2 и не трогает биржевой код.
"""
import json
import math
import os
import re
import time

from tb import fsutil, paths

PRI_WAKE = "wake"      # будит мозг (чат: строка в `events --follow`; API: триаж)
PRI_INFO = "info"      # только очередь и brief.recent_events
PRIORITIES = (PRI_WAKE, PRI_INFO)

# тип события -> приоритет по умолчанию (None = зависит от содержимого, см. таблицу в 03 п. 3.3)
EVENT_TYPES = {
    "scanner_signal": None,        # wake, если у источника wake:true
    "plan_armed": PRI_WAKE,
    "plan_rejected": PRI_WAKE,
    "trigger_fired": PRI_WAKE,
    "entry_blocked": PRI_WAKE,
    "entry_filled": PRI_WAKE,
    "milestone": None,             # wake: r_plus_1, r_minus_05, near_target, target_filled; прочие info
    "rule_fired": PRI_INFO,
    "stop_moved": None,            # info; wake, если ручная команда не от мозга
    "position_closed": PRI_WAKE,
    "plan_ended": PRI_WAKE,
    "action_rejected": PRI_WAKE,
    "brain_wake_request": PRI_WAKE,
    "limit_hit": PRI_WAKE,
    "kill": PRI_WAKE,
    "connectivity": PRI_WAKE,      # wake при down_s >= 20 и на восстановление; не чаще раза в 30 с на канал
    "reconcile_alert": PRI_WAKE,
    "session_ending": PRI_WAKE,
    "error": PRI_WAKE,
    "exec_started": PRI_WAKE,
    "exec_stopped": PRI_WAKE,
    "exec_down": PRI_WAKE,         # синтетика `events --follow`, в файл не пишется
    "service_down": PRI_WAKE,      # WP11-B: супервизор платформы сдался (сервис не поднимается N раз подряд)
    "macro_blackout_soon": PRI_WAKE,   # WP13a: скоро макро-событие (FOMC/CPI/NFP/...), окно blackout; только идентификатор, тип, минуты
    "risk_level_changed": PRI_WAKE,    # WP16: уровень риска сменён (владелец или авто-понижение); from/to 1..5
    "control_cmd": PRI_INFO,           # WP12: команда плоскости управления выполнена/отклонена (wake для действий live); только action, code, src, mode
    "day_report": PRI_WAKE,            # WP14: отчёт дня (старт / итог / аварийный): kind, net, carried_n, questions_n, verdict
    "brain_swap": PRI_INFO,            # WP11-C2/интеграция: смена модели/команды мозга или запасной профиль: kind, from, to, rev, by
    "day_state": PRI_INFO,             # WP14: переход состояния дня (stopped/starting/running/closing/halted)
}
WAKE_MILESTONES = ("r_plus_1", "r_minus_05", "near_target", "target_filled")
SCANNER_KINDS = ("range_5m", "turnover_surge", "hot_enter", "idea_near", "idea_level", "idea_invalidated", "idea_tp", "idea_expired")
BRAIN_WAKE_CODES = ("R_PLUS", "R_MINUS", "NEAR_TARGET", "CHECK_PLAN", "TIME")
LIMIT_WHICH = ("daily_loss", "stop_streak", "session_budget", "max_open", "drawdown_halt", "cooldown", "rate", "brain_budget", "brain_down")
RECONCILE_KINDS = ("orphan_position", "position_drift", "missing_stop", "unknown_order", "equity_mismatch")
ENTRY_BLOCKED_WHY = ("GUARD", "CHASE", "COOLDOWN", "STALE", "SPREAD", "MIN_SIZE")
CONNECTIVITY_CHANNELS = ("public", "private", "rest")
MACRO_KINDS = ("FOMC", "CPI", "NFP", "PCE", "GDP", "OTHER")
EVENT_KEYS = ("seq", "ts", "type", "pri", "plan", "sym", "d")   # plan и sym необязательны
MAX_EVENT_BYTES = 400
RE_D_IDENT = r"^[A-Za-z0-9_.-]{1,40}$"
RE_SYMBOL = r"^[A-Z0-9]{2,20}$"
RE_PLAN_ID = r"^PL-\d{4}-[A-Z0-9]{2,20}-\d{2,3}$"
RE_IDEA_ID = r"^I\d+$"
RE_CONSUMER = r"^[a-z0-9_-]{1,20}$"
DEFAULT_CONSUMER = "chat"
CONSUMERS = ("chat", "api")

# ---- параметры реализации ----
MAX_FILE_BYTES = 1024 * 1024        # ротация по размеру
RETENTION_DAYS = 14
DELIVERED_MIN_INTERVAL_S = 0.5      # не чаще раза в 0.5 с пишем курсор delivered
CONNECTIVITY_DEDUP_S = 30.0
LOCK_WAIT_S = 5.0
LOCK_STALE_S = 30.0
DEDUP_SCAN_LINES = 400              # сколько последних строк текущего файла смотрит дедупликация
MAX_D_LIST = 8
MAX_D_DICT = 6

_RE_IDENT = re.compile(RE_D_IDENT)
_RE_SYMBOL = re.compile(RE_SYMBOL)
_RE_PLAN = re.compile(RE_PLAN_ID)
_RE_IDEA = re.compile(RE_IDEA_ID)
_RE_CONSUMER = re.compile(RE_CONSUMER)
_RE_ROT = re.compile(r"^events-(\d{8})(?:-(\d+))?\.jsonl$")

# ---- белые списки полей `d` по типам: ключ -> вид значения ----
# виды: "num" число, "bool", "id" идентификатор, ("enum", (...)), "ids" список идентификаторов, "numdict" словарь число-значений,
# "sym" символ, "idea" идентификатор идеи
_ENUM_DIR = ("enum", ("up", "down", "long", "short"))
D_FIELDS = {
    "scanner_signal": {"kind": ("enum", SCANNER_KINDS), "px": "num", "score": "num", "dir": _ENUM_DIR, "range_5m_pct": "num", "ret_5m_pct": "num",
                       "turn_surge_5m": "num", "spread_bps": "num", "funding_pct": "num", "idea": "idea", "wake": "bool"},
    "plan_armed": {"rev": "num", "codes": "ids", "preview": "numdict"},
    "plan_rejected": {"rev": "num", "codes": "ids", "preview": "numdict"},
    "trigger_fired": {"cond": "id", "level": "num", "px": "num"},
    "entry_blocked": {"why": ("enum", ENTRY_BLOCKED_WHY), "attempt": "num"},
    "entry_filled": {"avg": "num", "qty": "num", "partial": "bool", "sl": "num", "risk_usdt": "num", "journal_id": "id"},
    "milestone": {"name": "id", "r": "num", "px": "num", "target": "id"},
    "rule_fired": {"rule": "id", "action": "id", "ok": "bool", "code": "id"},
    "stop_moved": {"from": "num", "to": "num", "by": ("enum", ("rule", "cmd", "protect", "owner"))},
    "position_closed": {"reason": "id", "pnl_usdt": "num", "r": "num", "fees": "num", "journal_id": "id", "mae_r": "num", "mfe_r": "num"},
    "plan_ended": {"status": ("enum", ("cancelled", "expired", "invalidated")), "why": "id"},
    "action_rejected": {"plan": "id", "what": "id", "code": "id"},
    "brain_wake_request": {"code": ("enum", BRAIN_WAKE_CODES), "plan": "id"},
    "limit_hit": {"which": ("enum", LIMIT_WHICH), "until_ts": "num"},
    "brain_swap": {"kind": "id", "from": "id", "to": "id", "rev": "num", "by": "id"},
    "kill": {"state": ("enum", ("on", "off")), "flatten": "bool"},
    "connectivity": {"channel": ("enum", CONNECTIVITY_CHANNELS), "state": "id", "down_s": "num"},
    "reconcile_alert": {"kind": ("enum", RECONCILE_KINDS), "sym": "sym"},
    "session_ending": {"min_left": "num"},
    "error": {"where": "id", "code": "id"},
    "exec_started": {"version": "id", "env": "id"},
    "exec_stopped": {"version": "id", "env": "id"},
    "exec_down": {"version": "id", "env": "id", "age_s": "num"},
    "service_down": {"service": "id", "tries": "num", "code": "id", "positions": "num"},
    "macro_blackout_soon": {"event": "id", "kind": ("enum", MACRO_KINDS), "in_min": "num", "impact": "num", "verified": "bool"},
    "risk_level_changed": {"from": "num", "to": "num", "by": ("enum", ("owner", "auto")), "why": "id"},
    "control_cmd": {"action": "id", "code": "id", "src": "id", "mode": "id"},
    "day_report": {"kind": ("enum", ("start", "stop", "emergency")), "day": "id", "net": "num", "carried_n": "num", "questions_n": "num",
                   "verdict": ("enum", ("ok", "attention", "alarm"))},
    "day_state": {"state": ("enum", ("stopped", "starting", "running", "closing", "halted")), "step": "id", "code": "id"},
}


# ------------------------------------------------------------------ вспомогательное
def _now():
    return time.time()


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def parse_iso(s):
    """'2026-10-28T10:15:02Z' -> epoch или None."""
    if not isinstance(s, str):
        return None
    try:
        import calendar
        return float(calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _num(v):
    """Число для записи: целые как есть, дробные не длиннее 8 значащих цифр."""
    if isinstance(v, int):
        return v
    f = float("%.8g" % v)
    return int(f) if f == int(f) and abs(f) < 1e15 else f


def _ident(v):
    return isinstance(v, str) and bool(_RE_IDENT.match(v))


def _clean_value(kind, v):
    """-> (ok, значение). Значение, не прошедшее проверку вида, отбрасывается целиком."""
    if kind == "num":
        return (True, _num(v)) if _isnum(v) else (False, None)
    if kind == "bool":
        return (True, v) if isinstance(v, bool) else (False, None)
    if kind == "id":
        return (True, v) if _ident(v) else (False, None)
    if kind == "sym":
        return (True, v) if isinstance(v, str) and _RE_SYMBOL.match(v) else (False, None)
    if kind == "idea":
        return (True, v) if isinstance(v, str) and _RE_IDEA.match(v) else (False, None)
    if kind == "ids":
        if not isinstance(v, (list, tuple)):
            return False, None
        out = [x for x in v if _ident(x)][:MAX_D_LIST]
        return (True, out) if out else (False, None)
    if kind == "numdict":
        if not isinstance(v, dict):
            return False, None
        out = {k: _num(x) for k, x in list(v.items())[:MAX_D_DICT * 2] if _ident(k) and _isnum(x)}
        out = dict(list(out.items())[:MAX_D_DICT])
        return (True, out) if out else (False, None)
    if isinstance(kind, tuple) and kind[0] == "enum":
        return (True, v) if isinstance(v, str) and v in kind[1] else (False, None)
    return False, None


def default_pri(type_, d):
    """Приоритет по таблице 03 п. 3.3 (d уже очищен; для scanner_signal смотрит подсказку `wake`)."""
    base = EVENT_TYPES.get(type_)
    d = d or {}
    if type_ == "scanner_signal":
        return PRI_WAKE if d.get("wake") is True else PRI_INFO
    if type_ == "milestone":
        return PRI_WAKE if d.get("name") in WAKE_MILESTONES else PRI_INFO
    if type_ == "stop_moved":
        return PRI_WAKE if d.get("by") == "owner" else PRI_INFO
    if type_ == "connectivity":
        down = d.get("down_s")
        return PRI_WAKE if d.get("state") == "up" or (_isnum(down) and down >= 20) else PRI_INFO
    return base or PRI_INFO


def sanitize(event):
    """Очищает событие по белым спискам -> {"type","pri","plan"?,"sym"?,"d"}. Без seq и ts (их ставит `emit`).

    Неизвестный `type` -> ValueError. Неверные plan/sym отбрасываются, поля `d` вне списка типа и значения не того вида отбрасываются,
    свободный текст (строки не из перечисления и не идентификаторы) не проходит. Если строка длиннее MAX_EVENT_BYTES, самые длинные поля
    `d` отбрасываются; не помещается и так -> ValueError."""
    if not isinstance(event, dict):
        raise ValueError("событие должно быть dict")
    type_ = event.get("type")
    if type_ not in EVENT_TYPES:
        raise ValueError("неизвестный тип события")
    fields = D_FIELDS.get(type_, {})
    raw = event.get("d") if isinstance(event.get("d"), dict) else {}
    d = {}
    for k, kind in fields.items():
        if k in raw:
            ok, v = _clean_value(kind, raw[k])
            if ok:
                d[k] = v
    pri = event.get("pri")
    if pri not in PRIORITIES:
        pri = default_pri(type_, d)
    d.pop("wake", None)               # подсказка источника, в конверт не пишется
    out = {"type": type_, "pri": pri}
    plan = event.get("plan")
    if isinstance(plan, str) and _RE_PLAN.match(plan):
        out["plan"] = plan
    sym = event.get("sym")
    if isinstance(sym, str) and _RE_SYMBOL.match(sym):
        out["sym"] = sym
    out["d"] = d
    while len(_probe(out)) > MAX_EVENT_BYTES and d:
        d.pop(max(d, key=lambda k: len(json.dumps(d[k], separators=(",", ":")))))
    if len(_probe(out)) > MAX_EVENT_BYTES:
        raise ValueError("событие длиннее %d байт" % MAX_EVENT_BYTES)
    return out


def _probe(clean):
    """Сериализованная строка события с запасом под seq/ts (для проверки длины)."""
    return _dumps(dict(seq=999999999, ts="2026-01-01T00:00:00Z", **clean)).encode("utf-8")


def _dumps(ev):
    order = [k for k in EVENT_KEYS if k in ev]
    return json.dumps({k: ev[k] for k in order}, ensure_ascii=True, separators=(",", ":"))


def clean_out(ev):
    """Страховка на выходе: перепроверяет событие, прочитанное из файла, теми же белыми списками (файл могли править руками).
    -> событие с исходными seq/ts или None, если не проходит."""
    if not isinstance(ev, dict) or not isinstance(ev.get("seq"), int) or isinstance(ev.get("seq"), bool):
        return None
    try:
        c = sanitize(ev)
    except ValueError:
        return None
    # pri берётся из файла только если он допустим (sanitize это уже проверил); wake-подсказка из d не нужна
    c["seq"] = ev["seq"]
    c["ts"] = ev["ts"] if isinstance(ev.get("ts"), str) and re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", ev["ts"]) else ""
    return {k: c[k] for k in EVENT_KEYS if k in c}


# ------------------------------------------------------------------ блокировка (самодостаточная, O_EXCL)
class _Lock(object):
    def __init__(self, path, wait=LOCK_WAIT_S, stale_s=LOCK_STALE_S):
        self.path, self.wait, self.stale_s = path, wait, stale_s
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.time() + self.wait
        delay = 0.002
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode("ascii"))
                return self
            except FileExistsError:
                pass
            except PermissionError:      # Windows: файл блокировки как раз удаляется другим процессом
                pass
            try:
                if time.time() - os.stat(self.path).st_mtime > self.stale_s:
                    os.remove(self.path)  # владелец умер, не сняв блокировку
                    continue
            except OSError:
                continue
            if time.time() > deadline:
                raise TimeoutError("шина: не удалось взять блокировку %s" % self.path)
            time.sleep(delay)
            delay = min(0.05, delay * 1.5)

    def __exit__(self, *exc):
        try:
            if self.fd is not None:
                os.close(self.fd)
        finally:
            self.fd = None
            try:
                os.remove(self.path)
            except OSError:
                pass


def _lock(root):
    return _Lock(paths.bus(".lock", root=root))


# ------------------------------------------------------------------ файлы
def _retention_days(root):
    try:
        with open(paths.exec_limits_file(root=root), encoding="utf-8") as f:
            v = json.load(f)["events"]["retention_days"]
        return v if _isnum(v) and v > 0 else RETENTION_DAYS
    except (OSError, ValueError, KeyError, TypeError):
        return RETENTION_DAYS


def _rot_key(name):
    m = _RE_ROT.match(name)
    return (m.group(1), int(m.group(2) or 0)) if m else None


def _rotated(root):
    """Ротированные файлы по возрастанию (дата, номер)."""
    d = paths.bus(root=root)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    keyed = sorted((_rot_key(n), n) for n in names if _rot_key(n))
    return [os.path.join(d, n) for _, n in keyed]


def _files(root):
    cur = paths.bus_events(root=root)
    return _rotated(root) + ([cur] if os.path.isfile(cur) else [])


def _parse_lines(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("seq"), int) and not isinstance(r.get("seq"), bool) and isinstance(r.get("type"), str):
            out.append(r)
    return out


def _read_events(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return _parse_lines(f.read())
    except OSError:
        return []


def _tail_events(path, nbytes=65536):
    """События из хвоста файла (последние nbytes; первая неполная строка пропускается)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
                f.readline()
            data = f.read()
    except OSError:
        return []
    return _parse_lines(data.decode("utf-8", errors="replace"))


def _seq_floor(root):
    """Нижняя граница seq из state/bus/.seq (счётчик переживает удаление старых файлов: нумерация не откатывается)."""
    try:
        with open(paths.bus(".seq", root=root), encoding="utf-8") as f:
            v = int(f.read().strip())
        return v if v > 0 else 0
    except (OSError, ValueError):
        return 0


def _last_seq_nolock(root):
    floor = _seq_floor(root)
    for path in reversed(_files(root)):
        evs = _tail_events(path)
        if not evs:
            evs = _read_events(path)          # хвост из мусора: читаем целиком
        if evs:
            return max(floor, max(e["seq"] for e in evs))
    return floor


def last_seq(root=None):
    return _last_seq_nolock(root)


def signature(root=None):
    """Дешёвый отпечаток состояния шины (для опроса `--follow`): меняется при записи и ротации."""
    cur = paths.bus_events(root=root)
    try:
        st = os.stat(cur)
        sig = (st.st_size, st.st_mtime_ns, getattr(st, "st_ino", 0))
    except OSError:
        sig = None
    return (sig, len(_rotated(root)))


def _rotate_if_needed(root, now):
    cur = paths.bus_events(root=root)
    try:
        st = os.stat(cur)
    except OSError:
        return
    if st.st_size <= 0:
        return
    day_of_file = time.strftime("%Y%m%d", time.gmtime(st.st_mtime))
    if st.st_size < MAX_FILE_BYTES and day_of_file == time.strftime("%Y%m%d", time.gmtime(now)):
        return
    n = 0
    while True:
        name = "events-%s%s.jsonl" % (day_of_file, "" if n == 0 else "-%d" % n)
        dst = paths.bus(name, root=root)
        if not os.path.exists(dst):
            break
        n += 1
    fsutil.replace(cur, dst)
    _prune(root, now)


def _prune(root, now):
    keep_from = time.strftime("%Y%m%d", time.gmtime(now - _retention_days(root) * 86400.0))
    for p in _rotated(root):
        key = _rot_key(os.path.basename(p))
        if key and key[0] < keep_from:
            try:
                os.remove(p)
            except OSError:
                pass


# ------------------------------------------------------------------ запись
def _dedup_hit(clean, now, root):
    """Есть ли в хвосте текущего файла событие-дубликат по правилам дедупликации -> seq или None."""
    t = clean["type"]
    if t not in ("milestone", "connectivity"):
        return None
    cur = paths.bus_events(root=root)
    for e in reversed(_tail_events(cur)[-DEDUP_SCAN_LINES:]):
        if e.get("type") != t or e.get("plan") != clean.get("plan") or e.get("sym") != clean.get("sym"):
            continue
        d, c = e.get("d") or {}, clean["d"]
        if t == "milestone" and d.get("name") == c.get("name") and c.get("name") is not None:
            return e["seq"]
        if t == "connectivity" and d.get("channel") == c.get("channel") and d.get("state") == c.get("state"):
            ts = parse_iso(e.get("ts"))
            if ts is not None and now - ts < CONNECTIVITY_DEDUP_S:
                return e["seq"]
    return None


def emit(type_, d=None, plan=None, sym=None, pri=None, root=None, now=None):
    """Дописывает событие в шину -> seq. Дубликаты (веха плана, connectivity чаще раза в 30 с) не пишутся: возвращается seq прежнего события.

    Неизвестный тип -> ValueError. Под блокировкой: читает последний seq, при необходимости ротирует файл, дописывает строку одним write."""
    clean = sanitize({"type": type_, "d": d, "plan": plan, "sym": sym, "pri": pri})
    now = _now() if now is None else now
    os.makedirs(paths.bus(root=root), exist_ok=True)
    with _lock(root):
        _rotate_if_needed(root, now)
        dup = _dedup_hit(clean, now, root)
        if dup is not None:
            return dup
        seq = _last_seq_nolock(root) + 1
        ev = dict(clean, seq=seq, ts=iso(now))
        line = _dumps(ev) + "\n"
        cur = paths.bus_events(root=root)
        data = line.encode("utf-8")
        try:
            with open(cur, "rb") as f:
                f.seek(0, os.SEEK_END)
                if f.tell() > 0:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":      # оборванная запись: отделяем её переводом строки
                        data = b"\n" + data
        except OSError:
            pass
        fd = os.open(cur, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        try:
            tmp = paths.bus(".seq.tmp", root=root)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(str(seq))
            fsutil.replace(tmp, paths.bus(".seq", root=root))
        except OSError:
            pass                                   # счётчик вторичен: события уже на диске
        return seq


# ------------------------------------------------------------------ чтение
def read(after_seq=0, limit=50, wake_only=False, root=None):
    """-> [событие] по возрастанию seq (первые `limit` с seq > after_seq). Битые строки и дубли seq пропускаются. Читает и ротированные файлы."""
    after_seq = after_seq or 0
    chunks = []
    for path in reversed(_files(root)):
        evs = _read_events(path)
        chunks.append(evs)
        if evs and min(e["seq"] for e in evs) <= after_seq:
            break                                   # более старые файлы не нужны
    by_seq = {}
    for evs in chunks:
        for e in evs:
            if e["seq"] > after_seq:
                by_seq.setdefault(e["seq"], e)
    out = [by_seq[s] for s in sorted(by_seq)]
    if wake_only:
        out = [e for e in out if e.get("pri") == PRI_WAKE]
    return out[:limit] if limit else out


def read_tail(n=8, root=None, wake_only=False):
    """Последние n событий по возрастанию seq."""
    got = {}
    for path in reversed(_files(root)):
        for e in _read_events(path):
            got.setdefault(e["seq"], e)
        if len(got) >= n * 3 + 20 or (not wake_only and len(got) >= n):
            break
    out = [got[s] for s in sorted(got)]
    if wake_only:
        out = [e for e in out if e.get("pri") == PRI_WAKE]
    return out[-n:]


# ------------------------------------------------------------------ курсоры
_PENDING = {}      # путь курсора -> seq delivered, ещё не записанный на диск
_LASTW = {}         # путь курсора -> monotonic последней записи delivered


def _consumer(consumer):
    if not isinstance(consumer, str) or not _RE_CONSUMER.match(consumer):
        raise ValueError("имя потребителя: %s" % RE_CONSUMER)
    return consumer


check_consumer = _consumer


def _cursor_read(path):
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        d, a = int(c.get("delivered", 0)), int(c.get("acked", 0))
        return {"delivered": max(d, a), "acked": a, "ts": c.get("ts")}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"delivered": 0, "acked": 0, "ts": None}


def _cursor_write(path, cur, now):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"delivered": cur["delivered"], "acked": cur["acked"], "ts": round(now, 3)}, f, separators=(",", ":"))
    fsutil.replace(tmp, path)


def cursor_exists(consumer, root=None):
    return os.path.isfile(paths.bus_cursor(_consumer(consumer), root=root))


def cursor_get(consumer, root=None):
    """-> {"delivered":N,"acked":M,"ts":..} (нет файла или мусор -> нули)."""
    return _cursor_read(paths.bus_cursor(_consumer(consumer), root=root))


def cursor_init(consumer, root=None):
    """Новый потребитель начинает с конца очереди (не получает всю историю); существующий курсор не трогается. -> курсор."""
    path = paths.bus_cursor(_consumer(consumer), root=root)
    with _Lock(path + ".lock"):
        if os.path.isfile(path):
            return _cursor_read(path)
        n = _last_seq_nolock(root)
        cur = {"delivered": n, "acked": n, "ts": None}
        _cursor_write(path, cur, _now())
        return cur


def cursor_reset(consumer, seq, root=None):
    """Сброс курсора на seq (после очистки шины: курсор не может быть впереди последнего события)."""
    path = paths.bus_cursor(_consumer(consumer), root=root)
    with _Lock(path + ".lock"):
        cur = {"delivered": seq, "acked": seq, "ts": None}
        _cursor_write(path, cur, _now())
        _PENDING.pop(path, None)
        return cur


def cursor_delivered(consumer, seq, root=None, force=False, now=None):
    """Обновляет delivered (монотонно; не чаще раза в 0.5 с, пропущенное допишет `cursor_flush`). -> True, если записано."""
    path = paths.bus_cursor(_consumer(consumer), root=root)
    _PENDING[path] = max(seq, _PENDING.get(path, 0))
    last = _LASTW.get(path)
    if not force and last is not None and time.monotonic() - last < DELIVERED_MIN_INTERVAL_S:
        return False
    return cursor_flush(consumer, root=root, now=now)


def cursor_flush(consumer, root=None, now=None):
    """Дописывает отложенный delivered. -> True, если на диск что-то записано."""
    path = paths.bus_cursor(_consumer(consumer), root=root)
    seq = _PENDING.pop(path, None)
    if seq is None:
        return False
    with _Lock(path + ".lock"):
        cur = _cursor_read(path)
        if seq <= cur["delivered"]:
            return False
        cur["delivered"] = seq
        _cursor_write(path, cur, _now() if now is None else now)
    _LASTW[path] = time.monotonic()
    return True


def ack(consumer, seq, root=None, now=None):
    """Подтверждает обработку (acked = max(acked, seq); delivered не отстаёт от acked). seq выше последнего события -> ValueError. -> курсор."""
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ValueError("seq должен быть целым >= 0")
    path = paths.bus_cursor(_consumer(consumer), root=root)
    if seq > _last_seq_nolock(root):
        raise ValueError("seq %d больше последнего события" % seq)
    with _Lock(path + ".lock"):
        cur = _cursor_read(path)
        cur["acked"] = max(cur["acked"], seq)
        cur["delivered"] = max(cur["delivered"], cur["acked"])
        _cursor_write(path, cur, _now() if now is None else now)
        _PENDING.pop(path, None)
        return cur


def unacked(consumer=DEFAULT_CONSUMER, root=None, limit=1000):
    """wake-события после acked (ещё не подтверждены мозгом)."""
    return read(after_seq=cursor_get(consumer, root=root)["acked"], limit=limit, wake_only=True, root=root)


def stats(consumer=DEFAULT_CONSUMER, root=None, now=None):
    """-> {"consumer","last_seq","delivered","acked","backlog","unacked_wake","oldest_unacked_age_s","age_s","files","bytes"}.
    backlog = число событий после acked (любых), unacked_wake = число wake после acked, age_s = возраст последнего события."""
    now = _now() if now is None else now
    cur = cursor_get(consumer, root=root)
    last = _last_seq_nolock(root)
    tail = read_tail(1, root=root)
    after = read(after_seq=cur["acked"], limit=0, root=root)
    wake = [e for e in after if e.get("pri") == PRI_WAKE]
    ts_last = parse_iso(tail[-1].get("ts")) if tail else None
    ts_old = parse_iso(wake[0].get("ts")) if wake else None
    files = _files(root)
    size = 0
    for p in files:
        try:
            size += os.path.getsize(p)
        except OSError:
            pass
    return {"consumer": consumer, "last_seq": last, "delivered": cur["delivered"], "acked": cur["acked"], "backlog": len(after),
            "unacked_wake": len(wake), "oldest_unacked_age_s": None if ts_old is None else max(0.0, round(now - ts_old, 1)),
            "age_s": None if ts_last is None else max(0.0, round(now - ts_last, 1)), "files": len(files), "bytes": size}
