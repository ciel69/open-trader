"""Источник уведомлений ОС: подписка на шину событий внутри сервиса ctl (отдельного процесса нет). Уведомления (WP-N).

`Notifier.poll()` читает новые события шины (`state/bus`), переводит нужные в категории (`protocol.NOTIFY_KEYS`), применяет настройки владельца
(`notifycfg`: мастер, категории, тихие часы) и антиспам, показывает уведомление через `notify.send_os(force=True)`. Текст ТОЛЬКО из шаблонов ниже с числами и
идентификаторами (в шине свободного текста третьих лиц нет, `notify.clean` режет кавычки и управляющие символы). Курсор и счётчики: `state/control/notify_state.json`.

Антиспам: не больше `limits.per_hour` показов на категорию в час (лишнее считается, не показывается); один вид (категория + объект) не чаще раза в
`limits.same_kind_min` минут, пропущенные повторы дописываются к следующему показу («+N повторов»). Тихие часы: события копятся (до 100) и после окончания выходят одним
сводным уведомлением; категории из `quiet.bypass` (по умолчанию emergency, service_down, day_close_failed) показываются сразу.
События старше `STALE_S` (ctl не работал) пропускаются. Если файла настроек нет, подписчик ничего не показывает (работает путь WP14 по config/lifecycle.json).
"""
import re
import threading
import time

from tb.control import ctlstate
from tb.core import bus
from tb.lifecycle import notify, notifycfg

STALE_S = 900
HELD_MAX = 100
HELD_KEEP_S = 86400
POLL_S = 2.0
BATCH = 200
CLOSE_WORD = {"tp": "тейку", "sl": "стопу", "liquidation": "ликвидации"}


def _n(d, k, dflt=0.0):
    v = d.get(k)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else dflt


_IDENT = re.compile(r"[^A-Za-z0-9_.:-]")


def _s(d, k, dflt="-"):
    """Только идентификатор: латиница, цифры и _.:- (всё остальное, включая пробелы и скобки, заменяется на «_»)."""
    v = d.get(k)
    return _IDENT.sub("_", v)[:40] if isinstance(v, str) and v else dflt


def classify(ev):
    """Событие шины -> (категория, ключ вида, текст) или None. Только числа и идентификаторы."""
    t, d = ev.get("type"), ev.get("d") or {}
    sym = ev.get("sym") or "-"
    if t == "day_report":
        kind = d.get("kind")
        if kind == "start":
            return "day_start", "day_start:%s" % _s(d, "day"), "День %s запущен, перенесено %d, вопросов %d" % (_s(d, "day"), _n(d, "carried_n"), _n(d, "questions_n"))
        if kind == "emergency":
            return "emergency", "emergency:%s" % _s(d, "day"), "Аварийная остановка дня %s, итог %+.2f USDT" % (_s(d, "day"), _n(d, "net"))
        return "day_report", "day_report:%s" % _s(d, "day"), "Итог дня %s: %+.2f USDT, перенесено %d, вопросов %d, оценка %s" % (
            _s(d, "day"), _n(d, "net"), _n(d, "carried_n"), _n(d, "questions_n"), _s(d, "verdict"))
    if t == "kill":
        if d.get("state") == "off":
            return None
        return "emergency", "emergency:kill", "KILL включён%s" % (" (с закрытием позиций)" if d.get("flatten") else "")
    if t == "service_down":
        return "service_down", "service_down:%s" % _s(d, "service"), "Сервис %s не поднимается: попыток %d, код %s%s" % (
            _s(d, "service"), _n(d, "tries"), _s(d, "code"), (", открытых позиций %d" % _n(d, "positions")) if "positions" in d else "")
    if t == "exec_down":
        return "service_down", "service_down:exec", "Исполнитель не отвечает %d с" % _n(d, "age_s")
    if t == "connectivity":
        if d.get("state") != "down" or _n(d, "down_s") < 20:
            return None
        return "service_down", "service_down:conn:%s" % _s(d, "channel"), "Связь %s потеряна на %d с" % (_s(d, "channel"), _n(d, "down_s"))
    if t == "reconcile_alert":
        return "service_down", "service_down:recon:%s" % _s(d, "kind"), "Сверка с биржей: %s %s" % (_s(d, "kind"), sym)
    if t == "error" and d.get("where") == "lifecycle" and d.get("code") == "DAY_CLOSE_FAILED":
        return "day_close_failed", "day_close_failed", "Закрытие дня не подтверждено исполнителем: проверьте позиции и стопы на бирже"
    if t == "control_cmd" and d.get("action") == "day.start" and d.get("mode") == "live":
        if d.get("code") == "OK":
            return "live_gate", "live_gate:ok", "Запущен live (источник %s)" % _s(d, "src")
        return "live_gate", "live_gate:%s" % _s(d, "code"), "Запуск live отклонён: %s (источник %s)" % (_s(d, "code"), _s(d, "src"))
    if t == "risk_level_changed":
        return "risk_level_changed", "risk:%d:%d" % (_n(d, "from"), _n(d, "to")), "Уровень риска %d -> %d (%s)" % (_n(d, "from"), _n(d, "to"), _s(d, "by"))
    if t in ("brain_swap", "brain_fallback"):
        return "brain_swap", "brain:%s:%s" % (t, _s(d, "to")), "Мозг: %s %s -> %s, ревизия %d, кто %s" % (_s(d, "kind", t), _s(d, "from"), _s(d, "to"), _n(d, "rev"), _s(d, "by"))
    if t == "position_closed" and d.get("reason") in CLOSE_WORD:
        r = d.get("r")
        return "position_closed", "position_closed:%s" % (ev.get("plan") or ev.get("seq")), "%s закрыта по %s: %+.2f USDT%s" % (
            sym, CLOSE_WORD[d["reason"]], _n(d, "pnl_usdt"), (" (%+.2fR)" % r) if isinstance(r, (int, float)) and not isinstance(r, bool) else "")
    if t == "macro_blackout_soon":
        return "macro_blackout_soon", "macro:%s" % _s(d, "event"), "Через %d мин макро-событие %s (влияние %d)" % (_n(d, "in_min"), _s(d, "kind"), _n(d, "impact"))
    return None


class Notifier(object):
    def __init__(self, root, clock=time.time, runner=None, system=None, environ=None, sender=None):
        """sender(text) -> (ok, how) подменяет показ (тесты); иначе notify.send_os(force=True) с runner/system/environ."""
        self.root, self.clock = root, clock
        self.runner, self.system, self.environ = runner, system, environ
        self.sender = sender
        self.st = ctlstate.read_json(notifycfg.state_path(root)) or {}
        for k, dflt in (("sent", {}), ("kind_last", {}), ("merged", {}), ("held", []), ("stats", {})):
            if not isinstance(self.st.get(k), type(dflt)):
                self.st[k] = dflt
        self.dirty = False

    # ------------------------------------------------------------ показ
    def show(self, text, force=False):
        if self.sender is not None:
            return self.sender(text)
        return notify.send_os(text, {}, runner=self.runner, system=self.system, environ=self.environ, force=True)

    def stat(self, k, n=1):
        self.st["stats"][k] = self.st["stats"].get(k, 0) + n

    def deliver(self, cat, text, now):
        ok, how = self.show(text)
        self.stat("sent" if ok else "failed")
        if ok:
            self.st["last_sent"] = {"ts": round(now, 1), "cat": cat, "text": text[:160]}
        self.dirty = True
        return ok

    # ------------------------------------------------------------ обработка события
    def handle(self, ev, s, now):
        c = classify(ev)
        if c is None:
            return
        cat, key, text = c
        if not s["master"] or not s["categories"].get(cat):
            return
        lim = s["limits"]
        last = self.st["kind_last"].get(key)
        if last is not None and now - last < lim["same_kind_min"] * 60:
            self.st["merged"][key] = self.st["merged"].get(key, 0) + 1
            self.stat("merged")
            self.dirty = True
            return
        sent = [t for t in self.st["sent"].get(cat, []) if now - t < 3600]
        self.st["sent"][cat] = sent
        if len(sent) >= lim["per_hour"]:
            self.stat("limited")
            self.dirty = True
            return
        n = self.st["merged"].pop(key, 0)
        if n:
            text += " (+%d повторов)" % n
        if notifycfg.in_quiet(s, now) and not notifycfg.bypasses(s, cat):
            if len(self.st["held"]) >= HELD_MAX:
                self.st["held"].pop(0)
                self.stat("dropped")
            self.st["held"].append({"cat": cat, "text": text, "ts": round(now, 1)})
            self.st["kind_last"][key] = now
            self.stat("held")
            self.dirty = True
            return
        self.st["kind_last"][key] = now
        sent.append(now)
        self.deliver(cat, text, now)

    def release_held(self, s, now):
        """Тихие часы закончились: одно сводное уведомление о накопленном (только включённые сейчас категории)."""
        if not self.st["held"] or notifycfg.in_quiet(s, now):
            return
        items = [h for h in self.st["held"] if now - h.get("ts", 0) < HELD_KEEP_S and s["master"] and s["categories"].get(h.get("cat"))]
        self.st["held"] = []
        self.dirty = True
        if not items:
            return
        if len(items) == 1:
            text = items[0]["text"]
        else:
            cnt = {}
            for h in items:
                cnt[h["cat"]] = cnt.get(h["cat"], 0) + 1
            text = "Накоплено за тихие часы: %d (%s)" % (len(items), ", ".join("%s ×%d" % (k, cnt[k]) for k in sorted(cnt)))
        self.deliver("quiet_release", text, now)

    # ------------------------------------------------------------ цикл
    def poll(self):
        """Один проход -> число обработанных событий. Исключения наружу не идут (уведомления не должны ронять ctl)."""
        now = self.clock()
        try:
            cur = notifycfg.load(self.root)
            s = cur["settings"]
            if "last_seq" not in self.st:
                self.st["last_seq"] = bus.last_seq(self.root)       # историю не пересылаем
                self.dirty = True
            if self.st["last_seq"] > bus.last_seq(self.root):       # шину очистили: курсор не может быть впереди
                self.st["last_seq"], self.dirty = bus.last_seq(self.root), True
            evs = bus.read(after_seq=self.st["last_seq"], limit=BATCH, root=self.root)
            for ev in evs:
                self.st["last_seq"] = ev["seq"]
                self.dirty = True
                if not cur["engine"]:
                    continue
                ts = bus.parse_iso(ev.get("ts")) if ev.get("ts") else None
                if ts is not None and now - ts > STALE_S:
                    continue
                self.handle(ev, s, now)
            if cur["engine"]:
                self.release_held(s, now)
            if self.dirty:
                ctlstate.write_json(notifycfg.state_path(self.root), self.st)
                self.dirty = False
            return len(evs)
        except Exception:
            return 0


class NotifyThread(object):
    """Фоновый поток подписчика внутри ctl (показ уведомления может занять секунды: основной цикл ctl не блокируется)."""

    def __init__(self, root, clock=time.time, notifier=None):
        self.n = notifier or Notifier(root, clock=clock)
        self.stop_ev = threading.Event()
        self.th = threading.Thread(target=self._run, name="tb-notify", daemon=True)

    def _run(self):
        while not self.stop_ev.is_set():
            self.n.poll()
            self.stop_ev.wait(POLL_S)

    def start(self):
        self.th.start()
        return self

    def stop(self):
        self.stop_ev.set()
        self.th.join(timeout=1.0)
