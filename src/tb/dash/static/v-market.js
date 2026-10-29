"use strict";
// Вкладка «Рынок»: горячие монеты сканера и отслеживаемые монеты фида (строки раскрываются по клику).

var hotAll = false;
function retc(x, warm) { return isn(x) ? sg(x * 100, 2) + "%" : (warm === false ? "–" : "ждёт"); }
// режим фида (EMA из тиков) после запуска ~1 ч «unknown»: тогда показываем режим по свечам 1ч из structure.json (поле regime_1h), иначе явное «прогрев»
function regimeChip(r, r1h) {
  var known = r && r !== "unknown";
  if (known) return chip(r, r === "storm" ? "warn" : "");
  if (r1h) return chip(r1h + " · 1ч", r1h === "storm" ? "warn" : "", "режим по свечам 1ч: фид после запуска набирает историю тиков для своего режима");
  return chip("прогрев", "", "режим фида ещё не определён: нужна история тиков (около часа после запуска фида)");
}

function renderMarket() {
  var m = S.market, tbl = $("market"), empty = $("mkt-empty");
  var cold = m.rows.length && m.rows.every(function (r) { return !isn(r.ret_5m); });
  put("mkt-age", m.ok ? ["· фид ", tk("mk", m.age_s, "ago"), cold ? " · фид только запущен: 5-мин метрики появятся через несколько минут" : ""] : "");
  if (!m.rows.length) { tbl.hidden = true; emptyState(empty, "Нет данных фида", "Запустите фид цен: файл runtime/market.json не найден или пуст."); return; }
  empty.hidden = true; tbl.hidden = false;
  if (!tbl.tHead) tbl.appendChild(el("thead", null, el("tr", null, [["Монета", 0], ["Цена", 1], ["Режим", 0], ["5 мин", 1]].map(function (h) { return el("th", { class: h[1] ? "num" : "", text: h[0] }); }))));
  syncList(tbl, m.rows, { key: function (r) { return r.sym; }, sig: function (r) { return sigOf(r, ["age_s"]); }, after: function () { if (m.ok) tkReg("mk", m.age_s, "ago"); }, build: function (r) {
    return rowGroup("m:" + r.sym, [
      { t: el("b", { text: r.sym }), l: "Монета" }, { t: price(r.price), c: "num", l: "Цена" }, { t: regimeChip(r.regime, r.regime_1h), l: "Режим" },
      { t: retc(r.ret_5m), c: "num " + cls(r.ret_5m), l: "5 мин" }
    ], 4, function (td) {
      var pbs = Object.keys(r.prices_by_src || {}).map(function (k) { return k + " " + price(r.prices_by_src[k]); }).join(" · ");
      td.appendChild(kvgrid([["Доходность 1 мин", retc(r.ret_1m), cls(r.ret_1m)], ["Доходность 15 мин", retc(r.ret_15m), cls(r.ret_15m)],
        ["Волатильность 5 мин", isn(r.rv_5m) ? fx(r.rv_5m * 100, 2) + "%" : "ждёт истории"], ["Волатильность 1 ч", isn(r.rv_1h) ? fx(r.rv_1h * 100, 2) + "%" : "ждёт истории"],
        ["Спред", isn(r.spread_bps) ? fx(r.spread_bps, 2) + " б.п." : "–"], ["Перекос top-5 уровней (фид; для агента и карты: «Стакан» в тепловой карте)", isn(r.imbalance) ? fx(r.imbalance, 2) : "–"],
        ["Funding в час", isn(r.funding_hourly) ? fx(r.funding_hourly * 100, 4) + "%" : "–"], ["Откр. интерес, 5 мин", isn(r.oi_chg_5m) ? spc(r.oi_chg_5m * 100, 2) : "–"],
        ["Цены по источникам", pbs || "–"], ["Источник цены", r.src || "–"]]));
    });
  } });
}

function renderHot() {
  var h = S.hot, tbl = $("hot"), empty = $("hot-empty"), more = $("hot-more");
  if (!h.available) {
    put("hot-age", ""); tbl.hidden = true; more.hidden = true; put("hot-blocked", "");
    emptyState(empty, "Сканер горячих монет не запущен", "Нет файла runtime/hot.json. Запуск: ./tb bybit read scan --watch");
    return;
  }
  put("hot-age", ["· ", tk("hot", h.age_s, "ago"), h.stats && h.stats.tradable != null ? " · допущено " + h.stats.tradable + " из " + h.stats.eligible : "", h.warm === false ? " · сканер прогревается: 5-мин метрики появятся позже" : "", isn(h.age_s) && h.age_s > 120 ? " · данные устарели, сканер не обновляет список" : ""]);
  if (!h.top.length) { tbl.hidden = true; more.hidden = true; emptyState(empty, "Пока никого", "Сканер не нашёл монет с всплеском активности."); }
  else {
    empty.hidden = true; tbl.hidden = false;
    var rows = hotAll ? h.top : h.top.slice(0, 8);
    if (!tbl.tHead) tbl.appendChild(el("thead", null, el("tr", null, [["Монета", 0], ["Счёт", 1], ["За 24 ч", 1], ["5 мин", 1]].map(function (x) { return el("th", { class: x[1] ? "num" : "", text: x[0] }); }))));
    syncList(tbl, rows, { key: function (r) { return r.sym; }, sig: function (r) { return sigOf(r); }, after: function () { tkReg("hot", h.age_s, "ago"); }, build: function (r) {
      return rowGroup("hot:" + r.sym, [
        { t: [el("b", { text: r.sym }), r.tradable ? null : [" ", chip("нельзя", "warn")]], l: "Монета" },
        { t: fx(r.score, 2), c: "num", l: "Счёт" }, { t: spc(r.chg_24h_pct, 1), c: "num " + cls(r.chg_24h_pct), l: "За 24 ч" }, { t: spc(r.ret_5m_pct, 2), c: "num " + cls(r.ret_5m_pct), l: "5 мин" }
      ], 4, function (td) {
        td.appendChild(kvgrid([["Цена", price(r.price)], ["Диапазон 5 мин", pc(r.range_5m_pct, 2)], ["Оборот к норме", isn(r.turn_surge_5m) ? "×" + fx(r.turn_surge_5m, 1) : "–"],
          ["Оборот за 24 ч", isn(r.turn_24h_musd) ? fx(r.turn_24h_musd, 1) + " млн USD" : "–"], ["Спред", isn(r.spread_bps) ? fx(r.spread_bps, 1) + " б.п." : "–"],
          ["Лот", isn(r.lot_usdt) ? fx(r.lot_usdt, 1) + " USDT" : "–"], ["Макс. плечо", isn(r.max_leverage) ? r.max_leverage + "×" : "–"], ["Ранг", r.rank != null ? "#" + r.rank : "–"]]));
        if ((r.reasons || []).length) td.appendChild(el("div", { class: "note-box", text: "Почему в списке: " + r.reasons.join("; ") }));
        if ((r.skip || []).length) td.appendChild(el("div", { class: "muted small", text: "Ограничения: " + r.skip.join("; ") }));
      });
    } });
    more.hidden = h.top.length <= 8;
    more.textContent = hotAll ? "Свернуть до 8" : "Показать все (" + h.top.length + ")";
    more.onclick = function () { hotAll = !hotAll; renderHot(); };
  }
  put("hot-blocked", h.blocked && h.blocked.length ? "Не допущены: " + h.blocked.map(function (b) { return b.sym + " (" + ((b.skip || [])[0] || "—") + ")"; }).join("; ") : "");
}
// ---- тепловая карта: топ монет по обороту, колонки из runtime/heat.json, ликвидации из runtime/liq.json; цвет = знак и величина, число всегда рядом
var HEAT_COLS_UI = [["chg_24h_pct", "24ч", 10, "pct"], ["ret_5m_pct", "5м", 1.5, "pct"], ["ret_15m_pct", "15м", 2.5, "pct"], ["range_5m_pct", "Диапазон 5м", 2, "mag"],
  ["turn_surge_5m", "Оборот ×", 5, "mag1"], ["oi_chg_15m_pct", "ΔOI 15м", 5, "pct"], ["oi_chg_1h_pct", "ΔOI 1ч", 10, "pct"], ["funding_pct", "Фандинг", 0.05, "fund"]];
var HEAT_SORTS = [["act", "Активность"], ["ret_15m_pct", "Цена 15м"], ["oi_chg_15m_pct", "ΔOI 15м"], ["funding_pct", "Фандинг"], ["liq", "Ликвидации"], ["flow", "Ордерфлоу"], ["turn_24h_musd", "Оборот 24ч"]];
var heatSort = "act";
var heatOpen = {};          // раскрытые строки карты переживают обновление таблицы (скан раз в 30 с)
function heatShade(v, scale, mode) {
  if (!isn(v)) return "";
  var k = Math.min(Math.abs(v) / scale, 1) * 52;
  if (mode === "mag" || mode === "mag1") return "background:color-mix(in srgb, var(--accent) " + k.toFixed(0) + "%, transparent)";
  if (mode === "fund") return Math.abs(v) >= scale * 0.6 ? "background:color-mix(in srgb, var(--warn) " + k.toFixed(0) + "%, transparent)" : "";
  return "background:color-mix(in srgb, var(" + (v >= 0 ? "--pos" : "--neg") + ") " + k.toFixed(0) + "%, transparent)";
}
function heatText(v, mode) {
  if (!isn(v)) return "–";
  if (mode === "pct") return spc(v, Math.abs(v) < 10 ? 2 : 1);
  if (mode === "mag") return pc(v, 2);
  if (mode === "mag1") return "×" + fx(v, 1);
  return sg(v, 3) + "%";
}
function kfmt(x) { return !isn(x) || x === 0 ? "0" : x >= 1e6 ? fx(x / 1e6, 1) + "M" : x >= 1e3 ? fx(x / 1e3, 0) + "K" : String(Math.round(x)); }
function liqMap(l, px) {
  var map = l && l.map || [];
  if (!map.length) return el("div", { class: "muted small", text: "Ликвидаций за сутки пока не было." });
  var mx = Math.max.apply(null, map.map(function (r) { return r.long + r.short; })) || 1;
  var rows = map.slice().sort(function (a, b) { return b.px - a.px; });
  return el("div", { class: "liqmap" }, rows.map(function (r) {
    var cur = isn(px) && px >= r.px * 0.9975 && px <= r.px * 1.0025;
    return el("div", { class: "lm-row" + (cur ? " is-cur" : "") },
      el("span", { class: "lm-px mono", text: price(r.px) }),
      el("span", { class: "lm-bars", title: "лонги " + kfmt(r.long) + " · шорты " + kfmt(r.short) + " USD" },
        el("i", { class: "lm-l", style: "width:" + (50 * r.long / mx).toFixed(1) + "%" }), el("i", { class: "lm-s", style: "width:" + (50 * r.short / mx).toFixed(1) + "%" })),
      el("span", { class: "lm-v small muted", text: (r.long ? "L " + kfmt(r.long) : "") + (r.long && r.short ? " · " : "") + (r.short ? "S " + kfmt(r.short) : "") }), cur ? el("span", { class: "chip", text: "цена здесь" }) : null);
  }));
}
function heatSortKey(r, liq, fl) {
  if (heatSort === "flow") return fl ? Math.abs(fl.cvd_5m_pct || 0) + Math.abs(fl.imb_pct || 0) : -1;           // монеты без потока ордерфлоу в конце
  if (heatSort === "act") return Math.abs(r.ret_15m_pct || 0) / 2.5 + Math.abs(r.oi_chg_15m_pct || 0) / 5 + Math.abs(r.funding_pct || 0) / 0.05 + (r.turn_surge_5m || 0) / 5 + (liq ? (liq.long_1h + liq.short_1h) / 2e5 : 0) + (fl ? Math.abs(fl.cvd_5m_pct || 0) / 25 : 0);
  if (heatSort === "liq") return liq ? liq.long_1h + liq.short_1h : 0;
  if (heatSort === "turn_24h_musd") return r.turn_24h_musd || 0;
  return Math.abs(r[heatSort] || 0);
}
function flowKv(fl) {
  return [["Агрессия за 5 мин", "купили " + kfmt(fl.buy_5m_usd) + " · продали " + kfmt(fl.sell_5m_usd) + " USD (" + spc(fl.cvd_5m_pct, 0) + ")"],
    ["Крупные принты 5м", fl.big_5m_n ? fl.big_5m_n + " шт., баланс " + (fl.big_5m_usd >= 0 ? "+" : "−") + kfmt(Math.abs(fl.big_5m_usd)) + " USD" : "нет"],
    ["Стакан у цены", isn(fl.bid_usd) ? "bid " + kfmt(fl.bid_usd) + " · ask " + kfmt(fl.ask_usd) + " USD, спред " + fx(fl.spread_bps, 2) + " б.п." : "–"],
    ["Binance", isn(fl.xex_bps) ? (fl.xex_bps >= 0 ? "Bybit дороже на " : "Bybit дешевле на ") + fx(Math.abs(fl.xex_bps), 1) + " б.п." : "нет данных"]];
}
function flowWalls(fl) {
  var w = fl.walls || [];
  return el("div", null, el("h3", { text: "Стены в стакане" }), w.length ? el("ul", { class: "lst" }, w.map(function (x) {
    return el("li", { class: "ev" }, el("b", { class: x.side === "bid" ? "pos" : "neg", text: x.side === "bid" ? "bid (поддержка)" : "ask (сопротивление)" }), el("span", { class: "mono", text: " " + price(x.px) }),
      el("span", { class: "muted small", text: " · " + kfmt(x.usd) + " USD · " + fx(x.dist_pct, 2) + "% от цены · " + (x.stable ? "стоит " + dur(x.age_s) : "появилась " + dur(x.age_s) + " назад, может мигать") }));
  })) : el("div", { class: "muted small", text: "Крупных стен в стакане нет." }));
}
var heatSig = null;
function renderHeat() {
  var D = (S.v2 && S.v2.derivs) || {}, H = D.heat || { available: false }, L = D.liq || { available: false }, FL = D.flow || { available: false }, tbl = $("heat"), empty = $("heat-empty");
  segment($("heat-sort"), HEAT_SORTS.map(function (s) { return [s[0], s[1]]; }), heatSort, function (v) { heatSort = v; heatSig = null; renderHeat(); });
  if (!H.available || !H.rows.length) {
    put("heat-age", ""); put("heat-liq", ""); tbl.hidden = true;
    emptyState(empty, "Тепловой карты пока нет", "Её пишет сканер горячих монет (runtime/heat.json): после запуска платформы появится через минуту.");
    return;
  }
  empty.hidden = true; tbl.hidden = false;
  put("heat-age", ["· ", tk("heat", H.age_s, "ago"), H.age_s > 120 ? " · данные устарели, сканер не обновляет карту" : ""]);
  var liqBy = {};
  (L.syms || []).forEach(function (q) { liqBy[q.sym] = q; });
  var flBy = {};
  (FL.syms || []).forEach(function (q) { flBy[q.sym] = q; });
  if (L.available) {
    var t = L.totals || {};
    put("heat-liq", ["Ликвидации за час (топ монет): лонги ", el("b", { class: "neg", text: kfmt(t.long) }), " · шорты ", el("b", { class: "pos", text: kfmt(t.short) }), " USD", isn(L.age_s) && L.age_s > 60 ? el("span", { class: "muted", text: " · поток ликвидаций не обновляется" }) : ""]);
  } else put("heat-liq", el("span", { class: "muted", text: "Поток ликвидаций не запущен или пока без данных (runtime/liq.json)." }));
  var rows = H.rows.slice().sort(function (a, b) { return heatSortKey(b, liqBy[b.sym], flBy[b.sym]) - heatSortKey(a, liqBy[a.sym], flBy[a.sym]); }).slice(0, 40);
  var sig = JSON.stringify([heatSort, rows.map(function (r) { return [r.sym, r.price, r.ret_15m_pct, r.oi_chg_15m_pct, r.funding_pct, (liqBy[r.sym] || {}).long_1h, (liqBy[r.sym] || {}).short_1h, (flBy[r.sym] || {}).cvd_5m_pct, (flBy[r.sym] || {}).imb_pct, (flBy[r.sym] || {}).xex_bps]; })]);
  if (sig === heatSig) { tkReg("heat", H.age_s, "ago"); return; }
  heatSig = sig;
  tbl.replaceChildren();
  var head = [["Монета", 0], ["Цена", 1]].concat(HEAT_COLS_UI.map(function (c) { return [c[1], 1]; }), [["Ликвидации 1ч", 1], ["CVD 5м", 1], ["Стакан", 1], ["Binance", 1]]);
  tbl.appendChild(el("thead", null, el("tr", null, head.map(function (h) { return el("th", { class: h[1] ? "num" : "", text: h[0] }); }))));
  var body = el("tbody");
  rows.forEach(function (r) {
    var q = liqBy[r.sym];
    var cells = [{ t: [el("b", { text: r.sym }), r.tradable ? null : [" ", chip("нельзя", "warn")]], l: "Монета" }, { t: price(r.price), c: "num", l: "Цена" }];
    HEAT_COLS_UI.forEach(function (c) { cells.push({ t: heatText(r[c[0]], c[3]), c: "num hc", s: heatShade(r[c[0]], c[2], c[3]), l: c[1] }); });
    cells.push({ t: q ? [el("span", { class: "neg", text: kfmt(q.long_1h) }), " / ", el("span", { class: "pos", text: kfmt(q.short_1h) })] : "–", c: "num", l: "Ликвидации 1ч" });
    var fl = flBy[r.sym];
    cells.push({ t: fl ? spc(fl.cvd_5m_pct, 0) : "–", c: "num hc", s: fl ? heatShade(fl.cvd_5m_pct, 40, "pct") : "", l: "CVD 5м" });
    cells.push({ t: fl && isn(fl.imb_pct) ? spc(fl.imb_pct, 0) : "–", c: "num hc", s: fl && isn(fl.imb_pct) ? heatShade(fl.imb_pct, 60, "pct") : "", l: "Стакан" });
    cells.push({ t: fl && isn(fl.xex_bps) ? sg(fl.xex_bps, 1) + " б.п." : "–", c: "num", l: "Binance" });
    var tr = el("tr", { class: "heat-row", tabindex: "0", "aria-expanded": heatOpen[r.sym] ? "true" : "false" });
    cells.forEach(function (c) { tr.appendChild(el("td", { class: c.c || "", style: c.s || null, "data-l": c.l }, c.t)); });
    var det = el("tr", { class: "heat-det", hidden: !heatOpen[r.sym] }, el("td", { colspan: String(cells.length) },
      el("div", { class: "cols2" }, [el("div", null, el("h3", { text: "Карта ликвидаций за сутки: " + r.sym }), q ? liqMap(q, r.price) : el("div", { class: "muted small", text: "По монете ликвидаций за сутки нет в потоке (или монета вне подписки)." })),
        el("div", null, el("h3", { text: "Метрики" }), kvgrid([["Оборот 24ч", isn(r.turn_24h_musd) ? fx(r.turn_24h_musd, 0) + " млн USD" : "–"], ["Открытый интерес", isn(r.oi_musd) ? fx(r.oi_musd, 1) + " млн USD" : "–"],
          ["Счёт сканера", fx(r.score, 2)], ["Ликвидаций за час", q ? String(q.n_1h) : "0"]].concat(fl ? flowKv(fl) : []))), fl ? flowWalls(fl) : null])));
    function toggle() { var on = det.hidden; det.hidden = !on; heatOpen[r.sym] = on; tr.setAttribute("aria-expanded", on ? "true" : "false"); }
    tr.addEventListener("click", toggle);
    tr.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    body.appendChild(tr); body.appendChild(det);
  });
  tbl.appendChild(body);
  tkReg("heat", H.age_s, "ago");
}
function renderMarketTab() { renderHeat(); renderHot(); renderMarket(); }
