"use strict";
// Вкладка «Модели»: сколько заработала каждая модель и насколько эффективен её вызов. Данные: /api/settings/models_perf (сделки с тегом model.<профиль> + учёт вызовов LLM).
var MP = { days: "7", mode: "paper", data: null, err: null, at: 0, busy: false, sort: "net" };
var MP_SORTS = [["net", "Чистый"], ["pnl", "PnL"], ["eff", "PnL/100 вызовов"], ["usd", "Расход"]];
var MP_MODES = [["paper", "Бумага"], ["live", "Live"], ["all", "Все"]];

// Бумага основной режим проверки теорий и экспериментов: вкладка показывает её по умолчанию и не зависит от общего фильтра режима вверху страницы
function mpMode() { return MP.mode; }
function mpFetch(force) {
  var now = performance.now();
  if (MP.busy || (!force && MP.at && now - MP.at < 12000)) return;
  MP.busy = true;
  fetch("/api/settings/models_perf?days=" + MP.days + "&mode=" + mpMode(), { cache: "no-store", credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (j) { MP.at = performance.now(); if (j && j.error) MP.err = j.error; else { MP.data = j; MP.err = null; } })
    .catch(function () { MP.err = "network"; })
    .then(function () { MP.busy = false; renderModels("data"); });
}
function mpSigned(x, d) { return !isn(x) ? "–" : (x > 0 ? "+" : "") + x.toFixed(d == null ? 2 : d); }
function mpCls(x) { return !isn(x) ? "" : x > 0 ? "pos" : x < 0 ? "neg" : ""; }

function mpSummary(rows) {
  var withT = rows.filter(function (r) { return r.n > 0; });
  if (!withT.length) return el("p", { class: "muted", text: "За период нет закрытых сделок с привязкой к модели: сравнивать пока нечего." });
  var best = withT.slice().sort(function (a, b) { return b.net - a.net; })[0];
  var eff = withT.filter(function (r) { return isn(r.pnl_per_100_calls); }).sort(function (a, b) { return b.pnl_per_100_calls - a.pnl_per_100_calls; })[0];
  var tiles = [el("div", { class: "stat" }, el("div", { class: "muted small", text: "Больше всего заработала (за вычетом расхода на ИИ)" }), el("div", { class: "big " + mpCls(best.net), text: best.id + "  " + mpSigned(best.net) + " USDT" }),
    el("div", { class: "muted small", text: best.n + " " + plural(best.n, "сделка", "сделки", "сделок") + (best.reliable ? "" : " · мало данных") }))];
  if (eff) tiles.push(el("div", { class: "stat" }, el("div", { class: "muted small", text: "Эффективнее на вызов" }), el("div", { class: "big " + mpCls(eff.pnl_per_100_calls), text: eff.id + "  " + mpSigned(eff.pnl_per_100_calls) + " USDT / 100 вызовов" }),
    el("div", { class: "muted small", text: eff.calls + " " + plural(eff.calls, "вызов", "вызова", "вызовов") })));
  return el("div", { class: "stats-row" }, tiles);
}

function mpTable(rows, minN) {
  var mx = Math.max.apply(null, rows.map(function (r) { return Math.abs(r.pnl); }).concat([1e-9]));
  var heads = [["Модель", 0, "Профиль модели (провайдер и настройки)."], ["Сделок", 1, "Закрытых сделок по планам этой модели; в скобках доля прибыльных."], ["PnL, USDT", 1, "Результат закрытых сделок до расхода на ИИ."],
    ["Расход ИИ, $", 1, "Сколько стоили вызовы модели за период (локальная модель: 0)."], ["Чистый, USDT", 1, "PnL минус расход на ИИ: то, что модель реально принесла."],
    ["Вызовов", 1, "Все вызовы профиля за период (поиск входа, сопровождение, разбор, новости)."], ["PnL / 100 вызовов", 1, "Сколько USDT приходится на 100 вызовов: чем больше, тем эффективнее модель на своём количестве вызовов."],
    ["PnL / $", 1, "Сколько USDT результата на 1 доллар расхода на ИИ. Для бесплатной модели не считается."], ["Ошибки", 1, "Доля неудачных вызовов и доля ошибок формата ответа."], ["Ответ, с", 1, "Медианное время ответа."]];
  var thead = el("thead", null, el("tr", null, heads.map(function (h) { return el("th", { class: h[1] ? "num" : "" }, tip(h[0], h[2], true)); })));
  var tbody = el("tbody", null, rows.map(function (r) {
    var bar = el("div", { class: "rbar", "aria-hidden": "true" }, el("i", { class: r.pnl >= 0 ? "pos-fill" : "neg-fill", style: "width:" + Math.max(r.n ? 3 : 0, Math.abs(r.pnl) / mx * 100).toFixed(1) + "%;background:var(" + (r.pnl >= 0 ? "--pos" : "--neg") + ")" }));
    var errs = isn(r.err_rate) ? pc(r.err_rate * 100, 0) + " · " + pc((r.fmt_err_rate || 0) * 100, 0) : "–";
    return el("tr", null,
      el("td", { class: "l" }, el("b", { text: r.id }), r.n && !r.reliable ? el("span", { class: "chip", title: "Меньше " + minN + " сделок: это наблюдение, а не вывод", text: "мало данных" }) : null),
      el("td", { class: "num", text: r.n ? r.n + " (" + Math.round(r.wins / r.n * 100) + "%)" : "0" }),
      el("td", { class: "num " + mpCls(r.pnl) }, r.n ? mpSigned(r.pnl) : "–", r.n ? bar : null),
      el("td", { class: "num", text: r.usd > 0 ? "$" + fx(r.usd, 3) : "0" }),
      el("td", { class: "num " + mpCls(r.n ? r.net : null), text: r.n ? mpSigned(r.net) : "–" }),
      el("td", { class: "num", text: r.calls ? String(r.calls) : "0" }),
      el("td", { class: "num " + mpCls(r.pnl_per_100_calls), text: mpSigned(r.pnl_per_100_calls) }),
      el("td", { class: "num " + mpCls(r.pnl_per_usd), text: isn(r.pnl_per_usd) ? mpSigned(r.pnl_per_usd, 1) : "–" }),
      el("td", { class: "num", text: errs }),
      el("td", { class: "num", text: isn(r.lat_p50_ms) ? (r.lat_p50_ms / 1000).toFixed(1) : "–" }));
  }));
  return el("div", { class: "tbl-wrap", style: "overflow-x:auto" }, el("table", { class: "tbl" }, thead, tbody));
}

var _mpSig = "";
function renderModels(mode) {
  var host = $("tab-models");
  if (!host) return;
  if (!host._built) {
    host._built = true;
    put(host, [el("div", { id: "mp-top" }), el("div", { id: "mp-page" })]);
    mpFetch(true);
  }
  if (mode === undefined) mpFetch(false);
  var sig = JSON.stringify([MP.data, MP.err, MP.days, MP.sort, mpMode()]);
  if (sig === _mpSig && host._drawn) return;
  _mpSig = sig; host._drawn = true;
  var seg = el("div", { class: "seg", role: "group", "aria-label": "Период" });
  segment(seg, [["1", "Сутки"], ["7", "7 дней"], ["30", "30 дней"]], MP.days, function (v) { MP.days = v; MP.at = 0; MP.data = null; mpFetch(true); renderModels("data"); });
  var d = MP.data;
  var body;
  if (MP.err) body = el("p", { class: "muted", text: "Не удалось загрузить: " + MP.err });
  else if (!d) body = el("div", { class: "skel", style: "height:120px" });
  else {
    var rows = d.rows.slice();
    var key = { net: function (r) { return r.n ? r.net : -1e9; }, pnl: function (r) { return r.n ? r.pnl : -1e9; }, eff: function (r) { return isn(r.pnl_per_100_calls) ? r.pnl_per_100_calls : -1e9; }, usd: function (r) { return r.usd; } }[MP.sort];
    rows.sort(function (a, b) { return key(b) - key(a); });
    var sortSeg = el("div", { class: "seg", role: "group", "aria-label": "Сортировка" });
    segment(sortSeg, MP_SORTS, MP.sort, function (v) { MP.sort = v; renderModels("data"); });
    body = [mpSummary(rows), el("div", { class: "mp-sort muted small" }, "Сортировка: ", sortSeg), mpTable(rows, d.min_n), el("p", { class: "muted small", text: d.note })];
  }
  var mseg = el("div", { class: "seg", role: "group", "aria-label": "Режим" });
  segment(mseg, MP_MODES, MP.mode, function (v) { MP.mode = v; MP.at = 0; MP.data = null; mpFetch(true); renderModels("data"); });
  put("mp-top", el("section", { class: "block" }, el("h2", null, "Результат по моделям", el("span", { class: "sub", text: MP.mode === "all" ? "бумага и live вместе: выводы по такой смеси осторожно" : "режим: " + (MP.mode === "paper" ? "бумага" : "live") })),
    el("div", { class: "mp-ctl" }, mseg, seg)));
  put("mp-page", el("section", { class: "block" }, body));
}
registerTab("models", "Модели", "M4 20V10m6 10V4m6 16v-7m4 7H2", renderModels);
