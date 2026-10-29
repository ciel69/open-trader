"""Периодический писатель случаев (WP15b, 08 п. 1.3 «код-писатель»): закрытые сделки, пропуски и итоги идей -> `state/cases.jsonl`.

Один шаг = `migrate.migrate(final_only=True)` (идемпотентно: case_id = функция источника, повторный запуск ничего не дублирует, whatif/разбор
дописываются update-строкой, один раз) + пересчёт уроков (`lessons.refresh`). Вызывается:
  * сервисом ctl каждый цикл через `tick()` (не чаще `tick.interval_s` из config/memory.json, по умолчанию 60 с; сбой не роняет ctl);
  * раннером мозга перед разбором закрытия (`sync_now`), чтобы случай для ревьюера точно существовал;
  * вручную `./tb memory sync`.
Ничего не торгует, сети нет; пишет только cases.jsonl и lessons.jsonl своего корня. Отдельного сервиса платформы не требуется.
"""
import time

from tb.memory import cases, cfg as memcfg, lessons, migrate

_LAST = {}


def sync_now(root=None, now=None, cfg=None, lessons_too=True):
    """Один шаг без ограничения частоты -> отчёт {"written","new","lessons"} (ошибки наружу как исключения)."""
    now = time.time() if now is None else now
    cfg = cfg or memcfg.load(root)
    rep = migrate.migrate(root, dry_run=False, cfg=cfg, final_only=True)
    out = {"written": rep.get("written", 0), "new": rep.get("new", {}), "errors": rep.get("errors", [])[:3]}
    if lessons_too and rep.get("written"):
        try:
            out["lessons"] = len(lessons.refresh(root, cases.load(root), cfg, now))
        except (OSError, ValueError, KeyError, TypeError):
            out["lessons"] = None
    return out


def tick(root=None, now=None, force=False):
    """Шаг с ограничением частоты (config/memory.json: tick.interval_s, tick.enabled). -> отчёт или None, если рано/выключено/сбой.
    Любая ошибка проглатывается: писатель памяти не должен мешать сервису, в котором вызван."""
    now = time.time() if now is None else now
    try:
        cfg = memcfg.load(root)
        tc = cfg.get("tick") or {}
        if not tc.get("enabled", True):
            return None
        key = root or ""
        if not force and now - _LAST.get(key, -1e12) < float(tc.get("interval_s", 60.0)):
            return None
        _LAST[key] = now
        return sync_now(root, now, cfg)
    except Exception:       # noqa: BLE001 - см. docstring
        return None
