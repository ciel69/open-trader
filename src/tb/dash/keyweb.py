"""Ввод ключа LLM-провайдера из дашборда (WRITE-ONLY): POST /api/ctl/llmkey {"provider": ID, "value": "<ключ>"}. Только ключи LLM-провайдеров: ключи бирж через дашборд не принимаются.

Путь секрета с минимальной экспозицией: тело запроса (loopback, Origin/Host/CSRF, сессия с токеном) -> эта функция -> файл ключа 0600 (tb.core.secretfile) -> значение сразу отбрасывается.
Секрет НЕ кладётся в очередь команд ctl (в `state/control/_inbox` и `_outbox` его нет), не пишется в журнал аудита (там только провайдер и результат), не попадает в логи сервера
(пишется только строка запроса без тела), не возвращается в ответе (только «есть» и маска последних 4 символов) и не читается обратно ни одним маршрутом. Имя файла и переменная берутся
из реестра провайдеров, а не из запроса; зарезервированные имена (биржи, control.json, токены) отклоняются `secretfile.check_spec`.
"""
import re

from tb.control import auth, client
from tb.core import localcfg, secretfile

RE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")
TEXT = {"E_ARGS": "некорректные параметры", "E_UNKNOWN": "провайдер неизвестен или ему не нужен ключ", "E_KEY": "ключ не принят", "E_INTERNAL": "ключ не записан"}


def _fail(http, code, detail=None):
    d = {"ok": False, "code": code, "msg": TEXT.get(code, TEXT["E_INTERNAL"])}
    if detail:
        d["detail"] = detail
    return http, d, {}


def handle(root, environ, data, label, now):
    """-> (http, объект, заголовки). data: тело запроса (после проверок входа ctlweb). Значение ключа из data удаляется."""
    pid = data.get("provider")
    value = data.pop("value", None)
    if not (isinstance(pid, str) and RE_ID.match(pid)) or not isinstance(value, str):
        value = None
        return _fail(400, "E_ARGS")
    try:
        prov = ((localcfg.load("providers", root) or {}).get("providers") or {}).get(pid)
    except Exception:
        prov = None
    kf, ke = (prov or {}).get("key_file"), (prov or {}).get("key_env")
    if not isinstance(prov, dict) or not kf or not ke:
        value = None
        return _fail(404, "E_UNKNOWN")
    try:
        st = secretfile.write_key(auth.keys_dir(environ), kf, ke, value)
    except secretfile.SecretError as e:
        client.audit_append(root, "-", "dash", label, "llm.key.set", client.args_digest("llm.key.set", {"provider": pid}), "rejected", "E_KEY", now=now)
        return _fail(400, "E_KEY", str(e))
    except OSError:
        return _fail(500, "E_INTERNAL")
    finally:
        value = None
    client.audit_append(root, "-", "dash", label, "llm.key.set", client.args_digest("llm.key.set", {"provider": pid}), "ok", "OK", now=now)
    return 200, {"ok": True, "provider": pid, "cred": {"present": st["present"], "mask": st["mask"], "perms_ok": st["perms_ok"]}}, {}
