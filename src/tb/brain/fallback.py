"""Запасные профили, охлаждение неисправных профилей и режим «только защита» (WP11-C2; 07b п. 6.5).

Состояние рантайма (не конфигурация, `rev` не меняется): `runtime/brain/state.json`
  {"cooldown": {профиль: {"until","n","kind"}}, "protect_only": {"on","since","reason","code"}, "last_fallback": {"ts","role","from","to","kind"}}.
Триггеры (brain.json:fallback.on): timeout (таймаут, сеть), http_5xx, rate (429 после повторов), budget (потолок профиля/провайдера), format_x2 (два подряд невалидных формата).
Охлаждение растёт экспоненциально (base, 2*base, ... до max). Для live-ролей в режиме live запасной (не основной) допускается только с включённым провайдером, свежей проверкой
провайдера и меткой live_ok (либо принятым при старте дня предупреждением W2 в live_waivers); иначе кандидата нет -> «только защита»: мозг не создаёт и не меняет планы,
исполнитель продолжает вести позиции по принятым планам и биржевым стопам. Смена на запасной внутри задания идёт только по цепочке задания.
"""
import threading
import time

from tb import paths
from tb.brain import common
from tb.core.filelock import atomic_write_json

KINDS = ("timeout", "http_5xx", "rate", "budget", "format_x2", "auth")
FALLBACK_PROBE_MAX_AGE_S = 86400.0


def state_file(root=None):
    return paths.runtime("brain", "state.json", root=root)


class Health(object):
    def __init__(self, root=None, clock=time.time):
        self.root, self.clock = root, clock
        self._lock = threading.RLock()
        d = common.read_json(state_file(root))
        d = d if isinstance(d, dict) else {}
        self.cooldown = d.get("cooldown") if isinstance(d.get("cooldown"), dict) else {}
        self.protect = d.get("protect_only") if isinstance(d.get("protect_only"), dict) else {"on": False}
        self.last_fallback = d.get("last_fallback") if isinstance(d.get("last_fallback"), dict) else None

    def _save(self):
        try:
            atomic_write_json(state_file(self.root), {"cooldown": self.cooldown, "protect_only": self.protect, "last_fallback": self.last_fallback}, indent=None)
        except OSError:
            pass

    # ---- охлаждение
    def cooling(self, profile, now=None):
        now = self.clock() if now is None else now
        e = self.cooldown.get(profile)
        return bool(e and e.get("until", 0) > now)

    def until(self, profile):
        return (self.cooldown.get(profile) or {}).get("until")

    def fail(self, profile, kind, base=60.0, mx=900.0, now=None):
        now = self.clock() if now is None else now
        with self._lock:
            e = self.cooldown.get(profile) or {}
            n = int(e.get("n") or 0) + 1 if e.get("until", 0) > now - mx else 1
            until = now + min(float(mx), float(base) * 2 ** (n - 1))
            self.cooldown[profile] = {"until": round(until, 1), "n": n, "kind": kind}
            self._save()
            return until

    def ok(self, profile):
        with self._lock:
            if profile in self.cooldown:
                del self.cooldown[profile]
                self._save()

    def note_fallback(self, role, frm, to, kind, now=None):
        now = self.clock() if now is None else now
        with self._lock:
            self.last_fallback = {"ts": round(now, 1), "role": role, "from": frm, "to": to, "kind": kind}
            self._save()

    # ---- только защита
    def set_protect(self, on, reason=None, code=None, now=None):
        """-> True, если состояние изменилось."""
        now = self.clock() if now is None else now
        with self._lock:
            was = bool(self.protect.get("on"))
            if on and not was:
                self.protect = {"on": True, "since": round(now, 1), "reason": reason, "code": code}
            elif on and was and reason and reason != self.protect.get("reason"):
                self.protect = dict(self.protect, reason=reason, code=code)
            elif not on and was:
                self.protect = {"on": False, "since": None, "reason": None, "code": None, "cleared": round(now, 1)}
            else:
                return False
            self._save()
            return was != bool(on)

    @property
    def protect_on(self):
        return bool(self.protect.get("on"))

    def protect_for_s(self, now=None):
        now = self.clock() if now is None else now
        s = self.protect.get("since")
        return (now - s) if self.protect_on and isinstance(s, (int, float)) else 0.0


# ------------------------------------------------------------------ выбор кандидатов
def waived_w2(root):
    d = common.read_json(paths.control_dir("platform.json", root=root))
    w = (d or {}).get("live_waivers") if isinstance(d, dict) else None
    return any(isinstance(x, dict) and x.get("id") == "W2" for x in (w or []))


def _provider_ok(reg, p):
    if reg is None:
        return True
    try:
        return bool(reg.provider(p.provider).enabled and reg.provider(p.provider).model(p.model))
    except Exception:
        return False


def eligible(root, conf, reg, profile_id, role, now, mode, is_primary):
    """-> (ok, причина|None) без учёта охлаждения."""
    p = conf.profiles.get(profile_id)
    if p is None:
        return False, "профиль неизвестен"
    if not _provider_ok(reg, p):
        return False, "провайдер отключён"
    if role.live_risk and mode == "live" and not is_primary and conf.fallback.get("live_risk_requires_live_ok", True):
        from tb.brain import swap
        age = swap.probe_age(root, profile_id, conf, now)
        if age is None or age > FALLBACK_PROBE_MAX_AGE_S:
            return False, "запасной профиль без свежей проверки провайдера"
        if not p.live_ok and not waived_w2(root):
            return False, "у запасного профиля нет live_ok"
    return True, None


def usable(root, conf, reg, chain, role, now, mode="live"):
    """Есть ли в цепочке хоть один допустимый профиль (охлаждение не считается: для пробы H6)."""
    return any(eligible(root, conf, reg, pid, role, now, mode, i == 0)[0] for i, pid in enumerate(chain))


def candidates(root, conf, reg, chain, role, health, now, mode):
    """-> ([(профиль, fallback_from|None)], [причины пропуска]) в порядке цепочки; охлаждающиеся пропускаются (задания идут сразу на запасной)."""
    out, why, seen = [], [], set()
    for i, pid in enumerate(chain):
        if pid in seen:
            continue
        seen.add(pid)
        ok, r = eligible(root, conf, reg, pid, role, now, mode, i == 0)
        if not ok:
            why.append("%s: %s" % (pid, r))
            continue
        if health.cooling(pid, now):
            why.append("%s: охлаждение до %s" % (pid, int(health.until(pid) or 0)))
            continue
        out.append((pid, chain[0] if i > 0 else None))
    return out, why
