"use strict";
// Вкладка «Система»: состояние узлов, предупреждения, сессия и лимиты, источники данных, события.

var LEVEL_RU = { ok: "норма", warn: "внимание", bad: "проблема", off: "не активно", info: "сведения" };
var GLYPH = { ok: "M5 12.5l4.2 4.2L19 7", warn: "M12 6.5v7M12 17.4v.1", bad: "M7 7l10 10M17 7L7 17", off: "M7 12h10", info: "M12 11v6M12 7.4v.1" };
function glyphSvg(level) {
  return sv("svg", { viewBox: "0 0 24 24", width: 12, height: 12, "aria-hidden": "true" }, sv("path", { d: GLYPH[level] || GLYPH.info, fill: "none", stroke: "currentColor", "stroke-width": 3, "stroke-linecap": "round", "stroke-linejoin": "round" }));
}
// Значок состояния: цвет + знак + скрытая подпись, чтобы смысл не держался только на цвете
function stIcon(level) {
  return el("span", { class: "st st-" + level, title: LEVEL_RU[level] || level }, glyphSvg(level), el("span", { class: "sr", text: LEVEL_RU[level] || level }));
}
var evLimit = 10;

function renderNodes() {
  var items = (S.status && S.status.items) || [];
  var ul = $("nodes");
  if (!changed(ul, items)) return;
  put(ul, items.map(function (i) { return el("li", null, stIcon(i.level), el("div", null, el("b", { text: i.label }), el("div", { class: "muted small", text: i.text }))); }));
  var w = S.warnings;
  put("warns", w.length ? w.map(function (x) {
    var lv = x.level === "bad" ? "bad" : x.level === "warn" ? "warn" : "info";
    return el("li", null, stIcon(lv), el("span", { text: x.text }));
  }) : el("li", null, stIcon("ok"), el("span", { text: "Предупреждений нет" })));
}

function bar(pct, warn, label) {
  return el("div", { class: "bar" + (warn ? " warn" : ""), role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(pct)), "aria-label": label },
    el("i", { style: "width:" + Math.max(0, Math.min(100, pct)).toFixed(1) + "%" }));
}
function renderSession() {
  var s = S.session, a = S.account, L = S.learning, host = $("sess");
  if (!changed(host, sigOf([s, a, L && L.active], ["left_s", "snapshot_age_s"]))) { if (s && isn(s.left_s)) tkReg("sl", s.left_s, "left"); return; }
  var nodes = [];
  if (!s) {
    var last = S.sessions[0];
    nodes.push(el("div", { class: "empty" }, el("b", { text: "Активной сессии нет" }), el("span", { text: (last ? "Последняя: " + last.id + (last.stopped ? " (остановлена)" : "") + ". " : "Сессий ещё не было. ") + "Входы разрешены только внутри сессии (./tb session)." })));
  } else {
    var tpct = isn(s.duration_s) && s.duration_s > 0 && isn(s.left_s) ? (1 - s.left_s / s.duration_s) * 100 : 0;
    var bpct = s.budget_usdt ? Math.min(100, s.budget_used / s.budget_usdt * 100) : 0;
    nodes.push(el("div", { class: "sess-hd" }, el("b", { text: "Сессия " + (s.type || "") }), chip(s.entries_allowed ? "входы разрешены" : "входы запрещены", s.entries_allowed ? "ok" : "warn"),
      el("span", { class: "muted small", text: s.id })));
    if (!s.entries_allowed) nodes.push(el("div", { class: "muted small", text: "Причина: " + s.block_reasons.join(", ") }));
    if (isn(s.left_s)) {          // у сессий без срока строки времени нет
      nodes.push(el("div", { class: "lab-row" }, el("span", { text: "Осталось времени" }), tk("sl", s.left_s, "left")));
      nodes.push(bar(tpct, false, "прошло времени сессии"));
    }
    nodes.push(el("div", { class: "lab-row" }, el("span", { text: "Бюджет убытка" }), el("span", { text: fx(s.budget_used, 2) + " из " + fx(s.budget_usdt, 2) + " USDT" })));
    nodes.push(bar(bpct, bpct > 70, "использовано бюджета убытка"));
    if (s.thesis) nodes.push(el("div", { class: "thesis", text: s.thesis }));
  }
  var more = el("details", { class: "xc-det" }, el("summary", { text: "Лимиты и риск" }));
  more.open = !!renderSession.open; more.addEventListener("toggle", function () { renderSession.open = more.open; });
  more.appendChild(kvgrid([
    ["Сделок в сессии", s ? s.trades + (s.max_trades ? " из " + s.max_trades : "") : "–"],
    ["Риск открытых", s ? fx(s.open_risk, 2) + " USDT" : "–"],
    ["Результат сессии", s ? usd(s.realized, 2) : "–", s ? cls(s.realized) : ""],
    [tip("Множитель риска", "ladder"), isn(a.ladder_mult) ? "×" + fx(a.ladder_mult) : "–"],
    s && isn(s.perf_mult) ? ["Множитель по результатам", "×" + fx(s.perf_mult) + " (" + s.perf_n + " сд.)"] : null,
    [tip("Просадка", "dd"), pc(a.drawdown * 100, 1) + " от пика " + fx(a.hwm)],
    ["Капитал по площадкам", Object.keys(a.venues || {}).map(function (n) { return venueLabel(n) + " " + fx(a.venues[n].equity) + " (своб. " + fx(a.venues[n].free) + ")"; }).join(" · ") || "нет снимка"],
    ["Режим обучения", L ? (L.active ? "включён" : "выключен") : "нет данных"]
  ]));
  nodes.push(more);
  put(host, nodes);
}

function renderSources() {
  var hl = S.health, host = $("sources");
  if (!changed(host, sigOf(hl, ["age_s", "last_msg_age_s", "uptime_s"]))) { if (hl.feed) tkReg("fd", hl.feed.age_s, "ago"); if (hl.context) tkReg("cx", hl.context.age_s, "ago"); return; }
  var nodes = [];
  if (!hl.feed && !hl.context) { put(host, el("div", { class: "empty" }, el("b", { text: "Нет данных о здоровье" }), el("span", { text: "Файлы runtime/health.json и context.json не найдены: фид не запущен." }))); return; }
  var rows = [];
  if (hl.feed) {
    rows.push(el("li", null, stIcon(hl.feed.running === false ? "bad" : hl.feed.age_s > 30 ? "warn" : "ok"), el("div", null, el("b", { text: "Фид цен" }),
      el("div", { class: "muted small" }, "последняя запись ", tk("fd", hl.feed.age_s, "ago")))));
    Object.keys(hl.feed.sources || {}).forEach(function (n) {
      var s = hl.feed.sources[n];
      rows.push(el("li", { class: "sub-li" }, stIcon(s.state === "ok" ? "ok" : "warn"), el("div", null, el("b", { text: n }),
        el("div", { class: "muted small", text: (s.state || "?") + " · переподключений " + (s.reconnects == null ? "–" : s.reconnects) + (isn(s.last_msg_age_s) ? " · сообщение " + dur(s.last_msg_age_s) + " назад" : "") + (s.last_error ? " · ошибка: " + s.last_error : "") }))));
    });
  }
  if (hl.context) {
    rows.push(el("li", null, stIcon(hl.context.risk_off ? "bad" : hl.context.age_s > 600 ? "warn" : "ok"), el("div", null, el("b", { text: "Контекст рынка" }),
      el("div", { class: "muted small" }, "обновлён ", tk("cx", hl.context.age_s, "ago"), hl.context.risk_off ? " · risk_off: " + (hl.context.risk_off_reasons || []).join(", ") : " · risk_off нет"))));
    var so = hl.context.sources_ok || {};
    if (Object.keys(so).length) rows.push(el("li", { class: "sub-li" }, el("div", { class: "chips" }, Object.keys(so).map(function (n) { return chip((so[n] ? "✓ " : "! ") + n, so[n] ? "" : "warn"); }))));
  }
  nodes.push(el("ul", { class: "lst" }, rows));
  put(host, nodes);
}

function renderEvents() {
  var ev = S.events, ul = $("events"), more = $("ev-more");
  put("ev-sub", ev.length ? "· " + ev.length : "");
  if (!ev.length) { put(ul, el("li", { class: "muted", text: "Событий пока нет: наблюдатель ничего не фиксировал." })); more.hidden = true; return; }
  var shown = ev.slice(0, evLimit);
  if (changed(ul, [shown, evLimit])) put(ul, shown.map(function (e) {
    var d = e.data && typeof e.data === "object" ? Object.keys(e.data).map(function (k) { return k + "=" + (isn(e.data[k]) ? +e.data[k].toFixed(4) : e.data[k]); }).join(" ") : "";
    return el("li", { class: "ev" }, el("span", { class: "t", text: (e.ts || "").slice(11, 19) }), el("span", { text: [e.type, e.symbol, d].filter(Boolean).join(" · ") }));
  }));
  more.hidden = ev.length <= 10;
  more.textContent = evLimit < ev.length ? "Показать ещё" : "Свернуть до 10";
  more.onclick = function () { evLimit = evLimit < ev.length ? ev.length : 10; renderEvents(); };
}

function renderSessions() {
  var l = S.sessions, host = $("sessions");
  if (!l.length) { put(host, el("div", { class: "empty" }, el("b", { text: "Сессий ещё не было" }))); return; }
  if (!changed(host, l)) return;
  put(host, el("ul", { class: "lst" }, l.map(function (s) {
    return el("li", null, chip(s.type || "?"), el("div", null, el("b", { text: s.id }), el("span", { class: "muted small", text: " · " + (s.venue || "") + (s.stopped ? " · остановлена " + s.stopped : " · идёт") }),
      s.thesis ? el("div", { class: "muted small clamp", text: s.thesis }) : null));
  })));
}
function renderRoomSys() {
  var R = S.risk_room, host = $("room-sys");
  if (!R) { put(host, el("div", { class: "empty" }, el("b", { text: "Нет данных о лимитах" }), el("span", { text: "Не найден config/risk_profile.json." }))); return; }
  if (changed(host, R)) put(host, roomView(R, true));
}

// ---------- верхняя сводка: общий вывод и плитки узлов
function renderSysHero() {
  var host = $("sys-hero"), v = V2(), items = [], seen = {};
  ((S.status && S.status.items) || []).concat(v && v.strip && v.strip.available ? v.strip.items : []).forEach(function (i) {
    if (seen[i.label]) return; seen[i.label] = 1; items.push(i);
  });
  items = items.filter(function (i) { return i.level !== "off" || i.label === "Исполнитель"; });
  var warns = S.warnings.filter(function (x) { return x.level === "bad" || x.level === "warn"; });
  var lv = overallLevel(), bad = items.filter(function (i) { return i.level === "bad" || i.level === "warn"; });
  var sig = JSON.stringify([lv, items, warns, FAIL]);
  var stamp = $("sys-stamp"); if (stamp) stamp.textContent = "";
  if (host._s === sig) return;
  host._s = sig;
  var title = lv === "ok" ? "Всё работает" : lv === "warn" ? "Есть замечания" : "Нужно внимание";
  var why = bad.length ? bad.map(function (i) { return i.label + ": " + i.text; }).join(" · ") : (warns.length ? warns[0].text : "Все узлы отвечают, данные свежие.");
  var tiles = el("div", { class: "sys-tiles" }, items.map(function (i) {
    return el("div", { class: "sys-tile lv-" + i.level, title: i.label + ": " + i.text }, el("div", { class: "sys-tl" }, el("i", { class: "st-dot lv-" + i.level, "aria-hidden": "true" }), i.label),
      el("div", { class: "sys-tv clamp", text: i.text }));
  }));
  put(host, [el("div", { class: "sys-top lv-" + lv }, el("i", { class: "st-dot lv-" + lv, "aria-hidden": "true" }), el("div", null, el("div", { class: "sys-h", text: title }), el("div", { class: "muted small", text: why }))), tiles]);
}
function renderSystem() { renderSysHero(); renderExecSys(); renderRoomSys(); renderNodes(); renderSession(); renderSources(); renderEvents(); renderSessions(); }

// ---------- исполнитель v2: здоровье, соединения, очередь событий, сверка, отказы Gate (S.v2; источники: exec_health.json, шина, decisions.jsonl)
function renderExecSys() {
  var v = V2(), host = $("exec-sys");
  if (!v) return;
  var st = v.strip, ex = st.exec, bl = st.backlog, day = v.decisions_day;
  var sig = sigOf([st, day, v.foreign_positions], ["age_s", "uptime_s", "reconcile_age_s", "last_age_s", "oldest_age_s", "loop_lag_ms"]);
  if (host._s === sig) return;
  host._s = sig;
  if (!ex.present && !st.kill && bl.last_seq == null && !v.plans.total) {
    put(host, el("div", { class: "empty" }, el("b", { text: "Исполнитель v2 не запущен" }), el("span", { text: "Нет runtime/exec_health.json, state/plans и шины событий. Торговля идёт прежним путём; панели планов и решений появятся, когда запустится исполнитель." })));
    return;
  }
  var nodes = [];
  var pairs = [];
  if (ex.present) {
    pairs.push(["Процесс", (ex.pid ? "pid " + ex.pid + " · " : "") + "версия " + (ex.version || "?") + " · uptime " + dur(ex.uptime_s)]);
    pairs.push(["Задержка цикла", isn(ex.loop_lag_ms) ? ex.loop_lag_ms + " мс" : "–"]);
    pairs.push(["Планы в исполнителе", Object.keys(ex.plans || {}).map(function (k) { return k + " " + ex.plans[k]; }).join(" · ") || "–"]);
    pairs.push(["Позиций на бирже", String(ex.positions)]);
  }
  pairs.push(["Очередь событий", bl.last_seq == null ? "событий нет" : "последний seq " + bl.last_seq + (Object.keys(bl.consumers || {}).map(function (c) {
    var x = bl.consumers[c]; return " · " + c + ": подтверждено " + x.acked + ", не подтверждено wake " + x.unacked;
  }).join(""))]);
  pairs.push(["Отказы Gate за сутки", day && day.rejects ? day.reject_codes.map(function (c) { return c.code + " ×" + c.n; }).join(", ") : "нет"]);
  pairs.push(["Расход токенов и USD мозга", el("span", { id: "sys-usage", text: "загрузка…" })]);
  if ((v.foreign_positions || []).length) pairs.push(["Позиции вне планов", v.foreign_positions.map(function (p) { return p.sym + " " + (p.side || "") + " (" + (p.owner || "?") + ")"; }).join(", ")]);
  nodes.push(kvgrid(pairs));
  put(host, nodes);
  fetchTab("sysusage", "/api/settings/overview", applySysUsage, true);
}
// расход мозга за сутки из /api/settings/overview (usage_today); пока API-режим не работал, calls = 0
function applySysUsage(j, fresh, err) {
  var n = $("sys-usage");
  if (!n) return;
  var u = j && j.usage_today;
  n.textContent = err && !j ? "нет данных: " + err : !u ? "нет данных (раздел «Настройки» недоступен)" : !u.calls ? "сегодня вызовов модели не было" :
    u.calls + " вызовов · " + (isn(u.toks) ? u.toks + " токенов · " : "") + fx(u.usd, 4) + " USD за сутки";
}
