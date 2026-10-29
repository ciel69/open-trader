"""Раннер заданий API-мозга (WP11-C2): задание -> досье -> роль (слой llm) -> проверка ответа -> действие через шлюз -> журнал решений -> подтверждение событий.

Один `Runner` обслуживает потоки-воркеры демона (`api_daemon.run`) и тесты (`step()` берёт и выполняет одно задание). Что делает на каждое задание:
  1. `EffectiveCache.get()`: действующие назначения (подписанный active.json или config/brain.json); ревизия фиксируется в задании (`job.rev`), задание доделывается своей цепочкой;
  2. защиты: лимит заданий в час и детектор зацикливания по ключу (guards.py); бюджет дня/часа (llm.budget): исчерпан -> режим «только защита»;
  3. роли по типам событий (`roles_runtime`), цепочка профилей роли с запасными (fallback.py), охлаждение неисправных, запрет запасного без live_ok для live-ролей;
  4. досье `get_brief`+`get_events` через шлюз от имени агента команды (права agents.json), ответ модели -> извлечение JSON -> схема роли -> смысловая проверка; повтор формата;
     запасной профиль один раз; затем «нет действия» (position_manager = hold + событие error BAD_FORMAT);
  5. исполнение решения теми же инструментами шлюза (stop_plan/close_plan/...), `meta` (профиль, задание) передаётся шлюзу: он ставит теги agent./model. сам;
  6. запись `state/decisions.jsonl` (actor brain:<агент>, basis: job/role/profile/usd), учёт токенов/долларов (llm_calls.jsonl пишет LLMClient), задание закрывается, события подтверждаются (ack).
Раннер не импортирует биржу и исполнитель: любые действия идут через `tb.gateway`.
Режим «только защита» (`fallback.Health.protect`): не исполняются submit_plan/amend_plan/release_plan, LLM вызывается только пока есть допустимый профиль; позиции ведёт исполнитель.
"""
import json
import os
import threading
import time

from tb import paths
from tb.brain import common, conf as conf_mod, entryplan, fallback, guards, jobs, roles_runtime, swap, validate
from tb.core import decisions, plan_schema

TRUNC_MAX_TOKENS = 8192         # потолок удвоения лимита вывода при обрезанном ответе
from tb.llm import budget as budget_mod, schema as schema_mod, toolloop, usage as usage_mod
from tb.llm.types import LLMError, Req, assistant, user

MAX_BRIEF_CHARS = 20000
MAX_EVENTS = 20
PROTECT_BLOCKED = ("submit_plan", "amend_plan", "release_plan")
DECISION_TOOLS = {"move_stop": "stop_plan", "close": "close_plan", "partial_close": "close_plan", "cancel": "cancel_plan", "amend": "amend_plan",
                  "hold_plan": "hold_plan", "release_plan": "release_plan"}


class Deferred(Exception):
    """Задание нельзя выполнить сейчас (нет профиля/бюджета): вернуть в очередь без траты попытки."""

    def __init__(self, code, retry_in_s=30.0):
        Exception.__init__(self, code)
        self.code, self.retry_in_s = code, retry_in_s


class JobBudget(Exception):
    pass


class DossierError(Exception):
    pass


def _num_str(x):
    s = ("%.10f" % float(x)).rstrip("0").rstrip(".")
    return s or "0"


class Runner(object):
    def __init__(self, root=None, clock=time.time, client_factory=None, gateway=None, probe_fn=None, conf=None, roles=None, registry=None):
        self.root = root or paths.root()
        self.clock = clock
        self.client_factory = client_factory
        self.probe_fn = probe_fn
        self._gw = gateway
        self._lk = threading.RLock()
        self._clients = {}
        self.inflight = {}
        self.health = fallback.Health(self.root, clock)
        self.cache = swap.EffectiveCache(self.root, clock)
        self.stop_event = threading.Event()
        self._conf_override, self._roles_override, self._reg_override = conf, roles, registry
        self.errors = []
        self.reload()

    # ------------------------------------------------------------------ конфигурация
    def reload(self):
        """Перечитывает providers.json, brain.json, роли, agents.json; профили/назначения активной ревизии не трогает (их читает EffectiveCache)."""
        errs = []
        reg = self._reg_override
        if reg is None:
            try:
                from tb.llm import providers
                reg = providers.load(self.root)
                errs += ["провайдер %s: %s" % (e["provider"], e["error"]) for e in reg.errors]
            except Exception:
                reg = None
                errs.append("providers.json не прочитан")
        self.reg = reg
        self.agents = conf_mod.read_agents(self.root) or {}
        self.conf = self._conf_override or conf_mod.load(self.root, reg, self.agents)
        self.roles = self._roles_override or roles_runtime.load(self.root)
        errs += self.conf.errors + self.roles.errors
        self.errors = errs
        g = self.conf.runtime.get("guards") or {}
        b = self.conf.budget
        self.rate = guards.JobRate(g.get("jobs_total_per_hour", 60), b.get("jobs_per_hour", 12), b.get("critical_per_hour", 30), g.get("scan_jobs_per_hour"))
        self.role_rate = guards.RoleRate(g.get("role_jobs_per_hour"))
        self.loop = guards.LoopGuard(g.get("loop_jobs_max", 8), g.get("loop_window_s", 600), g.get("loop_block_s", 600))
        self.repeat = guards.RepeatGuard(g.get("same_decision_max", 3), g.get("same_decision_window_s", 3600))
        lim = {}
        if reg is not None:
            self.budget = budget_mod.from_registry(reg, root=self.root, clock=self.clock)
            lim = self.budget.limits
        else:
            self.budget = budget_mod.Budget({}, root=self.root, clock=self.clock)
        for k in ("day_usd", "warn_frac"):
            v = self.conf.budget.get(k)
            if v and (not self.budget.limits.get(k) or k == "warn_frac" or v < self.budget.limits[k]):
                self.budget.limits[k] = v
        self.budget.limits.setdefault("job_usd", self.conf.budget.get("job_usd"))
        self._clients.clear()
        return errs

    @property
    def gateway(self):
        if self._gw is None:
            from tb.gateway import rpc
            self._gw = rpc.Gateway(self.root)
        return self._gw

    def sub_roles(self, sub_id):
        """Карта «роль -> типы событий» из подписки (config/subscriptions.json, поле roles у подписчика) или None (тогда действует wake_on роли).
        Строки без известной роли и типы вне шины отбрасываются: ошибка в карте не включает лишнего."""
        try:
            from tb.core import localcfg
            doc = localcfg.read("subscriptions", self.root)
            from tb.core import bus
            for it in doc.get("subscribers") or []:
                if isinstance(it, dict) and it.get("id") == sub_id and isinstance(it.get("roles"), dict):
                    return {r: [t for t in ts if t in bus.EVENT_TYPES] for r, ts in it["roles"].items() if r in self.roles.roles and isinstance(ts, list)}
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        return None

    def agent_consumer(self, agent_id):
        return ((self.agents.get(agent_id) or {}).get("consumer")) or "api"

    # ------------------------------------------------------------------ события
    def event(self, typ, d, pri=None):
        try:
            from tb.core import bus
            if typ in bus.EVENT_TYPES:
                bus.emit(typ, d, pri=pri, root=self.root, now=self.clock())
        except Exception:
            pass

    def enter_protect(self, reason, code):
        if self.health.set_protect(True, reason, code, self.clock()):
            if code == "brain_budget":
                self.event("limit_hit", {"which": "brain_budget", "until_ts": self._budget_until()})
            else:
                self.event("error", {"where": "brain", "code": "BRAIN_DOWN"})
                self.event("limit_hit", {"which": "brain_down"})

    def leave_protect(self):
        if self.health.set_protect(False, now=self.clock()):
            from tb.core import bus
            self.event("error", {"where": "brain", "code": "BRAIN_RECOVERED"}, pri=bus.PRI_INFO)

    def _budget_until(self):
        st = self.budget.status(None)
        return st.get("until_ts") or round(self.clock() + 3600)

    # ------------------------------------------------------------------ клиент LLM
    def client_for(self, profile_id):
        with self._lk:
            c = self._clients.get(profile_id)
            if c is not None:
                return c
            p = self.conf.profiles[profile_id]
            if self.client_factory is not None:
                c = self.client_factory(profile_id, p)
            else:
                from tb.llm.client import LLMClient
                c = LLMClient(self.reg, p.provider, p.model, root=self.root, budget=self.budget, clock=self.clock)
            self._clients[profile_id] = c
            return c

    def model_flags(self, p):
        try:
            m = self.reg.provider(p.provider).model(p.model)
            return {"json_mode": m.json_mode, "strict": m.strict, "tools": m.tools}
        except Exception:
            return {"json_mode": True, "strict": False, "tools": True}

    # ------------------------------------------------------------------ одно задание
    def step(self):
        """Берёт и выполняет одно готовое задание -> True, если задание было."""
        now = self.clock()
        assign = self.cache.get(self.conf)
        with self._lk:
            busy = set(v for v in self.inflight.values())
        rt = self.conf.runtime
        job = jobs.pick(self.root, now, owner="brain", busy_keys=busy, max_attempts=rt["max_attempts"], ttl_s=rt["job_ttl_s"], rev=assign.rev)
        if job is None:
            return False
        with self._lk:
            self.inflight[job["id"]] = job.get("key")
        try:
            self.process(job, assign)
        finally:
            with self._lk:
                self.inflight.pop(job["id"], None)
        return True

    def drain(self, limit=100):
        n = 0
        while n < limit and self.step():
            n += 1
        return n

    def _team_of(self, job, assign):
        sub = job.get("sub")
        for tid, t in assign.teams.items():
            if t.get("agent_id") == sub:
                return tid
        return assign.live_team

    def process(self, job, assign):
        now = self.clock()
        res = {"rev": assign.rev, "roles": []}
        status = "done"
        try:
            if not self.conf.loaded or not assign.live_team:
                self.enter_protect("config/brain.json не загружен или нет live_team", "brain_down")
                return self._close(job, "new", dict(res, outcome="deferred", code="NO_CONFIG", defer=True, retry_in_s=60.0))
            team = self._team_of(job, assign)
            if team != assign.live_team:
                return self._close(job, "skipped", dict(res, outcome="skipped", code="NOT_LIVE_TEAM"))
            if not (job.get("result") or {}).get("defer"):      # повтор отложенного задания защиты по частоте повторно не считает
                ok, code = self.rate.allow(job.get("sub"), job.get("prio") if isinstance(job.get("prio"), int) else 9, now)
                if not ok:
                    self.event("error", {"where": "brain", "code": "BRAIN_RATE"})
                    return self._close(job, "skipped", dict(res, outcome="skipped", code=code))
                lok, newblock = self.loop.check(job.get("key") or job["id"], now)
                if not lok:
                    if newblock:
                        self.event("error", {"where": "brain", "code": "BRAIN_LOOP"})
                    return self._close(job, "skipped", dict(res, outcome="skipped", code="LOOP_BLOCKED"))
            rls = [r for r in self.roles.for_event_types(job.get("types"), self.sub_roles(job.get("sub")), kinds=job.get("kinds") or []) if r.scope in ("team", "shared")]
            if not rls:
                return self._close(job, "skipped", dict(res, outcome="skipped", code="NO_ROLE"))
            meter = budget_mod.JobMeter(self.conf.budget.get("job_usd") or None, self.conf.budget.get("job_tokens") or None)
            agent_id = assign.agent_id(team)
            outcomes = []
            for role in rls:
                if not self.role_rate.allow(role.id, now):          # потолок заданий роли в час (entry_scout: runtime.guards.role_jobs_per_hour)
                    outcomes.append({"role": role.id, "outcome": "skipped", "code": "ROLE_RATE"})
                    continue
                try:
                    r = self.run_role(role, job, assign, team, agent_id, meter)
                except Deferred as d:
                    return self._close(job, "new", dict(res, outcome="deferred", code=d.code, defer=True, retry_in_s=d.retry_in_s, roles=outcomes))
                outcomes.append(r)
            res["roles"] = outcomes
            if outcomes and all(o.get("code") == "ROLE_RATE" for o in outcomes):
                status = "skipped"
            res["usd"] = round(meter.usd, 6)
            res["tokens"] = meter.tokens
            res["outcome"] = next((o["outcome"] for o in outcomes if o["outcome"] not in ("hold", "no_action", "skipped", "idle")), outcomes[-1]["outcome"] if outcomes else "skipped")
            res["code"] = outcomes[-1].get("code") if outcomes else None
            return self._close(job, status, res)
        except Exception as e:        # сбой посреди задания: событие остаётся неподтверждённым, задание вернётся в очередь (до max_attempts)
            try:    # причина в лог сервиса (stderr -> runtime/logs/brain.log): в шину и ответы уходит только код, без текста
                import sys
                import traceback
                sys.stderr.write("brain: сбой задания %s: %s\n%s" % (job.get("id"), e.__class__.__name__, "".join(traceback.format_exception(type(e), e, e.__traceback__)[-4:])))
                sys.stderr.flush()
            except Exception:
                pass
            self.event("error", {"where": "brain", "code": "JOB_ERROR"})
            st = "new" if int(job.get("attempts") or 1) < self.conf.runtime["max_attempts"] else "failed"
            return self._close(job, st, dict(res, outcome="error", code=e.__class__.__name__, retry_in_s=15.0))

    def _close(self, job, status, result):
        cur = jobs.finish(self.root, job, status, result, self.clock())
        if status != "new":
            try:
                jobs.ack_through(self.root, self.agent_consumer(self._agent_for_sub(job)), self.clock())
            except Exception:
                pass
        return cur

    def _agent_for_sub(self, job):
        a = self.cache.get(self.conf)
        for t in a.teams.values():
            if t.get("agent_id") == job.get("sub"):
                return t["agent_id"]
        return a.agent_id()

    # ------------------------------------------------------------------ роль
    def run_role(self, role, job, assign, team, agent_id, meter):
        if role.output_schema == "casereview/1":
            return self._run_reviews(role, job, assign, team, agent_id, meter)
        return self._run_role_one(role, job, assign, team, agent_id, meter)

    MAX_REVIEWS_PER_JOB = 3

    def _run_reviews(self, role, job, assign, team, agent_id, meter):
        """Ревьюер: по одному вызову модели на каждый закрытый случай задания (до MAX_REVIEWS_PER_JOB). Случая нет: прежний путь C2 (вызов с <event>, итог NO_CASE_ID,
        в память ничего не пишется)."""
        cids = self._review_case_ids(job)
        if not cids:
            return self._run_role_one(role, job, assign, team, agent_id, meter)
        outs = [self._run_role_one(role, dict(job, case_id=cid), assign, team, agent_id, meter) for cid in cids[:self.MAX_REVIEWS_PER_JOB]]
        return next((o for o in outs if o["outcome"] == "decision"), outs[-1])

    def _review_case_ids(self, job):
        """case_id для ревьюера: явный в задании, иначе по событиям position_closed задания (journal_id) через случаи памяти. Писатель (memory.tick) вызывается здесь же,
        чтобы случай закрытия точно существовал; уже разобранные, исключённые из статистики и не-сделки пропускаются."""
        cid = job.get("case_id")
        if isinstance(cid, str) and cid:
            return [cid]
        seqs = [x for x in (job.get("seqs") or []) if isinstance(x, int) and not isinstance(x, bool)]
        if not seqs:
            return []
        try:
            from tb.core import bus
            from tb.memory import cases as mcases, tick as mtick
            mtick.sync_now(self.root, self.clock())
            jids = [(e.get("d") or {}).get("journal_id") for e in bus.read(after_seq=min(seqs) - 1, limit=0, root=self.root)
                    if e.get("seq") in seqs and e.get("type") == "position_closed"]
            idx = dict(((c.get("refs") or {}).get("journal_id"), c) for c in mcases.load(self.root) if c.get("type") == "trade")
            return [idx[j]["case_id"] for j in jids if j in idx and not idx[j].get("review") and not idx[j].get("exclude_from_stats")]
        except Exception:
            return []

    def _run_role_one(self, role, job, assign, team, agent_id, meter):
        out = {"role": role.id, "outcome": "no_action", "code": None}
        meta = {"job": job["id"], "role": role.id, "team": team, "agent": agent_id, "rev": assign.rev, "event_seq": max(job.get("seqs") or [0])}
        if role.output_schema == "entry/1":                  # дёшево отсекаем до вызова модели: не paper / только защита / KILL
            blk = self._entry_block(agent_id, None)
            if blk:
                out.update(outcome="no_action", code=blk)
                self._journal(job, role, agent_id, None, "skip", blk, "entry_scout не работает: %s" % blk, meta, None, meter, assign)
                return out
        try:
            text, ctx = self._input(role, job, agent_id, meta)
        except DossierError as e:
            if str(e).startswith("NO_PLAN"):          # не ошибка: мозг просто ждёт (нет планов и позиций). Нейтральная запись «idle», события error на шине нет
                out.update(outcome="idle", code="IDLE_NO_PLAN")
                self._journal(job, role, agent_id, None, "note", "IDLE_NO_PLAN", "нет планов и позиций, управлять нечем (входы ищет entry_scout)", meta, None, meter, assign)
                return out
            out.update(outcome="no_action", code="DOSSIER")
            self._journal(job, role, agent_id, None, "skip", "DOSSIER", str(e)[:100], meta, None, meter, assign)
            return out
        if role.output_schema == "entry/1" and ctx.get("struct_missing") and int(job.get("attempts") or 1) < int(self.conf.runtime.get("max_attempts") or 3):
            raise Deferred("STRUCT", 45.0)                   # структуры по монете события ещё нет (свечи подтягиваются): ждём, а не пропускаем; последняя попытка идёт к модели
        if role.output_schema == "entry/1":                  # детерминированные отказы по данным досье ДО вызова модели: токены не тратятся
            pre = validate.entry_precheck(ctx)
            if pre:
                out.update(outcome="no_action", code=pre)
                self._journal(job, role, agent_id, None, "skip", pre, "вход невозможен по данным досье: %s" % pre, meta, None, meter, assign)
                return out
        for attempt in range(2):                      # второй проход только при устаревшей ревизии плана (STALE)
            llm = self._llm(role, text, meta, assign, team, meter, ctx)
            if llm["obj"] is None:
                out.update(outcome="no_action", code=llm["code"], profile=llm.get("profile"))
                if llm["code"] in ("BAD_FORMAT", "JOB_BUDGET", "LOOP", "TRUNCATED", "EMPTY", "MAX_ROUNDS", "TIMEOUT"):
                    self._no_action(role, job, agent_id, meta, llm, meter, assign)
                return out
            stale = self._stale(role, llm["obj"], ctx)
            if stale and attempt == 0:
                try:
                    text, ctx = self._input(role, job, agent_id, meta)
                except DossierError:
                    break
                continue
            if stale:
                out.update(outcome="no_action", code="STALE", profile=llm["profile"])
                self._journal(job, role, agent_id, llm["obj"], "skip", "STALE", "устаревшая ревизия плана", meta, llm, meter, assign)
                return out
            break
        res = self._execute(role, llm["obj"], job, agent_id, dict(meta, profile=llm["profile"], fallback_from=llm.get("fallback_from")), ctx, llm, meter)
        out.update(outcome=res["outcome"], code=res["code"], profile=llm["profile"], decision=(llm["obj"] or {}).get("decision"))
        return out

    def _no_action(self, role, job, agent_id, meta, llm, meter, assign):
        self.event("error", {"where": "brain", "code": llm["code"] if llm["code"] == "BAD_FORMAT" else "NO_ACTION"})
        det = "; ".join(str(x) for x in (llm.get("errors") or [])[:4])[:90]          # последние коды отказа проверки (RR_LOW, STOP_TIGHT, ...): почему ответ не принят
        self._journal(job, role, agent_id, None, "skip", llm["code"], "нет действия: %s%s" % (llm["code"], " (%s)" % det if det else ""), meta, llm, meter, assign)

    # ---- вход роли
    def _input(self, role, job, agent_id, meta):
        """-> (текст запроса, ctx). Для decision_json с brief:event досье собирает раннер; для tools-ролей модель сама зовёт инструменты."""
        seqs = [s for s in (job.get("seqs") or []) if isinstance(s, int)]
        first, last = (min(seqs), max(seqs)) if seqs else (0, 0)
        ctx = {"plan": job.get("plan"), "event_seq": last, "brief_rev": None, "brief_id": None, "protect": self.health.protect_on}
        info = {"job": job["id"], "plan": job.get("plan"), "sym": job.get("sym"), "types": job.get("types"), "kinds": job.get("kinds"), "seq_first": first, "seq_last": last,
                "events_n": job.get("count")}
        if role.output_schema == "casereview/1":
            ctx["case_id"] = job.get("case_id") if isinstance(job.get("case_id"), str) else None      # случай задаёт писатель памяти (memory.tick) по position_closed
            info["case_id"] = ctx["case_id"]
            text = self._review_input(ctx["case_id"], ctx)
            if text is not None:
                return text, ctx
        if "brief:start" in role.inputs and role.output_schema == "dayplan/1":
            b = self.gateway.call(meta["agent"], "get_brief", {"scope": "start"}, meta=meta)
            if not isinstance(b, dict) or b.get("ok") is not True:
                raise DossierError("get_brief: %s" % ((b or {}).get("errors") or [{}])[0].get("code", "?"))
            ctx.update(validate.dayplan_context({k: v for k, v in b.items() if k not in ("ok", "status", "errors", "warnings")}))
            braw = common.dumps({k: v for k, v in b.items() if k not in ("ok", "status", "errors", "warnings")})
            if len(braw) > MAX_BRIEF_CHARS:
                braw = braw[:MAX_BRIEF_CHARS] + "…[обрезано]"
            return "<brief>%s</brief>\n<event>%s</event>" % (braw, common.dumps(info)), ctx
        if role.io == "tools" or "brief:event" not in role.inputs:
            return "<event>%s</event>" % common.dumps(info), ctx
        gw = self.gateway
        bargs = {"scope": "event", "since": max(0, first - 1), "seqs": seqs[-40:]}      # события и монета задания: всегда в recent, монета первой в struct
        if isinstance(job.get("sym"), str) and common.RE_SYMBOL.match(job["sym"]):
            bargs["sym"] = job["sym"]
        b = gw.call(agent_id, "get_brief", bargs, meta=meta)
        if not isinstance(b, dict) or b.get("ok") is not True:
            raise DossierError("get_brief: %s" % ((b or {}).get("errors") or [{}])[0].get("code", "?"))
        ev = gw.call(agent_id, "get_events", {"after": max(0, first - 1), "limit": MAX_EVENTS}, meta=meta)
        evs = [e for e in (ev.get("events") or [] if isinstance(ev, dict) else []) if e.get("seq") in set(seqs)] if seqs else []
        if role.output_schema == "pm/1" and not job.get("plan") and not (b.get("positions") or b.get("plans")):
            raise DossierError("NO_PLAN: событие без плана, управлять нечем")        # связь/сверка без планов и позиций: модель не может назвать план, вызов не нужен
        ctx["brief_id"] = b.get("brief_id")
        ctx["brief_rev"] = self._plan_rev(b, job.get("plan"))
        if role.output_schema == "pm/1":
            ctx.update({k: v for k, v in validate.pm_context(b, job.get("plan"), info).items() if k != "plan"})
        elif role.output_schema == "entry/1":
            ctx.update(validate.entry_context(b, info))
        bb = {k: v for k, v in b.items() if k not in ("ok", "status", "errors", "warnings")}
        braw = common.dumps(bb)
        if len(braw) > MAX_BRIEF_CHARS:
            braw = braw[:MAX_BRIEF_CHARS] + "…[обрезано]"
        info["events"] = evs
        return "<brief>%s</brief>\n<event>%s</event>" % (braw, common.dumps(info)), ctx

    def _review_input(self, case_id, ctx):
        """Вход ревьюера из памяти: <case> = situation/decision/refs (известно в момент решения) + предложение кода по unlucky/lucky; <after_decision> = result и whatif
        отдельно. В ctx кладутся known_features и suggested_verdict для смысловой проверки (validate._casereview). Нет случая в памяти -> None (прежний вход <event>)."""
        if not case_id:
            return None
        try:
            from tb.memory import cases as mcases, cfg as mcfg, review as mreview
            c = mcases.get(case_id, self.root)
            if c is None:
                return None
            cfg = mcfg.load(self.root)
        except (OSError, ValueError):
            return None
        sv = mreview.suggest_verdict(c)
        ctx["known"], ctx["suggested_verdict"] = mreview.known_features(c, cfg), sv
        sit = dict(c.get("situation") or {})
        retro = [k for k in (sit.get("retro") or []) if k in sit]              # признаки, восстановленные ПОСЛЕ сделки (разбор журнала): в <case> их нет
        learned = dict((k, sit[k]) for k in retro)
        for k in retro:
            sit[k] = None
        head = {"case_id": c["case_id"], "type": c.get("type"), "situation": sit, "decision": c.get("decision"), "refs": c.get("refs"), "suggested_verdict": sv}
        after = {"result": c.get("result"), "whatif": c.get("whatif")}
        if learned:
            after["learned_after"] = learned
        return "<case>%s</case>\n<after_decision>%s</after_decision>" % (common.dumps(head), common.dumps(after))

    @staticmethod
    def _plan_rev(brief, plan):
        if not plan:
            return None
        for key in ("plans", "positions"):
            for it in brief.get(key) or []:
                if isinstance(it, dict) and plan in (it.get("id"), it.get("plan")) and isinstance(it.get("rev"), int):
                    return it["rev"]
        return None

    def _stale(self, role, obj, ctx):
        return bool(role.output_schema == "pm/1" and ctx.get("brief_rev") is not None and obj.get("rev") != ctx["brief_rev"])

    # ---- вызов модели по цепочке профилей
    def _llm(self, role, text, meta, assign, team, meter, ctx):
        """-> {"obj","code","profile","fallback_from","errors","rounds"}; Deferred, если вызывать нечего."""
        now = self.clock()
        st = self.budget.status(None)
        if st["state"] == "exhausted":
            self.enter_protect("бюджет LLM исчерпан (%s)" % st.get("key"), "brain_budget")
            raise Deferred("BUDGET", min(600.0, max(30.0, (st.get("until_ts") or now + 60) - now)))
        if self.health.protect_on and (self.health.protect.get("code") == "brain_budget"):
            self.leave_protect()
        rb = (self.conf.role_opts.get(role.id) or {}).get("budget") or {}
        if rb.get("day_usd") is not None:       # суточный потолок расхода роли (учёт llm_calls по полю role); исчерпан -> задание откладывается, остальные роли работают
            spent = usage_mod.window_totals([r for r in usage_mod.read_rows(self.root, since=budget_mod.window_start(now, 86400)) if r.get("role") == role.id])["usd"]
            if spent >= float(rb["day_usd"]):
                raise Deferred("ROLE_BUDGET", 900.0)
        chain, _t = assign.chain(role.id, team)
        if not chain:
            self.event("error", {"where": "brain", "code": "NO_ROLE_ASSIGNMENT"})
            raise Deferred("NO_ASSIGNMENT", 120.0)
        mode = swap.day_mode(self.root)
        cands, why = fallback.candidates(self.root, self.conf, self.reg, chain, role, self.health, now, mode)
        if not cands:
            self.enter_protect("; ".join(why[:2]) or "нет допустимого профиля", "brain_down")
            raise Deferred("NO_PROFILE", 60.0)
        last_code = "LLM_DOWN"
        fb = self.conf.fallback
        cd = fb.get("cooldown_s") or {}
        for pid, fb_from in cands:
            p = self.conf.profiles[pid]
            pst = self.budget.status(p.provider)
            if pst["state"] == "exhausted":
                self.health.fail(pid, "budget", cd.get("base", 60), cd.get("max", 900), now)
                self.health.note_fallback(role.id, chain[0], pid, "budget", now)
                last_code = "PROVIDER_BUDGET"
                continue
            m = dict(meta, profile=pid, fallback_from=fb_from)
            try:
                r = self._call_profile(role, p, pid, text, m, meter, ctx)
            except JobBudget:
                return {"obj": None, "code": "JOB_BUDGET", "profile": pid, "fallback_from": fb_from}
            except LLMError as e:
                kind = e.fallback_kind or "auth"
                if kind == "budget" and (meter.max_usd and meter.usd >= meter.max_usd or meter.max_tokens and meter.tokens >= meter.max_tokens):
                    return {"obj": None, "code": "JOB_BUDGET", "profile": pid, "fallback_from": fb_from}
                if kind == "budget" and self.budget.status(None)["state"] == "exhausted":
                    self.enter_protect("бюджет LLM исчерпан", "brain_budget")
                    raise Deferred("BUDGET", 300.0)
                if kind in ("format",):
                    kind = "format_x2"
                self.health.fail(pid, kind, cd.get("base", 60), cd.get("max", 900), self.clock())
                self.health.note_fallback(role.id, chain[0], pid, kind, self.clock())
                last_code = "LLM_" + (e.code or "ERR").upper()
                if kind in fb.get("on", ()) or kind == "auth":
                    continue
                break
            if r["obj"] is None and r["code"] in ("BAD_FORMAT",) and fb_from is None and len(cands) > 1 and "format_x2" in fb.get("on", ()):
                self.health.fail(pid, "format_x2", cd.get("base", 60), cd.get("max", 900), self.clock())
                self.health.note_fallback(role.id, chain[0], pid, "format_x2", self.clock())
                last_code = "BAD_FORMAT"
                continue
            self.health.ok(pid)
            if self.health.protect_on:
                self.leave_protect()
            try:
                swap.record_probe(self.root, p, True, self.clock())
            except Exception:
                pass
            r.update(profile=pid, fallback_from=fb_from)
            if fb_from:                 # запасной профиль: событие brain_swap (не чаще раза в 10 минут на пару)
                k = (fb_from, pid)
                if getattr(self, "_fb_emit", {}).get(k, -1e9) < now - 600:
                    self.__dict__.setdefault("_fb_emit", {})[k] = now
                    ident = lambda s: "".join(c for c in str(s) if c.isalnum() or c in "_.-")[:40] or "-"
                    self.event("brain_swap", {"kind": "fallback", "from": ident(fb_from), "to": ident(pid), "rev": assign.rev, "by": "runner"})
            return r
        self.enter_protect("все профили роли %s недоступны (%s)" % (role.id, last_code), "brain_down")
        raise Deferred("LLM_DOWN", 60.0)

    def _call_profile(self, role, p, pid, text, meta, meter, ctx):
        client = self.client_for(pid)
        fl = self.model_flags(p)
        rf = "json" if fl["json_mode"] else ({"schema": role.schema, "name": "respond"} if fl["strict"] else None)
        ro = (self.conf.role_opts.get(role.id) or {}).get("params") or {}       # переопределение владельца на уровне роли побеждает профиль и файл роли
        temp = ro["temperature"] if "temperature" in ro else role.params.get("temperature", p.params.get("temperature"))
        mx = min(role.max_output_tokens, ro.get("max_tokens") or p.params.get("max_tokens") or role.max_output_tokens)
        req = Req(system=role.system, messages=[user(text)], max_tokens=mx, temperature=temp, thinking=ro.get("thinking", p.params.get("thinking")), response_format=rf,
                  meta=dict(meta, prompt_hash=role.prompt_hash), meter=meter, timeout_s=role.timeout_s)
        deadline = self.clock() + role.timeout_s
        sem = lambda obj: validate.semantic(role.output_schema, obj, ctx)
        if role.io == "tools":
            return self._tools_mode(role, client, req, meta, deadline, sem, ctx)
        msgs, errs, last, cur = list(req.messages), [], None, req
        for attempt in range(role.retries_format + 1):
            if self.clock() > deadline:
                return {"obj": None, "code": "TIMEOUT", "errors": errs}
            resp = client(cur.copy(messages=msgs))
            last = resp
            obj, errs = schema_mod.check_text(resp.text, role.schema)
            if resp.finish == "length" and errs and errs[0] != "TRUNCATED":
                errs = ["TRUNCATED"] + errs
            if not errs:
                errs = sem(obj)
            if not errs:
                return {"obj": obj, "code": "OK", "errors": [], "attempts": attempt + 1}
            note = getattr(client, "note_format_error", None)
            if note:
                note(resp, errs)
            if errs[0] == "TRUNCATED":
                # ответ обрезан лимитом вывода (у DeepSeek рассуждения входят в лимит): подсказка по формату не поможет, повторяем тот же вопрос
                # с удвоенным лимитом, со второго раза без рассуждений
                cur = cur.copy(max_tokens=min(int(cur.max_tokens) * 2, TRUNC_MAX_TOKENS),
                               thinking=("off" if attempt >= 1 and cur.thinking not in (None, "off") else cur.thinking))
                continue
            msgs = msgs + [assistant((resp.text or "(пусто)")[:400]), user(schema_mod.hint(errs))]
        return {"obj": None, "code": "BAD_FORMAT", "errors": errs}

    def _tools_mode(self, role, client, req, meta, deadline, sem, ctx):
        agent = meta["agent"]
        allowed = set(role.tools)
        granted = set(t.name for t in self.gateway.tools_for(agent))
        from tb.gateway import catalog
        names = sorted(n for n in (allowed & granted) if n in catalog.TOOLS and n != "add_case_review")      # запись в память делает код после проверки, не модель
        tl = []
        for n in names:
            t = catalog.TOOLS[n]
            tl.append({"name": n, "description": t.desc, "schema": t.input_schema()})
        req = req.copy(tools=tl, tool_choice="auto" if tl else None)
        protect = self.health.protect_on or bool(ctx.get("protect"))

        def run_tool(name, args):
            if name not in names:
                return {"ok": False, "errors": [{"code": "E_TOOL_DENIED", "msg": "tool unavailable to the role"}]}
            if protect and name in PROTECT_BLOCKED:
                return {"ok": False, "errors": [{"code": "PROTECT_ONLY", "msg": "режим только защита"}]}
            return self.gateway.call(agent, name, args, meta=meta)
        lr = toolloop.run(client, req, run_tool, max_rounds=role.max_rounds, schema=role.schema, format_retries=role.retries_format,
                          should_stop=lambda: self.clock() > deadline or self.stop_event.is_set(), check=sem)
        if lr.status != "ok":
            code = {"format": "BAD_FORMAT", "loop": "LOOP", "max_rounds": "MAX_ROUNDS", "empty": "EMPTY", "truncated": "TRUNCATED", "cancelled": "TIMEOUT"}.get(lr.status, "BAD_FORMAT")
            return {"obj": None, "code": code, "errors": lr.errors}
        errs = sem(lr.obj)
        if errs:
            return {"obj": None, "code": "BAD_FORMAT", "errors": errs}
        return {"obj": lr.obj, "code": "OK", "errors": [], "rounds": lr.rounds}

    # ------------------------------------------------------------------ исполнение решения
    def _execute(self, role, obj, job, agent_id, meta, ctx, llm, meter):
        """-> {"outcome","code"}. pm/1 исполняется инструментами шлюза; остальные схемы в C2 не исполняются (результат только в журнале решений)."""
        assign = self.cache.get(self.conf)
        if role.output_schema == "casereview/1":
            return self._execute_review(role, obj, job, agent_id, meta, ctx, llm, meter, assign)
        if role.output_schema == "entry/1":
            return self._execute_entry(role, obj, job, agent_id, meta, ctx, llm, meter, assign)
        if role.output_schema != "pm/1":
            self._journal(job, role, agent_id, obj, "note", "OK", None, meta, llm, meter, assign)
            return {"outcome": "decision", "code": "OK"}
        d, a = obj["decision"], obj.get("args") or {}
        why = validate.redact(obj["why"], 200)
        if d == "hold":
            self._journal(job, role, agent_id, obj, "note", "HOLD", why, meta, llm, meter, assign)
            return {"outcome": "hold", "code": "HOLD"}
        if d == "escalate":
            code = a.get("reason_code") or "NEED_OWNER"
            self.event("error", {"where": "brain", "code": code})
            self._journal(job, role, agent_id, obj, "note", "ESCALATE", why, meta, llm, meter, assign)
            return {"outcome": "decision", "code": "ESCALATE"}
        key = guards.decision_key(role.id, obj.get("plan"), d, a)
        if not self.repeat.allow(key, self.clock()):
            self.event("error", {"where": "brain", "code": "REPEAT_DECISION"})
            self._journal(job, role, agent_id, obj, "skip", "REPEAT_DECISION", why, meta, llm, meter, assign)
            return {"outcome": "no_action", "code": "REPEAT_DECISION"}
        tool = DECISION_TOOLS[d]
        if (ctx.get("protect") or self.health.protect_on) and tool in PROTECT_BLOCKED:
            self._journal(job, role, agent_id, obj, "skip", "PROTECT_ONLY", why, meta, llm, meter, assign)
            return {"outcome": "no_action", "code": "PROTECT_ONLY"}
        args = {"id": obj["plan"], "reason": why or d, "event_seq": ctx["event_seq"], "wait_s": 8}
        if ctx.get("brief_id"):
            args["brief_id"] = ctx["brief_id"]
        if d in ("move_stop", "amend"):
            args["rev"] = obj["rev"]
        if d == "move_stop":
            args["to"] = "be" if a["to"] == "be" else _num_str(a["to"])
        elif d == "partial_close":
            args["frac"] = a["frac"]
        elif d == "amend":
            args["patch"] = a["patch"]
        r = self.gateway.call(agent_id, tool, args, meta=meta)
        ok = r.get("ok") if isinstance(r, dict) else False
        code = "OK" if ok is True else ("QUEUED" if ok is None else ((r.get("errors") or [{}])[0].get("code") or "REJECTED"))
        self._journal(job, role, agent_id, obj, "note" if ok is not False else "skip", code, why, meta, llm, meter, assign)
        return {"outcome": "decision" if ok is not False else "no_action", "code": code}

    def _entry_block(self, agent_id, ctx):
        """Жёсткие запреты entry_scout в коде (не зависят от ответа модели) -> код блокировки или None. NOT_PAPER: режим дня/платформы не paper, исполнитель не paper
        или у агента команды есть право не только paper; PROTECT_ONLY: мозг в режиме только защита; KILL: активен KILL."""
        ctx = ctx or {}
        modes = (((self.agents.get(agent_id) or {}).get("limits") or {}).get("modes"))
        if modes != ["paper"] or swap.day_mode(self.root) != "paper" or ctx.get("exec_mode") not in (None, "paper"):
            return "NOT_PAPER"
        if self.health.protect_on or ctx.get("protect"):
            return "PROTECT_ONLY"
        if ctx.get("kill") is True or os.path.exists(paths.kill_file(root=self.root)):
            return "KILL"
        return None

    def _execute_entry(self, role, obj, job, agent_id, meta, ctx, llm, meter, assign):
        """entry/1: skip -> запись; propose -> план tb.plan/1 (режим paper жёстко) через submit_plan шлюза. Размер/плечо считает исполнитель (guard), Gate отклонит лишнее."""
        why = validate.redact(obj["why"], 200)
        if obj["decision"] == "skip":
            self._journal(job, role, agent_id, obj, "note", "SKIP", why, meta, llm, meter, assign)
            return {"outcome": "hold", "code": "SKIP"}
        blk = self._entry_block(agent_id, ctx)
        if blk:
            self._journal(job, role, agent_id, obj, "skip", blk, why, meta, llm, meter, assign)
            return {"outcome": "no_action", "code": blk}
        if not ctx.get("session_type"):
            self._journal(job, role, agent_id, obj, "skip", "NO_SESSION", why, meta, llm, meter, assign)
            return {"outcome": "no_action", "code": "NO_SESSION"}
        lp = self.gateway.call(agent_id, "list_plans", {"limit": 100}, meta=meta)        # дедуп по монете: свежий список планов, не досье
        rows = lp.get("plans") if isinstance(lp, dict) and lp.get("ok") is True else None
        if rows is None:
            self._journal(job, role, agent_id, obj, "skip", "BUSY_UNKNOWN", why, meta, llm, meter, assign)
            return {"outcome": "no_action", "code": "BUSY_UNKNOWN"}
        if any(isinstance(x, dict) and x.get("symbol", x.get("sym")) == obj["sym"] and x.get("status") not in plan_schema.TERMINAL for x in rows):
            self._journal(job, role, agent_id, obj, "skip", "SYM_BUSY", why, meta, llm, meter, assign)
            return {"outcome": "no_action", "code": "SYM_BUSY"}
        key = guards.decision_key(role.id, obj["sym"], "propose", {"side": obj["side"], "level": obj["level"], "stop": obj["stop"], "tp": obj["tp"]})
        if not self.repeat.allow(key, self.clock()):
            self.event("error", {"where": "brain", "code": "REPEAT_DECISION"})
            self._journal(job, role, agent_id, obj, "skip", "REPEAT_DECISION", why, meta, llm, meter, assign)
            return {"outcome": "no_action", "code": "REPEAT_DECISION"}
        plan = entryplan.build(obj, ctx)
        args = {"plan": plan, "reason": why or "entry", "event_seq": ctx["event_seq"], "wait_s": 8}
        if ctx.get("brief_id"):
            args["brief_id"] = ctx["brief_id"]
        r = self.gateway.call(agent_id, "submit_plan", args, meta=meta)
        ok = r.get("ok") if isinstance(r, dict) else False
        code = "OK" if ok is True else ("QUEUED" if ok is None else ((r.get("errors") or [{}])[0].get("code") or "REJECTED"))
        self._journal(job, role, agent_id, obj, "note" if ok is not False else "skip", code, why, meta, llm, meter, assign)
        return {"outcome": "decision" if ok is not False else "no_action", "code": code}

    def _execute_review(self, role, obj, job, agent_id, meta, ctx, llm, meter, assign):
        """Вывод ревьюера (tb.casereview/1) -> память через инструмент шлюза add_case_review (право memory.write у агента команды; шлюз сам требует meta.role=reviewer и
        совпадение case_id с заданием); нет инструмента/права -> вывод остаётся только в журнале решений (код NO_MEMORY_RIGHT), без побочных действий."""
        names = set(t.name for t in self.gateway.tools_for(agent_id))
        cid = ctx.get("case_id")
        if "add_case_review" not in names or "add_case_review" not in role.tools:
            self._journal(job, role, agent_id, obj, "note", "NO_MEMORY_RIGHT", "вывод ревьюера: %s" % obj.get("verdict"), meta, llm, meter, assign)
            return {"outcome": "decision", "code": "NO_MEMORY_RIGHT"}
        if not cid:
            self._journal(job, role, agent_id, obj, "skip", "NO_CASE_ID", "нет case_id в задании", meta, llm, meter, assign)
            return {"outcome": "no_action", "code": "NO_CASE_ID"}
        r = self.gateway.call(agent_id, "add_case_review", {"case_id": cid, "review": obj}, meta=dict(meta, case_id=cid))
        ok = r.get("ok") if isinstance(r, dict) else False
        code = "OK" if ok is True else ((r.get("errors") or [{}])[0].get("code") or "REJECTED") if isinstance(r, dict) else "REJECTED"
        self._journal(job, role, agent_id, obj, "note" if ok is True else "skip", code, "вывод ревьюера: %s" % obj.get("verdict"), meta, llm, meter, assign)
        return {"outcome": "decision" if ok is True else "no_action", "code": code}

    # ------------------------------------------------------------------ журнал решений
    def _journal(self, job, role, agent_id, obj, kind, code, why, meta, llm, meter, assign):
        basis = {"job": job["id"], "role": role.id, "agent": agent_id, "cfg_rev": assign.rev, "event_seq": meta.get("event_seq")}
        if meter is not None:
            basis["usd"], basis["tokens"] = round(meter.usd, 6), meter.tokens
        if llm:
            basis["profile"] = llm.get("profile")
            if llm.get("fallback_from"):
                basis["fallback_from"] = llm["fallback_from"]
        rec = {"actor": "brain:%s" % agent_id, "kind": kind, "what": "brain_decision", "why": validate.redact(why or "", 200),
               "basis": basis, "res": {"ok": code in ("OK", "HOLD", "ESCALATE", "QUEUED", "SKIP", "IDLE_NO_PLAN"), "code": str(code)[:40]}}
        if job.get("plan"):
            rec["plan"] = job["plan"]
        if obj:
            rec["args"] = {"decision": obj.get("decision")} if obj.get("decision") else {}
            if isinstance(obj.get("rev"), int):
                rec["rev"] = obj["rev"]
        try:
            decisions.append(rec, root=self.root)
        except (ValueError, OSError, TimeoutError):
            pass

    # ------------------------------------------------------------------ вызов роли вне заданий (lifecycle, intel)
    def ask_role(self, role_id, text, team=None, note="adhoc", ctx=None):
        """Одноразовый вызов роли с готовым текстом запроса (morning_analyst, day_auditor, news_reader, reviewer) -> {"ok","obj","code","profile"}.
        ctx: контекст смысловой проверки (validate.dayplan_context / audit_context / casereview known), строит вызывающий из данных системы.
        Действий не исполняет: решение применяет вызывающий. Бюджет, запасные профили и «только защита» те же."""
        role = self.roles.get(role_id)
        if role is None:
            return {"ok": False, "obj": None, "code": "NO_ROLE"}
        assign = self.cache.get(self.conf)
        tm = team or assign.live_team
        meta = {"job": "J-%s-%d" % (note, int(self.clock())), "role": role.id, "team": tm, "agent": assign.agent_id(tm), "rev": assign.rev}
        meter = budget_mod.JobMeter(self.conf.budget.get("job_usd") or None, self.conf.budget.get("job_tokens") or None)
        try:
            r = self._llm(role, text, meta, assign, tm, meter, ctx or {})
        except Deferred as d:
            return {"ok": False, "obj": None, "code": d.code}
        return {"ok": r["obj"] is not None, "obj": r["obj"], "code": r["code"], "profile": r.get("profile"), "usd": round(meter.usd, 6)}

    # ------------------------------------------------------------------ восстановление после «только защиты»
    def recovery_tick(self, every_s=60.0):
        """Если мозг в «только защите» из-за недоступности: раз в every_s проверка провайдера основного профиля live-роли (GET списка моделей, без токенов)."""
        now = self.clock()
        if not self.health.protect_on:
            return False
        if self.health.protect.get("code") == "brain_budget":
            if self.budget.status(None)["state"] != "exhausted":
                self.leave_protect()
                return True
            return False
        last = getattr(self, "_rec_at", 0.0)
        if now - last < every_s:
            return False
        self._rec_at = now
        a = self.cache.get(self.conf)
        for role in self.roles.roles.values():
            if role.live_risk and role.scope == "team":
                chain, _ = a.chain(role.id)
                for pid in chain[:1]:
                    p = self.conf.profiles.get(pid)
                    if p is None:
                        continue
                    try:
                        ok = bool(self.probe_fn(pid, p)) if self.probe_fn else self._probe(p)
                    except Exception:
                        ok = False
                    if ok:
                        self.health.ok(pid)
                        swap.record_probe(self.root, p, True, now)
                        self.leave_protect()
                        return True
        return False

    def _probe(self, p):
        from tb.llm import adapters
        from tb.llm.transport import Transport
        tr = Transport()
        try:
            adapters.make(self.reg.provider(p.provider), self.reg.provider(p.provider).model(p.model), transport=tr).probe(ping=False)
            return True
        except LLMError:
            return False
        finally:
            tr.close()

    # ------------------------------------------------------------------ состояние для heartbeat/статуса
    def snapshot(self):
        now = self.clock()
        a = self.cache.get(self.conf)
        cooling = sorted(p for p in self.conf.profiles if self.health.cooling(p, now))
        bst = self.budget.status(None)
        if self.health.protect_on:
            color = "red"
        elif cooling or self.errors or a.source == "untrusted" or bst["state"] == "warn":
            color = "yellow"
        else:
            color = "green"
        return {"protect_only": self.health.protect_on, "reason": self.health.protect.get("reason"), "health": color, "rev": a.rev, "cfg_source": a.source,
                "cooling": cooling, "budget": bst["state"], "inflight": len(self.inflight), "queue": jobs.stats(self.root), "errors": len(self.errors)}
