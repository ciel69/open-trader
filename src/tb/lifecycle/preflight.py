"""Preflight S1: проверки P1-P16 (07 п. 2.2). Чистые функции от словаря фактов `collect()`; сеть и биржа не нужны. WP14.

Проверка: {"id","name","sev":"R"|"A","ok","msg","numbers"}. R = красная (блокирует), A = жёлтая. В live красные = жёсткие гейты H1-H8 (их держит ctl),
жёлтые = мягкие предупреждения W (владелец принял их при запуске), поэтому в live жёлтые не блокируют.
"""
import json
import math
import os
import shutil
import time

from tb import paths
from tb.control import brain_iface, ctlstate, gates  # noqa: F401
from tb.core import blackout as BLACKOUT
from tb.core import localcfg
from tb.lifecycle import days
from tb.platform import services as S

MODES_PROFILE = gates.TARGET_PROFILE


FIX = S.PREFLIGHT_FIX      # одна строка действия на проверку (словарь лежит в platform.services: его читает и дашборд)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def chk(cid, name, sev, ok, msg, **numbers):
    return {"id": cid, "name": name, "sev": sev, "ok": bool(ok), "msg": msg, "numbers": numbers}


def _mtime_age(path, now):
    try:
        return max(0.0, now - os.stat(path).st_mtime)
    except OSError:
        return None


def collect(ctx, args, day_id=None):
    """Факты для проверок: файлы gates.collect + дополнительные (мозг, диск, возраст данных, карри)."""
    now = ctx.now()
    f = gates.collect(ctx.root, now, ctlstate.load_cfg(ctx.root), dict(args or {}))
    f["mode"] = (args or {}).get("mode") or "paper"
    f["lc"] = ctx.cfg
    f["day_id"] = day_id
    f["data_age"] = {n: _mtime_age(p, now) for n, p in (("market.json", paths.runtime("market.json", root=ctx.root)), ("context.json", paths.runtime("context.json", root=ctx.root)),
                                                         ("levels.json", paths.levels_file(root=ctx.root)))}
    f["has_plans"] = _has_plans(f)
    try:
        f["disk_free_mb"] = shutil.disk_usage(ctx.root).free / 1048576.0
    except OSError:
        f["disk_free_mb"] = None
    f["logs_mb"] = _dir_mb(paths.runtime("platform", "logs", root=ctx.root))
    f["probe"] = _probe(ctx, now)
    f["other_days"] = [d for d in (days.load(ctx.root, i) for i in days.list_ids(ctx.root)) if d and d.get("day_id") != day_id]
    f["carry_plans"] = carry_plan_ids(ctx.root, day_id)
    return f


def _has_plans(f):
    """Есть ли у исполнителя планы или позиции (тогда нужен levels.json: исполнитель пишет его только для символов с книгой)."""
    d = (f.get("health") or {}).get("data") or {}
    pl = d.get("plans") or {}
    n = sum(v for v in pl.values() if isinstance(v, int) and not isinstance(v, bool)) if isinstance(pl, dict) else 0
    snap = f.get("snapshot") or {}
    return bool(n or d.get("positions") or snap.get("positions") or snap.get("orders"))


def _probe(ctx, now):
    try:
        ok, msg = ctx.brain.available(ctx.root, now)
        return {"ok": bool(ok), "msg": str(msg or "")[:160]}
    except Exception:
        return {"ok": False, "msg": "проба мозга не удалась"}


def _dir_mb(path):
    total = 0
    try:
        for d, _dirs, fs in os.walk(path):
            for n in fs:
                try:
                    total += os.path.getsize(os.path.join(d, n))
                except OSError:
                    pass
    except OSError:
        pass
    return total / 1048576.0


def carry_plan_ids(root, exclude=None):
    """Планы из carry.json ПРЕДЫДУЩЕГО дня (позиции, перенесённые осознанно)."""
    prev = days.last_day(root, exclude=exclude)
    c = ctlstate.read_json(os.path.join(days.day_dir(root, prev["day_id"]), "carry.json")) if prev else None
    return [x.get("plan") for x in (c or {}).get("items") or [] if isinstance(x, dict) and x.get("plan")]


# ------------------------------------------------------------------ P1-P16
def p1(f):
    lc = f["lc"]["preflight"]
    st = f.get("platform") or {}
    if not lc.get("require_supervisor", True):
        return chk("P1", "Супервизор, ctl, здоровье платформы", "R", True, "проверка супервизора отключена настройкой")
    if not st.get("running"):
        return chk("P1", "Супервизор, ctl, здоровье платформы", "R", False, "платформа не запущена (./tb up)")
    if st.get("color") == "red":
        return chk("P1", "Супервизор, ctl, здоровье платформы", "R", False, "здоровье платформы красное: %s" % "; ".join((st.get("reasons") or [])[:2])[:150])
    ctl = (st.get("services") or {}).get("ctl") or {}
    if ctl and ctl.get("state") not in ("running",):
        return chk("P1", "Супервизор, ctl, здоровье платформы", "R", False, "сервис ctl: %s" % ctl.get("state"))
    return chk("P1", "Супервизор, ctl, здоровье платформы", "R", True, "платформа жива (%s)" % st.get("color"))


def p2(f):
    name = "Исполнитель: связь и сверка"
    h, lc = f["health"], f["lc"]["preflight"]
    d = h.get("data") or {}
    if not h.get("present") or not h.get("alive"):
        return chk("P2", name, "R", False, "исполнитель не жив", age_s=h.get("age_s"))
    if h.get("age_s") is not None and h["age_s"] >= float(lc["hb_max_s"]):
        return chk("P2", name, "R", False, "heartbeat исполнителя %d с (лимит %d)" % (h["age_s"], lc["hb_max_s"]), age_s=h["age_s"])
    want = MODES_PROFILE.get(f["mode"], "paper")
    if d.get("profile") != want or bool(d.get("observe")) != (f["mode"] == "observe"):
        return chk("P2", name, "R", False, "профиль исполнителя %s%s, нужен %s%s" % (d.get("profile"), " (observe)" if d.get("observe") else "", want,
                                                                               " (observe)" if f["mode"] == "observe" else ""))
    if d.get("hold_reasons"):
        return chk("P2", name, "R", False, "hold у исполнителя: %s" % ",".join(str(x) for x in d["hold_reasons"][:3]))
    ra = d.get("reconcile_age_s")
    if not _num(ra) or ra >= float(lc["reconcile_max_s"]):
        return chk("P2", name, "R", False, "сверка с биржей устарела (%s с)" % ("нет" if not _num(ra) else int(ra)), reconcile_age_s=ra)
    if d.get("diffs"):
        return chk("P2", name, "R", False, "расхождений с биржей: %s" % d["diffs"], diffs=d["diffs"])
    return chk("P2", name, "R", True, "исполнитель в норме (сверка %d с назад)" % ra, reconcile_age_s=ra)


def p3(f):
    d = (f["health"].get("data") or {})
    pub = ((d.get("ws") or {}).get("public")) or {}
    lim = float(((f.get("limits") or {}).get("connectivity") or {}).get("public_price_stale_s") or 10.0)
    if not pub.get("up") or (_num(pub.get("age_s")) and pub["age_s"] > lim):
        return chk("P3", "Фид цен", "R", False, "публичный фид не отдаёт цены (up=%s, пауза %s с, лимит %d)" % (pub.get("up"), pub.get("age_s"), lim))
    return chk("P3", "Фид цен", "R", True, "фид отвечает")


def p4(f):
    st = f.get("platform") or {}
    lim = float(f["lc"]["preflight"]["scanner_hb_s"])
    sc = (st.get("scanner") or {}).get("age_s")
    disp = (st.get("services") or {}).get("dispatch") or {}
    bad = []
    if not _num(sc) or sc > lim:
        bad.append("сканер молчит (%s с)" % ("нет данных" if not _num(sc) else int(sc)))
    if disp.get("state") != "running":
        bad.append("диспетчер: %s" % (disp.get("state") or "нет"))
    if bad:
        return chk("P4", "Сканер и диспетчер", "A", False, "; ".join(bad))
    return chk("P4", "Сканер и диспетчер", "A", True, "сканер и диспетчер живы")


def p5(f):
    g = gates.h1(f)
    return chk("P5", "KILL чист", "R", g["ok"], g["msg"])


def _key_probe(f):
    """Наличие ключа биржи БЕЗ чтения значений: launcher.key_status, если есть; иначе косвенно (исполнитель в профиле жив)."""
    prof = MODES_PROFILE.get(f["mode"], "paper")
    if prof == "paper":
        return True, "ключ биржи не нужен (paper)"
    try:
        from tb import launcher
        fn = getattr(launcher, "key_status", None)
        if callable(fn):
            r = fn(prof)
            ok = bool(r.get("ok")) if isinstance(r, dict) else bool(r)
            why = "" if ok else (" (%s)" % (r.get("reason") if isinstance(r, dict) else "?"))
            return ok, "ключ %s: %s%s" % (prof, "на месте" if ok else "не найден", why)
    except Exception:
        pass
    d = f["health"].get("data") or {}
    if f["health"].get("alive") and d.get("profile") == prof:
        return True, "ключ подтверждён косвенно: исполнитель в профиле %s жив" % prof
    return False, "ключ %s не подтверждён" % prof


def _provider_key(f):
    """Ключ провайдера основного профиля (только наличие). -> (ok, msg) | None (профиль не определён)."""
    try:
        import json as _j
        from tb.llm import keys
        act = f["brain"].get("profiles") or {}
        prof = act.get("morning_analyst") or next(iter(act.values()), None)
        if not prof or "/" not in str(prof):
            return None
        pid = str(prof).split("/")[0]
        provs = (localcfg.read("providers", f["root"]) or {}).get("providers") or {}
        p = provs.get(pid) or {}
        if not p.get("key_file") and not p.get("key_env"):
            return None
        ok = bool(keys.present(p.get("key_file"), p.get("key_env"))[0])
        return ok, "ключ провайдера %s: %s" % (pid, "на месте" if ok else "не найден")
    except Exception:
        return None


def p6(f):
    ok, msg = _key_probe(f)
    pk = _provider_key(f)
    if pk is not None and not pk[0]:
        return chk("P6", "Ключи", "R", False, pk[1])
    return chk("P6", "Ключи", "R", ok, msg + ("; " + pk[1] if pk else ""))


def p7(f):
    sev = "A" if f["mode"] in ("paper", "observe") else "R"
    snap = f["snapshot"] or {}
    acct = snap.get("account") or {}
    eq, free = acct.get("equity"), acct.get("free")
    if not _num(eq):
        return chk("P7", "Баланс и сверка", sev, False, "нет equity в снимке исполнителя")
    tot = sum(float(v.get("equity") or 0.0) for v in (f["portfolio"].get("venues") or {}).values() if isinstance(v, dict))
    dev = float(((f.get("limits") or {}).get("equity_sync") or {}).get("max_dev_frac") or 0.05)
    frac = abs(eq - tot) / max(eq, tot, 1e-9) if tot else 0.0 if f["mode"] in ("paper", "observe") else 1.0
    if frac > dev:
        return chk("P7", "Баланс и сверка", sev, False, "equity биржи %.2f vs портфель %.2f (отклонение %.1f%% > %.1f%%)" % (eq, tot, frac * 100, dev * 100), equity=eq, portfolio=tot)
    need = float(f["lc"]["preflight"]["min_free_usdt"])
    if not _num(free) or free < need:
        return chk("P7", "Баланс и сверка", sev, False, "свободной маржи %s < %.2f" % (free, need), free=free)
    return chk("P7", "Баланс и сверка", sev, True, "equity %.2f сверена, свободно %.2f" % (eq, free))


def p8(f):
    snap = f["snapshot"] or {}
    bad = [p.get("sym") for p in snap.get("positions") or [] if not p.get("owner")]       # перенесённая позиция имеет план-владельца (owner)
    bad += [o.get("sym") for o in snap.get("orders") or [] if o.get("role") == "other" and not o.get("reduce_only")]
    bad = [x for x in bad if x]
    if bad:
        return chk("P8", "Нет позиций/ордеров без плана", "R", False, "без плана: %s" % ", ".join(sorted(set(bad))[:5]), count=len(bad))
    return chk("P8", "Нет позиций/ордеров без плана", "R", True, "лишних позиций и ордеров нет")


def p9(f):
    bad = []
    for n in ("agents.json", "exec_limits.json", "risk_profile.json", "risk_levels.json"):
        try:
            with open(paths.config(n, root=f["root"]), encoding="utf-8") as fh:
                json.load(fh)
        except (OSError, ValueError):
            bad.append(n)
    if f.get("limits_err"):
        bad.append("exec_limits: %s" % f["limits_err"][:60])
    if f.get("limits_warn"):
        bad.append("предупреждения exec_cfg: %d" % len(f["limits_warn"]))
    if bad:
        return chk("P9", "Конфигурации читаются, без предупреждений", "R", False, "; ".join(bad[:4]))
    return chk("P9", "Конфигурации читаются, без предупреждений", "R", True, "конфиги целы")


def p10(f):
    lim = float(f["lc"]["preflight"]["data_max_age_s"])
    need_levels = bool(f.get("has_plans"))          # levels.json исполнитель пишет только при планах/позициях: без них его отсутствие не замечание
    items = [(n, a) for n, a in f["data_age"].items() if n != "levels.json" or need_levels]
    bad = ["%s: %s" % (n, "нет" if a is None else "%d с" % a) for n, a in items if a is None or a > lim]
    if bad:
        return chk("P10", "Свежесть рыночных данных", "A", False, "устарели: " + ", ".join(bad), limit_s=lim)
    return chk("P10", "Свежесть рыночных данных", "A", True, "market, context свежие" + (", levels" if need_levels else "; levels не нужны (планов нет)"))


def p11(f):
    sev = "R" if f["mode"] == "live" else "A"
    pr = f["probe"]
    if not pr["ok"]:
        return chk("P11", "Бюджет LLM и ответ основного профиля", sev, False, pr["msg"] or "мозг недоступен")
    return chk("P11", "Бюджет LLM и ответ основного профиля", sev, True, pr["msg"] or "мозг отвечает")


def p12(f):
    lc = f["lc"]["preflight"]
    bad = []
    if f["disk_free_mb"] is not None and f["disk_free_mb"] < float(lc["disk_min_mb"]):
        bad.append("свободно %d МБ < %d" % (f["disk_free_mb"], lc["disk_min_mb"]))
    if f["logs_mb"] > float(lc["logs_max_mb"]):
        bad.append("логи %d МБ > %d" % (f["logs_mb"], lc["logs_max_mb"]))
    if bad:
        return chk("P12", "Диск и логи", "A", False, "; ".join(bad))
    return chk("P12", "Диск и логи", "A", True, "диск в норме")


def p13(f):
    if f["mode"] != "live":
        return chk("P13", "Дневной лимит убытка", "A", True, "вне live не проверяется")
    g = gates.h5(f)
    return chk("P13", "Дневной лимит убытка", "R", g["ok"], g["msg"])


def p14(f):
    bl = ((f.get("facts") or {}).get("calendar") or {}).get("blackout") if isinstance(f.get("facts"), dict) else None
    lim = int(f["lc"]["preflight"]["blackout_min"])
    if isinstance(bl, dict) and _num(bl.get("in_min")) and bl["in_min"] <= lim:
        note = ""
        if BLACKOUT.policy(f["root"], f.get("mode"))["mode"] == "warn":      # paper + blackout_mode=warn: окно = предупреждение (жёлтый), не запрет
            note = "; paper: блэкаут = предупреждение, вход только с тегом blackout и размером не выше потолка"
        return chk("P14", "Календарь: нет blackout в первые 30 минут", "A", False, "старт в окне новостей: %s (%s)%s" % (bl.get("name") or "событие",
                                                                                                                 "идёт" if bl["in_min"] <= 0 else "через %d мин" % bl["in_min"], note), in_min=bl["in_min"])
    return chk("P14", "Календарь: нет blackout в первые 30 минут", "A", True, "окна новостей в ближайшие %d минут нет" % lim)


def p15(f):
    fx = f.get("facts")
    lim = float(f["lc"]["preflight"]["facts_max_age_s"])
    if not isinstance(fx, dict):
        return chk("P15", "Фактура (intel)", "A", False, "facts.json нет: сервис intel не запущен или ещё не опубликовал (запусти ./tb intel verify и перезапусти платформу)")
    age = f["now"] - fx["ts"] if _num(fx.get("ts")) else None
    bad = []
    if age is None or age > lim:
        bad.append("устарела (%s с)" % ("?" if age is None else int(age)))
    if fx.get("degraded"):
        bad.append("источники недоступны: %s" % ",".join(str(x) for x in fx["degraded"][:4]))
    srcs = fx.get("sources") if isinstance(fx.get("sources"), dict) else {}
    if srcs and not any(isinstance(v, dict) and v.get("v") for v in srcs.values()):
        bad.append("источники не проверены владельцем, запусти ./tb intel verify")
    if bad:
        return chk("P15", "Фактура (intel)", "A", False, "; ".join(bad))
    return chk("P15", "Фактура (intel)", "A", True, "фактура свежая")


def p16(f):
    bad = [d["day_id"] for d in f["other_days"] if d.get("state") in ("starting", "closing")]
    if bad:
        return chk("P16", "Нет незавершённого предыдущего дня", "R", False, "день %s не завершён" % ", ".join(bad[:2]))
    return chk("P16", "Нет незавершённого предыдущего дня", "R", True, "предыдущих незавершённых дней нет")


CHECKS = (p1, p2, p3, p4, p5, p6, p7, p8, p9, p10, p11, p12, p13, p14, p15, p16)


def evaluate(f):
    out = []
    for fn in CHECKS:
        try:
            out.append(fn(f))
        except Exception as e:      # сбой проверки = её провал, но не падение дня
            out.append(chk(fn.__name__.upper(), fn.__name__, "R", False, "проверка не выполнилась: %s" % e.__class__.__name__))
    return out


def verdict(checks, mode, allow_amber):
    """-> {"ok","red":[...],"amber":[...],"blocking":[...]}: blocking = то, из-за чего старт отклонён."""
    red = [c for c in checks if not c["ok"] and c["sev"] == "R"]
    amber = [c for c in checks if not c["ok"] and c["sev"] == "A"]
    blocking = list(red) if mode == "live" else list(red) + ([] if allow_amber else list(amber))
    return {"ok": not blocking, "red": red, "amber": amber, "blocking": blocking}


def public(c):
    """Проверка для интерфейса: id, имя, важность, сообщение и одна строка действия."""
    return {"id": c["id"], "name": c.get("name"), "sev": c["sev"], "ok": c["ok"], "msg": c["msg"], "action": FIX.get(c["id"], "")}


def run(ctx, args, day_id=None):
    f = collect(ctx, args, day_id)
    checks = evaluate(f)
    v = verdict(checks, f["mode"], bool((args or {}).get("allow_amber")))
    v["checks"] = checks
    v["facts"] = f
    return v
