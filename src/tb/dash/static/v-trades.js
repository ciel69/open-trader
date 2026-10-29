"use strict";
// Вкладка «Сделки»: сводка, графики, распределения, история с разбором (строки раскрываются по клику), разрезы.

function statTile(lab, help, val, valCls, sub) {
  return el("div", { class: "tile" }, el("div", { class: "lab" }, help ? tip(lab, help) : lab), el("div", { class: "val " + (valCls || "") }, val), sub ? el("div", { class: "det", text: sub }) : null);
}
function renderMetrics() {
  var m = S.metrics;
  var tiles = [
    { id: "n", t: statTile("Сделок", null, String(m.n), "", "плюс " + m.wins + " · минус " + m.losses) },
    { id: "wr", t: statTile("Доля плюсовых", null, isn(m.winrate) ? pc(m.winrate, 0) : "–", "") },
    { id: "pnl", t: statTile("PnL", null, usd(m.pnl, 2), cls(m.pnl), "комиссии " + fx(m.fees, 2) + " · funding " + fx(m.funding, 2)) },
    { id: "avgm", t: statTile("Среднее к марже", "margin", tpc(m.avg_margin_pct), cls(m.avg_margin_pct), "лучшая " + tpc(m.best_margin_pct) + " · худшая " + tpc(m.worst_margin_pct)) },
    { id: "r", t: statTile("Средний R", "r", isn(m.avg_r) ? sg(m.avg_r, 2) : "–", cls(m.avg_r), isn(m.r_se) ? "± " + m.r_se.toFixed(2) + " по " + m.r_n + " сделкам" : null) },
    { id: "pf", t: statTile("Профит-фактор", "pf", pf(m), "", "ср. выигрыш " + (isn(m.avg_win) ? fx(m.avg_win, 2) : "нет") + " · ср. убыток " + (isn(m.avg_loss) ? fx(m.avg_loss, 2) : "нет")) },
    { id: "dd", t: statTile("Макс. просадка", "dd", fx(m.max_dd, 2) + NB + "USDT", "", "по кривой накопленного PnL") }
  ];
  var host = $("metrics");
  host.querySelectorAll(".skel").forEach(function (n) { n.remove(); });
  syncList(host, tiles, { key: function (x) { return x.id; }, sig: function () { return sigOf(m); }, build: function (x) { return x.t; } });
}

function renderCharts() {
  var pts = S.curve || [];
  lineChart($("chart2"), pts);
  barChart($("bars"), pts, 40);
  put("bars-sub", pts.length > 40 ? "· последние 40 из " + pts.length : pts.length ? "· " + pts.length : "");
}

function renderDist() {
  var bd = S.breakdown, rs = (bd.reason || []).filter(function (g) { return g.name !== "-"; });
  var es = (bd.err || []).filter(function (g) { return g.name !== "(без разбора)"; });
  var unrev = (bd.err || []).filter(function (g) { return g.name === "(без разбора)"; })[0];
  if (!changed($("dist"), [rs, es, unrev])) return;
  var a = hbars(rs, "сд."), b = hbars(es, "сд.");
  put("dist", [
    el("div", null, el("h3", null, "Причины выхода"), a || el("div", { class: "empty" }, el("b", { text: "Нет закрытых сделок" }))),
    el("div", null, el("h3", null, "Теги ошибок из разбора"), b || el("div", { class: "empty" }, el("b", { text: "Разбор ещё не записан" }), el("span", { text: "Теги ставятся командой ./tb journal post после закрытия сделки." })),
      unrev && b ? el("div", { class: "muted small", text: "Без разбора: " + unrev.n + " " + plural(unrev.n, "сделка", "сделки", "сделок") }) : null)
  ]);
}

function reviewChips(t) {
  var kids = (t.err || []).map(function (g) { return chip(g, g === "good" || g === "good-process-bad-luck" ? "" : "tag-err"); });
  if (t.rule_followed) kids.push(chip("правила: " + ({ yes: "соблюдены", no: "нарушены", partial: "частично" }[t.rule_followed] || t.rule_followed)));
  return kids;
}
function histDetail(t) {
  var held = isn(t.ts_close_epoch) && isn(t.ts_open_epoch) ? t.ts_close_epoch - t.ts_open_epoch : null;
  var pairs = [
    ["Вход → выход", price(t.entry) + " → " + price(t.exit)],
    [tip("Движение цены", "move"), tpc(t.move_pct), cls(t.move_pct)],
    ["Время в сделке", dur(held)],
    ["Комиссии", fx(t.fees_usdt, 3) + " USDT" + (t.funding_usdt ? " · funding " + fx(t.funding_usdt, 3) : "")],
    ["Плечо × маржа", (isn(t.lev) ? t.lev + "× " : "") + fx(t.margin, 2) + " USDT"],
    ["Риск до стопа", fx(t.risk_usdt, 3) + " USDT"],
    t.path ? [el("span", null, tip("MAE", "mae"), " / ", tip("MFE", "mfe")), pathText(t.path)] : null,
    t.path && isn(t.path.mfe_to_tp) ? ["Путь до тейка", Math.round(t.path.mfe_to_tp * 100) + "%"] : null,
    ["Тип · площадка", (t.stype || "–") + " · " + venueLabel(t.venue) + " · " + t.mode],
    ["Открыта", dt(t.ts_open_epoch)]
  ];
  if (t.plan_id) {
    pairs.push(["План", el("span", null, el("span", { class: "mono", text: t.plan_id }), isn(t.plan_rev) ? " · r" + t.plan_rev : "")]);
    pairs.push(["Эксперимент · вариант", [t.exp, t.var, t.setup, t.hyp].filter(Boolean).join(" / ") || "–"]);
  }
  if ((t.adds || []).length || (t.parts || []).length) {
    pairs.push(["Добор · частичные закрытия", (t.adds || []).length + " · " + (t.parts || []).length + (isn(t.realized_parts) ? " (реализовано " + sg(t.realized_parts, 3) + " USDT)" : "")]);
  }
  if (isn(t.sl0) && t.sl0 !== t.sl) pairs.push(["Стоп: начальный → итоговый", price(t.sl0) + " → " + price(t.sl)]);
  var d = el("div", { class: "hd-det" }, kvgrid(pairs));
  if (t.plan_id) d.appendChild(el("div", { class: "pl-links" }, tabLink("хронология решений по плану", "decisions", { dp: t.plan_id }, "pl-a"), tabLink("план на доске", "plans", {}, "pl-a")));
  var rv = reviewChips(t);
  if (rv.length) d.appendChild(el("div", { class: "chips" }, rv));
  if (t.tags && t.tags.length) d.appendChild(el("div", { class: "chips" }, t.tags.map(function (g) { return chip(g); })));
  if (t.lesson) d.appendChild(el("div", { class: "note-box" }, el("b", { text: "Урок: " }), t.lesson));
  if (t.thesis) d.appendChild(el("div", { class: "note-box" }, el("b", { text: "Тезис: " }), t.thesis));
  if (t.note) d.appendChild(el("div", { class: "muted small", text: "Заметка: " + t.note }));
  d.appendChild(el("div", { class: "muted small mono", text: t.id }));
  return d;
}

// Шкала результата в R: от стопа (−1R) до +3R; заливка идёт от нуля к итогу
function rBar(t) {
  var r = isn(t.r) ? Math.max(-1.2, Math.min(3.2, t.r)) : null;
  if (r == null) return null;
  var lo = -1.2, hi = 3.2, z = (0 - lo) / (hi - lo) * 100, v = (r - lo) / (hi - lo) * 100;
  return el("div", { class: "rbar2", role: "img", "aria-label": "Результат " + sg(t.r, 2) + "R, от стопа −1R до +3R" },
    el("i", { class: "rb2-fill " + (r >= 0 ? "pos" : "neg"), style: "left:" + Math.min(z, v) + "%;width:" + Math.abs(v - z) + "%" }),
    el("i", { class: "rb2-zero", style: "left:" + z + "%" }), el("i", { class: "rb2-end", style: "left:" + v + "%" }));
}
function exitWord(r) { return ({ sl: "стоп", tp: "тейк", manual: "вручную", time: "по времени" })[r] || r || "–"; }
function tradeCard(t) {
  var key = "h:" + t.id, win = t.pnl_usdt > 0, det = null;
  var btn = el("button", { type: "button", class: "pc-toggle", "aria-expanded": "false" }, el("span", { class: "cv", "aria-hidden": "true" }), "Разбор");
  var held = isn(t.ts_close_epoch) && isn(t.ts_open_epoch) ? t.ts_close_epoch - t.ts_open_epoch : null;
  var tags = (t.err || []).slice(0, 3).map(function (g) { return chip(g, g === "good" || g === "good-process-bad-luck" ? "" : "tag-err"); });
  var card = el("article", { class: "tcard " + (win ? "is-win" : "is-loss") },
    el("div", { class: "tc-top" }, el("span", { class: "sym", text: t.sym || "?" }), chip((t.side || "?").toUpperCase() + (isn(t.lev) ? " " + t.lev + "×" : ""), "side"),
      t.mode === "paper" ? chip("paper", "paper") : null, el("span", { class: "tc-date muted small", text: dt(t.ts_close_epoch) })),
    el("div", { class: "tc-main" }, el("div", { class: "tc-big " + cls(t.pnl_pct_margin), text: tpc(t.pnl_pct_margin) }),
      el("div", { class: "tc-side" }, el("span", { class: "pc-usd " + cls(t.pnl_usdt), text: usd(t.pnl_usdt, 3) }), el("span", { class: "muted small " + cls(t.r), text: isn(t.r) ? rr(t.r) : "" }))),
    rBar(t),
    el("div", { class: "tc-path muted small" }, price(t.entry), el("span", { class: "tc-arrow", "aria-hidden": "true", text: " → " }), price(t.exit), " · ", exitWord(t.reason), held != null ? " · " + dur(held) : ""),
    tags.length ? el("div", { class: "chips tight" }, tags) : null,
    btn);
  function setOpen(on) {
    if (on && !det) { det = histDetail(t); card.appendChild(det); }
    if (det) det.hidden = !on;
    card.classList.toggle("open", on);
    btn.setAttribute("aria-expanded", on ? "true" : "false"); btn.lastChild.textContent = on ? "Свернуть" : "Разбор";
    if (on) ui.open[key] = true; else delete ui.open[key];
  }
  btn.addEventListener("click", function () { setOpen(btn.getAttribute("aria-expanded") !== "true"); });
  if (ui.open[key]) setOpen(true);
  return card;
}
function renderHistory() {
  var all = S.history, f = all.filter(function (t) { return ui.res === "win" ? t.pnl_usdt > 0 : ui.res === "loss" ? t.pnl_usdt <= 0 : true; });
  var nWin = all.filter(function (t) { return t.pnl_usdt > 0; }).length;
  var n = ui.hn === "all" ? f.length : Math.min(parseInt(ui.hn, 10) || 15, f.length);
  segment($("res-seg"), [["all", "Все", all.length], ["win", "Плюс", nWin], ["loss", "Минус", all.length - nWin]], ui.res, function (v) { ui.res = v; ui.hn = "15"; saveUi(); renderHistory(); });
  put("hist-count", all.length ? "· показано " + n + " из " + (f.length === all.length ? S.history_total : f.length) : "");
  var box = $("history"), empty = $("hist-empty"), more = $("more");
  if (!f.length) {
    box.hidden = true; more.hidden = true;
    emptyState(empty, all.length ? "Нет сделок по этому фильтру" : "Закрытых сделок пока нет", all.length ? "Выберите «Все» над списком." : "История появится после первой закрытой сделки (фильтр: " + S.filter.mode + " / " + S.filter.venue + ").");
    return;
  }
  empty.hidden = true; box.hidden = false;
  syncList(box, f.slice(0, n), { key: function (t) { return t.id; }, sig: function (t) { return sigOf(t) + (ui.open["h:" + t.id] ? 1 : 0); }, build: tradeCard });
  var tot = f.length;
  more.hidden = tot <= 15;
  more.textContent = n < tot ? "Показать ещё " + Math.min(25, tot - n) : "Свернуть до 15";
  more.onclick = function () { ui.hn = n < tot ? (n + 25 >= tot ? "all" : String(n + 25)) : "15"; saveUi(); renderHistory(); };
}

var BD0 = [["reason", "Выход"], ["err", "Ошибки"], ["tag", "Теги"], ["sym", "Монета"], ["side", "Сторона"], ["stype", "Тип сессии"], ["venue", "Площадка"]];
var BD_PLAN = [["exp", "Эксперимент"], ["var", "Вариант"], ["setup", "Сетап"]];
function renderBreakdown() {
  var fold = $("bd-fold");
  if (fold.open !== ui.bdo) fold.open = ui.bdo;
  var BD = S.history.some(function (t) { return t.exp || t.var || t.setup; }) ? BD0.concat(BD_PLAN) : BD0;
  if (!BD.some(function (b) { return b[0] === ui.bd; })) ui.bd = "reason";
  segment($("bd-seg"), BD, ui.bd, function (v) { ui.bd = v; saveUi(); renderBreakdown(); });
  var rows = S.breakdown[ui.bd] || [];
  if (!changed($("bd-table"), [ui.bd, rows])) return;
  var head = el("tr", null, [["Группа", 0], ["Сд.", 1], ["Плюсовых", 1], ["PnL", 1], ["Комис.", 1], ["Ср. R", 1, "r"], ["PF", 1, "pf"]].map(function (h) { return el("th", { class: h[1] ? "num" : "" }, h[2] ? tip(h[0], h[2]) : h[0]); }));
  var body = rows.length ? rows.map(function (g) {
    return el("tr", null, el("td", { text: g.name }), el("td", { class: "num", text: g.n }), el("td", { class: "num", text: isn(g.winrate) ? pc(g.winrate, 0) : "–" }),
      el("td", { class: "num " + cls(g.pnl), text: sg(g.pnl, 2) }), el("td", { class: "num", text: fx(g.fees, 2) }), el("td", { class: "num " + cls(g.avg_r), text: isn(g.avg_r) ? sg(g.avg_r, 2) : "–" }), el("td", { class: "num", text: pf(g) }));
  }) : el("tr", null, el("td", { class: "empty-c", colspan: 7, text: "Нет данных для этого разреза" }));
  put("bd-table", [el("thead", null, head), el("tbody", null, body)]);
}
function renderTrades() { renderMetrics(); renderCharts(); renderDist(); renderHistory(); renderBreakdown(); renderExp(); }
