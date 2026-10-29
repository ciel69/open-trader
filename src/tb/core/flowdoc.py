"""Ордерфлоу монеты для досье мозга и дашборда (чтение): runtime/flow.json пишет только marketdata/flowwatch.py (поток сделок и стакана Bybit). Здесь лишь компактная выжимка по одной
монете, чтобы слои brain/gateway/dash не импортировали marketdata. Только числа, enum и идентификаторы; сети нет."""
from tb import paths
from tb.core import ideas

MAX_AGE_S = 30.0


def flow_for(root, sym, now, max_age_s=MAX_AGE_S):
    """-> {cvd_5m_pct, cvd_1m_usd, big_5m_usd, imb_pct, spread_bps, walls[<=2], xex_bps, warm_s, age_s} по монете или None (нет файла, устарел, монеты нет в потоке).
    cvd_5m_pct: перекос агрессии (покупки минус продажи) к объёму за 5 минут; imb_pct: перекос заявок bid/ask у цены; walls: крупные устойчивые стены; warm_s: сколько секунд ленты накоплено
    (меньше 120: CVD неполный)."""
    doc = ideas.read_json(paths.runtime("flow.json", root=root))
    if not isinstance(doc, dict) or not isinstance(doc.get("syms"), list):
        return None
    ts = doc.get("ts_epoch")
    if not isinstance(ts, (int, float)) or now - ts > max_age_s:
        return None
    row = next((r for r in doc["syms"] if isinstance(r, dict) and r.get("sym") == sym), None)
    if row is None:
        return None
    walls = [{"side": w.get("side"), "usd": w.get("usd"), "dist_pct": w.get("dist_pct")} for w in (row.get("walls") or []) if isinstance(w, dict) and w.get("stable") and w.get("side") in ("bid", "ask")][:2]
    out = {"cvd_5m_pct": row.get("cvd_5m_pct"), "cvd_1m_usd": row.get("cvd_1m_usd"), "big_5m_usd": row.get("big_5m_usd"), "imb_pct": row.get("imb_pct"),
           "spread_bps": row.get("spread_bps"), "walls": walls or None, "xex_bps": row.get("xex_bps"), "warm_s": row.get("warm_s"), "age_s": int(max(0.0, now - ts))}
    return {k: v for k, v in out.items() if v is not None}
