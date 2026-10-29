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
        ["Спред", isn(r.spread_bps) ? fx(r.spread_bps, 2) + " б.п." : "–"], ["Перекос стакана", isn(r.imbalance) ? fx(r.imbalance, 2) : "–"],
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
function renderMarketTab() { renderHot(); renderMarket(); }
