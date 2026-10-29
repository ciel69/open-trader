"use strict";
// Общие окна «Системы»: простое подтверждение действий и выбор параметров (старт дня, режим, риск, аварийная остановка).
// Движок сценария (challenge -> подтверждение -> команда -> результат) остаётся в v-control.js; здесь только человеческий текст и вёрстка.
// Тексты данных вставляются через textContent.

// ---------- человеческое описание действия для окна подтверждения
function stDescribe(a, g) {
  var MODE = { observe: "наблюдение", paper: "бумага", testnet: "тестовая сеть", live: "реальные деньги" };
  if (a === "day.start") return ["Начать торговлю: " + (MODE[g.mode] || g.mode) + "?", (g.session === "day" ? "Торговый день без срока (до остановки); горизонт каждой сделки (скальп, день, свинг, позиция) выбирает агент." : "Сессия «" + (DLG_SES[g.session] || g.session) + "».") + " Перед стартом проверим условия безопасности." + (g.mode === "live" ? " Это реальные деньги." : ""), g.mode === "live"];
  if (a === "day.stop") return ["Остановить торговый день?", "Новые входы прекратятся, день завершится штатно. Платформа останется запущенной."];
  if (a === "mode.set") return ["Перейти в режим «" + (MODE[g.mode] || g.mode) + "»?", "Переключаться можно только на более безопасный режим."];
  if (a === "risk.set") return ["Установить уровень риска " + g.level + "?", "Меняются размеры и лимиты новых сделок. Уже открытые позиции не затрагиваются."];
  if (a === "kill.set") return ["Аварийная остановка?", g.flatten ? "Новые входы запрещаются сразу, все позиции будут закрыты по рынку. Снять остановку можно только в терминале: ./tb exec resume." : "Новые входы запрещаются сразу, открытые позиции остаются под защитой стопов. Снять остановку можно только в терминале: ./tb exec resume.", true];
  if (a === "notify.set") return ["Сохранить настройки уведомлений?", "Новые правила начнут действовать сразу."];
  if (a === "notify.test") return ["Показать пробное уведомление?", "На компьютере появится одно настоящее уведомление."];
  var P = function (x) { return stProvName(x); };
  if (a === "llm.provider") {
    var n = P(g.id);
    return ({
      enable: ["Включить «" + n + "»?", "Мозг сможет отправлять запросы этому провайдеру. Расходы пойдут с вашего ключа в пределах заданных лимитов."],
      disable: ["Выключить «" + n + "»?", "Запросы к нему прекратятся. Если он стоит в цепочке роли, мозг перейдёт на запасные модели."],
      add: ["Добавить провайдера «" + n + "»?", "Он появится в списке выключенным. Ключ и включение делаются отдельными шагами."],
      check: ["Проверить «" + n + "»?", "Проверка без интернета: смотрим настройки и есть ли ключ. Денег не тратит."],
      key_clear: ["Удалить ключ «" + n + "»?", "Файл с ключом будет стёрт. Чтобы вернуть провайдера, ключ придётся ввести заново.", true],
      remove: ["Удалить провайдера «" + n + "»?", "Запись о нём исчезнет из настроек. Ключ останется на диске, если не удалить его отдельно.", true]
    })[g.op] || ["Изменить провайдера «" + n + "»?", ""];
  }
  if (a === "brain.profile") {
    if (g.op === "delete") return ["Удалить профиль «" + g.id + "»?", "Профиль пропадёт из списка. Он не должен использоваться ни одной ролью.", true];
    var m = g.params || {}, pr = ((stD("providers") || {}).providers || []).filter(function (x) { return x.id === g.provider; })[0];
    var mod = pr ? (pr.models || []).filter(function (x) { return x.id === g.model; })[0] : null;
    return ["Сохранить профиль «" + g.id + "»?", (mod ? mod.name : g.model) + " у " + P(g.provider) + "; температура " + m.temperature + ", ответ до " + m.max_tokens + " токенов, размышление: " + stThinkName(m.thinking) + "."];
  }
  if (a === "brain.use") {
    var pf = stProf(g.profile);
    return ["Назначить «" + (pf ? pf.model_name : g.profile) + "» всем ролям?", "Эта модель станет основной у всех ролей сразу. Запасные цепочки останутся как есть. Откатить можно в разделе «Дополнительно».", true];
  }
  if (a === "brain.role") {
    var role = String(g.role || "").split(".").pop();
    if (g.op === "chain") {
      var cur = (((stD("roles") || {}).roles || []).filter(function (r) { return r.role === role; })[0]) || {}, pf2 = stProf(g.primary);
      var fb = (g.fallback || []).map(function (x) { var q = stProf(x); return q ? q.model_name : x; });
      return ["Сменить модель для роли «" + stRoleName(role) + "»?", "Сейчас: " + (cur.primary ? cur.primary.model_name : "—") + ". Станет: " + (pf2 ? pf2.model_name : g.primary) + "." + (g.fallback ? " Запасные: " + (fb.length ? fb.join(", ") : "нет") + "." : " Запасные не меняются.")];
    }
    if (g.op === "opts_clear") return ["Сбросить особые параметры роли «" + stRoleName(role) + "»?", "Роль вернётся к параметрам своего профиля."];
    var pr2 = g.params || {}, parts = [];
    if (pr2.temperature != null) parts.push("температура " + pr2.temperature);
    if (pr2.max_tokens != null) parts.push("ответ до " + pr2.max_tokens + " токенов");
    if (pr2.thinking) parts.push("размышление: " + stThinkName(pr2.thinking));
    var bd = g.budget && g.budget.day_usd;
    parts.push(bd != null ? "бюджет $" + bd + " в день" : "без суточного бюджета");
    return ["Изменить параметры роли «" + stRoleName(role) + "»?", parts.join("; ") + "."];
  }
  if (a === "exchange.set") {
    var nm = g.id ? g.id.toUpperCase() : "";
    return ({
      activate: ["Сделать " + nm + " основной биржей?", "Новые планы пойдут на неё. Вступит в силу при следующем запуске платформы."],
      enable: ["Включить " + nm + "?", "Для неё запустится исполнитель при следующем старте платформы."],
      disable: ["Выключить " + nm + "?", "Исполнитель для неё не запустится."],
      mode: ["Режим биржи " + nm + ": " + g.mode + "?", g.mode === "live" ? "Это реальные деньги. Сделки пойдут на настоящий счёт." : "Исполнитель этой биржи будет работать в режиме «" + g.mode + "»."],
      limits: ["Лимит позиций на " + nm + ": " + (g.max_open_positions == null ? "без лимита" : g.max_open_positions) + "?", "Ограничивает число одновременно открытых позиций именно на этой бирже."],
      allowed: ["Изменить список бирж?", "Исполнители запустятся только для выбранных бирж."]
    })[g.op] || ["Изменить настройки биржи?", ""];
  }
  if (a === "brain.rollback") return ["Откатить настройки мозга к ревизии r" + g.to + "?", "Модели ролей и цепочки вернутся к состоянию той ревизии. Текущая ревизия останется в истории.", true];
  return ["Применить изменение?", ""];
}

// ---------- ожидание результата: индикатор и ход шагов дня (старт S1-S9 / закрытие C1-C8 из /api/day/status)
var DLG_STEPS = {
  S1: "Предстартовые проверки", S2: "Свежие факты: новости и календарь", S3: "Сверка с биржей", S4: "Досье для мозга", S5: "Утренний аналитик (модель думает)",
  S6: "План дня", S7: "Сессия и бюджет дня", S8: "Включение торговли", S9: "Стартовый отчёт",
  C1: "Отмена ждущих входов", C2: "Снимок позиций", C3: "Чек-лист переноса", C4: "Аудитор дня (модель)", C5: "Вопросы владельцу", C6: "Исполнение решений", C7: "Остановка сессии", C8: "Отчёт дня"
};
var DLG_CTL_STEPS = { switch: "переключение режима", recheck: "повторная проверка условий", day: "запуск дня", stop: "остановка дня" };
function stWaitView(f) {
  var day = f.action === "day.start" || f.action === "day.stop";
  var lead = day ? (f.action === "day.start" ? "Запускаю день: проверки, свежие факты, досье, план дня от модели, сессия. Обычно 20–60 секунд." : "Завершаю день: ждущие входы, чек-лист переноса, аудитор, отчёт. Обычно 10–40 секунд.")
    : "Применяю… это занимает несколько секунд.";
  var kids = [el("div", { class: "st-wait" }, el("span", { class: "st-spin", "aria-hidden": "true" }), el("span", { class: "muted", text: lead }))];
  if (day) { kids.push(el("div", { id: "st-prog", class: "st-prog", "aria-live": "polite" })); stProgPoll(f); }
  return kids;
}
function stProgPoll(f) {
  if (f._pt) return;
  var phase = f.action === "day.start" ? "start" : "close", t0 = performance.now();
  var tick = function () {
    if (CTL.flow !== f || (f.step !== "wait" && f.step !== "sending")) { clearInterval(f._pt); f._pt = null; return; }
    ctlGet("/api/day/status").then(function (j) {
      var cur = (j && j.current) || {}, steps = (cur.steps || []).filter(function (s) { return s.phase === phase; });
      if (cur.day_id && f._day && cur.day_id !== f._day) steps = [];
      f._day = cur.day_id || f._day;
      stProgDraw(steps, phase, (CTL.st || {}).step, performance.now() - t0);
    }).catch(function () { stProgDraw([], phase, (CTL.st || {}).step, performance.now() - t0); });
  };
  f._pt = setInterval(tick, 1000);
  tick();
}
function stProgDraw(steps, phase, ctlStep, elapsedMs) {
  var host = document.getElementById("st-prog");
  if (!host) return;
  var names = Object.keys(DLG_STEPS).filter(function (k) { return k[0] === (phase === "start" ? "S" : "C"); });
  var by = {}; steps.forEach(function (s) { by[s.name] = s; });
  var running = null;
  names.forEach(function (n) { if (!by[n] && !running) running = n; });                 // первый ещё не записанный шаг = идёт сейчас
  var items = names.map(function (n) {
    var s = by[n], st = s ? (s.ok === true ? "ok" : s.ok === false ? "bad" : "run") : (n === running && steps.length ? "run" : "todo");
    if (!steps.length && ctlStep && ctlStep.name !== "day" && ctlStep.name !== "stop") st = "todo";
    var extra = s && s.detail && s.detail.amber ? " · с предупреждением" : s && s.code ? " · " + s.code : "";
    return el("li", { class: "st-ps " + st }, el("i", { class: "st-pd", "aria-hidden": "true" }), el("span", { text: n + " " + DLG_STEPS[n] + extra }));
  });
  var head = ctlStep && ctlStep.name && ctlStep.name !== "day" && ctlStep.name !== "stop" ? "Сейчас: " + (DLG_CTL_STEPS[ctlStep.name] || ctlStep.name) + (ctlStep.n ? " (" + ctlStep.i + " из " + ctlStep.n + ")" : "") : (steps.length ? "Шаги дня" : "Ожидаю сервис управления…");
  put(host, [el("div", { class: "muted small st-ph", text: head + " · " + Math.round(elapsedMs / 1000) + " с" }), el("ul", { class: "st-pl" }, items)]);
}

// ---------- окно подтверждения
var DLG_SES = { day: "Торговый день", scalp: "Скальпинг", intraday: "Внутри дня", swing: "Свинг", position: "Позиционная" };
var DLG_MODES = [["observe", "Наблюдение", "ничего не торгуется, только наблюдение", "Наблюдение"], ["paper", "Бумага", "виртуальные сделки без реальных денег", "Бумага"], ["testnet", "Тестовая сеть", "настоящая биржа, но не настоящие деньги", "Тестнет"], ["live", "Реальные деньги", "сделки на настоящем счёте", "Реальные"]];   // [id, название, подсказка, короткая подпись для переключателя]
function dBtn(text, onclick, kind) { var b = el("button", { type: "button", class: "btn" + (kind ? " " + kind : ""), text: text }); b.addEventListener("click", onclick); return b; }
function dlgHost() {
  var h = document.getElementById("st-modal");
  if (!h) { h = el("div", { id: "st-modal", hidden: true }); document.body.appendChild(h); }
  return h;
}
function dlgRefreshPage() {
  if (ui.tab === "settings" && typeof renderSettings === "function") renderSettings("user");
  else if (ui.tab === "control" && typeof xControlDraw === "function") xControlDraw(true);
  else if (ui.tab === "notify" && typeof renderNotify === "function") renderNotify(true);
}
function dlgAfterDone() {
  if (ui.tab === "settings" && typeof stReload === "function") stReload().then(dlgRefreshPage);
  else if (ui.tab === "notify" && typeof ntPoll === "function") { if (CTL.flow && CTL.flow.res && CTL.flow.res.code === "OK") NT.dirty = false; ntPoll(); }
  else ctlPoll();
}
function stModalClose() { if (CTL.flow) { flowCancel(); } else if (CTL.pick) { CTL.pick = null; stFlowDraw(); } dlgRefreshPage(); }
document.addEventListener("keydown", function (e) { if (e.key === "Escape" && (CTL.flow || CTL.pick)) { e.stDone = true; stModalClose(); } });
function stModalShell(title, kids, foot, danger, compact) {
  return el("div", { class: "st-ov", id: "st-ov" }, el("div", { class: "st-modal" + (danger ? " danger" : "") + (compact ? " compact" : ""), role: "dialog", "aria-modal": "true", "aria-labelledby": "st-mt" },
    el("h2", { id: "st-mt", text: title }), el("div", { class: "st-mb" }, kids), el("div", { class: "st-mf" }, foot)));
}
var _stFlowSig = "", _stPickObj = null;
function stFlowDraw() {
  var host = dlgHost();
  var f = CTL.flow;
  if (!f) {
    _stFlowSig = "";
    // окно выбора параметров перерисовывается только при новом выборе (сам picker перерисовывает себя при кликах); опрос статуса раз в секунду его не трогает
    if (CTL.pick) { if (CTL.pick !== _stPickObj || host.hidden) { _stPickObj = CTL.pick; dlgPicker(host); } return; }
    _stPickObj = null; put(host, []); host.hidden = true; document.body.classList.remove("st-lock"); return;
  }
  _stPickObj = null;
  // окно сценария перерисовывается только когда изменилось его содержимое (шаг, ответ challenge, ошибка, результат): иначе опрос статуса платформы
  // раз в секунду пересобирал DOM, окно мерцало, фокус и наведение терялись. Галочки и фразы живут в самом DOM (ctlUpdateConfirm), в подпись не входят.
  var sig = JSON.stringify([f.action, f.args, f.step, f.err, f.res, f.ch, f.chOk, f.chMsg, f.title, f.id, f.opts]);
  if (sig === _stFlowSig && !host.hidden) { if (f.step === "confirm") ctlUpdateConfirm(); return; }
  _stFlowSig = sig;
  var d = stDescribe(f.action, f.args), ch = f.ch || {}, kids = [], foot = [], title = d[0], danger = !!d[2] || !!(ch.phrases && ch.phrases.length);
  if (d[1]) kids.push(el("p", { class: "st-lead", text: d[1] }));
  if (f.step === "challenge") { kids.push(el("p", { class: "muted", text: "Проверяю, можно ли это сделать…" })); foot.push(dBtn("Отмена", stModalClose)); }
  else if (f.step === "elevate") {
    title = "Нужен токен управления";
    var tk = el("input", { type: "password", id: "ctl-token", autocomplete: "off", spellcheck: "false", placeholder: "токен управления", "aria-label": "Токен управления" });
    tk.addEventListener("keydown", function (e) { if (e.key === "Enter") ctlLogin(); });
    kids = [el("p", { class: "st-lead", text: "Чтобы выполнить: «" + d[0].replace(/\?$/, "") + "», введите токен. Он защищает настройки от случайных изменений; браузер запомнится на 30 дней." }), tk, el("div", { class: "small", id: "ctl-login-msg" }),
      el("p", { class: "muted small", text: "Нет токена? В терминале: ./tb control token new" })];
    foot = [dBtn("Продолжить", ctlLogin, "primary"), dBtn("Отмена", stModalClose)];
    foot[0].id = "ctl-login";
  } else if (f.step === "confirm") {
    var gates = (ch.gates || []).filter(function (g) { return !g.ok; });
    if (!f.chOk || gates.length) {
      title = "Сейчас это сделать нельзя";
      kids = [el("p", { class: "st-lead", text: d[0] })];
      if (gates.length) kids.push(el("ul", { class: "st-why" }, gates.map(function (g) { return el("li", { text: g.msg || g.name || g.id }); })));
      else kids.push(el("p", { class: "neg", text: f.chMsg || "Жёсткие условия не выполнены." }));
      foot = [dBtn("Понятно", stModalClose, "primary")];
    } else {
      var warns = ctlAllWarns(ch);
      if (warns.length && f.opts.ackAll) {          // повышение риска: последствия списком, принятие одним нажатием кнопки (без галочек и набора текста)
        kids.push(el("p", { class: "st-lead warn-t", text: "Что изменится на более высоком уровне:" }));
        kids.push(el("ul", { class: "st-why" }, warns.map(function (w) { return el("li", { text: w.msg }); })));
        kids.push(el("p", { class: "muted small", text: "Кнопка ниже означает, что вы понимаете эти последствия и принимаете риск." }));
      } else if (warns.length) {
        kids.push(el("p", { class: "st-lead warn-t", text: "Прежде чем продолжить, обратите внимание:" }));
        warns.forEach(function (w) {
          var cb = el("input", { type: "checkbox", id: "ctl-w-" + w.id }); cb.checked = !!f.accepted[w.id];
          cb.addEventListener("change", function () { f.accepted[w.id] = cb.checked; ctlUpdateConfirm(); });
          kids.push(el("label", { class: "st-check", for: "ctl-w-" + w.id }, cb, el("span", { text: w.msg })));
        });
      }
      (ch.phrases || []).forEach(function (p, i) {
        var inp = el("input", { type: "text", class: "ctl-phrase", autocomplete: "off", spellcheck: "false", autocapitalize: "off", "aria-label": "Фраза подтверждения" });
        ["paste", "drop"].forEach(function (ev) { inp.addEventListener(ev, function (e) { e.preventDefault(); }); });
        inp.value = f.typed[i] || "";
        inp.addEventListener("input", function () { f.typed[i] = inp.value; ctlUpdateConfirm(); });
        kids.push(el("div", { class: "st-phrase" }, el("p", { class: "st-lead neg", text: "Это влияет на реальную торговлю. Чтобы подтвердить, наберите вручную (вставка отключена):" }), el("code", { class: "ctl-ph", text: p }), inp));
      });
      if (f.err) kids.push(el("p", { class: "neg", role: "alert", text: f.err }));
      var go = dBtn(f.opts.go || ((ch.pre_amber || []).length ? "Запустить с предупреждениями" : danger ? "Да, применить" : "Применить"), flowSend, "primary" + (danger ? " danger" : "")); go.id = "ctl-go";
      foot = [go, dBtn("Отмена", stModalClose), el("span", { class: "muted small", id: "ctl-left" })];
    }
  } else if (f.step === "sending" || f.step === "wait") {
    kids = kids.concat(stWaitView(f));
  } else if (f.step === "done" || f.step === "error") {
    var r = f.res || {}, good = f.step === "done" && r.code === "OK";
    title = good ? "Готово" : "Не получилось";
    kids = [el("p", { class: "st-lead " + (good ? "pos" : "neg"), text: f.step === "error" ? (f.err || "Ошибка") : (r.msg || (good ? "Изменения применены." : "Команда отклонена.")) })];
    if (good && (r.data || {}).ok === false) kids.push(el("div", { class: "st-note warn", text: "Проверка нашла замечания: " + (r.data.checked || []).map(function (c) { return c.config_error || c.note; }).filter(Boolean).join("; ") }));
    if (!good && (r.data || {}).detail) kids.push(el("details", { class: "st-more" }, el("summary", { text: "Подробности" }), el("p", { class: "muted small", text: typeof r.data.detail === "string" ? r.data.detail : JSON.stringify(r.data.detail) }), r.code ? el("p", { class: "muted small mono", text: r.code }) : null));
    foot = [dBtn("Закрыть", stModalClose, "primary")];
  }
  put(host, [stModalShell(title, kids, foot, danger && f.step === "confirm")]);
  host.hidden = false; document.body.classList.add("st-lock");
  if (f._shown !== f.step) {
    f._shown = f.step;
    var first = host.querySelector("input:not([type=checkbox]), .btn.primary"); if (first) first.focus();
  }
  if (f.step === "done" && !f._refreshed) { f._refreshed = true; dlgAfterDone(); }
  ctlUpdateConfirm();
}


// ---------- выбор параметров действия перед подтверждением (старт дня, режим, риск, аварийная остановка)
function dlgField(label, input, hint) { return el("label", { class: "st-f" }, el("span", { class: "st-fl", text: label }), input, hint ? el("span", { class: "muted small", text: hint }) : null); }
function dlgPicker(host) {
  var pk = CTL.pick, s = CTL.st || {}, p = s.platform || {}, kids = [], foot = [], title = "", danger = false, compact = false;
  var cancel = dBtn("Отмена", function () { CTL.pick = null; stFlowDraw(); dlgRefreshPage(); });
  var redraw = function () { dlgPicker(host); };
  var radios = function (opts, cur, onpick) {
    return el("div", { class: "st-radios", role: "radiogroup" }, opts.map(function (o) {
      var b = el("button", { type: "button", role: "radio", "aria-checked": o[0] === cur ? "true" : "false", class: "st-radio" + (o[0] === cur ? " on" : "") }, el("b", { text: o[1] }), el("span", { class: "muted small", text: o[2] || "" }));
      b.addEventListener("click", function () { onpick(o[0]); redraw(); });
      return b;
    }));
  };
  if (pk.type === "start") {
    // Компактное окно: режим одним переключателем, длительность одной строкой. Тип сессии владелец не выбирает:
    // сессия = торговый день, горизонт каждой сделки (скальп, день, свинг, позиция) выбирает агент в плане (session_type плана).
    title = "Начать торговлю";
    pk.session = "day";
    var cur = DLG_MODES.filter(function (m) { return m[0] === pk.mode; })[0] || DLG_MODES[1];
    var seg = el("div", { class: "seg st-seg", role: "radiogroup", "aria-label": "Режим" }, DLG_MODES.map(function (o) {
      var b = el("button", { type: "button", role: "radio", "aria-checked": o[0] === pk.mode ? "true" : "false", class: (o[0] === pk.mode ? "on" : "") + (o[0] === "live" ? " live" : ""), text: o[3] || o[1], title: o[1] });
      b.addEventListener("click", function () { pk.mode = o[0]; redraw(); });
      return b;
    }));
    kids = [seg, el("p", { class: "st-hint muted small", text: cur[1] + ": " + cur[2] }),
      el("p", { class: "st-note-l muted small", text: "Срока у дня нет: он идёт, пока вы его не остановите. Горизонт каждой сделки (скальп, день, свинг, позиция) агент выбирает сам в рамках уровня риска." })];
    if (pk.mode === "live") kids.push(el("p", { class: "neg small", text: "Реальные деньги: дальше проверим условия безопасности и покажем предупреждения." }));
    danger = pk.mode === "live";
    foot = [dBtn(pk.mode === "live" ? "Далее" : "Начать", function () {
      CTL.pick = null; flowStart("day.start", { mode: pk.mode, session: pk.session },"", { go: pk.mode === "live" ? "Торговать на реальные деньги" : "Начать", danger: pk.mode === "live" });
    }, "primary" + (danger ? " danger" : "")), cancel];
    compact = true;
  } else if (pk.type === "mode") {
    title = "Сменить режим";
    var order = { observe: 0, paper: 1, testnet: 2, live: 3 }, cur = p.mode || "observe";
    var opts = DLG_MODES.filter(function (m) { return order[m[0]] < order[cur]; });
    if (!opts.length) kids = [el("p", { class: "muted", text: "Безопаснее текущего режима уже некуда." })];
    else { if (!opts.some(function (o) { return o[0] === pk.mode; })) pk.mode = opts[opts.length - 1][0]; kids = [el("p", { class: "st-lead", text: "Сейчас: " + (DLG_MODES.filter(function (m) { return m[0] === cur; })[0] || [0, cur])[1] + ". Внутри дня можно перейти только на более безопасный режим." }), radios(opts, pk.mode, function (v) { pk.mode = v; })]; }
    foot = [opts.length ? dBtn("Далее", function () { CTL.pick = null; flowStart("mode.set", { mode: pk.mode }, "", { go: "Сменить режим" }); }, "primary") : null, cancel];
  } else if (pk.type === "risk") {
    title = "Уровень риска";
    var lv = (s.risk && s.risk.levels) || [];
    kids = [el("p", { class: "st-lead", text: "Сейчас: " + (s.risk ? s.risk.level + " · " + s.risk.name : "—") + ". Уровень задаёт размер риска на сделку и лимиты." }),
      radios(lv.map(function (l) { return [l.n, l.n + " · " + l.name, l.desc || l.note || ""]; }), pk.level, function (v) { pk.level = v; })];
    foot = [dBtn("Далее", function () { CTL.pick = null; flowStart("risk.set", { level: pk.level }, "", { go: pk.level > ((s.risk && s.risk.level) || 0) ? "Принимаю риски, повысить" : "Установить уровень", ackAll: true }); }, "primary"), cancel];
  } else if (pk.type === "kill") {
    title = "Аварийная остановка"; danger = true;
    var cb = el("input", { type: "checkbox", id: "ctl-flat" }); cb.checked = !!pk.flatten;
    cb.addEventListener("change", function () { pk.flatten = cb.checked; });
    kids = [el("p", { class: "st-lead", text: "Сразу запрещает новые входы. Открытые позиции остаются под защитой стопов. Снять остановку можно только в терминале: ./tb exec resume." }),
      el("label", { class: "st-check", for: "ctl-flat" }, cb, el("span", { text: "Ещё и закрыть все позиции по рынку. Понимаю, что цена закрытия может быть хуже." }))];
    foot = [dBtn("Остановить", function () { CTL.pick = null; flowStart("kill.set", { flatten: !!pk.flatten, reason: "кнопка на дашборде" }, "", { go: "Остановить торговлю", danger: true, needCheck: pk.flatten ? "Понимаю: позиции будут закрыты по рынку" : null }); }, "primary danger"), cancel];
  } else { host.hidden = true; return; }
  put(host, [stModalShell(title, kids, foot.filter(Boolean), danger, compact)]);
  host.hidden = false; document.body.classList.add("st-lock");
  if (pk._shown !== pk.type) { pk._shown = pk.type; var b0 = host.querySelector(".st-radio.on, select, .btn.primary"); if (b0) b0.focus(); }
}
