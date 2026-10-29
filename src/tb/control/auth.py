"""Ключ подписи и токен управления. Владелец: WP12. ЕДИНСТВЕННЫЙ модуль плоскости управления, который знает путь `~/.config/traider-bot`.

Файл `<каталог ключей>/control.json` (каталог: TB_KEYS_DIR или ~/.config/traider-bot; права 0600): {"hmac_key" (hex 32 байта), "salt", "token_hash" = sha256(salt + токен), "created"}.
Токен в открытом виде нигде не хранится: `new_token()` возвращает его ОДИН раз (печатает CLI владельца). Сравнения константные по времени.
Подпись: mac = HMAC-SHA256(hmac_key, канонический JSON без поля mac). Без ключа подпись и проверка невозможны (возврат None/False), ошибок наружу не бросаем.
Значения ключей наружу не отдаются ни одной функцией; статус показывает только наличие.
"""
import hashlib
import hmac
import json
import os
import secrets
import time

from tb.control import protocol


def keys_dir(environ=None):
    env = os.environ if environ is None else environ
    if env.get("TB_KEYS_DIR"):
        return env["TB_KEYS_DIR"]
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, ".config", "traider-bot")


def control_file(environ=None):
    return os.path.join(keys_dir(environ), "control.json")


def _read(environ=None):
    try:
        with open(control_file(environ), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _restrict(path):
    """0600 на POSIX. Windows: ACL выставляет cli.harden() (icacls) после создания файла; здесь только chmod."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _write(d, environ=None):
    path = control_file(environ)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    _restrict(tmp)
    os.replace(tmp, path)
    _restrict(path)


def status(environ=None):
    """-> {"key":bool,"token":bool,"perms_ok":bool|None}: только наличие, без значений."""
    d = _read(environ)
    perms = None
    if d is not None and os.name == "posix":
        try:
            perms = (os.stat(control_file(environ)).st_mode & 0o077) == 0
        except OSError:
            perms = None
    return {"key": bool(d and isinstance(d.get("hmac_key"), str) and len(d["hmac_key"]) >= 32),
            "token": bool(d and d.get("token_hash") and d.get("salt")), "perms_ok": perms}


def init_key(environ=None):
    """Создаёт ключ подписи, если его нет. -> True, если создан; существующий не трогается."""
    d = _read(environ) or {}
    if isinstance(d.get("hmac_key"), str) and len(d["hmac_key"]) >= 32:
        return False
    d["hmac_key"] = secrets.token_hex(32)
    d.setdefault("created", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    _write(d, environ)
    return True


def ensure(environ=None):
    """Автосоздание при первом запуске платформы: ключ подписи (молча) и токен для опасных действий. Идемпотентно, существующее НЕ перезаписывается.
    -> {"key_created": bool, "token": str|None}: токен возвращается только если создан сейчас (показать владельцу ОДИН раз; позже: `./tb control token new`)."""
    created = init_key(environ)
    token = None
    st = status(environ)
    if st["key"] and not st["token"]:
        token = new_token(environ)
    return {"key_created": created, "token": token}


def new_token(environ=None):
    """Новый токен управления (старый перестаёт действовать). -> токен (показать владельцу один раз) или None, если ключа подписи нет."""
    d = _read(environ)
    if not d or not (isinstance(d.get("hmac_key"), str) and len(d["hmac_key"]) >= 32):
        return None
    token = secrets.token_urlsafe(32)
    d["salt"] = secrets.token_hex(16)
    d["token_hash"] = hashlib.sha256((d["salt"] + token).encode("utf-8")).hexdigest()
    d["token_created"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write(d, environ)
    return token


def verify_token(token, environ=None):
    d = _read(environ)
    if not d or not d.get("token_hash") or not d.get("salt") or not isinstance(token, str) or not token or len(token) > 200:
        return False
    h = hashlib.sha256((d["salt"] + token).encode("utf-8")).hexdigest()
    return hmac.compare_digest(h, str(d["token_hash"]))


def token_epoch(environ=None):
    """Метка токена (меняется при новом токене): сессии, выданные по старому токену, перестают действовать."""
    d = _read(environ)
    return str((d or {}).get("token_hash") or "")[:12]


def _key(environ=None):
    d = _read(environ)
    k = (d or {}).get("hmac_key")
    if isinstance(k, str) and len(k) >= 32:
        try:
            return bytes.fromhex(k)
        except ValueError:
            return None
    return None


def mac_of(obj, environ=None, tag=b""):
    """HMAC объекта без поля mac. None, если ключа нет."""
    k = _key(environ)
    if k is None or not isinstance(obj, dict):
        return None
    body = dict((x, v) for x, v in obj.items() if x != "mac")
    return hmac.new(k, tag + protocol.canon(body).encode("utf-8"), hashlib.sha256).hexdigest()


def sign(obj, environ=None):
    """-> копия объекта с полем mac (или None без ключа)."""
    m = mac_of(obj, environ)
    if m is None:
        return None
    out = dict(obj)
    out["mac"] = m
    return out


def verify_signed(obj, environ=None):
    """Подпись объекта верна? (для команд и для state/control/risk_level.json). Без ключа и при неверной подписи False."""
    if not isinstance(obj, dict) or not isinstance(obj.get("mac"), str):
        return False
    m = mac_of(obj, environ)
    return m is not None and hmac.compare_digest(m, obj["mac"])


# ---- одноразовое подтверждение (nonce): без состояния, подписано ключом; одноразовость и время проверяют ctl и дашборд ----
def _nonce_mac(sess, digest, issued_ms, rnd, environ):
    k = _key(environ)
    if k is None:
        return None
    msg = "nonce|%s|%s|%s|%s" % (sess or "-", digest, issued_ms, rnd)
    return hmac.new(k, msg.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def make_nonce(sess, digest, now=None, environ=None):
    now = time.time() if now is None else now
    ms, rnd = int(now * 1000), secrets.token_hex(6)
    m = _nonce_mac(sess, digest, ms, rnd, environ)
    return None if m is None else "%d.%s.%s" % (ms, rnd, m)


def check_nonce(nonce, sess, digest, now, ttl_s, min_delay_s, environ=None):
    """-> (ok, причина): подпись и привязка к сессии и аргументам, возраст в [min_delay_s, ttl_s]."""
    try:
        ms, rnd, m = str(nonce).split(".")
        ms = int(ms)
    except ValueError:
        return False, "format"
    want = _nonce_mac(sess, digest, ms, rnd, environ)
    if want is None or not hmac.compare_digest(want, m):
        return False, "mac"
    age = now - ms / 1000.0
    if age < min_delay_s:
        return False, "early"
    if age > ttl_s:
        return False, "late"
    return True, "ok"


# ---- cookie «запомнить браузер»: без состояния, подписана ключом, переживает перезапуск дашборда ----
# Вид: `<12 hex>.<срок unix>.<mac 32 hex>`; mac привязан к метке токена (новый токен отзывает все запомненные браузеры). CSRF выводится из cookie тем же ключом.
def _remember_mac(rnd, exp, environ):
    k = _key(environ)
    if k is None:
        return None
    msg = "remember|%s|%s|%s" % (rnd, exp, token_epoch(environ))
    return hmac.new(k, msg.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def make_remember(now, days, environ=None):
    """-> cookie на `days` суток или None (нет ключа или токена)."""
    if not status(environ)["token"]:
        return None
    rnd, exp = secrets.token_hex(6), int(now + max(1, min(365, int(days))) * 86400)
    m = _remember_mac(rnd, exp, environ)
    return None if m is None else "%s.%d.%s" % (rnd, exp, m)


def check_remember(cookie, now, environ=None):
    """cookie подписана ключом, не просрочена и выдана по текущему токену?"""
    try:
        rnd, exp, m = str(cookie).split(".")
        exp = int(exp)
        if len(rnd) != 12 or len(m) != 32:
            return False
    except ValueError:
        return False
    if now > exp or not status(environ)["token"]:
        return False
    want = _remember_mac(rnd, exp, environ)
    return want is not None and hmac.compare_digest(want, m)


def csrf_of(cookie, environ=None):
    """CSRF-токен запомненной сессии (производный от cookie и ключа; без cookie его не получить)."""
    k = _key(environ)
    if k is None:
        return None
    return hmac.new(k, ("csrf|" + str(cookie)).encode("utf-8"), hashlib.sha256).hexdigest()[:32]
