"use strict";
// Дашборд (только чтение): каркас страницы, вкладки, опрос /api/state, индикатор состояния, настройки и тема.
// Части: core.js (ядро), charts.js, v-*.js (вкладки). Здесь только склейка.
var POLL_MS = 4000;
var timer = null;
var inflight = false;

// ---------- разделы и вкладки
var lastInGroup = {};
function buildNav() {
  var nav = $("nav");
  GROUPS.forEach(function (g) {
    var bt = el("button", { type: "button", class: "nav-b z-" + g[0], role: "tab", title: g[1], id: "g-" + g[0], "data-group": g[0] },
      navIcon(g[0]), el("span", { class: "nav-l", text: g[1] }), el("span", { class: "badge", hidden: true }));
    bt.addEventListener("click", function () { goTab(lastInGroup[g[0]] && groupTabs(g).indexOf(lastInGroup[g[0]]) >= 0 ? lastInGroup[g[0]] : groupTabs(g)[0]); });
    bt.addEventListener("keydown", function (e) {
      var i = GROUPS.indexOf(groupOf(ui.tab)), step = (e.key === "ArrowDown" || e.key === "ArrowRight") ? 1 : (e.key === "ArrowUp" || e.key === "ArrowLeft") ? -1 : 0;
      if (e.key === "Home") step = -99; if (e.key === "End") step = 99;
      if (!step) return;
      e.preventDefault();
      var j = step === -99 ? 0 : step === 99 ? GROUPS.length - 1 : (i + step + GROUPS.length) % GROUPS.length;
      goTab(groupTabs(GROUPS[j])[0]); $("g-" + GROUPS[j][0]).focus();
    });
    nav.appendChild(bt);
  });
}
var ICONS = {
  now: "M4 13h6V4H4v9zm10 7h6V4h-6v16zM4 20h6v-5H4v5z",
  trades: "M4 18l5-6 4 3 7-9M4 21h16",
  market: "M4 20V10m6 10V4m6 16v-7m4 7H2",
  system: "M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7zM12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10l2 2M19 5l-2 2M7 17l-2 2",
  brain: "M4 4h4v16H4zM10 4h4v10h-4zM16 4h4v6h-4z"
};
function navIcon(id) {
  return sv("svg", { viewBox: "0 0 24 24", width: 20, height: 20, "aria-hidden": "true", class: "nav-i" },
    sv("path", { d: ICONS[id] || "M4 12h16", fill: "none", stroke: "currentColor", "stroke-width": 1.9, "stroke-linecap": "round", "stroke-linejoin": "round" }));
}
function goTab(id) { ui.tab = id; saveUi(true); showTab(); if (S) renderActiveTab(); window.scrollTo(0, 0); }
function showTab() {
  var g = groupOf(ui.tab);
  lastInGroup[g[0]] = ui.tab;
  GROUPS.forEach(function (x) {
    var on = x === g, b = $("g-" + x[0]);
    b.setAttribute("aria-selected", on ? "true" : "false"); b.tabIndex = on ? 0 : -1; b.classList.toggle("on", on);
  });
  TABS.forEach(function (t) { var p = $("tab-" + t[0]); p.hidden = t[0] !== ui.tab; p.removeAttribute("aria-labelledby"); p.setAttribute("aria-label", t[1]); });
  $("page-title").textContent = g[1];
  var pt = $("page-tile"); pt.className = "page-tile z-" + g[0];
  pt.replaceChildren(navIcon(g[0]));
  var subs = groupTabs(g), host = $("subtabs");
  host.hidden = subs.length < 2;
  if (subs.length > 1) segment(host, subs.map(function (id) { return [id, tabTitle(id), subCount[id] || ""]; }), ui.tab, goTab, true);
  document.title = tabTitle(ui.tab) + " · open-trader";
}
function renderActiveTab() {
  if (EXT[ui.tab]) { EXT[ui.tab].render(); tickAll(); return; }
  ({ overview: renderOverview, plans: renderPlans, decisions: renderDecisions, trades: renderTrades, brief: renderBrief, ideas: renderIdeas, market: renderMarketTab, system: renderSystem })[ui.tab]();
  tickAll();
}

// ---------- счётчики разделов и вкладок (видны на любой странице)
var subCount = {}, subWarn = {};
// Отказы Gate за сутки копятся целый день и не гасятся: на значке показываем только новые с момента, когда вкладка «Решения» была открыта (счётчик суточный, при падении сбрасывается)
var REJ_KEY = "tb.rejects_seen";
function newRejects(total) {
  var seen = 0;
  try { seen = parseInt(localStorage.getItem(REJ_KEY), 10) || 0; } catch (e) { seen = 0; }
  if (ui.tab === "decisions" || total < seen) {
    seen = total;
    try { localStorage.setItem(REJ_KEY, String(seen)); } catch (e) { /* без хранилища счётчик просто растёт до открытия вкладки */ }
  }
  return Math.max(0, total - seen);
}
function renderBadges() {
  var v = V2(), c = {}, w = {};
  var extra = v ? v.positions.filter(function (p) {
    return (ui.mode === "all" || p.mode === ui.mode) && !S.open_trades.some(function (t) { return t.plan_id === p.plan || t.id === p.journal_id; });
  }).length : 0;
  c.overview = S.open_trades.length + (S.pending || []).length + extra;
  c.plans = v ? (v.plans.counts.waiting || 0) + (v.plans.counts.watching || 0) + (v.plans.counts.in_trade || 0) : 0;
  c.decisions = newRejects(v && v.decisions_day ? v.decisions_day.rejects : 0); w.decisions = c.decisions > 0;
  var I = S.ideas; c.ideas = I ? (I.counts.idea || 0) + (I.counts.watching || 0) : 0;
  c.system = S.warnings.filter(function (x) { return x.level === "bad" || x.level === "warn"; }).length; w.system = c.system > 0;
  subCount = c; subWarn = w;
  GROUPS.forEach(function (g) {
    var n = 0, wn = false;
    groupTabs(g).forEach(function (id) { n += c[id] || 0; wn = wn || !!w[id]; });
    var b = $("g-" + g[0]).querySelector(".badge");
    b.hidden = !n; b.textContent = n ? String(n) : ""; b.classList.toggle("warn", wn);
    b.setAttribute("aria-label", n ? n + (wn ? " замечаний" : " шт.") : "");
  });
  showTab();
}

// ---------- индикатор состояния
function linkLevel() { return FAIL > 0 ? "bad" : (S && elapsed() > 15 ? "warn" : "ok"); }
function overallLevel() {
  var lv = S && S.status ? S.status.level : "ok", order = { ok: 0, warn: 1, bad: 2 }, l = linkLevel(), v = V2();
  if (v && v.strip && v.strip.available && order[v.strip.level] > order[lv]) lv = v.strip.level;
  return order[l] > order[lv] ? l : lv;
}
function renderStatus() {
  var lv = overallLevel(), btn = $("status-btn");
  btn.className = "pill st-" + lv;
  $("status-text").textContent = !S ? (FAIL ? "Нет связи" : "Загрузка") : lv === "ok" ? "Всё в порядке" : lv === "warn" ? "Есть замечания" : "Нужно внимание";
  btn.setAttribute("aria-label", "Состояние системы: " + $("status-text").textContent + ". Нажмите для расшифровки");
  var items = ((S && S.status && S.status.items) || []).slice();
  var v2 = V2();
  if (v2 && v2.strip && v2.strip.available) v2.strip.items.forEach(function (i) { if (i.level !== "off") items.push({ id: "x-" + i.id, label: i.label, level: i.level, text: i.text, action: i.action }); });
  var ll = linkLevel();
  var rank = { bad: 0, warn: 1, ok: 2, off: 3 };
  items.sort(function (x, y) { return (rank[x.level] == null ? 2 : rank[x.level]) - (rank[y.level] == null ? 2 : rank[y.level]); });   // замечания (красные и жёлтые) сверху
  items.unshift({ id: "link", label: "Связь с дашбордом", level: ll, text: FAIL > 0 ? "нет ответа от ./tb dash (" + FAIL + " попыток подряд)" : ll === "warn" ? "данные не обновлялись " + dur(elapsed()) : "данные свежие" });
  var pop = $("status-pop");
  if (!pop.hidden || !pop._built) {
    pop._built = true;
    var sig = JSON.stringify(items.map(function (i) { return [i.id, i.level, i.text, i.action]; }));
    if (pop._sig !== sig) {
      pop._sig = sig;
      put(pop, [el("div", { class: "pop-h", text: "Состояние системы" }), el("ul", { class: "lst" }, items.map(function (i) {
        return el("li", null, stIcon(i.level), el("div", null, el("b", { text: i.label }), el("div", { class: "muted small", text: i.text }),
          i.action && (i.level === "warn" || i.level === "bad") ? el("div", { class: "small", text: "Как закрыть: " + i.action }) : null));
      })), el("a", { href: "#system", class: "pop-a", text: "Подробнее в разделе «Система»" })]);
      pop.querySelector("a").addEventListener("click", function (e) { e.preventDefault(); closePops(); goTab("system"); });
    }
  }
}
function renderUpdated() {
  var u = $("updated"), ut = $("updated-t");
  if (!S) { ut.textContent = "…"; return; }
  var age = Math.round(elapsed());
  ut.textContent = "обновлено " + (age < 2 ? "только что" : age + " с назад");
  u.className = "updated" + (age > 15 || FAIL ? " stale" : "");
  document.body.classList.toggle("is-stale", age > 15 || FAIL > 0);
}
window.onTick = function () { renderUpdated(); renderStatus(); };

// ---------- всплывающие панели
function closePops() {
  ["status", "set"].forEach(function (n) { $(n + "-pop").hidden = true; $(n + "-btn").setAttribute("aria-expanded", "false"); });
}
function togglePop(name) {
  var pop = $(name + "-pop"), open = pop.hidden;
  closePops();
  pop.hidden = !open; $(name + "-btn").setAttribute("aria-expanded", open ? "true" : "false");
  if (open && name === "status") { renderStatus(); }
}

// ---------- фильтры и тема
// подписи площадок (ключи API не меняем): "platform" это прежний кошелёк Walt (Telegram), больше не используется
function venueLabel(v) { return v === "all" ? "все" : v === "platform" ? "Walt (кошелёк, закрыт)" : v === "bybit" ? "Bybit" : v; }
function fillFilters() {
  var fm = $("f-mode"), fv = $("f-venue"), f = S.filter;
  var modes = f.modes.concat(["all"]), venues = ["all"].concat(f.venues);
  if (fm.options.length !== modes.length || fv.options.length !== venues.length) {
    fm.replaceChildren(); fv.replaceChildren();
    modes.forEach(function (m) { fm.appendChild(el("option", { value: m, text: m === "all" ? "все" : m })); });
    venues.forEach(function (v) { fv.appendChild(el("option", { value: v, text: venueLabel(v) })); });
  }
  fm.value = ui.mode; fv.value = ui.venue;
  // переключатель режима виден всегда (раньше только в окне «Настройки»): live по умолчанию, paper и все данные рядом
  segment($("mode-seg"), modes.map(function (m) { return [m, m === "all" ? "все" : m]; }), ui.mode, function (m) { ui.mode = m; saveUi(); load(); });
  var v2 = V2(), ex = v2 && v2.strip && v2.strip.exec, hint = $("mode-hint");
  var execMode = ex && ex.present ? (ex.mode || ex.profile) : null;
  hint.hidden = !(execMode && ui.mode !== "all" && ui.mode !== execMode && (execMode === "paper" || execMode === "live"));
  hint.textContent = hint.hidden ? "" : "исполнитель работает в " + execMode + ", показан режим " + ui.mode;
  var chipEl = $("filter-chip");
  chipEl.hidden = filterIsDefault();
  chipEl.textContent = ui.mode + " · " + (ui.venue === "all" ? "все площадки" : venueLabel(ui.venue));
}
function currentTheme() { try { var t = localStorage.getItem("tbdash.theme"); return t === "light" || t === "auto" ? t : "dark"; } catch (e) { return "dark"; } }
function setTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem("tbdash.theme", t); } catch (e) { /* приватный режим */ }
  segment($("theme-seg"), [["dark", "Тёмная"], ["light", "Светлая"], ["auto", "Как в системе"]], t, setTheme);
  if (S) { ["chart", "chart2", "bars"].forEach(function (id) { $(id)._drawn = null; }); renderActiveTab(); }
}

// ---------- запросы
function load() {
  if (inflight) return Promise.resolve();
  inflight = true;
  var ctl = typeof AbortController === "function" ? new AbortController() : null;
  var to = setTimeout(function () { if (ctl) ctl.abort(); }, 6000);
  var url = "/api/state?mode=" + encodeURIComponent(ui.mode) + "&venue=" + encodeURIComponent(ui.venue);
  return fetch(url, { cache: "no-store", signal: ctl ? ctl.signal : undefined })
    .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(function (j) { clearTimeout(to); inflight = false; S = j; fetchedAt = performance.now(); FAIL = 0; render(); })
    .catch(function (e) { clearTimeout(to); inflight = false; FAIL++; onFail(String((e && e.message) || e)); });
}
function onFail(msg) {
  var b = $("banner");
  b.hidden = false;
  b.textContent = S
    ? "Нет связи с дашбордом (" + msg + "). Показаны последние данные: обновлено " + hms(new Date(Date.now() - elapsed() * 1000)) + ". Страница повторит попытку сама."
    : "Дашборд не отвечает (" + msg + "). Проверьте, что запущен ./tb dash; страница повторит попытку сама.";
  renderUpdated(); renderStatus();
}
function schedule() { clearTimeout(timer); if (!document.hidden) timer = setTimeout(function () { load().then(schedule); }, POLL_MS); }
function render() {
  $("banner").hidden = true;
  fillFilters(); renderBadges(); renderStrip(); renderActiveTab(); renderUpdated(); renderStatus();
}

// ---------- запуск
function buildExtSections() {
  Object.keys(EXT).forEach(function (id) {
    if ($("tab-" + id)) return;
    var t = TABS.filter(function (x) { return x[0] === id; })[0];
    $("main").appendChild(el("section", { id: "tab-" + id, class: "panel-tab", role: "tabpanel", "aria-labelledby": "t-" + id, hidden: true }, el("h2", { text: t ? t[1] : id })));
  });
}
function init() {
  buildExtSections();
  loadUi();
  buildNav(); showTab();
  segment($("theme-seg"), [["dark", "Тёмная"], ["light", "Светлая"], ["auto", "Как в системе"]], currentTheme(), setTheme);
  $("f-mode").addEventListener("change", function (e) { ui.mode = e.target.value; saveUi(); load(); });
  $("f-venue").addEventListener("change", function (e) { ui.venue = e.target.value; saveUi(); load(); });
  $("status-btn").addEventListener("click", function (e) { e.stopPropagation(); togglePop("status"); });
  $("set-btn").addEventListener("click", function (e) { e.stopPropagation(); togglePop("set"); });
  document.addEventListener("click", function (e) { if (!e.target.closest || !e.target.closest(".pop")) closePops(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closePops(); });
  $("bd-fold").addEventListener("toggle", function (e) { ui.bdo = e.target.open; });
  $("to-trades").addEventListener("click", function (e) { e.preventDefault(); goTab("trades"); });
  $("to-plans").addEventListener("click", function (e) { e.preventDefault(); goTab("plans"); });
  $("to-system").addEventListener("click", function (e) { e.preventDefault(); goTab("system"); });
  $("exp-fold").addEventListener("toggle", function (e) { expOpen = e.target.open; });
  window.addEventListener("hashchange", function () {
    var h = parseHash(location.hash);
    if (!h) return;
    var m = ui.mode, v = ui.venue;
    for (var k in UI_DEF) if (k !== "tab" && !(k in h)) h[k] = UI_DEF[k];
    applyUi(h); showTab(); saveUi();
    if (ui.mode !== m || ui.venue !== v) load(); else if (S) renderActiveTab();
  });
  document.addEventListener("visibilitychange", function () { if (document.hidden) clearTimeout(timer); else load().then(schedule); });
  saveUi();
  load().then(schedule);
}
init();
