"""Сторож позиции: дешёвая проверка кодом (без токенов), которая будит мозг (position_manager) только когда решение неочевидно.

Два триггера, оба только пока на позиции есть прибыль, которую стоит защищать (r >= stalled_min_r):
  STALLED    прибыль N минут не обновляла максимум (боковик с плюсом);
  FLOW_TURN  ордерфлоу (дельта 1м и перекос стакана) против позиции не меньше flow_turn_s секунд подряд.
Сам сторож позицию не закрывает и стоп не двигает: решает мозг с полной картиной рынка. Защита от лишних трат: не больше max_wakes_per_hour
пробуждений в час на позицию и пауза между повторами одного кода. Состояние лежит в rt["counters"]["watch"].

>>> cfg = {"stalled_min": 15, "stalled_min_r": 0.5, "flow_turn_s": 30, "max_wakes_per_hour": 3}
>>> st = {}
>>> evaluate(st, 1, 0.8, None, 0.0, cfg)
>>> evaluate(st, 1, 0.8, None, 15 * 60 + 1, cfg)
'STALLED'
>>> evaluate(st, 1, 0.8, None, 15 * 60 + 2, cfg)
"""

PEAK_EPS_R = 0.05          # новый максимум = выше прежнего не меньше чем на 0.05R (шум не сбрасывает таймер боковика)
REPEAT_S = {"STALLED": 15 * 60.0, "FLOW_TURN": 10 * 60.0}


def against(side_sign, flow):
    """True, если дельта 1м и перекос стакана оба против позиции (long: продажи и перевес asks)."""
    if not isinstance(flow, dict):
        return False
    cvd, imb = flow.get("cvd_1m_usd"), flow.get("imb_pct")
    if not isinstance(cvd, (int, float)) or not isinstance(imb, (int, float)):
        return False
    return side_sign * cvd < 0 and side_sign * imb < 0


def evaluate(st, side_sign, r, flow, now, cfg):
    """-> код пробуждения (STALLED | FLOW_TURN) или None; st (dict состояния) меняется на месте."""
    if r is None:
        return None
    if st.get("peak_r") is None or r > st["peak_r"] + PEAK_EPS_R:
        st["peak_r"], st["peak_t"] = r, now
    if against(side_sign, flow):
        st.setdefault("against_since", now)
    else:
        st.pop("against_since", None)
    if r < float(cfg["stalled_min_r"]):
        return None
    wakes = [t for t in st.get("wakes", []) if now - t < 3600.0]
    st["wakes"] = wakes
    if len(wakes) >= int(cfg["max_wakes_per_hour"]):
        return None
    last = st.setdefault("last", {})
    code = None
    if st.get("against_since") is not None and now - st["against_since"] >= float(cfg["flow_turn_s"]):
        code = "FLOW_TURN"
    elif now - st["peak_t"] >= float(cfg["stalled_min"]) * 60.0:
        code = "STALLED"
    if code is None or now - last.get(code, -1e18) < REPEAT_S[code]:
        return None
    last[code] = now
    wakes.append(now)
    return code
