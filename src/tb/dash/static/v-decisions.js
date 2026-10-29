"use strict";
// Вкладка «Решения»: лента state/decisions.jsonl (что решил мозг и почему, что сделал исполнитель, отказы Gate) и сводка дисциплины за сутки.
// Данные: /api/v2/decisions (новые сверху, фильтры в хэше URL), раскрытие строки: basis, args, link_id, время ответа биржи, ссылка на брифинг.

var ACTOR_RU = { "brain": "мозг", "exec": "исполнитель", "owner": "владелец", "system": "система" };
var KIND_RU = { cmd: "команда", accept: "принято", reject: "отказ", transition: "переход", intent: "намерение", result: "результат", rule: "правило", skip: "пропуск", protect: "защита",
  reconcile: "сверка", event: "событие", note: "заметка" };
var decLimit = 60;
var DEC = null;

function actorKey(a) { return (a || "").split(":")[0]; }
function actorChip(a) {
  var k = actorKey(a);
  return el("span", { class: "chip actor a-" + k, title: a, text: ACTOR_RU[k] || a || "?" });
}
// OK / отказ с кодом (красным) / пропуск правила (жёлтым)
function resultChip(d) {
  if (d.denied) return el("span", { class: "chip bad", title: "отказ", text: "✕ " + (d.code || "отказ") });
  if (d.idle) return el("span", { class: "chip", title: "служебная запись: мозг ждёт, это не отказ", text: "ждёт" });
  if (d.noop) return el("span", { class: "chip warn", title: "правило сработало, действие не нужно или не выполнено", text: "пропуск " + (d.code || "") });
  if (d.ok === true) return el("span", { class: "chip ok", text: "OK" });
  return el("span", { class: "muted small", text: d.kind === "cmd" || d.kind === "intent" ? "–" : (KIND_RU[d.kind] || d.kind || "–") });
}
function decDay(day) {
  if (!day) return;
  var codes = (day.reject_codes || []).map(function (c) { return c.code + " ×" + c.n; }).join(", ");
  var tiles = [
    { id: "n", t: statTile("Решений за сутки", null, String(day.n), "", "записей журнала") },
    { id: "brain", t: statTile("Команд мозга", null, String(day.brain), "", "submit, amend, stop, close") },
    { id: "rej", t: statTile("Отказов Gate", "Отказы проверок (guard, валидатор, стоп не строже): индикатор дисциплины. Много отказов = мозг предлагает недопустимое.", String(day.rejects), day.rejects ? "neg" : "", codes || "отказов нет") },
    { id: "auto", t: statTile("Авто-действий правил", null, String(day.auto), "", "пропусков правил " + day.skips + (day.protect ? " · защит " + day.protect : "")) }
  ];
  var host = $("dec-sum");
  syncList(host, tiles, { key: function (x) { return x.id; }, sig: function () { return sigOf(day); }, build: function (x) { return x.t; } });
}
function decDetail(d) {
  var pairs = [
    ["Номер · вид", "#" + d.seq + " · " + (KIND_RU[d.kind] || d.kind) + " · " + d.what],
    ["Время", dts(d.t) + (d.ts ? " (UTC " + d.ts + ")" : "")],
    ["Исполнитель решения", d.actor],
    ["План", d.plan ? d.plan + (isn(d.rev) ? " · r" + d.rev : "") + (d.mode ? " · " + d.mode : "") : "–"],
    ["Причина", d.why || "–"],
    Object.keys(d.args || {}).length ? ["Параметры", kvList(d.args)] : null,
    Object.keys(d.basis || {}).length ? ["Основание", kvList(d.basis)] : null,
    isn(d.px) || isn(d.r) ? ["Цена · R на момент", (isn(d.px) ? price(d.px) : "–") + " · " + (isn(d.r) ? rr(d.r) : "–")] : null,
    Object.keys(d.res || {}).length ? ["Ответ", kvList(d.res)] : null,
    d.link_id ? ["link_id", el("span", { class: "mono", text: d.link_id })] : null,
    isn(d.exch_ms) ? ["Ответ биржи", d.exch_ms + " мс"] : null,
    d.action_id ? ["action_id", el("span", { class: "mono", text: d.action_id })] : null,
    d.h ? ["Хеш записи", el("span", { class: "mono", text: d.h })] : null
  ];
  var nodes = [kvgrid(pairs)];
  var links = [];
  if (d.brief_id) links.push(tabLink("брифинг, на который опирался мозг", "brief", { b: d.brief_id }, "pl-a"));
  if (d.plan) links.push(tabLink("карточка плана", "plans", {}, "pl-a"));
  if (links.length) nodes.push(el("div", { class: "pl-links" }, links));
  return el("div", { class: "hd-det" }, nodes);
}
function decRow(d) {
  var bad = d.denied;
  return rowGroup("d:" + d.seq, [
    { t: el("span", { class: "mono small", text: dts(d.t) }), l: "Время", c: "dec-t" },
    { t: actorChip(d.actor), l: "Кто" },
    { t: d.plan ? el("span", { class: "mono small nowrap", title: d.plan, text: planShort(d.plan) }) : el("span", { class: "muted", text: "–" }), l: "План" },
    { t: [el("b", { text: d.action }), d.mode === "paper" ? " " : null, d.mode === "paper" ? chip("paper", "paper") : null], l: "Действие", c: "wrapc" },
    { t: el("span", { class: "clamp", title: d.why, text: d.why || "–" }), c: "wrapc", l: "Почему" },
    { t: [resultChip(d), isn(d.px) ? el("span", { class: "muted small", text: " " + price(d.px) + (isn(d.r) ? " · " + rr(d.r) : "") }) : null], l: "Результат", c: "num" }
  ], 6, function (td) { td.appendChild(decDetail(d)); }, bad ? "row-bad" : "");
}
// Карточка «Мозг: что он видит» (GET /api/brain/watch, контракт docs/spec_v2/BRAIN_WATCH.md). Минимальная вёрстка: только textContent, без внешних ресурсов.
var WATCH_RU = { working: "работает", waiting_targets: "ждёт целей", blocked_by_dayplan: "закрыт планом дня", blackout: "окно новостей", protect_only: "только защита", brain_down: "мозг недоступен" };
var OUTCOME_RU = { idle: "простой: нет планов и позиций", no_action: "без действия", acted: "выполнил действие", done: "выполнено", error: "ошибка", skipped: "пропуск" };
var REGIME_RU = { range: "флэт", storm: "шторм", trend_up: "тренд вверх", trend_down: "тренд вниз", unknown: "не определён", unclear: "неясен" };
function watchLine(b) {
  var bl = b.blocks || {};
  var parts = [];
  if (bl.hot && bl.hot.present) parts.push("горячих " + bl.hot.n + (isn(bl.hot.stale_s) ? " (устарели " + bl.hot.stale_s + " с)" : ""));
  else parts.push("горячих: нет данных");
  if (bl.struct) parts.push("структура: " + bl.struct.n + " монет" + (bl.struct.regimes ? " (" + Object.keys(bl.struct.regimes).map(function (k) { return (REGIME_RU[k] || k) + " " + bl.struct.regimes[k]; }).join(", ") + ")" : ""));
  if (bl.ideas) parts.push("идей " + bl.ideas.active);
  if (bl.intel && isn(bl.intel.age_s)) parts.push("интел " + Math.round(bl.intel.age_s / 60) + " мин");
  if (bl.news_digest) parts.push("новости: " + bl.news_digest.n + (isn(bl.news_digest.age_s) ? " (" + Math.round(bl.news_digest.age_s / 60) + " мин)" : ""));
  if (bl.recent) parts.push("событий " + bl.recent.n);
  return parts.join(" · ");
}
function applyWatch(j) {
  var host = $("dec-watch");
  if (!host) return;
  if (!j || !j.state) { host.hidden = true; return; }
  host.hidden = false;
  var nw = j.next_wake || {}, lj = j.last_job;
  var roles = Object.keys(nw.roles || {}).map(function (r) { return r + ": " + nw.roles[r].join(", "); }).join(" | ");
  var cls = j.state.code === "brain_down" || j.state.code === "protect_only" ? "chip bad" : (j.state.code === "working" ? "chip ok" : "chip");
  var kids = [
    el("div", { class: "h-row" }, el("h2", { text: "Мозг: " + (WATCH_RU[j.state.code] || j.state.code) }), el("span", { class: cls, text: j.state.code })),
    el("p", { text: j.state.text || "" }),
    el("p", { class: "muted small", text: "Видит: " + watchLine(j) + (j.brief && j.brief.id ? " · досье " + j.brief.id : "") }),
    el("p", { class: "muted small", text: "Последнее задание: " + (lj ? (lj.role || "?") + " · " + (OUTCOME_RU[lj.outcome] || lj.outcome || "?") + (lj.code ? " · " + lj.code : "") + (isn(lj.ts) ? " · " + dts(lj.ts) : "") : "нет") }),
    j.dayplan ? el("p", { class: "muted small", text: "План дня " + (j.dayplan.day_id || "") + ": режим " + (REGIME_RU[j.dayplan.regime] || j.dayplan.regime || "–") + " · новых планов до " + (isn(j.dayplan.max_new_plans) ? j.dayplan.max_new_plans : "–") + ((j.dayplan.risks || []).length ? " · риски: " + j.dayplan.risks.join(", ") : "") }) : null,
    el("p", { class: "muted small", text: "Просыпается на: " + (roles || "подписка не настроена") + (nw.subscriber_enabled === false ? " (подписчик api выключен)" : "") + (isn(nw.jobs_per_hour) ? " · заданий в час " + nw.jobs_per_hour : "") })
  ].filter(Boolean);
  host.replaceChildren.apply(host, kids);
}
function renderDecisions() {
  fetchTab("watch", "/api/brain/watch", applyWatch);
  var q = { limit: decLimit, actor: ui.da === "all" ? "" : ui.da, only: ui.df === "fail" ? "fail" : "", mode: ui.dm === "all" ? "" : ui.dm, plan: ui.dp };
  fetchTab("dec", "/api/v2/decisions?" + qs(q), applyDecisions);
}
function applyDecisions(j, fresh, err) {
  if (ui.tab !== "decisions") return;
  var empty = $("dec-empty"), tbl = $("dec-table"), more = $("dec-more");
  segment($("dec-actor"), [["all", "Все"], ["brain", "Мозг"], ["exec", "Исполнитель"], ["owner", "Владелец"]], ui.da, function (v) { ui.da = v; saveUi(); renderDecisions(); });
  segment($("dec-fail"), [["all", "Все"], ["fail", "Отказы и пропуски"]], ui.df, function (v) { ui.df = v; saveUi(); renderDecisions(); });
  segment($("dec-mode"), [["all", "Все"], ["live", "live"], ["paper", "paper"]], ui.dm, function (v) { ui.dm = v; saveUi(); renderDecisions(); });
  var sel = $("dec-plan");
  if (j) {
    var plans = [""].concat(j.plans || []);
    if (ui.dp && plans.indexOf(ui.dp) < 0) plans.push(ui.dp);
    if (sel.options.length !== plans.length || !plans.every(function (p, i) { return sel.options[i].value === p; })) {
      sel.replaceChildren();
      plans.forEach(function (p) { sel.appendChild(el("option", { value: p, text: p ? planShort(p) : "все планы" })); });
    }
    sel.value = ui.dp;
  }
  if (err && !j) { emptyState(empty, "Не удалось загрузить решения", err); tbl.hidden = true; more.hidden = true; return; }
  if (!j) return;
  decDay(j.day);
  DEC = j;
  var rows = j.decisions || [];
  put("dec-count", j.total ? "· показано " + rows.length + " из " + j.total : "");
  if (!rows.length) {
    tbl.hidden = true; more.hidden = true;
    emptyState(empty, j.last_seq ? "Нет решений по этому фильтру" : "Журнал решений пуст", j.last_seq ? "Снимите фильтры над таблицей." : "state/decisions.jsonl появляется, когда мозг или исполнитель v2 начнут работу.");
    return;
  }
  empty.hidden = true; tbl.hidden = false;
  if (!tbl.tHead) tbl.appendChild(el("thead", null, el("tr", null, ["Время", "Кто", "План", "Действие", "Почему", "Результат"].map(function (h, i) { return el("th", { text: h, class: i === 5 ? "num" : "" }); }))));
  syncList(tbl, rows, { key: function (d) { return d.seq; }, sig: function (d) { return sigOf(d); }, build: decRow });
  more.hidden = !(j.total > rows.length || decLimit > 60);
  more.textContent = j.total > rows.length ? "Показать ещё " + Math.min(50, j.total - rows.length) : "Свернуть до 60";
  more.onclick = function () { decLimit = j.total > rows.length ? Math.min(200, decLimit + 50) : 60; renderDecisions(); };
}
