"""Веб-часть плоскости управления дашборда: `/api/ctl/*`. Владелец: WP12.

Дашборд «тупой»: проверяет вход (токен, cookie-сессия, CSRF, Origin/Host/Content-Type/Sec-Fetch-Site), подписывает команду и кладёт её в `state/control/_inbox`
(`tb.control.client`); исполняет команды сервис ctl. Единственное, что дашборд делает сам: создаёт `state/KILL` (`exec_cfg.set_kill`), чтобы аварийная остановка
работала при мёртвом ctl. Кнопки «снять KILL» нет. Биржевого кода, процессов и ключей Bybit/LLM здесь нет; ключ подписи читает только tb.control.auth.
Вход (config/control.json, `auth.mode`): `open_local` (по умолчанию) = с 127.0.0.1 при выполненных проверках Origin/Host/CSRF сессия «open» выдаётся сама и разрешает безопасные
действия; опасные (live, риск вверх, смена live-команды, выключение) требуют токена ОДИН раз: после входа cookie «запомнить» (подписана ключом, N суток, переживает рестарт).
`token` = как раньше: любое действие только после входа по токену. Проверки Origin/Host/Sec-Fetch-Site/Content-Type/CSRF одинаковы в обоих режимах и не отключаются.
Чистые методы `post()` и `get()` принимают заголовки/тело/время и возвращают (код HTTP, объект, заголовки): так их проверяют тесты без сокетов.
"""
import hashlib
import hmac
import json
import secrets
import time

from tb import paths
from tb.control import auth, client
from tb.dash import keyweb
from tb.core import exec_cfg

COOKIE = "tb_sess"
MAX_BODY = 8192
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1")
LOCAL_ACTIONS = ("kill.set", "stop.emergency")
SAFE_MODES = ("paper", "testnet", "observe")     # режимы платформы, где смена мозга/решение о переносе не считаются опасными
SEC_FETCH_OK = (None, "same-origin", "none")     # сессию «open» выдаём только запросам со своей страницы (или переходу по адресу)


def needs_full(action, args, plat_mode):
    """Нужен ли токен (сессия «full») для действия в режиме open_local. Неизвестное действие = нужен (fail closed).
    plat_mode: режим платформы по status.json ctl (None/неизвестно = как live). Дополняет решение ctl: опасное по его challenge тоже требует токена."""
    live_like = plat_mode not in SAFE_MODES
    if action in ("day.stop", "day.resume", "kill.set", "stop.emergency", "report.ack", "intel.refresh", "notify.test", "notify.set"):
        return False
    if action in ("day.start", "mode.set"):
        return args.get("mode") not in SAFE_MODES
    if action == "carry.decide":
        return args.get("decision") == "carry" and live_like
    if action in ("brain.use", "brain.rollback", "brain.reload"):
        return live_like or args.get("role") == "live" or bool(args.get("team"))
    if action in ("brain.role", "brain.profile", "llm.provider"):
        return live_like       # настройки моделей/ролей/провайдеров в paper/testnet/observe без токена; ctl по challenge может потребовать токен сам
    return True            # risk.set, platform.shutdown, exchange.set, всё новое и неизвестное

ERR_TEXT = dict(client.CODES)
ERR_TEXT.update({"E_ORIGIN": "запрос отклонён: неверный источник", "E_CSRF": "защитный токен страницы неверен: обновите страницу и войдите заново",
                 "E_CTYPE": "ожидается application/json", "E_RATE": "слишком много запросов: подождите минуту", "E_LOGIN": "неверный токен",
                 "E_BLOCKED": "вход заблокирован после неудачных попыток: подождите", "E_CONFIG": "управление не настроено: запустите платформу (./tb start): ключ и токен создаются сами",
                 "E_BODY": "некорректное тело запроса", "E_CTL_DOWN": "сервис управления не ответил", "E_NOT_FOUND": "нет такого адреса"})


def _h(headers, name):
    try:
        v = headers.get(name)
        if v is None:
            v = headers.get(name.lower())
        return v
    except Exception:
        return None


def _err(http, code, extra=None):
    d = {"ok": False, "code": code, "msg": ERR_TEXT.get(code, ERR_TEXT["E_INTERNAL"])}
    d.update(extra or {})
    return http, d, {}


class CtlWeb(object):
    def __init__(self, root=None, environ=None, cfg=None):
        self.root, self.environ = root, environ
        self.cfg = cfg
        self.sessions = {}            # cookie -> {"csrf","created","last","label","epoch","level":"open|full","remember","posts":[ts]}
        self.nonces = {}              # nonce -> {"sess","digest","phrases","issued","action"}
        self.fails = []               # времена неудачных входов
        self.blocked_until = 0.0
        self.audit_flood = []

    # ------------------------------------------------------------ настройки
    def conf(self):
        return self.cfg or client.load_cfg(self.root)

    def mode(self):
        """open_local | token. Неизвестное значение = token (fail closed)."""
        a = self.conf().get("auth")
        return "open_local" if isinstance(a, dict) and a.get("mode") == "open_local" else "token"

    def remember_days(self):
        try:
            return max(1, min(365, int((self.conf().get("auth") or {}).get("remember_days", 30))))
        except (TypeError, ValueError):
            return 30

    def new_session(self, now, level, remember=False, ck=None, ttl_s=None):
        ck = ck or secrets.token_urlsafe(32)
        csrf = (auth.csrf_of(ck, self.environ) if remember else None) or secrets.token_urlsafe(24)
        s = {"csrf": csrf, "created": now, "last": now, "label": "s-" + hashlib.sha256(ck.encode()).hexdigest()[:8],
             "epoch": auth.token_epoch(self.environ), "level": level, "remember": remember, "posts": []}
        self.sessions[ck] = s
        if len(self.sessions) > 20:
            for k in sorted(self.sessions, key=lambda x: (self.sessions[x]["level"] == "full", self.sessions[x]["last"]))[:len(self.sessions) - 20]:
                self.sessions.pop(k, None)
        return ck, s

    def cookie_hdr(self, ck, max_age):
        return {"Set-Cookie": "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (COOKIE, ck, int(max_age))}

    # ------------------------------------------------------------ проверки запроса
    def check_request(self, headers, port):
        """Host/Origin/Sec-Fetch-Site/Content-Type для POST -> код ошибки или None."""
        host = (_h(headers, "Host") or "")
        hname = host.rsplit(":", 1)[0].strip("[]").lower() if host else ""
        if hname not in ALLOWED_HOSTS:
            return "E_ORIGIN"
        if host.rsplit(":", 1)[-1] != str(port):
            return "E_ORIGIN"
        origin = _h(headers, "Origin")
        if not origin or origin != "http://%s" % host:
            return "E_ORIGIN"
        sfs = _h(headers, "Sec-Fetch-Site")
        if sfs is not None and sfs != "same-origin":
            return "E_ORIGIN"
        ct = (_h(headers, "Content-Type") or "").replace(" ", "").lower()
        if ct not in ("application/json", "application/json;charset=utf-8"):
            return "E_CTYPE"
        return None

    def cookie_of(self, headers):
        raw = _h(headers, "Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and 16 <= len(v) <= 80:
                return v
        return None

    def session(self, headers, now):
        """-> (cookie, сессия) или (None, None). Просроченная, чужого токена и пустая отбрасываются; активность продлевает."""
        ck = self.cookie_of(headers)
        if not ck:
            return None, None
        s = self.sessions.get(ck)
        if s is None:
            # запомненный браузер после рестарта дашборда: подпись cookie ключом, срок и метка токена (только в open_local)
            if self.mode() == "open_local" and auth.check_remember(ck, now, self.environ):
                return ck, self.new_session(now, "full", True, ck)[1]
            return None, None
        if s.get("remember"):
            if self.mode() != "open_local" or not auth.check_remember(ck, now, self.environ):
                self.sessions.pop(ck, None)
                return None, None
        else:
            ttl = float(self.conf()["session_ttl_min"]) * 60
            stale_epoch = s["level"] == "full" and s["epoch"] != auth.token_epoch(self.environ)
            if now - s["last"] > ttl or stale_epoch:
                self.sessions.pop(ck, None)
                return None, None
        s["last"] = now
        return ck, s

    def audit(self, cid, sess, action, code, now, args=None):
        """Отказ на входе дашборда в общий журнал аудита (с защитой от наводнения: не чаще 30 записей в минуту)."""
        self.audit_flood = [t for t in self.audit_flood if now - t < 60]
        if len(self.audit_flood) >= 30:
            return
        self.audit_flood.append(now)
        client.audit_append(self.root, cid or "-", "dash", sess, action or "-", client.args_digest(action or "-", args or {}), "rejected", code, now=now)

    # ------------------------------------------------------------ GET
    def get(self, path, query, headers, now=None):
        now = time.time() if now is None else now
        st = auth.status(self.environ)
        if path == "/api/ctl/session":
            ck, s = self.session(headers, now)
            extra = {}
            openl = self.mode() == "open_local"
            if s is None and openl and st["key"] and _h(headers, "Sec-Fetch-Site") in SEC_FETCH_OK:
                ck, s = self.new_session(now, "open")          # вход без токена: сессия «open» со своей страницы на 127.0.0.1 (Host уже проверен сервером)
                extra = self.cookie_hdr(ck, float(self.conf()["session_ttl_min"]) * 60)
            out = {"authed": s is not None, "mode": "open_local" if openl else "token", "configured": bool(st["key"] and (st["token"] or openl)),
                   "key": st["key"], "token": st["token"], "remember_days": self.remember_days(), "level": s["level"] if s else None, "full": bool(s and s["level"] == "full")}
            if s:
                out.update({"csrf": s["csrf"], "label": s["label"], "remembered": bool(s.get("remember")),
                            "ttl_s": int(float(self.conf()["session_ttl_min"]) * 60 - (now - s["last"]))})
            return 200, out, extra
        if path == "/api/ctl/status":
            return 200, client.read_status(self.root, now, float(self.conf()["status_stale_s"])), {}
        if path == "/api/ctl/result":
            cid = (query.get("id") or [""])[0]
            r = client.read_result(cid, self.root) if client.RE_ID.match(cid) else None
            return 200, {"result": r}, {}
        if path == "/api/ctl/audit":
            try:
                n = max(1, min(50, int((query.get("n") or ["10"])[0])))
            except ValueError:
                n = 10
            keep = ("seq", "ts", "id", "src", "action", "result", "code", "mode")
            return 200, {"rows": [dict((k, r.get(k)) for k in keep) for r in client.audit_tail(self.root, n)]}, {}
        return _err(404, "E_NOT_FOUND")

    # ------------------------------------------------------------ POST
    def post(self, path, headers, body, port, now=None):
        now = time.time() if now is None else now
        why = self.check_request(headers, port)
        if why:
            self.audit(None, None, "post:" + path.rsplit("/", 1)[-1][:12], why, now)
            return _err(403 if why == "E_ORIGIN" else 415, why)
        if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_BODY:
            return _err(400, "E_BODY")
        try:
            data = json.loads(bytes(body).decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            return _err(400, "E_BODY")
        if not isinstance(data, dict):
            return _err(400, "E_BODY")
        st = auth.status(self.environ)
        if not (st["key"] and (st["token"] or (self.mode() == "open_local" and path != "/api/ctl/login"))):
            return _err(503, "E_CONFIG")
        if path == "/api/ctl/login":
            return self.login(data, headers, now)
        ck, s = self.session(headers, now)
        if s is None:
            self.audit(None, None, "post:" + path.rsplit("/", 1)[-1][:12], "E_AUTH", now)
            return _err(401, "E_AUTH")
        tok = _h(headers, "X-TB-CSRF") or ""
        if not hmac.compare_digest(tok.encode("utf-8", "replace"), s["csrf"].encode("ascii")):
            self.audit(None, s["label"], "post:" + path.rsplit("/", 1)[-1][:12], "E_CSRF", now)
            return _err(403, "E_CSRF")
        s["posts"] = [t for t in s["posts"] if now - t < 60]
        if len(s["posts"]) >= int(self.conf()["max_post_per_min"]):
            self.audit(None, s["label"], "post:" + path.rsplit("/", 1)[-1][:12], "E_RATE", now)
            return _err(429, "E_RATE")
        s["posts"].append(now)
        if path == "/api/ctl/logout":
            self.sessions.pop(ck, None)      # запомненный браузер: cookie стирается; отозвать все сразу = новый токен (./tb control token new)
            return 200, {"ok": True}, {"Set-Cookie": "%s=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict" % COOKIE}
        if path == "/api/ctl/challenge":
            return self.challenge(data, s, now)
        if path == "/api/ctl/command":
            return self.command(data, s, now)
        if path == "/api/ctl/llmkey":        # ключ LLM-провайдера, write-only (dash/keyweb.py); только сессия с токеном
            if s.get("level", "full") != "full":
                return self.deny_elevate(s, "llm.key.set", {"provider": str(data.get("provider"))[:30]}, now)
            return keyweb.handle(self.root, self.environ, data, s["label"], now)
        return _err(404, "E_NOT_FOUND")

    def login(self, data, headers, now):
        c = self.conf()
        self.fails = [t for t in self.fails if now - t < float(c["login_fail_window_s"])]
        if now < self.blocked_until:
            return _err(429, "E_BLOCKED", {"retry_s": int(self.blocked_until - now)})
        tok = data.get("token")
        if not isinstance(tok, str) or not auth.verify_token(tok, self.environ):
            self.fails.append(now)
            if len(self.fails) >= int(c["login_fail_max"]):
                self.blocked_until = now + float(c["login_block_s"])
                self.fails = []
            self.audit(None, None, "login", "E_LOGIN", now)
            return _err(401, "E_LOGIN")
        self.fails = []
        rem = auth.make_remember(now, self.remember_days(), self.environ) if (self.mode() == "open_local" and data.get("remember", True) is not False) else None
        if rem:       # «авторизовался один раз»: cookie на N суток, переживает рестарт платформы
            ck, s = self.new_session(now, "full", True, rem)
            max_age = self.remember_days() * 86400
        else:
            ck, s = self.new_session(now, "full")
            max_age = float(c["session_ttl_min"]) * 60
        client.audit_append(self.root, "-", "dash", s["label"], "login", "-", "ok", "OK", now=now)
        return 200, {"ok": True, "csrf": s["csrf"], "label": s["label"], "level": "full", "remembered": bool(rem)}, self.cookie_hdr(ck, max_age)

    # ------------------------------------------------------------ challenge / command
    def _norm(self, data):
        action = data.get("action")
        if action not in client.ACTIONS or action == "challenge":
            return None, None, "E_UNKNOWN"
        ok, _msg, args = client.validate_args(action, data.get("args") if data.get("args") is not None else {})
        return (action, args, None) if ok else (None, None, "E_ARGS")

    def elevate_needed(self, action, args, s, now):
        """open_local: сессия «open» не может опасное: нужен токен (один раз). В режиме token все сессии «full»."""
        if s.get("level") == "full":
            return False
        mode = (client.read_status(self.root, now, float(self.conf()["status_stale_s"])).get("platform") or {}).get("mode")
        return needs_full(action, args, mode)

    def deny_elevate(self, s, action, args, now):
        self.audit(None, s["label"], action, "E_ELEVATE", now, args)
        return _err(401, "E_ELEVATE")

    def challenge(self, data, s, now):
        action, args, err = self._norm(data)
        if err:
            return _err(400, err)
        if self.elevate_needed(action, args, s, now):
            return self.deny_elevate(s, action, args, now)
        c = self.conf()
        digest = client.args_digest(action, args)
        if action in LOCAL_ACTIONS:       # аварийная остановка не зависит от ctl: подтверждение выдаёт сам дашборд тем же ключом
            out = {"ok": True, "code": "OK", "data": {"action": action, "danger": bool(args.get("flatten")), "phrases": [], "gates": [], "warnings": [], "blocked": False,
                                                       "expires_in": int(c["nonce_ttl_s"]), "min_delay": int(c["min_delay_s"]), "nonce": None}}
            if args.get("flatten"):
                n = auth.make_nonce(s["label"], digest, now, self.environ)
                out["data"]["nonce"] = n
                self.nonces[n] = {"sess": s["label"], "digest": digest, "phrases": [], "issued": now, "action": action}
            return 200, out, {}
        cmd = client.build("challenge", {"action": action, "args": args}, "dash", s["label"], now=now, environ=self.environ)
        if cmd is None:
            return _err(503, "E_CONFIG")
        client.submit(cmd, self.root)
        r = client.wait_result(cmd["id"], self.root, timeout=4.0)
        if r is None or r.get("status") not in ("done", "rejected", "failed", "expired"):
            return _err(504, "E_CTL_DOWN")
        d = r.get("data") or {}
        if (d.get("danger") or d.get("nonce")) and s.get("level") != "full":      # ctl сам счёл действие опасным: токен обязателен (слой поверх статического списка)
            return self.deny_elevate(s, action, args, now)
        n = d.get("nonce")
        if n and r.get("code") == "OK":
            self.nonces[n] = {"sess": s["label"], "digest": digest, "phrases": list(d.get("phrases") or []), "issued": now, "action": action}
        self.nonces = dict((k, v) for k, v in self.nonces.items() if now - v["issued"] < 3 * float(c["nonce_ttl_s"]))
        return 200, {"ok": r.get("code") == "OK", "code": r.get("code"), "msg": r.get("msg"), "data": d}, {}

    def command(self, data, s, now):
        action, args, err = self._norm(data)
        if err:
            return _err(400, err)
        if self.elevate_needed(action, args, s, now) or (data.get("nonce") is not None and action not in LOCAL_ACTIONS and s.get("level") != "full"):
            return self.deny_elevate(s, action, args, now)
        c = self.conf()
        nonce = data.get("nonce")
        phrases = data.get("phrases")
        if phrases is not None and not (isinstance(phrases, list) and len(phrases) <= 3 and all(isinstance(p, str) and len(p) <= 200 for p in phrases)):
            return _err(400, "E_BODY")
        rec = None
        if nonce is not None:
            if not isinstance(nonce, str):
                return _err(400, "E_BODY")
            rec = self.nonces.get(nonce)
            digest = client.args_digest(action, args)
            ok, why = auth.check_nonce(nonce, s["label"], digest, now, float(c["nonce_ttl_s"]), float(c["min_delay_s"]), self.environ)
            if rec is None or rec["digest"] != digest or rec["sess"] != s["label"] or not ok:
                self.audit(None, s["label"], action, "E_NONCE", now, args)
                return _err(403, "E_NONCE", {"why": why if not ok else "unknown"})
            exp = [client.norm_phrase(p) for p in rec["phrases"]]
            got = [client.norm_phrase(p) for p in (phrases or [])]
            if exp:
                if got != exp:
                    return _err(400, "E_PHRASE")       # nonce не сгорает: можно поправить фразу, пока он действует
                if now - rec["issued"] < float(c["min_type_s"]):
                    return _err(403, "E_NONCE", {"why": "typed_too_fast"})
            self.nonces.pop(nonce, None)
        if action in LOCAL_ACTIONS:
            return self.kill(action, args, nonce, phrases, s, now)
        cmd = client.build(action, args, "dash", s["label"], nonce, phrases if (rec and rec["phrases"]) else None, now=now, environ=self.environ)
        if cmd is None:
            return _err(503, "E_CONFIG")
        client.submit(cmd, self.root)
        return 200, {"ok": True, "id": cmd["id"]}, {}

    def kill(self, action, args, nonce, phrases, s, now):
        """KILL создаёт сам дашборд (работает без ctl); ctl получает подписанное уведомление для аудита, шины и состояния halted."""
        if args.get("flatten") and not nonce:
            return _err(403, "E_NONCE")
        exec_cfg.set_kill(bool(args.get("flatten")), "dash: " + (args.get("reason") or "аварийная остановка"), self.root)
        args = dict(args, applied=True)
        cmd = client.build(action, args, "dash", s["label"], None, None, now=now, environ=self.environ)
        cid = None
        if cmd is not None:
            cid = client.submit(cmd, self.root)
        client.audit_append(self.root, cid or "-", "dash", s["label"], action, client.args_digest(action, {"flatten": bool(args.get("flatten"))}), "ok", "OK", now=now)
        return 200, {"ok": True, "id": cid, "killed": True, "flatten": bool(args.get("flatten"))}, {}
