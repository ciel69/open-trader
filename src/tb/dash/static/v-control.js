"use strict";
// Вкладка «Управление» (WP12). Дашборд ничего не исполняет: команды подписывает и кладёт в очередь сервер дашборда, исполняет сервис ctl.
// Вход по токену (cookie HttpOnly + CSRF в заголовке X-TB-CSRF), опасные действия через challenge (nonce 2..60 с) и набранные вручную фразы.
// Все тексты вставляются через textContent (el/add из core.js).
var CTL = { st: null, ss: null, ok: false, flow: null, res: null, audit: null, busy: false, timer: null, lastAudit: 0 };
var CTL_MODES = [["observe", "Наблюдение"], ["paper", "Бумага"], ["testnet", "Testnet"], ["live", "LIVE"]];
var CTL_STATE = { stopped: "остановлена", starting: "запускается", running: "работает", closing: "закрывается", halted: "аварийно остановлена", unknown: "нет данных" };
var CTL_MODE_CLS = { observe: "m-observe", paper: "m-paper", testnet: "m-testnet", live: "m-live" };

// add из core.js принимает один аргумент; здесь можно несколько
function addAll(n) { for (var i = 1; i < arguments.length; i++) add(n, arguments[i]); }
function ctlGet(path) {
  return fetch(path, { cache: "no-store", credentials: "same-origin" }).then(function (r) { return r.json(); });
}
function ctlPost(path, body) {
  var h = { "Content-Type": "application/json" };
  if (CTL.ss && CTL.ss.csrf) h["X-TB-CSRF"] = CTL.ss.csrf;
  return fetch(path, { method: "POST", cache: "no-store", credentials: "same-origin", headers: h, body: JSON.stringify(body || {}) })
    .then(function (r) { return r.json().then(function (j) { j._http = r.status; return j; }); });
}
function ctlPoll() {
  if (document.hidden) return Promise.resolve();
  return Promise.all([ctlGet("/api/ctl/status"), ctlGet("/api/ctl/session")]).then(function (a) {
    CTL.st = a[0]; CTL.ss = a[1]; CTL.ok = true; ctlChrome();
    if (ui.tab === "control") renderControl();
    if (ui.tab === "settings" && typeof renderSettings === "function") renderSettings();
  }).catch(function () { CTL.ok = false; ctlChrome(); if (ui.tab === "control") renderControl(); });
}
// Ввод ключа LLM-провайдера: write-only, значение уходит один раз и нигде не возвращается (dash/keyweb.py)
function ctlSendKey(provider, value) { return ctlPost("/api/ctl/llmkey", { provider: provider, value: value }); }
function ctlLive() { return !!(CTL.st && CTL.st.platform && CTL.st.platform.mode === "live" && CTL.st.platform.state === "running"); }
function ctlAlive() { return !!(CTL.st && !CTL.st.stale); }

// ---------- шапка: чип состояния, рамка live, кнопка аварийной остановки
function ctlChrome() {
  var chipEl = $("ctl-chip"), p = CTL.st && CTL.st.platform;
  if (!p || !CTL.ok) { chipEl.hidden = true; document.body.classList.remove("ctl-live"); return; }
  chipEl.hidden = false;
  var text = (p.mode || "?") + " · " + (CTL_STATE[p.state] || p.state) + (CTL.st.stale ? " · ctl молчит" : "");
  chipEl.textContent = text;
  chipEl.className = "chip ctl-chip " + (CTL_MODE_CLS[p.mode] || "") + (CTL.st.stale || p.state === "halted" ? " bad" : "");
  document.body.classList.toggle("ctl-live", ctlLive());
}

// ---------- вход
function ctlLogin() {
  var inp = $("ctl-token"), msg = $("ctl-login-msg");
  var tok = inp.value; inp.value = "";
  msg.textContent = "Проверяю…";
  ctlPost("/api/ctl/login", { token: tok, remember: true }).then(function (j) {
    msg.textContent = j.ok ? "" : (j.msg || "Не удалось войти");
    return ctlPoll().then(function () {
      var f = CTL.flow;      // токен введён для опасного действия: повторяем проверку условий уже с правами
      if (j.ok && f && f.step === "elevate") flowStart(f.action, f.args, f.title, f.opts);
    });
  }).catch(function () { msg.textContent = "Нет связи с дашбордом"; });
}
function ctlLogout() { ctlPost("/api/ctl/logout", {}).then(ctlPoll); }

// ---------- общий сценарий: challenge -> подтверждение -> команда -> результат
function flowStart(action, args, title, opts) {
  CTL.pick = null;
  CTL.flow = { action: action, args: args, title: title, step: "challenge", opts: opts || {}, ch: null, accepted: {}, typed: [], err: null, res: null, shown: performance.now() };
  CTL.res = null;
  ctlPost("/api/ctl/challenge", { action: action, args: args }).then(function (j) {
    var f = CTL.flow; if (!f || f.action !== action) return;
    f.shown = performance.now();
    if (j.code === "E_ELEVATE") { f.step = "elevate"; f.err = null; }
    else if (j._http === 401 || j.code === "E_AUTH") { f.step = "error"; f.err = "Нужно войти по токену управления."; }
    else if (j.data) { f.ch = j.data; f.chOk = !!j.ok; f.chMsg = j.msg; f.step = "confirm"; }
    else { f.step = "error"; f.err = j.msg || "Ошибка"; }
    ctlDraw();
  }).catch(function () { if (CTL.flow) { CTL.flow.step = "error"; CTL.flow.err = "Нет связи с дашбордом"; ctlDraw(); } });
  ctlDraw();
}
function flowCancel() { CTL.flow = null; ctlDraw(); }
function flowSend() {
  var f = CTL.flow, ch = f.ch || {};
  var args = JSON.parse(JSON.stringify(f.args));
  var allw = ctlAllWarns(ch);
  if (allw.length) args.accepted = allw.map(function (w) { return w.id; });
  if (ch.pre_amber && ch.pre_amber.length) args.allow_amber = true;      // жёлтые предстартовые проверки приняты владельцем (каждая отмечена)
  f.step = "sending"; ctlDraw();
  var body = { action: f.action, args: args };
  if (ch.nonce) { body.nonce = ch.nonce; body.phrases = f.typed.slice(0, (ch.phrases || []).length); }
  ctlPost("/api/ctl/command", body).then(function (j) {
    if (j.code === "E_ELEVATE") { f.step = "elevate"; f.err = null; ctlDraw(); return; }
    if (!j.ok) { f.step = "confirm"; f.err = j.msg || "Команда отклонена"; ctlDraw(); return; }
    f.err = null; f.step = "wait"; f.id = j.id; f.sentAt = performance.now();
    if (j.killed) { f.step = "done"; f.res = { status: "done", code: "OK", msg: "Файл state/KILL создан. Снять: ./tb exec resume в терминале." }; }
    ctlDraw(); ctlPollResult();
  }).catch(function () { f.step = "confirm"; f.err = "Нет связи с дашбордом"; ctlDraw(); });
}
function ctlPollResult() {
  var f = CTL.flow;
  if (!f || f.step !== "wait") return;
  ctlGet("/api/ctl/result?id=" + encodeURIComponent(f.id)).then(function (j) {
    var r = j.result;
    if (r && r.status && r.status !== "running" && r.status !== "accepted") { f.step = "done"; f.res = r; CTL.lastAudit = 0; ctlDraw(); ctlPoll(); return; }
    if (performance.now() - f.sentAt > 30000 && !r) { f.step = "done"; f.res = { status: "failed", code: "E_CTL_DOWN", msg: "Сервис управления не ответил за 30 с. Команда остаётся в очереди." }; ctlDraw(); return; }
    if (r) f.res = r;
    setTimeout(ctlPollResult, 700);
  }).catch(function () { setTimeout(ctlPollResult, 1500); });
}

// ---------- отрисовка
function ctlAllWarns(ch) { return (ch.warnings || []).concat(ch.pre_amber || []); }
function ctlNorm(s) { return String(s || "").split(/\s+/).filter(Boolean).join(" "); }
function ctlUpdateConfirm() {
  var f = CTL.flow, b = $("ctl-go");
  if (!f || !b || !f.ch) return;
  var ch = f.ch, ok = true, left = ch.nonce ? Math.round(ch.expires_in - (performance.now() - f.shown) / 1000) : 99;
  var early = ch.nonce ? (performance.now() - f.shown) / 1000 < ch.min_delay : false;
  if (!f.opts.ackAll) ctlAllWarns(ch).forEach(function (w) { if (!f.accepted[w.id]) ok = false; });      // ackAll: принятие = нажатие кнопки
  (ch.phrases || []).forEach(function (p, i) { if (ctlNorm(f.typed[i]) !== ctlNorm(p)) ok = false; });
  if (f.opts.needCheck && !f.checked) ok = false;
  b.disabled = !ok || early || left < 0 || f.step !== "confirm";
  var t = $("ctl-left");
  if (t) t.textContent = ch.nonce ? (left < 0 ? "Подтверждение истекло: запросите заново." : early ? "Подождите пару секунд…" : "Подтверждение действует ещё " + left + " с") : "";
}
function ctlBusy() { return !!(CTL.flow && CTL.flow.step !== "done" && CTL.flow.step !== "error"); }
function ctlAuthed() { return !!(CTL.ss && CTL.ss.authed); }
// мягкая отрисовка (опрос): только если изменилось видимое; ввод пользователя не сбрасывается
// страница «Управление» рисуется в x-control.js, окна подтверждения и выбора: x-dialog.js
function ctlDraw() { if (typeof stFlowDraw === "function") stFlowDraw(); if (ui.tab === "control" && typeof xControlDraw === "function") xControlDraw(true); }
function renderControl() { if (typeof xControlDraw === "function") xControlDraw(); }
function ctlSig() {
  return JSON.stringify([CTL.st, CTL.ss, CTL.ok, CTL.pick, CTL.flow && [CTL.flow.step, CTL.flow.err, CTL.flow.res, CTL.flow.chOk, CTL.flow.title]],
    function (k, v) { return k === "ts" || k === "age_s" || k === "ttl_s" || k === "since" ? undefined : v; });
}
// ---------- запуск
registerTab("control", "Управление", "M12 3v9m5.7-6.7a8 8 0 1 1-11.4 0", renderControl);
(function () {
  var k = $("ctl-kill");
  if (k) k.addEventListener("click", function () {
    CTL.flow = null; CTL.pick = { type: "kill", flatten: false }; ctlDraw();
  });
  var d = $("day-btn");
  if (d) d.addEventListener("click", function () { if (typeof xcDayClick === "function") xcDayClick(); });
  var c = $("ctl-chip");
  if (c) c.addEventListener("click", function () { goTab("control"); });
  setInterval(function () { if (typeof xcDaySync === "function") xcDaySync(); if (!document.hidden) { ctlPoll(); if (CTL.flow && CTL.flow.step === "confirm") ctlUpdateConfirm(); } }, 1000);
  setInterval(function () { if (CTL.flow && CTL.flow.step === "confirm") ctlUpdateConfirm(); }, 500);
  ctlPoll();
})();
