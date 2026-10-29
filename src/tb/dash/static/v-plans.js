"use strict";
// Вкладка «Планы»: доска по статусам (наблюдаю / ждёт входа / в сделке / закрыт), карточка плана, раскрытие с полным JSON,
// ревизиями, хронологией решений. Данные: S.v2.plans (доска), по раскрытию /api/v2/plan?id= (spec, runtime, ревизии, решения).

var COLS = [["waiting", "Ждёт входа", "Ждут входа: условие частично выполнено, цена в зоне уровня (≤ 0.3%) или лимит уже стоит."],
  ["watching", "Далеко от входа", "План уже создан и взведён (исполнитель сам войдёт по условию), но до триггера далеко или ордера ещё нет. Это не идея: идеи ждут уровня на вкладке «Идеи»."],
  ["in_trade", "В сделке", "Вход исполнен, идёт сопровождение правилами. Подробности в блоке «Позиции» на Обзоре."],
  ["closed", "Закрыт", "Завершены за последние 24 часа: закрыт, отменён, истёк, отозван, отклонён."]];
var STATUS_PLAN_RU = { draft: "черновик", armed: "взведён", entered: "вход исполнен", managing: "сопровождается", closed: "закрыт", cancelled: "отменён", expired: "истёк", invalidated: "отозван", rejected: "отклонён" };
var PD = {};   // кэш деталей плана: id -> {data, at}
var WARN_RU = { W_NO_INVALIDATION: "нет условий отмены тезиса", W_LADDER_COLLAPSE: "лесенка тейков вырождена", W_TTL_CLAMPED: "срок обрезан по сессии", W_ADD_DISABLED: "добор выключен",
  W_NO_RUNNER_RULE: "остаток без трейла и срока", W_PRICE_STALE_AT_ACCEPT: "цена была устаревшей при приёме", W_FIELD_IGNORED: "лишнее поле проигнорировано" };

function sideChip(side) { return el("span", { class: "chip side " + (side === "long" ? "s-long" : side === "short" ? "s-short" : ""), text: (side || "?").toUpperCase() }); }
function planTags(p) {
  var kids = [];
  if (p.status === "draft") kids.push(chip("черновик", "warn", "принят CLI, исполнитель ещё не принял"));
  if (p.mode === "paper") kids.push(chip("paper", "paper", "бумажный план: на бирже ничего не ставит, считается как эксперимент"));
  if (p.exp) kids.push(chip(p.exp + (p.var ? "/" + p.var : ""), "exp", "эксперимент / вариант"));
  if (p.setup) kids.push(chip(p.setup, "", "сетап"));
  if (p.src === "shadow") kids.push(chip("тень" + (p.parent ? " от " + planShort(p.parent) : ""), "", "теневая копия живого плана"));
  return kids;
}
function proxBar(dist) {
  if (!isn(dist)) return null;
  var f = Math.max(0, Math.min(1, 1 - dist / 3));        // полная шкала = «дальше 3%»
  return el("div", { class: "prox", role: "img", "aria-label": "близость к триггеру: " + Math.round(f * 100) + "%", title: "чем полнее полоса, тем ближе цена к уровню (шкала до 3%)" },
    el("i", { style: "width:" + (f * 100).toFixed(0) + "%", class: dist <= 0.3 ? "near" : "" }));
}
function triggerBlock(p) {
  var t = p.trigger;
  if (!t || !(p.status === "armed" || p.status === "draft")) return null;
  var parts = [];
  if (t.seq) parts.push(el("span", { class: "chip", text: "шаг " + Math.min(t.seq.done + 1, t.seq.total) + " из " + t.seq.total }));
  if (p.entry_state === "resting") parts.push(chip("лимит стоит на бирже", "ok"));
  if (p.entry_state === "blocked") parts.push(chip("вход заблокирован" + (p.blocked_why ? ": " + p.blocked_why : ""), "bad"));
  var line = el("div", { class: "pl-trig" }, el("span", { class: "muted", text: "Ближайший триггер: " }), el("span", { text: t.text || "–" }));
  var meta = [];
  if (isn(t.dist_pct)) meta.push(el("span", { text: "до уровня " + pc(t.dist_pct, 2) + (isn(t.level) ? " (" + price(t.level) + ")" : "") }));
  if (isn(t.price)) meta.push(el("span", { class: "muted", text: "цена " + price(t.price) + (p.price_stale ? " (устарела)" : "") }));
  if (isn(t.eta_s)) meta.push(el("span", null, "свеча " + t.eta_tf + " закроется через ", tk("eta:" + p.id, t.eta_s, "left")));
  if (t.missing && t.missing.length > 1) meta.push(el("span", { class: "muted", title: t.missing.join("; "), text: "не хватает: " + t.missing.length }));
  return el("div", { class: "pl-trigbox" }, line, el("div", { class: "pl-meta small" }, meta), proxBar(t.dist_pct), parts.length ? el("div", { class: "chips tight" }, parts) : null);
}
function planFoot(p) {
  var bits = [];
  if (isn(p.ttl_left_s) && (p.status === "armed" || p.status === "draft")) bits.push(el("span", { title: "срок жизни плана до входа" }, "TTL ", tk("ttl:" + p.id, p.ttl_left_s, "left")));
  if (isn(p.risk_usdt)) bits.push(el("span", { text: "риск " + fx(p.risk_usdt, 2) + " USDT" }));
  if (isn(p.net_rr)) bits.push(el("span", { title: "отношение прибыли к риску после издержек" }, tip("net RR " + fx(p.net_rr, 1), "rr", true)));
  if (isn(p.leverage)) bits.push(el("span", { text: p.leverage + "×" }));
  if (p.hold) bits.push(chip("hold" + (p.hold_reason ? ": " + p.hold_reason : ""), "warn", "запрещены вход и добор, защитные действия разрешены"));
  (p.warnings || []).forEach(function (w) { bits.push(chip(w, "warn", WARN_RU[w] || w)); });
  return bits.length ? el("div", { class: "pl-foot small" }, bits) : null;
}
function closedLine(p) {
  var bits = [chip(STATUS_PLAN_RU[p.status] || p.status, p.status === "closed" && isn(p.close_pnl) ? "" : "")];
  if (p.close_reason) bits.push(el("span", { class: "small", text: p.close_reason }));
  if (p.status === "closed" && isn(p.close_pnl)) bits.push(el("b", { class: "small " + cls(p.close_pnl), text: sg(p.close_pnl, 3) + " USDT" }));
  if (isn(p.ts_closed)) bits.push(el("span", { class: "muted small", text: dt(p.ts_closed) }));
  return el("div", { class: "pl-closed" }, bits);
}
function inTradeLine(p) {
  var v = V2(), pos = v && v.positions.filter(function (x) { return x.plan === p.id; })[0];
  if (!pos) return el("div", { class: "pl-trig muted small", text: "позиция сопровождается правилами" });
  return el("div", { class: "pl-closed" }, el("b", { class: cls(pos.r_now), text: rr(pos.r_now) }), el("span", { class: "small muted", text: "стоп " + price(pos.cur_stop) + " (" + pos.stop_src + ")" }),
    pos.next_stop ? el("span", { class: "small", text: "→ " + price(pos.next_stop.to) }) : null, tabLink("правила и лесенка", "overview", {}, "small"));
}
function kvList(obj) {
  return Object.keys(obj || {}).map(function (k) { return k + "=" + (obj[k] === null ? "–" : obj[k]); }).join(" · ");
}
function planDetailBody(d) {
  var p = d.card, nodes = [];
  nodes.push(kvgrid([
    ["Статус", (STATUS_PLAN_RU[p.status] || p.status) + (p.hold ? " · hold" : "") + " · ревизия " + p.rev + (isn(p.rev_applied) ? " (применена " + p.rev_applied + ")" : "")],
    ["Тип сессии · создал", (p.session_type || "–") + " · " + (p.created_by || "–")],
    ["Источник", (p.src || "–") + (p.parent ? " от " + p.parent : "")],
    ["Создан · взведён", dt(p.ts_created) + " · " + dt(p.ts_armed)],
    p.ts_entered ? ["Вход", dt(p.ts_entered)] : null,
    ["Предрегистрация", p.prereg || "–"],
    ["Стоп · тейки", price(p.stop) + " · " + (p.targets || []).map(function (t) { return t.id + " " + price(t.price) + (t.state ? " (" + t.state + ")" : ""); }).join(", ")],
    ["Эксперимент", [p.exp, p.var, p.setup, p.hyp].filter(Boolean).join(" / ") || "–"]
  ]));
  var brief = null;
  (d.decisions || []).some(function (x) { if (x.brief_id) { brief = x.brief_id; return true; } return false; });
  var links = [tabLink("все решения плана", "decisions", { dp: p.id, tab: "decisions" }, "pl-a")];
  if (brief) links.push(tabLink("брифинг, на который опирался мозг", "brief", { b: brief }, "pl-a"));
  nodes.push(el("div", { class: "pl-links" }, links));
  if ((d.revs || []).length) {
    nodes.push(el("h3", { text: "Ревизии" }));
    nodes.push(el("ul", { class: "lst revs" }, d.revs.slice().reverse().map(function (r) {
      return el("li", null, el("span", { class: "t mono", text: "r" + r.rev }), el("div", null, el("div", { class: "small" }, dts(r.ts) + " " + (r.by || ""), r.why ? " · " + r.why : ""),
        (r.changes || []).map(function (c) { return el("div", { class: "mono small diff", text: c }); })));
    })));
  }
  nodes.push(el("h3", { text: "Хронология решений" }));
  nodes.push((d.decisions || []).length ? el("ul", { class: "lst" }, d.decisions.slice(0, 15).map(function (x) {
    return el("li", { class: "ev" }, el("span", { class: "t", text: dts(x.t) }), el("span", null, actorChip(x.actor), " ", el("b", { text: x.action }), x.why ? el("span", { class: "muted", text: " · " + x.why }) : null, " ", resultChip(x)));
  })) : el("div", { class: "muted small", text: "Решений по этому плану пока нет." }));
  var js = el("details", { class: "fold-in" }, el("summary", { text: "Полный JSON плана (spec) и состояние исполнителя (runtime)" }));
  js.addEventListener("toggle", function () {
    if (js.open && !js._built) { js._built = true; js.appendChild(el("pre", { class: "json", text: JSON.stringify(d.spec, null, 2) })); js.appendChild(el("h3", { text: "runtime.json" })); js.appendChild(el("pre", { class: "json", text: JSON.stringify(d.runtime, null, 2) })); }
  });
  nodes.push(js);
  return nodes;
}
function loadPlanDetail(id, host) {
  var c = PD[id];
  if (!host.firstChild) put(host, c ? planDetailBody(c.data) : el("div", { class: "muted small", text: "Загрузка…" }));   // пересборка только пустого узла: открытые вложенные блоки не схлопываются
  if (c && performance.now() - c.at < 4000) return;
  fetch("/api/v2/plan?id=" + encodeURIComponent(id), { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(function (j) {
      var changedData = !c || JSON.stringify(c.data) !== JSON.stringify(j);
      PD[id] = { data: j, at: performance.now() };
      if (changedData && host.isConnected) { var openJs = host.querySelector("details.fold-in"); var was = openJs && openJs.open; put(host, planDetailBody(j)); if (was) { var n = host.querySelector("details.fold-in"); n.open = true; n.dispatchEvent(new Event("toggle")); } }
    }).catch(function (e) { if (!c) put(host, el("div", { class: "muted small", text: "Не удалось загрузить: " + ((e && e.message) || e) })); });
}
function planCard(p) {
  var key = "pl:" + p.id;
  var head = el("div", { class: "pl-top" }, el("span", { class: "sym", text: p.sym || "?" }), sideChip(p.side), el("span", { class: "muted small mono", text: planShort(p.id) }), el("span", { class: "pl-rev muted small", text: "r" + p.rev }));
  var tags = planTags(p);
  var body = [head, tags.length ? el("div", { class: "chips tight" }, tags) : null,
    p.thesis ? el("div", { class: "pl-thesis clamp", title: p.thesis, text: p.thesis }) : null,
    el("div", { class: "pl-entry small" }, el("span", { class: "muted", text: "Вход: " }), el("span", { text: p.entry_text })),
    p.col === "closed" ? closedLine(p) : p.col === "in_trade" ? inTradeLine(p) : triggerBlock(p), planFoot(p)];
  var btn = el("button", { type: "button", class: "pc-toggle", "aria-expanded": "false" }, el("span", { class: "cv", "aria-hidden": "true" }), "Подробнее");
  var det = el("div", { class: "pl-det", hidden: true });
  var card = el("article", { class: "plan col-" + p.col + " st-" + p.status + (p.mode === "paper" ? " is-paper" : ""), "aria-label": "План " + p.id }, body, btn, det);
  function setOpen(on) {
    det.hidden = !on;
    btn.setAttribute("aria-expanded", on ? "true" : "false"); btn.lastChild.textContent = on ? "Свернуть" : "Подробнее";
    if (on) { ui.open[key] = true; loadPlanDetail(p.id, det); } else delete ui.open[key];
  }
  btn.addEventListener("click", function () { setOpen(btn.getAttribute("aria-expanded") !== "true"); });
  card._reopen = function () { if (ui.open[key]) setOpen(true); };
  card._reopen();
  return card;
}
function planSig(p) {
  return sigOf(p, ["ttl_left_s", "price_age_s", "eta_s"]) + "|" + (p.col === "in_trade" ? sigOf((V2().positions.filter(function (x) { return x.plan === p.id; })[0] || {}), ["held_s", "hold_left_s", "snap_age_s", "history"]) : "");
}
function renderPlans() {
  var v = V2(), host = $("pl-board");
  if (!v) return;
  var P = v.plans, cards = P.cards.filter(function (p) { return ui.pm === "all" || p.mode === ui.pm; });
  var nLive = P.cards.filter(function (p) { return p.mode === "live"; }).length, nPaper = P.cards.length - nLive;
  segment($("pl-mode"), [["all", "Все", P.cards.length], ["live", "live", nLive], ["paper", "paper", nPaper]], ui.pm, function (m) { ui.pm = m; saveUi(); renderPlans(); });
  put("pl-sum", P.total ? "· всего " + P.total + (P.broken ? " · битых папок " + P.broken : "") : "");
  $("pl-empty").hidden = !!P.cards.length;
  host.hidden = !P.cards.length;
  if (!P.cards.length) {
    emptyState($("pl-empty"), P.total ? "Нет планов за последние сутки" : "Планов пока нет",
      v.available ? "Мозг создаёт планы командой ./tb plan submit; исполнитель ведёт их по условиям." : "Слой планов (v2) ещё не используется: нет state/plans, исполнитель не запущен. Прежний способ торговли работает как раньше.");
    return;
  }
  COLS.forEach(function (c) {
    var col = host.querySelector('[data-col="' + c[0] + '"]');
    var rows = cards.filter(function (p) { return p.col === c[0]; });
    if (!col) {
      col = el("section", { class: "bcol", "data-col": c[0], "aria-label": c[1] },
        el("h3", { class: "bcol-h" }, tip(c[1], c[2], true), el("span", { class: "bcol-n" })), el("div", { class: "bcol-b" }));
      host.appendChild(col);
    }
    col.querySelector(".bcol-n").textContent = String(rows.length);
    var body = col.querySelector(".bcol-b");
    body.querySelectorAll(".bcol-empty").forEach(function (n) { n.remove(); });
    if (!rows.length) body.appendChild(el("div", { class: "bcol-empty muted small", text: "пусто" }));
    syncList(body, rows, { key: function (p) { return p.id; }, sig: planSig, build: planCard,
      after: function (p) { if (p.trigger) { if (isn(p.trigger.eta_s)) tkReg("eta:" + p.id, p.trigger.eta_s, "left"); } if (isn(p.ttl_left_s)) tkReg("ttl:" + p.id, p.ttl_left_s, "left"); } });
    // раскрытая карточка обновляет деталь в фоне, пока видна доска
    body.querySelectorAll(".pl-det:not([hidden])").forEach(function (d) { var id = d.parentNode.getAttribute("data-key"); if (id) loadPlanDetail(id, d); });
  });
}
