"""Горячая смена моделей/команд мозга без перезапуска (WP11-C2; 07b п. 6.3-6.4, 7).

Состояние: `state/brain/active.json` (подписано HMAC тем же ключом и тем же алгоритмом, что файл уровня риска, через `core/statesign`; пишет ТОЛЬКО этот модуль
по команде владельца: ctl `brain.use|rollback|reload` или `./tb brain use|rollback|reload` в терминале), копии ревизий `state/brain/history/R-<rev>.json` (последние 50), журнал
`state/brain/swaps.jsonl` (append-only, хеш-цепочка prev/h). Раннер перед КАЖДЫМ заданием берёт `EffectiveCache.get()`: файл не подписан или подпись неверна -> действует
`config/brain.json` и предупреждение в статусе (fail closed: подделка файла не меняет модель). Текущее задание доделывает старая модель (ревизия фиксируется в задании).

Уровни смены (06 п. 6.4): T0 без влияния на деньги (общие роли news_reader/reviewer, любая смена при режиме не live); T1 live-роль внутри live_team в режиме live; T2 смена live_team в live.
Жёсткие условия (нельзя обойти): E_SWAP_NO_LIVE_RIGHTS, E_SWAP_AMBIGUOUS_STATE, E_SWAP_PHRASE, E_SWAP_PROBE, E_SWAP_LIMIT_DAY (+ E_SWAP_PROFILE, E_SWAP_TARGET, E_SWAP_NO_KEY).
Мягкие (владелец принимает по одному, `accepted`): W_SWAP_PAPER_HISTORY, W_SWAP_NOT_LIVE_OK, W_SWAP_COOLDOWN, W_SWAP_NO_AB. Автоматические смены (fallback) live_team не меняют.

Интерфейс для ctl (`tb.control.brain_iface`): status(root), probe(root, now), classify(root, args, now), use|rollback|reload(root, args, now, by="ctl", phrase_ok=False).
Модуль не импортирует tb.control, биржевой код и исполнитель.
"""
import copy
import hashlib
import json
import os
import time

from tb import paths
from tb.brain import common, conf as conf_mod
from tb.core import decisions, statesign
from tb.core.filelock import FileLock, atomic_write_json

SCHEMA = "tb.brainactive/1"
KEEP_HISTORY = 50
GENESIS = "0" * 16
INVARIANT_CODES = ("STOP_NOT_TIGHTER", "STOP_ON_WRONG_SIDE", "NO_STOP", "ADD_DISABLED", "E_HOLD_EXTEND", "E_NO_CONFIRM", "E_STOP_WIDEN", "E_SYMBOL_FORBIDDEN")
W_IDS = {"W_SWAP_PAPER_HISTORY": "paper_history", "W_SWAP_NOT_LIVE_OK": "not_live_ok", "W_SWAP_COOLDOWN": "cooldown", "W_SWAP_NO_AB": "no_ab"}


# ------------------------------------------------------------------ пути
def bdir(root=None, *parts):
    return paths.state("brain", *parts, root=root)


def active_file(root=None):
    return bdir(root, "active.json")


def history_dir(root=None):
    return bdir(root, "history")


def swaps_file(root=None):
    return bdir(root, "swaps.jsonl")


def reload_req_file(root=None):
    return bdir(root, "reload.req")


def probe_file(root=None):
    return bdir(root, "probe.json")


def history_ok_file(root=None):
    return bdir(root, "history_ok.json")


# ------------------------------------------------------------------ подпись
def _key(environ=None):
    return statesign.key(environ)


def sign(obj, key):
    return statesign.sign(obj, key)


# ------------------------------------------------------------------ назначения
class Assign(object):
    """Действующие назначения моделей: ревизия, live_team, команды, общие роли. source: active | config | untrusted."""

    def __init__(self, rev, live_team, teams, shared_roles, source, by=None, ts=None, prev_rev=None, warn=None):
        self.rev, self.live_team, self.teams, self.shared_roles = rev, live_team, teams, shared_roles
        self.source, self.by, self.ts, self.prev_rev, self.warn = source, by, ts, prev_rev, warn

    def agent_id(self, team=None):
        return (self.teams.get(team or self.live_team) or {}).get("agent_id")

    def chain(self, role, team=None):
        """-> (список профилей [primary]+fallback, команда|None) для роли; роль без назначения -> ([], ...)."""
        sh = self.shared_roles.get(role)
        if sh:
            return [sh["primary"]] + list(sh.get("fallback") or []), None
        t = team or self.live_team
        ch = ((self.teams.get(t) or {}).get("roles") or {}).get(role)
        if ch:
            return [ch["primary"]] + list(ch.get("fallback") or []), t
        return [], t

    def profiles_map(self):
        out = {}
        for role, ch in ((self.teams.get(self.live_team) or {}).get("roles") or {}).items():
            out[role] = ch["primary"]
        for role, ch in self.shared_roles.items():
            out.setdefault(role, ch["primary"])
        return out

    def body(self):
        return {"live_team": self.live_team, "teams": copy.deepcopy(self.teams), "shared_roles": copy.deepcopy(self.shared_roles)}


def _valid_body(o):
    if not isinstance(o, dict) or not isinstance(o.get("rev"), int) or isinstance(o.get("rev"), bool):
        return False
    if not isinstance(o.get("live_team"), str) or not isinstance(o.get("teams"), dict) or not isinstance(o.get("shared_roles"), dict):
        return False
    for t in o["teams"].values():
        if not isinstance(t, dict) or not isinstance(t.get("agent_id"), str) or not isinstance(t.get("roles"), dict):
            return False
        for ch in t["roles"].values():
            if not (isinstance(ch, dict) and isinstance(ch.get("primary"), str) and isinstance(ch.get("fallback", []), list)):
                return False
    for ch in o["shared_roles"].values():
        if not (isinstance(ch, dict) and isinstance(ch.get("primary"), str) and isinstance(ch.get("fallback", []), list)):
            return False
    return o["live_team"] in o["teams"]


def read_active(root=None, key=None, path=None):
    """-> (obj|None, состояние): none (файла нет) | ok | untrusted (нет ключа/подпись неверна) | broken."""
    p = path or active_file(root)
    if not os.path.exists(p):
        return None, "none"
    o = common.read_json(p)
    if not isinstance(o, dict) or o.get("schema") != SCHEMA or not _valid_body(o):
        return None, "broken"
    key = _key() if key is None else key
    if not key or not statesign.verify(o, key):
        return None, "untrusted"
    return o, "ok"


def from_conf(conf, source="config", warn=None):
    return Assign(0, conf.live_team, copy.deepcopy(conf.teams), copy.deepcopy(conf.shared_roles), source, by="config", warn=warn)


def load_effective(root=None, conf=None, key=None):
    """-> Assign: подписанный active.json, иначе config/brain.json (source config/untrusted)."""
    conf = conf or conf_mod.load(root)
    o, st = read_active(root, key)
    if o is not None:
        return Assign(o["rev"], o["live_team"], o["teams"], o["shared_roles"], "active", by=o.get("by"), ts=o.get("ts"), prev_rev=o.get("prev_rev"))
    warn = None
    if st in ("untrusted", "broken"):
        warn = "active.json %s: игнорируется, действует config/brain.json" % ("не подписан или подпись неверна" if st == "untrusted" else "повреждён")
    return from_conf(conf, "untrusted" if warn else "config", warn)


class EffectiveCache(object):
    """Проверка active.json перед КАЖДЫМ заданием: stat (дёшево) и перечитывание с проверкой подписи только при смене (mtime, размер) или `force`."""

    def __init__(self, root=None, clock=time.time):
        self.root, self.clock = root, clock
        self._sig, self._val, self._conf_id = None, None, None

    def get(self, conf, force=False):
        try:
            st = os.stat(active_file(self.root))
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if force or self._val is None or sig != self._sig or self._conf_id != id(conf):
            self._val = load_effective(self.root, conf)
            self._sig, self._conf_id = sig, id(conf)
        return self._val


# ------------------------------------------------------------------ журнал смен
def _hash(prev, rec):
    body = {k: v for k, v in rec.items() if k != "h"}
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)).encode("utf-8")).hexdigest()[:16]


def read_swaps(root=None, limit=0):
    rows = [r for r in common.read_jsonl(swaps_file(root)) if isinstance(r.get("seq"), int)]
    return rows[-limit:] if limit else rows


def append_swap(root, rec, now):
    path = swaps_file(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with FileLock(bdir(root, "swaps.lock"), wait=10.0):
        rows = read_swaps(root)
        prev = rows[-1]["h"] if rows else GENESIS
        rec = dict(rec, seq=(rows[-1]["seq"] + 1 if rows else 1), ts=round(now, 3), prev=prev)
        rec["h"] = _hash(prev, rec)
        with open(path, "ab") as f:
            f.write((json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    return rec


def verify_swaps(root=None):
    prev, n = GENESIS, 0
    for r in read_swaps(root):
        if r.get("prev") != prev or r.get("h") != _hash(prev, r) or r["seq"] != n + 1:
            return {"ok": False, "n": n, "bad_seq": r["seq"]}
        prev, n = r["h"], n + 1
    return {"ok": True, "n": n, "bad_seq": None}


def last_swap_ts(root=None, min_tier=1):
    """Время последней смены live-ролей (T1/T2); смены T0 не считаются."""
    best = None
    for r in read_swaps(root):
        if r.get("kind") in ("use", "rollback", "reload") and (r.get("tier") or 0) >= min_tier and isinstance(r.get("ts"), (int, float)):
            best = r["ts"]
    return best


def swaps_today(root, now):
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    return sum(1 for r in read_swaps(root) if r.get("kind") in ("use", "rollback") and (r.get("tier") or 0) >= 1
               and isinstance(r.get("ts"), (int, float)) and time.strftime("%Y-%m-%d", time.gmtime(r["ts"])) == day)


# ------------------------------------------------------------------ окружение проверок
def _env(root):
    """-> (conf, roles, registry|None, agents|None)."""
    from tb.brain import roles_runtime
    try:
        from tb.llm import providers
        reg = providers.load(root)
    except Exception:
        reg = None
    agents = conf_mod.read_agents(root)
    return conf_mod.load(root, reg, agents), roles_runtime.load(root), reg, agents


def day_mode(root):
    """Режим дня из состояния платформы (state/control/platform.json): live только пока платформа не остановлена."""
    d = common.read_json(paths.control_dir("platform.json", root=root))
    d = d if isinstance(d, dict) else {}
    if d.get("state", "stopped") == "stopped":
        return "observe"
    return d.get("mode") if d.get("mode") in ("observe", "paper", "testnet", "live") else "observe"


def _parse_target(args, assign):
    """-> (список изменений [{team, role, scope}], kind, ошибка). kind: live_team | roles."""
    role, prof = args.get("role"), args.get("profile")
    if args.get("all") and prof:          # «профиль X для ВСЕХ ролей»: роли команды (live-команда или args.team) и все общие роли одной ревизией
        team = args.get("team") or assign.live_team
        if team not in assign.teams:
            return None, None, "команда неизвестна"
        return ([{"team": team, "role": r, "scope": "team"} for r in assign.teams[team]["roles"]]
                + [{"team": None, "role": r, "scope": "shared"} for r in assign.shared_roles]), "roles", None
    if args.get("team") and not role:
        role = "live"
    if role == "live":
        team = args.get("team") or args.get("profile")
        if team not in assign.teams:
            return None, None, "команда неизвестна"
        return [{"team": team, "role": None, "scope": "live"}], "live_team", None
    if not role or not prof:
        return None, None, "нужны role и profile"
    if "." in role:
        head, rname = role.split(".", 1)
    else:
        head, rname = None, role
    changes = []
    if head == "shared" or (head is None and rname in assign.shared_roles and rname not in ((assign.teams.get(assign.live_team) or {}).get("roles") or {})):
        if rname not in assign.shared_roles:
            return None, None, "общая роль неизвестна"
        changes.append({"team": None, "role": rname, "scope": "shared"})
    elif head in assign.teams or head is None:
        team = head or assign.live_team
        if rname in (assign.teams[team]["roles"]):
            changes.append({"team": team, "role": rname, "scope": "team"})
        elif head is None and rname in assign.teams:     # use <команда> <профиль>: все роли команды
            changes += [{"team": rname, "role": r, "scope": "team"} for r in assign.teams[rname]["roles"]]
        elif head is None and any(t["agent_id"] == rname for t in assign.teams.values()):
            tid = [k for k, t in assign.teams.items() if t["agent_id"] == rname][0]
            changes += [{"team": tid, "role": r, "scope": "team"} for r in assign.teams[tid]["roles"]]
        else:
            return None, None, "роль неизвестна"
    else:
        return None, None, "команда неизвестна"
    return changes, "roles", None


def _compat(role, profile, conf, reg):
    """-> текст проблемы или None: профиль есть, провайдер включён, флаги модели подходят роли."""
    p = conf.profiles.get(profile)
    if p is None:
        return "профиль %s неизвестен или отключён" % profile
    if reg is None:
        return None
    try:
        prov = reg.provider(p.provider)
        m = prov.model(p.model)
    except Exception:
        return "нет %s/%s в providers.json" % (p.provider, p.model)
    if not prov.enabled:
        return "провайдер %s отключён" % p.provider
    if role is not None and role.io == "tools" and not m.tools:
        return "модель профиля %s не умеет вызывать инструменты" % profile
    return None


def live_rights(agents, agent_id):
    """-> список проблем (пусто = агент может торговать в live): modes с live, enabled, autonomy.live != false, конечные лимиты."""
    ag = (agents or {}).get(agent_id)
    if not isinstance(ag, dict):
        return ["агента %s нет в agents.json" % agent_id]
    out = []
    lim = ag.get("limits") if isinstance(ag.get("limits"), dict) else {}
    if ag.get("enabled") is False:
        out.append("агент отключён")
    if "live" not in (lim.get("modes") or []):
        out.append("в modes нет live")
    au = ag.get("autonomy")
    if isinstance(au, dict) and au.get("live") is False:
        out.append("autonomy.live=false")
    for k in ("max_risk_usdt", "max_risk_pct"):
        v = lim.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v <= 0 or v == float("inf"):
            out.append("%s не задан или не конечен" % k)
    ms = lim.get("max_size_mult")
    if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not 0 < ms <= 1:
        out.append("max_size_mult должен быть в (0, 1]")
    return out


def ambiguous_state(root, now):
    """Неоднозначные состояния (жёсткое условие смены live): -> список причин."""
    out = []
    try:
        if os.listdir(paths.plans_inbox(root=root)):
            out.append("в _inbox плана есть необработанные команды")
    except OSError:
        pass
    from tb.brain import jobs
    n = jobs.count_open(root)
    if n:
        out.append("в очереди заданий мозга %d" % n)
    items, _ = common.load_plans(root)
    bad = [i["id"] for i in items if common.plan_status(i) == "draft" or (i["rt"].get("entry") or {}).get("state") in ("placing", "blocked")]
    if bad:
        out.append("планы в placing/blocked/draft: %s" % ",".join(bad[:3]))
    if [i["id"] for i in items if i["rt"].get("actions", {}).get("pending")]:
        out.append("есть ждущие действия (actions.pending)")
    if [i["id"] for i in items if i["rt"].get("hold") == "brain"]:
        out.append("есть hold по причине brain")
    try:
        from tb.core import bus
        st = bus.stats("api", root=root, now=now)
        if st.get("unacked_wake"):
            out.append("не подтверждено wake-событий: %d" % st["unacked_wake"])
    except Exception:
        pass
    h = common.exec_health(root=root, now=now)
    hr = (h.get("data") or {}).get("hold_reasons")
    if hr:
        out.append("hold_reasons исполнителя: %s" % ",".join(str(x) for x in (hr if isinstance(hr, list) else [hr])[:3]))
    return out


def probe_age(root, profile, conf, now):
    """Возраст последней успешной проверки провайдера профиля (с) или None."""
    p = conf.profiles.get(profile)
    if p is None:
        return None
    d = common.read_json(probe_file(root))
    e = (d or {}).get("%s/%s" % (p.provider, p.model)) if isinstance(d, dict) else None
    if isinstance(e, dict) and e.get("ok") and isinstance(e.get("ts"), (int, float)):
        return max(0.0, now - e["ts"])
    return None


def record_probe(root, conf_profile, ok, now):
    """Раннер и `brain probe` отмечают проверку провайдера профиля (provider/model)."""
    d = common.read_json(probe_file(root))
    d = d if isinstance(d, dict) else {}
    d["%s/%s" % (conf_profile.provider, conf_profile.model)] = {"ok": bool(ok), "ts": round(now, 3)}
    atomic_write_json(probe_file(root), d, indent=None)


def history_ok(root, agent, profiles, min_trades, min_days, now=None):
    """07b п. 7: дисциплина paper-истории пары (агент, профили): число закрытых paper-сделок с тегом agent.<id>, дни, нарушения инвариантов. Прибыль не учитывается."""
    now = time.time() if now is None else now
    opened, trades, days = {}, 0, set()
    for r in common.read_jsonl(paths.state("trades.jsonl", root=root)):
        if r.get("ev") == "open" and r.get("id"):
            opened[r["id"]] = r
        elif r.get("ev") == "close" and r.get("id") in opened:
            o = opened[r["id"]]
            tg = o.get("tags")
            tags = [str(x) for x in tg] if isinstance(tg, list) else str(tg or "").split(",")
            if o.get("mode") == "paper" and ("agent.%s" % agent) in tags:
                trades += 1
                days.add(str(r.get("ts_close") or "")[:10])
    viol = 0
    for d in decisions.read(0, limit=0, root=root):
        if d.get("actor") == "brain:%s" % agent and (d.get("res") or {}).get("code") in INVARIANT_CODES:
            viol += 1
    res = {"agent": agent, "profiles": sorted(profiles), "trades": trades, "days": len(days), "violations": viol, "need_trades": min_trades, "need_days": min_days,
           "ok": trades >= min_trades and len(days) >= min_days and viol == 0, "ts": round(now, 1)}
    try:
        atomic_write_json(history_ok_file(root), res, indent=None)
    except OSError:
        pass
    return res


# ------------------------------------------------------------------ проверка смены
def _w(wid, msg, **numbers):
    return {"id": wid, "code": wid, "msg": msg, "numbers": numbers}


def check(root, args, now=None, rollback=False):
    """Проверка смены БЕЗ применения -> {"ok","tier","danger","phrases","hard":[{id,ok,msg}],"warnings":[...],"changes":[...],"code","msg"}.
    ok=False только при провале жёстких условий (кроме фразы: её собирает ctl/CLI и передаёт phrase_ok)."""
    now = time.time() if now is None else now
    conf, roles, reg, agents = _env(root)
    assign = load_effective(root, conf)
    res = {"ok": False, "tier": 0, "danger": False, "phrases": [], "hard": [], "warnings": [], "changes": [], "code": None, "msg": "", "rev": assign.rev}
    if not conf.loaded or not conf.live_team:
        res.update(code="E_SWAP_TARGET", msg="config/brain.json не загружен")
        return res
    changes, kind, err = _parse_target(args or {}, assign)
    if err:
        res.update(code="E_SWAP_TARGET", msg=err)
        return res
    mode = day_mode(root)
    live = mode == "live"
    prof = (args or {}).get("profile")
    plan = []
    for ch in changes:
        if kind == "live_team":
            plan.append({"kind": "live_team", "from": assign.live_team, "to": ch["team"]})
        else:
            cur, _ = assign.chain(ch["role"], ch["team"])
            plan.append({"kind": "role", "team": ch["team"], "role": ch["role"], "from": cur[0] if cur else None, "to": prof})
    res["changes"] = plan
    hard = res["hard"]
    tier = 0
    if kind == "live_team":
        new_team = changes[0]["team"]
        if new_team == assign.live_team:
            res.update(ok=True, code="OK", msg="команда уже торгует", noop=True)
            return res
        tier = 2 if live else 0
        for r in roles.roles.values():           # профили новой команды должны быть пригодны для её ролей
            ch = assign.chain(r.id, new_team)[0] if r.scope == "team" else []
            for pid in ch[:1]:
                pb = _compat(r, pid, conf, reg)
                if pb:
                    hard.append({"id": "E_SWAP_PROFILE", "ok": False, "msg": pb})
        new_prof = [assign.teams[new_team]["roles"][r]["primary"] for r in assign.teams[new_team]["roles"]]
        agent_new = assign.agent_id(new_team)
    else:
        for c, p0 in zip(changes, plan):
            if p0["from"] == prof:
                continue
            pb = _compat(roles.get(c["role"]), prof, conf, reg)
            if pb:
                hard.append({"id": "E_SWAP_PROFILE", "ok": False, "msg": pb})
        if all(p0["from"] == prof for p0 in plan):
            res.update(ok=True, code="OK", msg="без изменений", noop=True)
            return res
        live_role = any((roles.get(c["role"]) is not None and roles.get(c["role"]).live_risk) and (c["team"] in (None, assign.live_team)) for c in changes)
        tier = 1 if (live and live_role) else 0
        new_prof, agent_new = [prof], assign.agent_id(assign.live_team)
    res["tier"] = tier
    if tier >= 1 and not rollback:
        sr = conf.swap_rules
        bad = live_rights(agents, agent_new)
        hard.append({"id": "E_SWAP_NO_LIVE_RIGHTS", "ok": not bad, "msg": "; ".join(bad) or "права live есть"})
        amb = ambiguous_state(root, now)
        hard.append({"id": "E_SWAP_AMBIGUOUS_STATE", "ok": not amb, "msg": "; ".join(amb) or "неоднозначных состояний нет"})
        age = [probe_age(root, p, conf, now) for p in new_prof]
        okp = all(a is not None and a <= float(sr["probe_max_age_s"]) for a in age)
        hard.append({"id": "E_SWAP_PROBE", "ok": okp, "msg": "проверка провайдера свежая" if okp else "нет свежей проверки провайдера (./tb brain probe %s --live)" % ",".join(sorted(set(new_prof)))})
        n = swaps_today(root, now)
        hard.append({"id": "E_SWAP_LIMIT_DAY", "ok": n < int(sr["max_swaps_day"]), "msg": "смен сегодня %d из %d" % (n, sr["max_swaps_day"])})
        w = res["warnings"]
        hist = history_ok(root, agent_new, new_prof, int(sr["min_paper_trades"]), int(sr["min_paper_days"]), now)
        if not hist["ok"]:
            w.append(_w("W_SWAP_PAPER_HISTORY", "paper-сделок %d из %d; дней %d из %d; нарушений инвариантов %d" % (hist["trades"], hist["need_trades"], hist["days"],
                                                                                                              hist["need_days"], hist["violations"]),
                        trades=hist["trades"], days=hist["days"], violations=hist["violations"]))
        nolive = sorted(set(p for p in new_prof if not (conf.profiles.get(p) and conf.profiles[p].live_ok)))
        if nolive and sr.get("require_live_ok", True):
            w.append(_w("W_SWAP_NOT_LIVE_OK", "профиль без метки live_ok: %s" % ",".join(nolive), profiles=len(nolive)))
        ls = last_swap_ts(root)
        if ls is not None and now - ls < float(sr["cooldown_min"]) * 60:
            w.append(_w("W_SWAP_COOLDOWN", "предыдущая смена %d мин назад (порог %d)" % ((now - ls) / 60, sr["cooldown_min"]), age_min=round((now - ls) / 60, 1)))
        w.append(_w("W_SWAP_NO_AB", "смена без A/B-сравнения (сравнительные команды отложены)"))
        old = assign.live_team if kind == "live_team" else ",".join(sorted(set(p0["from"] or "-" for p0 in plan)))
        new = changes[0]["team"] if kind == "live_team" else prof
        tpl = sr["phrase"] if kind == "live_team" else sr["phrase_model"]
        res["phrases"] = [tpl.format(old=old, new=new, role=changes[0].get("role") or "live")]
        res["danger"] = True
    elif rollback and tier >= 1:
        res["danger"] = True
    failed = [h for h in hard if not h["ok"]]
    if failed:
        res.update(code=failed[0]["id"], msg=failed[0]["msg"])
    else:
        res.update(ok=True, code="OK")
    return res


# ------------------------------------------------------------------ применение
def _emit_swap(root, kind, frm, to, rev, by, now):
    """Событие шины brain_swap (тип добавляет WP14 в bus.py; пока типа нет, молча пропускаем)."""
    try:
        from tb.core import bus
        if "brain_swap" not in bus.EVENT_TYPES:
            return False
        ident = lambda s: "".join(c for c in str(s) if c.isalnum() or c in "_.-")[:40] or "-"
        bus.emit("brain_swap", {"kind": ident(kind), "from": ident(frm), "to": ident(to), "rev": rev, "by": ident(by)}, root=root, now=now)
        return True
    except Exception:
        return False


def _write_rev(root, assign, rev, by, kind, now, key, prev_rev):
    obj = sign(dict(assign.body(), schema=SCHEMA, rev=rev, ts=round(now, 3), by=by, prev_rev=prev_rev), key)
    os.makedirs(history_dir(root), exist_ok=True)
    atomic_write_json(os.path.join(history_dir(root), "R-%d.json" % rev), obj, indent=None)
    atomic_write_json(active_file(root), obj, indent=None)
    try:
        names = sorted((n for n in os.listdir(history_dir(root)) if n.startswith("R-") and n.endswith(".json")),
                       key=lambda n: int(n[2:-5]) if n[2:-5].isdigit() else 0)
        for n in names[:-KEEP_HISTORY]:
            os.remove(os.path.join(history_dir(root), n))
    except OSError:
        pass
    return obj


def _next_rev(root, cur_rev):
    mx = cur_rev
    try:
        for n in os.listdir(history_dir(root)):
            if n.startswith("R-") and n.endswith(".json") and n[2:-5].isdigit():
                mx = max(mx, int(n[2:-5]))
    except OSError:
        pass
    return mx + 1


def _result(ok, code, msg="", **data):
    return {"ok": ok, "code": code, "msg": msg, "data": data}


def use(root, args, now=None, by="ctl", phrase_ok=False, rollback=False):
    """Смена назначений. args: role ("A.position_manager"|"shared.news_reader"|"position_manager"|"live"|команда), profile (или team для live), accepted [id предупреждений].
    -> {"ok","code","msg","data"}; данные: ревизия, изменения, предупреждения. phrase_ok=True ставит вызывающий, проверивший набранную фразу (ctl/CLI)."""
    now = time.time() if now is None else now
    args = args if isinstance(args, dict) else {}
    key = _key()
    if not key:
        return _result(False, "E_SWAP_NO_KEY", "нет ключа подписи (./tb control init-key)")
    with FileLock(bdir(root, "active.lock"), wait=10.0):
        chk = check(root, args, now, rollback=rollback)
        if chk.get("noop"):
            return _result(True, "OK", chk["msg"], rev=chk["rev"], changes=[])
        if not chk["ok"]:
            return _result(False, chk["code"] or "E_SWAP_TARGET", chk["msg"], hard=[h for h in chk["hard"] if not h["ok"]], tier=chk["tier"])
        if chk["tier"] >= 1 and not rollback:
            if not phrase_ok:
                return _result(False, "E_SWAP_PHRASE", "нужна фраза подтверждения", phrases=chk["phrases"], tier=chk["tier"], warnings=chk["warnings"])
            want = sorted(w["id"] for w in chk["warnings"])
            acc = sorted(set(args.get("accepted") or []))
            if want and not acc:
                return _result(False, "E_WARN", "не приняты предупреждения", warnings=chk["warnings"], tier=chk["tier"])
            if want and acc != want:
                return _result(False, "E_WARN_CHANGED", "набор предупреждений изменился", warnings=chk["warnings"], tier=chk["tier"])
        conf = conf_mod.load(root)
        cur = load_effective(root, conf, key)
        new = Assign(cur.rev, cur.live_team, copy.deepcopy(cur.teams), copy.deepcopy(cur.shared_roles), "active")
        for c in chk["changes"]:
            if c["kind"] == "live_team":
                new.live_team = c["to"]
            else:
                tgt = new.shared_roles if c["team"] is None else new.teams[c["team"]]["roles"]
                old = tgt[c["role"]]
                tgt[c["role"]] = {"primary": c["to"], "fallback": [x for x in old.get("fallback", []) if x != c["to"]]}
        rev = _next_rev(root, cur.rev)
        _write_rev(root, new, rev, by, "rollback" if rollback else "use", now, key, cur.rev)
        frm = ",".join(str(c["from"]) for c in chk["changes"])
        to = ",".join(str(c["to"]) for c in chk["changes"])
        accepted = [w for w in chk["warnings"] if w["id"] in set(args.get("accepted") or [])]
        rec = append_swap(root, {"rev": rev, "prev_rev": cur.rev, "kind": "rollback" if rollback else "use", "tier": chk["tier"],
                                 "target": [c.get("role") or "live" for c in chk["changes"]], "from": frm, "to": to, "by": by, "phrase_ok": bool(phrase_ok),
                                 "gates": [{"id": h["id"], "ok": h["ok"]} for h in chk["hard"]], "warnings_accepted": [{"id": w["id"], "numbers": w["numbers"]} for w in accepted],
                                 "result": "ok"}, now)
    _emit_swap(root, "rollback" if rollback else "use", frm, to, rev, by, now)
    return _result(True, "OK", "ревизия %d применена; следующие задания пойдут на новых назначениях" % rev, rev=rev, prev_rev=cur.rev, changes=chk["changes"], tier=chk["tier"],
                   seq=rec["seq"])


def role_tier(root, role_target):
    """Уровень влияния назначения роли: 1 = live-роль live-команды в режиме live (нужны гейты и фраза), иначе 0. role_target: строка роли (как у use)."""
    conf, roles, reg, agents = _env(root)
    assign = load_effective(root, conf)
    changes, kind, err = _parse_target({"role": role_target, "profile": "-"}, assign)
    if err or kind != "roles":
        return 0
    live = day_mode(root) == "live"
    return 1 if live and any((roles.get(c["role"]) is not None and roles.get(c["role"]).live_risk) and (c["team"] in (None, assign.live_team)) for c in changes) else 0


def set_chain(root, args, now=None, by="ctl", phrase_ok=False):
    """Цепочка роли целиком: args {role, primary?, fallback?[список профилей]}. Меняет подписанные назначения новой ревизией (откат: rollback).
    Не live-роль или режим не live: применяется сразу (аудит делает вызывающий). Live-роль live-команды в режиме live: можно сменить ТОЛЬКО основной профиль
    (через use: гейты, проба провайдера, фраза), порядок и состав запасных в live не меняются (остановите день)."""
    now = time.time() if now is None else now
    args = args if isinstance(args, dict) else {}
    key = _key()
    if not key:
        return _result(False, "E_SWAP_NO_KEY", "нет ключа подписи (./tb control init-key)")
    with FileLock(bdir(root, "active.lock"), wait=10.0):
        conf, roles, reg, _ag = _env(root)
        cur = load_effective(root, conf, key)
        changes, kind, err = _parse_target({"role": args.get("role"), "profile": args.get("primary") or "-"}, cur)
        if err or kind != "roles" or len(changes) != 1:
            return _result(False, "E_SWAP_TARGET", err or "нужна ровно одна роль")
        c = changes[0]
        tgt = cur.shared_roles if c["team"] is None else cur.teams[c["team"]]["roles"]
        old = tgt[c["role"]]
        new_primary = args.get("primary") or old["primary"]
        fb = args["fallback"] if "fallback" in args else [x for x in old.get("fallback", []) if x != new_primary]
        if not isinstance(fb, list) or len(fb) > 4 or len(set(fb)) != len(fb) or new_primary in fb or any(not isinstance(x, str) for x in fb):
            return _result(False, "E_SWAP_PROFILE", "запасные профили: список до 4 без повторов и без основного")
        # совместимость проверяется у того, что меняется: новый основной и НОВЫЕ запасные. Уже стоящий запасной с выключенным провайдером
        # (например an-sonnet при выключенном anthropic) не должен блокировать смену основного профиля: иначе роль нельзя переключить вовсе.
        fresh = [new_primary] if new_primary != old["primary"] else []
        fresh += [x for x in fb if x not in old.get("fallback", [])]
        bad = [("%s: %s" % (pid, pb)) for pid, pb in ((pid, _compat(roles.get(c["role"]), pid, conf, reg)) for pid in fresh) if pb]
        if bad:
            return _result(False, "E_SWAP_PROFILE", bad[0])
        if new_primary == old["primary"] and list(fb) == list(old.get("fallback", [])):
            return _result(True, "OK", "без изменений", rev=cur.rev, changes=[])
        live_role = bool(roles.get(c["role"]) is not None and roles.get(c["role"]).live_risk and c["team"] in (None, cur.live_team) and day_mode(root) == "live")
        if live_role:
            if list(fb) != list(old.get("fallback", [])) and not (new_primary != old["primary"] and list(fb) == [x for x in old.get("fallback", []) if x != new_primary]):
                return _result(False, "E_STATE", "в режиме live запасные профили live-роли не меняются: остановите день или смените только основной через brain.use")
            sub = dict(args, profile=new_primary)
            sub.pop("fallback", None)
            sub.pop("primary", None)
            ctx = True
        else:
            ctx = False
    if ctx:
        return use(root, sub, now, by=by, phrase_ok=phrase_ok)
    with FileLock(bdir(root, "active.lock"), wait=10.0):
        cur = load_effective(root, conf, key)
        new = Assign(cur.rev, cur.live_team, copy.deepcopy(cur.teams), copy.deepcopy(cur.shared_roles), "active")
        tgt = new.shared_roles if c["team"] is None else new.teams[c["team"]]["roles"]
        tgt[c["role"]] = {"primary": new_primary, "fallback": list(fb)}
        rev = _next_rev(root, cur.rev)
        _write_rev(root, new, rev, by, "chain", now, key, cur.rev)
        rec = append_swap(root, {"rev": rev, "prev_rev": cur.rev, "kind": "use", "tier": 0, "target": [c["role"]], "from": ",".join([old["primary"]] + list(old.get("fallback", []))),
                                 "to": ",".join([new_primary] + list(fb)), "by": by, "phrase_ok": False, "gates": [], "warnings_accepted": [], "result": "ok", "op": "chain"}, now)
    _emit_swap(root, "chain", old["primary"], new_primary, rev, by, now)
    return _result(True, "OK", "цепочка роли %s сохранена (ревизия %d)" % (c["role"], rev), rev=rev, prev_rev=cur.rev, role=c["role"], primary=new_primary, fallback=list(fb), seq=rec["seq"])


def rollback(root, args=None, now=None, by="ctl", phrase_ok=False):
    """Возврат к предыдущей ревизии (или args.to): новая ревизия с содержимым целевой. Отказ, если профиль целевой ревизии исчез/отключён."""
    now = time.time() if now is None else now
    args = args if isinstance(args, dict) else {}
    key = _key()
    if not key:
        return _result(False, "E_SWAP_NO_KEY", "нет ключа подписи (./tb control init-key)")
    with FileLock(bdir(root, "active.lock"), wait=10.0):
        conf, roles, reg, _ag = _env(root)
        cur = load_effective(root, conf, key)
        if cur.source != "active":
            return _result(False, "E_SWAP_ROLLBACK", "нет сохранённой ревизии для отката")
        to = args.get("to")
        if to is None:
            to = cur.prev_rev
        try:
            to = int(to)
        except (TypeError, ValueError):
            return _result(False, "E_SWAP_ROLLBACK", "ревизия не задана")
        if to <= 0:
            return _result(False, "E_SWAP_ROLLBACK", "предыдущей ревизии нет (была конфигурация brain.json): используйте reload --from-config")
        o, st = read_active(root, key, os.path.join(history_dir(root), "R-%d.json" % to))
        if o is None:
            return _result(False, "E_SWAP_ROLLBACK", "ревизия R-%d недоступна (%s)" % (to, st))
        tgt = Assign(o["rev"], o["live_team"], o["teams"], o["shared_roles"], "active")
        bad = []
        for chain in [c for t in tgt.teams.values() for c in t["roles"].values()] + list(tgt.shared_roles.values()):
            for pid in [chain["primary"]] + list(chain.get("fallback") or []):
                pb = _compat(None, pid, conf, reg)
                if pb:
                    bad.append(pb)
        if bad:
            return _result(False, "E_SWAP_PROFILE", "откат невозможен: " + "; ".join(sorted(set(bad))[:3]))
        rev = _next_rev(root, cur.rev)
        _write_rev(root, tgt, rev, by, "rollback", now, key, cur.rev)
        diff = [r for r in set(list(cur.profiles_map().items())) ^ set(list(tgt.profiles_map().items()))]
        rec = append_swap(root, {"rev": rev, "prev_rev": cur.rev, "kind": "rollback", "tier": 1 if day_mode(root) == "live" else 0, "target": ["rev-%d" % to],
                                 "from": str(cur.rev), "to": str(to), "by": by, "phrase_ok": bool(phrase_ok), "gates": [], "warnings_accepted": [], "result": "ok",
                                 "changed": len(diff)}, now)
    _emit_swap(root, "rollback", cur.rev, to, rev, by, now)
    return _result(True, "OK", "возврат к ревизии %d (новая ревизия %d)" % (to, rev), rev=rev, prev_rev=cur.rev, to=to, seq=rec["seq"])


def reload(root, args=None, now=None, by="ctl", phrase_ok=False):
    """Без параметров: сигнал `reload.req` демону (перечитать providers/roles/brain.json; профили ролей не меняются). from_config=true: новая ревизия из brain.json
    (в режиме live с изменением live-ролей отказ: используйте use)."""
    now = time.time() if now is None else now
    args = args if isinstance(args, dict) else {}
    if not args.get("from_config"):
        atomic_write_json(reload_req_file(root), {"ts": round(now, 3), "by": by}, indent=None)
        append_swap(root, {"rev": load_effective(root).rev, "kind": "reload", "tier": 0, "target": ["files"], "from": "-", "to": "-", "by": by, "phrase_ok": False,
                           "gates": [], "warnings_accepted": [], "result": "signal"}, now)
        return _result(True, "OK", "сигнал перечитывания отправлен демону")
    key = _key()
    if not key:
        return _result(False, "E_SWAP_NO_KEY", "нет ключа подписи (./tb control init-key)")
    with FileLock(bdir(root, "active.lock"), wait=10.0):
        conf = conf_mod.load(root)
        cur = load_effective(root, conf, key)
        if not conf.loaded or not conf.live_team:
            return _result(False, "E_SWAP_TARGET", "config/brain.json не загружен")
        base = from_conf(conf)
        if day_mode(root) == "live" and (base.live_team != cur.live_team or base.profiles_map() != cur.profiles_map()):
            return _result(False, "E_STATE", "в режиме live назначения меняются только через use с подтверждением")
        rev = _next_rev(root, cur.rev)
        _write_rev(root, base, rev, by, "reload", now, key, cur.rev)
        rec = append_swap(root, {"rev": rev, "prev_rev": cur.rev, "kind": "reload", "tier": 0, "target": ["config"], "from": str(cur.rev), "to": "config", "by": by,
                                 "phrase_ok": False, "gates": [], "warnings_accepted": [], "result": "ok"}, now)
    _emit_swap(root, "reload", cur.rev, "config", rev, by, now)
    return _result(True, "OK", "назначения взяты из config/brain.json (ревизия %d)" % rev, rev=rev, seq=rec["seq"])


# ------------------------------------------------------------------ интерфейс для ctl (brain_iface)
def classify(root, args, now=None, action="use"):
    """Для challenge/requirements ctl: {"danger","phrases","warnings","blocked","gates","tier"} без применения."""
    if action == "rollback":          # откат: условия T0 (без фразы и предупреждений), подтверждение nonce только в live
        live = day_mode(root) == "live"
        return {"danger": live, "phrases": [], "warnings": [], "blocked": False, "gates": [], "tier": 1 if live else 0, "code": "OK"}
    c = check(root, args, now)
    if c.get("noop"):
        return {"danger": False, "phrases": [], "warnings": [], "blocked": False, "gates": [], "tier": 0, "code": "OK"}
    return {"danger": c["danger"], "phrases": c["phrases"], "warnings": c["warnings"], "blocked": not c["ok"], "tier": c["tier"], "code": c["code"],
            "gates": [{"id": h["id"], "ok": h["ok"], "msg": h["msg"]} for h in c["hard"]] if c["hard"] else ([] if c["ok"] else [{"id": c["code"], "ok": False, "msg": c["msg"]}])}


def status(root, now=None):
    """-> {"live_team","agent","profiles","live_ok":{profile:{ok,eval_ts}},"last_swap_ts","rev","source","warn",...} (контракт brain_iface)."""
    now = time.time() if now is None else now
    conf = conf_mod.load(root)
    a = load_effective(root, conf)
    health = common.read_json(paths.runtime("brain", "state.json", root=root))
    health = health if isinstance(health, dict) else {}
    return {"live_team": a.live_team, "agent": a.agent_id(), "profiles": a.profiles_map(),
            "live_ok": {pid: {"ok": p.live_ok, "eval_ts": p.eval_ts} for pid, p in conf.profiles.items()},
            "last_swap_ts": last_swap_ts(root), "rev": a.rev, "source": a.source, "warn": a.warn, "by": a.by, "config_errors": conf.errors[:10],
            "protect_only": bool((health.get("protect_only") or {}).get("on")), "teams": {t: {"agent_id": v["agent_id"], "roles": {r: c["primary"] for r, c in v["roles"].items()}}
                                                                                         for t, v in a.teams.items()}}


def probe(root, now=None):
    """H6 для ctl: сервис brain жив (heartbeat), бюджет LLM не исчерпан, не «только защита», у live-ролей live_team есть пригодный профиль -> {"ok","msg","numbers"}."""
    now = time.time() if now is None else now
    from tb.platform import services as S
    age, hb = S.read_hb("brain", root, now)
    stale = float((((S.load_cfg(root).get("svc") or {}).get("brain")) or {}).get("hb_stale_s") or 300.0)
    if age is None or age > stale:
        return {"ok": False, "msg": "сервис мозга не отвечает (heartbeat %s)" % ("нет" if age is None else "%d с" % age), "numbers": {"hb_age_s": age, "limit_s": stale}}
    if hb.get("protect_only"):
        return {"ok": False, "msg": "мозг в режиме «только защита» (%s)" % str(hb.get("reason") or "-")[:60], "numbers": {"hb_age_s": age}}
    conf, roles, reg, agents = _env(root)
    a = load_effective(root, conf)
    ag = (agents or {}).get(a.agent_id()) if agents is not None else {}
    if not a.live_team or ag is None or (isinstance(ag, dict) and ag.get("enabled") is False):
        return {"ok": False, "msg": "агент live-команды %s не включён в agents.json" % a.agent_id(), "numbers": {"agent": a.agent_id()}}
    try:
        from tb.llm import budget as B
        st = B.from_registry(reg, root=root, clock=lambda: now).status() if reg is not None else {"state": "ok"}
        st.get("state")
        if st.get("state") == "exhausted":
            return {"ok": False, "msg": "суточный бюджет LLM исчерпан", "numbers": {"state": "exhausted"}}
    except Exception:
        pass
    from tb.brain import fallback
    for r in roles.roles.values():
        if r.live_risk and r.scope == "team" and r.id in (a.teams.get(a.live_team) or {}).get("roles", {}):
            if not fallback.usable(root, conf, reg, a.chain(r.id)[0], r, now, mode="live"):
                return {"ok": False, "msg": "для роли %s нет пригодного профиля" % r.id, "numbers": {"role": r.id}}
    return {"ok": True, "msg": "мозг отвечает (hb %d с)" % age, "numbers": {"hb_age_s": age, "rev": a.rev}}
