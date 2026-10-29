"""Мост прежних команд `./tb bybit ...` к исполнителю v2 (WP1B). Только файлы runtime/ и очередь планов, к бирже не обращается.

  * executor_state(env): жив ли исполнитель ЭТОЙ площадки (heartbeat runtime/exec_health.json свежий, env совпадает);
  * owns_position(env, sym, plan_id): исполнитель жив, снимок runtime/exec_snapshot.json свежий и владелец позиции по монете = `plan:<plan_id>`;
  * build_plan(a, ...): план схемы WP2 из аргументов старой команды `open` (тег `legacy`);
  * submit_plan / close_plan: отправка в _inbox исполнителя (через brain/plan_cli.send, то есть как `plan submit` / `plan close`) и ожидание ответа из _outbox.
Исполнитель не запущен -> понятная ошибка с подсказкой; на прямой ордер НИКОГДА не падаем (это решение владельца: тихий откат скрыл бы сбой).
"""
import argparse
import math
import os

from tb import paths
from tb.brain import common, plan_cli

SNAP_MAX_AGE_S = 90.0      # снимок старше этого не доказывает, что позиции нет/она чья-то
WAIT_S = 15.0           # подача плана
CLOSE_WAIT_S = 30.0     # закрытие: REST на testnet временами зависает на ~16 с (WP1A T10), а закрытие идёт рыночным ордером
LEGACY_TAG = "legacy"


def executor_state(env, root=None, now=None):
    """-> {"alive","age_s","env","reason"}: alive только при свежем heartbeat и совпадении env (testnet/mainnet)."""
    h = common.exec_health(root=root, now=now)
    d = h["data"] or {}
    reason = None
    if not h["present"]:
        reason = "нет runtime/exec_health.json"
    elif not h["alive"]:
        reason = ("исполнитель остановлен (последний heartbeat %s с назад)" % h["age_s"]) if d.get("alive") is False else "heartbeat устарел (%s с)" % h["age_s"]
    elif d.get("env") != env:
        reason = "исполнитель работает на площадке %s, а команда для %s" % (d.get("env"), env)
    return {"alive": reason is None, "age_s": h["age_s"], "env": d.get("env"), "reason": reason}


def snapshot(root=None, now=None):
    """Свежий снимок исполнителя или None."""
    import time
    now = time.time() if now is None else now
    d = common.read_json(paths.exec_snapshot_file(root=root))
    if not isinstance(d, dict):
        return None
    ts = common.num(d.get("ts_epoch"))
    if ts is None or now - ts > SNAP_MAX_AGE_S:
        return None
    return d


def position_of(snap, sym):
    for x in (snap or {}).get("positions") or []:
        if x.get("sym") == sym:
            return x
    return None


def owns_position(env, sym, plan_id, root=None, now=None):
    """Исполнитель жив и его снимок показывает позицию по sym с владельцем plan:<plan_id>."""
    if not executor_state(env, root, now)["alive"]:
        return False
    pos = position_of(snapshot(root, now), sym)
    return bool(pos and pos.get("owner") == "plan:%s" % plan_id)


def not_running_error(env, st):
    prof = {"mainnet": "trade", "testnet": "testnet"}.get(env, "testnet")
    return {"ok": False, "stage": "executor", "error": "исполнитель v2 не запущен (%s): план не отправлен, прямой ордер НЕ ставится. "
            "Запусти в отдельной вкладке: ./tb exec run --profile %s (или команду без --via-plan: прежний прямой путь)" % (st["reason"], prof)}


# ------------------------------------------------------------------ план из аргументов старой команды
def _ceil_min(seconds):
    return max(1, int(math.ceil(float(seconds) / 60.0)))


def no_confirm_cap(root=None):
    """size_mult, допустимый для входа без подтверждения (exec_limits.entry.no_confirm_max_size_mult, правило E_NO_CONFIRM)."""
    try:
        from tb.core import exec_cfg
        return float(exec_cfg.load(root)["entry"]["no_confirm_max_size_mult"])
    except Exception:
        return 0.5


def build_plan(a, ref_price, session_type, quote=None, size_mult=None):
    """Аргументы `bybit open` -> план tb.plan/1. ref_price: последняя цена (для предпросмотра и max_chase).
    quote: (bid, ask) для режима --exec maker без --limit-price. Размер и плечо считает guard исполнителя, не план.
    Старый `open` входит сразу, без условия подтверждения: по правилу плана это требует risk.confirm_waiver=true и size_mult не выше
    no_confirm_max_size_mult (умолчание 0.5), поэтому вход через план вдвое меньше прямого, пока не добавлено условие подтверждения."""
    long_ = a.side == "long"
    sym = a.sym.upper()
    lp = getattr(a, "limit_price", None)
    how, valid, ttl_min = {"type": "market"}, {}, None
    if lp is not None:
        how = {"type": "limit", "price": float(lp), "post_only": True}
        if getattr(a, "snap", False):
            how["snap"] = True
        ttl_min = int(math.ceil(float(a.ttl))) if getattr(a, "ttl", None) else 60
        for k, v in (("cancel_above", getattr(a, "cancel_above", None)), ("cancel_below", getattr(a, "cancel_below", None))):
            if v is not None:
                valid[k] = float(v)
    elif getattr(a, "exec", "taker") == "maker":
        if not quote or not all(quote):
            raise ValueError("нет котировки для maker-входа")
        how = {"type": "limit", "price": float(quote[0] if long_ else quote[1]), "post_only": True}
        ttl_min = _ceil_min(getattr(a, "timeout", 30.0))
    if ttl_min is not None:
        valid["ttl_min"] = ttl_min
    entry = {"how": how}
    if ref_price:
        entry["ref_price"] = float(ref_price)
    if valid:
        entry["valid"] = valid
    extra = [LEGACY_TAG]
    if getattr(a, "regime", None) and a.regime != "unknown":
        extra.append("regime-%s" % "".join(ch if ch.isalnum() else "-" for ch in a.regime.lower())[:20])
    plan = {"schema": "tb.plan/1", "symbol": sym, "side": a.side, "mode": "live", "session_type": session_type,
            "source": {"kind": "idea", "idea_id": a.idea} if getattr(a, "idea", None) else {"kind": "manual"},
            "thesis": str(a.thesis), "tags": {"setup": LEGACY_TAG, "extra": extra[:5]},
            "entry": entry, "stop": {"price": float(a.sl), "trigger_by": "last"},
            "targets": [{"id": "t1", "price": float(a.tp), "frac": 1.0, "exec": "resting"}],
            "risk": {"score": float(getattr(a, "score", 0.5)), "size_mult": float(size_mult if size_mult is not None else no_confirm_cap()),
                     "confirm_waiver": True}}
    if ttl_min is not None:
        plan["lifecycle"] = {"ttl_min": ttl_min}
    return plan


def _envelope_args(reason):
    return argparse.Namespace(reason=reason, event_seq=None, brief=None)


def submit_plan(plan, reason, wait=WAIT_S, root=None):
    """Как `plan submit`: -> ответ исполнителя (ok true|false) либо ok:null/queued, если ответа нет за wait секунд."""
    cmd = plan_cli._envelope("submit", _envelope_args(reason), payload=plan)
    return plan_cli.send(cmd, wait, root=root)


def close_plan(plan_id, reason, frac=1.0, wait=CLOSE_WAIT_S, root=None):
    """Как `plan close PLAN_ID`."""
    cmd = plan_cli._envelope("close", _envelope_args(reason), plan_id=plan_id, payload={"frac": float(frac)})
    return plan_cli.send(cmd, wait, root=root)
