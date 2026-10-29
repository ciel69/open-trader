"use strict";
// Страница «Управление»: состояние платформы и четыре понятных действия. Все параметры и подтверждения выбираются в окнах (x-dialog.js),
// движок сценария (challenge -> подтверждение -> команда) живёт в v-control.js. Тексты данных вставляются через textContent.

var XC_MODE = { observe: "Наблюдение", paper: "Бумага", testnet: "Тестовая сеть", live: "Реальные деньги" };
var XC_MODE_HINT = { observe: "ничего не торгуется", paper: "виртуальные сделки", testnet: "тестовая биржа", live: "реальные сделки на счёте" };
var XC_STATE = { stopped: ["off", "Остановлена"], starting: ["warn", "Запускается"], running: ["ok", "Работает"], closing: ["warn", "Закрывается"], halted: ["bad", "Аварийно остановлена"], unknown: ["off", "Нет данных"] };
var xcSig = "";

function xcRow(main, side) { return el("div", { class: "st-row" }, el("div", { class: "st-main" }, main), side ? el("div", { class: "st-side" }, side) : null); }
function xcTitle(t, sub) { return [el("div", { class: "st-t" }, t), sub ? el("div", { class: "st-s", text: sub }) : null]; }
function xcDot(l) { return el("i", { class: "st-dot lv-" + l, "aria-hidden": "true" }); }
function xcBlock(title, sub, kids) { return el("section", { class: "block st-block" }, el("div", { class: "st-bh" }, el("h2", null, title, sub ? el("span", { class: "sub", text: sub }) : null)), kids); }
function xcBtn(text, fn, kind, disabled, title) {
  var b = el("button", { type: "button", class: "btn" + (kind ? " " + kind : ""), text: text, title: title || null, disabled: disabled ? true : null });
  b.addEventListener("click", fn);
  return b;
}
function xcPick(p) { CTL.flow = null; CTL.pick = p; ctlDraw(); }

function xcPlatform(s, canAct) {
  var p = s.platform || {}, st = XC_STATE[p.state] || XC_STATE.unknown, rows = [];
  var main, act;
  if (p.state === "stopped") act = xcBtn("Запустить день", function () { xcPick({ type: "start", mode: "paper", session: "day" }); }, "primary", !canAct);
  else if (p.state === "running") act = xcBtn("Остановить день", function () { flowStart("day.stop", { mode: "soft" }, "", { go: "Остановить день" }); }, "", !canAct);
  rows.push(xcRow(xcTitle([xcDot(st[0]), st[1], p.mode ? chip(XC_MODE[p.mode] || p.mode, p.mode === "live" ? "bad" : "") : null],
    (p.day_id ? "день " + p.day_id : "") + (p.session ? " · сессия «" + (DLG_SES[p.session] || p.session) + "»" : "") + (p.state === "running" ? (p.trading ? " · торговля включена" : " · торговля выключена") : "")), act));
  if (s.banner && s.banner.text) rows.push(el("div", { class: "st-note " + (s.banner.level === "bad" ? "bad" : s.banner.level === "warn" ? "warn" : "") , text: s.banner.text }));
  if (p.state === "stopped") rows.push(el("div", { class: "st-note", text: "Холодный старт кнопкой невозможен: дашборд живёт внутри платформы. Запустите её в терминале: ./tb up --detach (подробности: docs/CONTROL.md)." }));
  if (s.kill) rows.push(el("div", { class: "st-note bad", text: "Аварийная остановка активна" + (s.kill.reason ? ": " + s.kill.reason : "") + ". Снять: ./tb exec resume в терминале." }));
  rows.push(xcRow(xcTitle("Режим", (XC_MODE[p.mode] || p.mode || "—") + ": " + (XC_MODE_HINT[p.mode] || "")),
    xcBtn("Сменить", function () { xcPick({ type: "mode", mode: "paper" }); }, "", !(canAct && p.state === "running" && p.mode !== "observe"), "Внутри дня можно перейти только на более безопасный режим")));
  var r = s.risk;
  rows.push(xcRow(xcTitle("Уровень риска", r ? r.level + " · " + r.name + (r.source === "signed" ? "" : " · " + r.source) : "нет данных"),
    xcBtn("Изменить", function () { xcPick({ type: "risk", level: r.level }); }, "", !(canAct && r && r.levels && r.levels.length))));
  var tm = s.team || {};
  var profs = Object.keys(tm.profiles || {}).map(function (k) { return tm.profiles[k]; }).filter(function (x, i, a) { return a.indexOf(x) === i; });
  rows.push(xcRow(xcTitle("Мозг и модели", profs.length ? "модели: " + profs.join(", ") : "настройки не загружены"),
    xcBtn("Открыть настройки", function () { goTab("settings"); })));
  return xcBlock("Платформа", null, el("div", { class: "st-rows" }, rows));
}
function xcLive(s) {
  var checks = s.checks || [], hard = checks.filter(function (c) { return c.sev === "red"; }), soft = checks.filter(function (c) { return c.sev === "amber"; });
  if (!checks.length) return null;
  var bad = hard.filter(function (c) { return !c.ok; }), warn = soft.filter(function (c) { return !c.ok; });
  var line = bad.length ? bad.length + " " + plural(bad.length, "условие не выполнено", "условия не выполнены", "условий не выполнено") : "все жёсткие условия выполнены";
  var lvl = bad.length ? "bad" : warn.length ? "warn" : "ok";
  var det = el("details", { class: "xc-det" }, el("summary", { text: "Показать все проверки" }),
    el("ul", { class: "st-checks" }, hard.concat(soft).map(function (c) {
      return el("li", { class: c.ok ? "ok" : (c.sev === "red" ? "bad" : "warn") }, xcDot(c.ok ? "ok" : (c.sev === "red" ? "bad" : "warn")), el("div", null, el("b", { text: c.name }), el("div", { class: "muted small", text: c.msg || "" })));
    })));
  det.open = !!xcLive.open; det.addEventListener("toggle", function () { xcLive.open = det.open; });
  return xcBlock("Готовность к live", "условия допуска к реальным деньгам", [
    el("div", { class: "st-row" }, el("div", { class: "st-main" }, el("div", { class: "st-t" }, xcDot(lvl), line), el("div", { class: "st-s", text: warn.length ? "и предупреждений: " + warn.length + " (принимаются вручную при старте)" : "обойти их нельзя, только исправить причину" }))), det]);
}
function xcKill(canKill) {
  return xcBlock("Аварийная остановка", null, el("div", { class: "st-row" }, el("div", { class: "st-main" }, el("div", { class: "st-t", text: "Остановить торговлю немедленно" }),
    el("div", { class: "st-s", text: "Запрещает новые входы; позиции остаются под защитой стопов или закрываются по рынку (на ваш выбор). Работает и когда остальное недоступно." })),
    el("div", { class: "st-side" }, xcBtn("Аварийная остановка…", function () { xcPick({ type: "kill", flatten: false }); }, "danger-o"))));
}
function xcLast(s) {
  var last = s.last || [];
  var det = el("details", { class: "block st-block st-adv" }, el("summary", null, el("h2", null, "Последние команды", el("span", { class: "sub", text: last.length ? String(last.length) : "пока нет" }))));
  det.open = !!xcLast.open; det.addEventListener("toggle", function () { xcLast.open = det.open; });
  det.appendChild(el("div", { class: "st-adv-in" }, last.length ? el("ul", { class: "lst" }, last.map(function (r) {
    return el("li", null, xcDot(r.code === "OK" ? "ok" : "bad"), el("div", null, el("b", { text: r.action || "?" }), el("div", { class: "muted small", text: (r.code === "OK" ? "выполнено" : "отказано: " + (r.msg || r.code)) + (r.status ? " · " + r.status : "") })));
  })) : el("div", { class: "muted", text: "Команд ещё не было." })));
  return det;
}
function xcAuth() {
  if (!CTL.ss) return null;
  var a = ctlAuthed(), tokenSess = CTL.ss.mode === "token" || CTL.ss.full;
  return el("div", { class: "xc-auth muted small" }, a ? (CTL.ss.mode === "open_local" && !CTL.ss.full ? "Локальный вход без токена: просмотр и безопасные действия." : "Вы вошли по токену.") : "Вход не выполнен: просмотр доступен, для действий запросим токен.",
    tokenSess && a ? el("button", { type: "button", class: "xc-link", text: "Выйти", onclick: null, id: "xc-logout" }) : null);
}

function xControlDraw(force) {
  var host = $("tab-control");
  if (!host) return;
  var s = CTL.st;
  if (!force) {
    if (CTL.flow || CTL.pick) return;
    var sig = JSON.stringify([s, CTL.ss, CTL.ok, ctlAlive(), ctlBusy()], function (k, v) { return k === "ts" || k === "age_s" || k === "ttl_s" || k === "since" ? undefined : v; });
    if (sig === xcSig && host._drawn) return;
    xcSig = sig;
  }
  host._drawn = true;
  if (!CTL.ok && !s) { put(host, [el("div", { class: "empty" }, el("b", { text: "Нет связи с сервисом управления" }), el("span", { text: "Проверьте, что платформа запущена: ./tb up --detach" }))]); return; }
  var canAct = ctlAlive() && !ctlBusy();
  var kids = [];
  if (!ctlAlive()) kids.push(el("div", { class: "st-note bad", text: "Сервис управления не отвечает: кнопки заблокированы, кроме аварийной остановки. Запустите платформу: ./tb up --detach." }));
  kids.push(xcPlatform(s || {}, canAct), xcLive(s || {}), xcKill(), xcLast(s || {}), xcAuth());
  put(host, kids.filter(Boolean));
  var lo = $("xc-logout"); if (lo) lo.addEventListener("click", ctlLogout);
}

// Кнопка старта/остановки торгового дня в шапке: главное действие платформы доступно с любой страницы.
function xcDaySync() {
  var b = $("day-btn");
  if (!b) return;
  var p = CTL.st && CTL.st.platform, st = p && p.state;
  var want = !CTL.ok || !p ? "" : st === "stopped" ? "start" : st === "running" ? "stop" : "";
  var off = !ctlAlive() || ctlBusy();
  if (b._k === want && b._off === off) return;
  b._k = want; b._off = off;
  b.hidden = !want;
  b.disabled = off;
  b.textContent = want === "start" ? "Начать торговлю" : "Закончить день";
  b.classList.toggle("primary", want === "start");
}
function xcDayClick() {
  var b = $("day-btn");
  if (!b || b.disabled) return;
  if (b._k === "start") xcPick({ type: "start", mode: "paper", session: "day" });
  else if (b._k === "stop") flowStart("day.stop", { mode: "soft" }, "", { go: "Остановить день" });
}
