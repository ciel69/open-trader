"use strict";
// Вкладка «Идеи»: статусы идей агента (Взято / Наблюдаем / Идеи / Пропущено) и авто-история сканера со статистикой.

// Воронка: идея (цены у уровня ещё нет) -> цена у уровня, агент проверяет -> ПЛАН (вкладка «Планы», его ведёт исполнитель) -> сделка. Здесь только первый шаг и итог.
var IDEA_TABS = [["active", "Ждут уровня", ["watching", "idea"]], ["taken", "Стали планом", ["planned", "taken"]],
  ["skipped", "Не сработали", ["skipped", "expired", "invalidated"]], ["auto", "Авто-история", null]];
var STATUS_RU = { idea: "заметка", watching: "ждём уровень", planned: "план ждёт входа", taken: "стала сделкой", skipped: "пропущена", expired: "истекла", invalidated: "тезис сломан", auto_skipped: "авто" };
var ideaLimit = 10;
var autoLimit = 8;

function tabCount(I, t) { return t[2] ? t[2].reduce(function (a, s) { return a + (I.counts[s] || 0); }, 0) : (I.counts.auto_skipped || 0); }
// «Что было бы» по стороне идеи: через каждый горизонт до и после издержек
function whatIf(o) {
  var parts = [];
  Object.keys(o || {}).sort(function (a, b) { return a - b; }).forEach(function (h) {
    var x = o[h];
    parts.push(!isn(x.dir_pct) ? h + "м " + spc(x.ret_pct, 2) + " (сторона не задана)" : h + "м " + spc(x.dir_pct, 2) + " → " + spc(x.net_pct, 2) + " после издержек");
  });
  return parts.length ? parts.join(" · ") : "ждём замеры +15 и +60 мин (нужен ./tb bybit read scan --watch)";
}
function lastOutcome(o) {
  var hs = Object.keys(o || {}).filter(function (h) { return isn(o[h].net_pct); }).sort(function (a, b) { return b - a; });
  return hs.length ? { h: hs[0], v: o[hs[0]].net_pct } : null;
}
function ideaResult(i) {
  var tr = i.trade;
  if (i.status === "planned") return el("span", { class: "muted", text: "план " + (i.plan_id || "") });
  if (i.status === "taken") {
    if (!tr) return el("span", { class: "muted", text: "сделка не найдена" });
    return el("span", null, tr.status === "open" ? chip("в сделке", "") : null, " ", el("b", { class: cls(tr.pnl), text: isn(tr.pnl) ? sg(tr.pnl, 3) : "–" }), isn(tr.r) ? el("span", { class: "muted small", text: " " + rr(tr.r) }) : null);
  }
  var lo = lastOutcome(i.outcomes);
  return lo ? el("span", { class: cls(lo.v), title: "через " + lo.h + " мин после издержек, по стороне идеи" }, spc(lo.v, 2), el("span", { class: "muted small", text: " " + lo.h + "м" })) : el("span", { class: "muted", text: "ждём" });
}
function ideaDetail(i) {
  var d = el("div", { class: "hd-det" });
  if (i.note) d.appendChild(el("div", { class: "note-box", text: i.note }));
  d.appendChild(kvgrid([
    ["Уровень", i.level != null ? price(i.level) + (isn(i.dist_level_pct) ? " (цена " + spc(i.dist_level_pct, 2) + " от уровня)" : "") : "–"],
    ["Стоп / тейк", price(i.stop) + " / " + price(i.tp)],
    ["Цена записи → сейчас", isn(i.price) ? price(i.price) + (isn(i.now_price) ? " → " + price(i.now_price) : "") + (isn(i.shift_pct) ? " (" + spc(i.shift_pct, 2) + ")" : "") : "–"],
    ["Тег", i.tag || "–"],
    i.zone ? ["Зона", "цена у уровня идеи"] : null,
    i.plan_id ? ["План", i.plan_id + " (вкладка «Планы»)"] : null,
    ["Что было бы", whatIf(i.outcomes)],
    i.trade ? ["Сделка", i.trade.id + " · " + (i.trade.status === "open" ? "открыта" : (i.trade.reason || "закрыта"))] : (i.trade_id ? ["Сделка", i.trade_id + " не найдена в журнале"] : null)
  ]));
  if ((i.mark_notes || []).length) d.appendChild(el("div", { class: "muted small", text: "Пометки: " + i.mark_notes.join("; ") }));
  d.appendChild(el("div", { class: "muted small mono", text: i.id }));
  return d;
}
// ---- карточки вкладки «Ждут уровня»: куда цена должна дойти, сколько осталось, где стоп и тейк, сколько живёт идея
function pctFrom(base, x) { return isn(base) && base && isn(x) ? 100 * (x / base - 1) : null; }
function levelPhrase(i) {
  var d = i.dist_level_pct;
  if (!isn(d)) return { big: "–", cap: "цены сейчас нет", cls: "" };
  if (i.zone) return { big: spc(d, 2), cap: "цена у уровня (в пределах 0.3%)", cls: "near" };
  return { big: (d > 0 ? "↓ " : "↑ ") + pc(Math.abs(d), 2), cap: d > 0 ? "цена выше уровня, ждём снижения" : "цена ниже уровня, ждём роста", cls: Math.abs(d) < 1 ? "close" : "" };
}
function ladder(i) { return ladderBar([["stop", i.stop], ["level", i.level], ["tp", i.tp], ["now", i.now_price]]); }
function ideaCard(i, now) {
  var ph = levelPhrase(i), left = isn(i.expires_epoch) ? i.expires_epoch - now : null;
  var risk = isn(i.level) && isn(i.stop) ? Math.abs(pctFrom(i.level, i.stop)) : null, rew = isn(i.level) && isn(i.tp) ? Math.abs(pctFrom(i.level, i.tp)) : null;
  var rrv = risk && rew ? rew / risk : null;
  var head = function () {
    return el("header", { class: "ic-head" },
      el("b", { class: "ic-sym", text: i.sym }), chip((i.side || "?").toUpperCase(), "side " + (i.side === "long" ? "s-long" : i.side === "short" ? "s-short" : "")),
      i.tag ? chip(i.tag, "") : null,
      el("span", { class: "ic-ttl muted small", title: "идея сама истечёт, если цена до этого не дойдёт", text: left == null ? "без срока" : left > 0 ? "истечёт через " + dur(left) : "срок вышел" }));
  };
  if (!isn(i.level)) {                                   // заметка без уровня: сканер агента по ней не разбудит
    return el("article", { class: "icard" }, head(), el("p", { class: "ic-note", text: i.note || "–" }),
      el("div", { class: "muted small", text: "Заметка без уровня: сканер по ней агента не разбудит." }), el("details", { class: "fold-in" }, el("summary", { text: "Подробнее" }), ideaDetail(i)));
  }
  var card = el("article", { class: "icard" + (i.zone ? " is-near" : "") },
    head(),
    el("div", { class: "ic-main" },
      el("div", { class: "ic-goal" }, el("div", { class: "ic-lab", text: "Ждём цену у уровня" }), el("div", { class: "ic-lvl", text: i.level != null ? price(i.level) : "–" }),
        el("div", { class: "muted small", text: isn(i.now_price) ? "сейчас " + price(i.now_price) : "цена недоступна" })),
      el("div", { class: "ic-dist " + ph.cls }, el("div", { class: "ic-lab", text: "До уровня" }), el("div", { class: "ic-big", text: ph.big }), el("div", { class: "muted small", text: ph.cap }))),
    ladder(i),
    el("div", { class: "lad-leg small" },
      el("span", { class: "lg lg-stop", text: "стоп " + price(i.stop) + (risk != null ? " (риск " + fx(risk, 2) + "%)" : "") }),
      el("span", { class: "lg lg-level", text: "уровень " + price(i.level) }),
      el("span", { class: "lg lg-tp", text: "тейк " + price(i.tp) + (rew != null ? " (до цели " + fx(rew, 2) + "%)" : "") }),
      rrv != null ? el("span", { class: "lg", title: "награда/риск от уровня, без издержек", text: "RR " + fx(rrv, 1) }) : null),
    i.note ? el("p", { class: "ic-note clamp", text: i.note }) : null,
    el("details", { class: "fold-in" }, el("summary", { text: "Подробнее" }), ideaDetail(i)));
  return card;
}
function activeCards(rows) {
  var now = Date.now() / 1000;
  var sorted = rows.slice().sort(function (a, b) {          // ближе к уровню первыми; без цены в конец
    var da = isn(a.dist_level_pct) ? Math.abs(a.dist_level_pct) : 1e9, db = isn(b.dist_level_pct) ? Math.abs(b.dist_level_pct) : 1e9;
    return da - db;
  });
  return [el("div", { class: "icards" }, sorted.slice(0, ideaLimit).map(function (i) { return ideaCard(i, now); }))].concat(
    sorted.length > ideaLimit ? [el("div", { class: "more-row" }, (function () {
      var b = el("button", { type: "button", class: "btn", text: "Показать ещё " + Math.min(20, sorted.length - ideaLimit) });
      b.addEventListener("click", function () { ideaLimit += 20; ideaSig = null; renderIdeas(); });
      return b;
    })())] : []);
}
function ideaTable(rows, taken) {
  var shown = rows.slice(0, ideaLimit);
  var tbl = el("table", { class: "rt" }, el("thead", null, el("tr", null, [["Монета", 0], ["Статус", 0], ["Когда", 0], ["Тезис", 0], [taken ? "Результат сделки" : "Что было бы (после издержек)", 1]].map(function (h) { return el("th", { class: h[1] ? "num" : "", text: h[0] }); }))));
  shown.forEach(function (i) {
    tbl.appendChild(rowGroup("i:" + i.id, [
      { t: [el("b", { text: i.sym }), " ", chip((i.side || "?").toUpperCase(), "side"), i.zone ? chip("у уровня", "warn") : null], l: "Монета" },
      { t: chip(STATUS_RU[i.status] || i.status, i.status === "taken" || i.status === "planned" ? "ok" : ""), l: "Статус" },
      { t: dt(i.ts_epoch), c: "muted-c", l: "Когда" },
      { t: el("span", { class: "clamp", text: i.note || "–" }), c: "wrapc", l: "Тезис" },
      { t: ideaResult(i), c: "num", l: taken ? "Результат" : "Что было бы" }
    ], 5, function (td) { td.appendChild(ideaDetail(i)); }));
  });
  var out = [el("div", { class: "scroll-x" }, tbl)];
  if (rows.length > 10) {
    var b = el("button", { type: "button", class: "btn", text: rows.length > ideaLimit ? "Показать ещё " + Math.min(20, rows.length - ideaLimit) : "Свернуть до 10" });
    b.addEventListener("click", function () { ideaLimit = rows.length > ideaLimit ? ideaLimit + 20 : 10; ideaSig = null; renderIdeas(); });
    out.push(el("div", { class: "more-row" }, b));
  }
  return out;
}

function cellTxt(c, k, w) { return c && c.n ? spc(c[k], 2) + " (" + pc(100 * c[w], 0) + ")" : "–"; }
function autoPanel(I) {
  var st = I.stats, hs = st.horizons, nodes = [];
  nodes.push(el("p", { class: "muted small", text: "События сканера (резкие движения, всплески оборота), в том числе тихие: агент по ним не торговал. Вход по цене события, издержки круга taker " + fx(st.costs.taker_rt_pct, 3) + "% вычтены. «Прод.» = дальше по направлению движения, «откат» = против; в скобках доля плюсовых." }));
  var head = ["Группа", "Значение", "События"];
  hs.forEach(function (h) { head.push(h + "м прод."); head.push(h + "м откат"); });
  var rows = st.groups.map(function (g) {
    var tds = [el("td", { class: "l", text: g.dim }), el("td", { class: "l", text: g.name }), el("td", { class: "num", text: g.events })];
    hs.forEach(function (h) {
      var c = g.h[String(h)], low = c && c.n && c.n < 30;
      tds.push(el("td", { class: "num " + (low ? "muted-c" : cls(c && c.n ? c.cont_net : null)), title: c && c.n ? "n=" + c.n + (low ? " (меньше 30: это не вывод)" : "") + " · до издержек " + spc(c.cont_gross, 2) : "", text: cellTxt(c, "cont_net", "cont_win") + (low ? " *" : "") }));
      tds.push(el("td", { class: "num " + (low ? "muted-c" : cls(c && c.n ? c.fade_net : null)), title: c && c.n ? "n=" + c.n + " · до издержек " + spc(c.fade_gross, 2) : "", text: cellTxt(c, "fade_net", "fade_win") + (low ? " *" : "") }));
    });
    return el("tr", null, tds);
  });
  if (!rows.length) {
    nodes.push(el("div", { class: "empty" }, el("b", { text: "Исходов ещё нет" }), el("span", { text: "Нужен работающий ./tb bybit read scan --watch: первые данные через 15 минут после события." })));
  } else {
    nodes.push(el("div", { class: "scroll-x" }, el("table", { class: "tbl" }, el("thead", null, el("tr", null, head.map(function (h, i) { return el("th", { class: i > 1 ? "num" : "", text: h }); }))), el("tbody", null, rows))));
  }
  var ig = st.idea_groups.filter(function (g) { return g.dim === "все"; })[0];
  if (ig) {
    nodes.push(el("div", { class: "note-box", text: "Идеи агента по стороне идеи (до → после издержек, доля плюсовых): " + hs.map(function (h) {
      var c = ig.h[String(h)];
      return h + "м: " + (c.n ? "n=" + c.n + " " + spc(c.cont_gross, 2) + " → " + spc(c.cont_net, 2) + " (" + pc(100 * c.cont_win, 0) + ")" : "n=0");
    }).join(" · ") }));
  }
  nodes.push(el("h3", { text: "Последние события (" + I.auto_recent.length + ")" }));
  var rec = I.auto_recent.slice(0, autoLimit);
  nodes.push(rec.length ? el("ul", { class: "lst" }, rec.map(function (a) {
    var hh = Object.keys(a.h).sort(function (x, y) { return x - y; }).map(function (h) { return h + "м " + (isn(a.h[h].dir_pct) ? spc(a.h[h].dir_pct, 2) + " → " + spc(a.h[h].net_pct, 2) : spc(a.h[h].ret_pct, 2)); }).join(" · ");
    return el("li", { class: "ev" }, el("span", { class: "t", text: dt(a.ts0) }), el("b", { text: a.sym }), el("span", { text: a.type + (a.dir === "up" ? " ↑" : a.dir === "down" ? " ↓" : "") }), el("span", { class: "mono muted", text: hh || "ждём исходов" }));
  })) : el("div", { class: "muted small", text: "Событий пока нет." }));
  if (I.auto_recent.length > autoLimit) {
    var mb = el("button", { type: "button", class: "btn", text: "Показать ещё" });
    mb.addEventListener("click", function () { autoLimit += 12; ideaSig = null; renderIdeas(); });
    nodes.push(el("div", { class: "more-row" }, mb));
  }
  nodes.push(el("details", { class: "fold-in" }, el("summary", { text: "Как читать эту таблицу" }),
    el("p", { class: "muted small", text: "Звёздочка: меньше 30 событий, это не вывод. Сравнений в таблице: " + st.comparisons + ", поэтому при 5% уровне часть «плюсов» случайна. События одного часа коррелируют. В очереди на замер: " + I.pending + "." })));
  return nodes;
}

var ideaSig = null;
function renderIdeas() {
  var I = S.ideas;
  if (!I) { put("idea-tabs", ""); put("idea-body", ""); return; }
  // вкладка по умолчанию (первая непустая) в URL не пишется: хэш хранит только выбор пользователя
  var cur = IDEA_TABS.some(function (t) { return t[0] === ui.it; }) ? ui.it : (IDEA_TABS.filter(function (t) { return t[2] && tabCount(I, t) > 0; })[0] || IDEA_TABS[3])[0];
  var A = I.stats.agent;
  var sum = [
    { id: "a", t: statTile("Идей агента", null, String(A.total), "", "по статусам во вкладках ниже") },
    { id: "b", t: statTile("Доля взятых", null, A.taken_share != null ? pc(100 * A.taken_share, 0) : "–", "", "взято в сделку из всех идей") },
    { id: "c", t: statTile("Авто-событий", null, String(I.counts.auto_skipped || 0), "", "сканер, без участия агента") },
    { id: "d", t: statTile("Ждут замера", null, String(I.pending || 0), "", "исход через 15/60 мин") }
  ];
  syncList($("ideas-sum"), sum, { key: function (x) { return x.id; }, sig: function () { return JSON.stringify([A, I.counts, I.pending]); }, build: function (x) { return x.t; } });
  segment($("idea-tabs"), IDEA_TABS.map(function (t) { return [t[0], t[1], tabCount(I, t)]; }), cur, function (v) { ui.it = v; ideaLimit = 10; saveUi(); ideaSig = null; renderIdeas(); }, true);
  var tab = IDEA_TABS.filter(function (t) { return t[0] === cur; })[0];
  var body = $("idea-body"), sig = JSON.stringify([cur, ideaLimit, autoLimit, tab[2] ? I.items : [I.stats, I.auto_recent, I.pending]]);
  if (ideaSig === sig) return;
  ideaSig = sig;
  if (!tab[2]) { put(body, autoPanel(I)); return; }
  var rows = I.items.filter(function (i) { return tab[2].indexOf(i.status) >= 0; });
  if (!rows.length) {
    put(body, el("div", { class: "empty" }, el("b", { text: "Здесь пока пусто" }), el("span", { text: cur === "active" ? "Идея появляется, когда агент видит уровень, но входить рано: утренний разбор или пропуск с уровнем. Когда цена дойдёт до уровня, агент проверит вход и, если всё сходится, создаст план." : cur === "taken" ? "Появится, когда по идее будет подан план." : "Появится, когда идея истечёт или не сработает." })));
    return;
  }
  put(body, cur === "active" ? activeCards(rows) : ideaTable(rows, cur === "taken"));
}
