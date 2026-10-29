"""Метка `live_ok` профиля по итогам живого eval: подписанное состояние `state/brain/live_ok.json` (WP11-C3; 07b п. 5.10, 6.4).

Метку ставит ТОЛЬКО `./tb brain eval --live --confirm` владельца, когда пороги выполнены; запись подписана тем же HMAC-ключом, что `active.json`
(`core/statesign`; без ключа метку поставить нельзя). Метка привязана к «отпечатку» профиля (провайдер, модель, параметры) и хешам промптов оценённых ролей:
смена модели профиля или текста роли делает метку устаревшей (показывается в статусе, `live_ok` остаётся только если отпечаток профиля совпал; хеши промптов
проверяет `stale_roles`). Рантайм читает метку в `conf.load`: подделанный/неподписанный файл игнорируется (fail closed). Агент и шлюз файл не пишут.
Формат: {"schema":"tb.liveok/1","ts","profiles":{pid:{"ok":bool,"eval_ts","fp","valid_rate","violations","roles":{роль:prompt_hash},"report"}},"mac"}.
"""
import hashlib
import json

from tb import paths
from tb.brain import common
from tb.core import statesign
from tb.core.filelock import FileLock, atomic_write_json

SCHEMA = "tb.liveok/1"


def liveok_file(root=None):
    return paths.state("brain", "live_ok.json", root=root)


def fingerprint(profile):
    """Отпечаток профиля: провайдер, модель, параметры (без live_ok и eval_ts)."""
    body = {"provider": profile.provider, "model": profile.model, "params": profile.params}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def read(root=None, key=None):
    """-> (dict профилей|{}, состояние none|ok|untrusted|broken)."""
    p = liveok_file(root)
    o = common.read_json(p)
    if o is None:
        return {}, "none"
    if not isinstance(o, dict) or o.get("schema") != SCHEMA or not isinstance(o.get("profiles"), dict):
        return {}, "broken"
    key = statesign.key() if key is None else key
    if not key or not statesign.verify(o, key):
        return {}, "untrusted"
    return o["profiles"], "ok"


def overlay(conf, root=None, key=None):
    """Поднимает live_ok и eval_ts профилям, у которых есть подписанная метка с тем же отпечатком. Только повышение: метку не из файла не снимает."""
    labels, st = read(root, key)
    if st in ("untrusted", "broken"):
        conf.warnings.append("state/brain/live_ok.json %s: метки live_ok из него игнорируются" % ("не подписан" if st == "untrusted" else "повреждён"))
    for pid, ent in labels.items():
        p = conf.profiles.get(pid)
        if p is not None and isinstance(ent, dict) and ent.get("ok") is True and ent.get("fp") == fingerprint(p):
            p.live_ok = True
            if isinstance(ent.get("eval_ts"), (int, float)) and not isinstance(ent.get("eval_ts"), bool):
                p.eval_ts = ent["eval_ts"]
    return conf


def stale_roles(conf, roles, pid, root=None):
    """Роли, чей prompt_hash изменился после eval профиля (метка устарела по тексту) -> список ролей."""
    labels, _ = read(root)
    ent = labels.get(pid) or {}
    old = ent.get("roles") or {}
    return sorted(r for r, h in old.items() if r in roles.roles and roles.roles[r].prompt_hash != h)


def write_label(root, profile, ok, now, valid_rate, violations, roles_hash, report, key=None):
    """Записывает/обновляет метку профиля (подписанный файл). -> (True, None) | (False, код). Метка ok=False тоже пишется (снимает прежнюю)."""
    key = statesign.key() if key is None else key
    if not key:
        return False, "E_NO_KEY"
    with FileLock(paths.state("brain", "live_ok.lock", root=root), wait=10.0):
        labels, st = read(root, key)
        if st in ("untrusted", "broken"):
            labels = {}
        labels = dict(labels)
        labels[profile.id] = {"ok": bool(ok), "eval_ts": round(now, 3), "fp": fingerprint(profile), "valid_rate": round(valid_rate, 4),
                              "violations": int(violations), "roles": dict(roles_hash), "report": str(report)[:120]}
        body = statesign.sign({"schema": SCHEMA, "ts": round(now, 3), "profiles": labels}, key)
        atomic_write_json(liveok_file(root), body)
    return True, None
