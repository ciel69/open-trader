"use strict";
// Вкладка «Уведомления» (WP-N): уведомления ОС (macOS/Windows) без правки конфига. Чтение: /api/notify/status; запись и проверка идут командами notify.set /
// notify.test через сервис ctl (нужен вход на вкладке «Управление»; подтверждение фразами не требуется). Любой текст вставляется через textContent (el/add из core.js).
var NT = { st: null, err: null, draft: null, dirty: false, msg: null, msgKind: "", busy: false };

function ntGet(path) { return fetch(path, { cache: "no-store", credentials: "same-origin" }).then(function (r) { return r.json(); }); }
function ntAuthed() { return typeof CTL !== "undefined" && !!(CTL.ss && CTL.ss.authed); }
function ntDraftFrom(st) {
  var s = st.settings;
  return { master: !!s.master, categories: JSON.parse(JSON.stringify(s.categories)), quiet: JSON.parse(JSON.stringify(s.quiet)), limits: JSON.parse(JSON.stringify(s.limits)) };
}
function ntPoll() {
  if (document.hidden) return Promise.resolve();
  return ntGet("/api/notify/status").then(function (j) {
    NT.st = j; NT.err = null;
    if (!NT.dirty) {      // объект черновика заменяется только при реальном изменении: обработчики формы держат ссылку на текущий
      var nd = ntDraftFrom(j);
      if (!NT.draft || JSON.stringify(nd) !== JSON.stringify(NT.draft)) NT.draft = nd;
    }
    renderNotify();
  }).catch(function () { NT.err = "Нет связи с дашбордом"; renderNotify(); });
}
function ntSay(text, kind) { NT.msg = text; NT.msgKind = kind || ""; renderNotify(true); }

// отправка команды в ctl и ожидание результата (до ~10 с)
function ntRun(action, args, okText) {
  if (!ntAuthed()) { flowStart(action, args, "", {}); return; }      // окно попросит токен, затем подтверждение
  NT.busy = true; ntSay("Отправляю…", "");
  ctlPost("/api/ctl/command", { action: action, args: args }).then(function (j) {
    if (!j.ok) { NT.busy = false; ntSay(j.msg || "Команда отклонена", "err"); return; }
    var tries = 0;
    (function wait() {
      ctlGet("/api/ctl/result?id=" + encodeURIComponent(j.id)).then(function (r) {
        var res = r && r.result;
        if (res && res.status !== "running") {
          NT.busy = false;
          if (res.code === "OK") { if (action === "notify.set") NT.dirty = false; ntSay(okText, "ok"); ntPoll(); }
          else ntSay((res.msg || res.code) + (action === "notify.test" ? ". Проверьте системные настройки уведомлений («Редактор скриптов» на macOS, PowerShell на Windows)." : ""), "err");
          return;
        }
        if (++tries > 20) { NT.busy = false; ntSay("ctl не ответил: платформа запущена?", "err"); return; }
        setTimeout(wait, 500);
      }).catch(function () { NT.busy = false; ntSay("Нет связи с дашбордом", "err"); });
    })();
  }).catch(function () { NT.busy = false; ntSay("Нет связи с дашбордом", "err"); });
}

function ntSwitch(id, label, checked, onchange, hint) {
  var inp = el("input", { type: "checkbox", id: id, role: "switch", class: "sw", "aria-label": label });
  inp.checked = !!checked;
  inp.addEventListener("change", function () { NT.dirty = true; onchange(inp.checked); renderNotify(true); });
  return inp;
}
function ntRow(title, sub, ctl) { return el("div", { class: "st-row" }, el("div", { class: "st-main" }, el("div", { class: "st-t", text: title }), sub ? el("div", { class: "st-s", text: sub }) : null), el("div", { class: "st-side" }, ctl)); }
function ntTime(id, label, value, onchange) {
  var inp = el("input", { type: "time", id: id, value: value, "aria-label": label });
  inp.value = value;
  inp.addEventListener("change", function () { if (inp.value) { NT.dirty = true; onchange(inp.value); renderNotify(true); } });
  return el("label", { class: "nt-field", for: id }, el("span", { text: label }), inp);
}
function ntNum(id, label, value, lo, hi, onchange) {
  var inp = el("input", { type: "text", inputmode: "numeric", id: id, "aria-label": label });
  inp.value = value;
  inp.addEventListener("change", function () { var v = parseInt(inp.value, 10); if (v >= lo && v <= hi) { NT.dirty = true; onchange(v); renderNotify(true); } });
  return el("label", { class: "nt-field", for: id }, el("span", { text: label }), inp);
}

function ntSave() {
  var d = NT.draft;
  ntRun("notify.set", { master: d.master, categories: d.categories, quiet: { enabled: d.quiet.enabled, from: d.quiet.from, to: d.quiet.to, tz: d.quiet.tz, bypass: d.quiet.bypass },
    limits: { per_hour: d.limits.per_hour, same_kind_min: d.limits.same_kind_min } }, "Сохранено.");
}
function ntBlock(title, sub, kids) { return el("section", { class: "block st-block" }, el("div", { class: "st-bh" }, el("h2", null, title, sub ? el("span", { class: "sub", text: sub }) : null)), kids); }
function ntBuild() {
  var st = NT.st, d = NT.draft, kids = [];
  if (NT.err && !st) return [el("div", { class: "empty" }, el("b", { text: NT.err }))];
  if (!st || !d) return [el("div", { class: "skel", style: "height:120px" })];
  if (st.source === "config") kids.push(el("div", { class: "st-note", text: "Своих настроек ещё нет: пока действуют значения из config/lifecycle.json (сейчас " + (st.settings.master ? "включено" : "выключено") + "). После первого сохранения приоритет получит файл настроек." }));
  if (st.warn) kids.push(el("div", { class: "st-note warn", role: "alert", text: st.warn }));
  var master = ntBlock("Уведомления на компьютере", "всплывающие сообщения системы", [
    ntRow("Показывать уведомления", "Тексты только из шаблонов, без ключей и личных данных. По умолчанию всё выключено.", ntSwitch("nt-master", "Показывать уведомления", d.master, function (v) { d.master = v; })),
    ntRow("Проверить, как это выглядит", "Покажет одно настоящее уведомление.", el("button", { class: "btn", type: "button", id: "nt-test", text: "Показать пробное", disabled: NT.busy ? true : null }))]);
  if (st.quiet_now) master.appendChild(el("div", { class: "st-note", text: "Сейчас тихие часы: уведомления копятся (накоплено " + st.held_n + "), критичные показываются сразу." }));
  kids.push(master);
  var crit = st.categories.filter(function (c) { return c.critical; }), rest = st.categories.filter(function (c) { return !c.critical; });
  var catRow = function (c) { return ntRow(c.label, null, ntSwitch("nt-c-" + c.id, c.label, d.categories[c.id], function (v) { d.categories[c.id] = v; })); };
  kids.push(ntBlock("О чём сообщать", d.master ? "" : "сначала включите уведомления", [
    crit.length ? el("div", { class: "nt-grp" }, "Важное") : null, crit.length ? el("div", { class: "st-rows" }, crit.map(catRow)) : null,
    rest.length ? el("div", { class: "nt-grp" }, "Остальное") : null, rest.length ? el("div", { class: "st-rows" }, rest.map(catRow)) : null]));
  var quiet = ntBlock("Тихие часы", "чтобы не отвлекало ночью", [
    ntRow("Не беспокоить в заданное время", "Уведомления копятся и выходят одним сообщением.", ntSwitch("nt-q-on", "Тихие часы", d.quiet.enabled, function (v) { d.quiet.enabled = v; })),
    d.quiet.enabled ? el("div", { class: "nt-fields" }, ntTime("nt-q-from", "С", d.quiet.from, function (v) { d.quiet.from = v; }), ntTime("nt-q-to", "До", d.quiet.to, function (v) { d.quiet.to = v; }),
      el("span", { class: "muted small", text: "Можно через полночь, например 23:00 – 07:00." })) : null]);
  kids.push(quiet);
  var adv = el("details", { class: "block st-block st-adv" }, el("summary", null, el("h2", null, "Дополнительно", el("span", { class: "sub", text: "часовой пояс, антиспам, исключения" }))));
  adv.open = !!ntBuild.open; adv.addEventListener("toggle", function () { ntBuild.open = adv.open; });
  var sel = el("select", { id: "nt-q-tz", "aria-label": "Часовой пояс" }, el("option", { value: "local", text: "местное время" }), el("option", { value: "utc", text: "UTC" }));
  sel.value = d.quiet.tz; sel.addEventListener("change", function () { NT.dirty = true; d.quiet.tz = sel.value; renderNotify(true); });
  var s2 = st.stats || {};
  adv.appendChild(el("div", { class: "st-adv-in" },
    el("h3", { text: "Часовой пояс тихих часов" }), el("label", { class: "nt-field" }, sel),
    el("h3", { text: "Антиспам" }), el("div", { class: "nt-fields" }, ntNum("nt-l-h", "Не больше в час на категорию", d.limits.per_hour, 1, 60, function (v) { d.limits.per_hour = v; }),
      ntNum("nt-l-m", "Один вид не чаще, минут", d.limits.same_kind_min, 0, 1440, function (v) { d.limits.same_kind_min = v; })),
    el("p", { class: "muted small", text: "Повторы одного вида объединяются в следующее уведомление («+N повторов»)." }),
    el("h3", { text: "Показывать и в тихие часы" }), el("div", { class: "nt-bypass" }, st.categories.map(function (c) {
      var id = "nt-b-" + c.id, cb = el("input", { type: "checkbox", id: id }); cb.checked = d.quiet.bypass.indexOf(c.id) >= 0;
      cb.addEventListener("change", function () { NT.dirty = true; var i = d.quiet.bypass.indexOf(c.id); if (cb.checked && i < 0) d.quiet.bypass.push(c.id); else if (!cb.checked && i >= 0) d.quiet.bypass.splice(i, 1); renderNotify(true); });
      return el("label", { class: "st-check", for: id }, cb, el("span", { text: c.label }));
    })),
    el("h3", { text: "Статистика" }), el("p", { class: "muted small", text: "Показано " + (s2.sent || 0) + ", объединено повторов " + (s2.merged || 0) + ", отсечено лимитом " + (s2.limited || 0) + ", сбоев показа " + (s2.failed || 0) + (st.sent_last ? ". Последнее: " + st.sent_last.cat : "") }),
    st.hint ? el("p", { class: "muted small", text: st.hint }) : null));
  kids.push(adv);
  if (NT.msg) kids.push(el("div", { class: "nt-msg " + NT.msgKind, id: "nt-msg", role: "status", text: NT.msg }));
  if (NT.dirty) kids.push(el("div", { class: "nt-bar", role: "status" }, el("span", { text: "Есть несохранённые изменения" }), el("button", { class: "btn", type: "button", id: "nt-reset", text: "Отменить" }),
    el("button", { class: "btn primary", type: "button", id: "nt-save", text: "Сохранить", disabled: NT.busy ? true : null })));
  return kids;
}

function renderNotify(force) {
  var host = $("tab-notify");
  if (!host) return;
  var sig = JSON.stringify([NT.st, NT.draft, NT.err, NT.dirty, NT.msg, NT.busy, ntAuthed()]);
  if (!force && host._sig === sig) return;
  if (!force && typing(host, function () { renderNotify(); })) return;
  host._sig = sig;
  var focus = document.activeElement && document.activeElement.id;
  put(host, ntBuild());
  var b;
  if ((b = $("nt-save"))) b.addEventListener("click", ntSave);
  if ((b = $("nt-test"))) b.addEventListener("click", function () { ntRun("notify.test", {}, "Команда выполнена: уведомление должно появиться на экране."); });
  if ((b = $("nt-reset"))) b.addEventListener("click", function () { NT.dirty = false; NT.msg = null; NT.draft = NT.st ? ntDraftFrom(NT.st) : null; renderNotify(true); });
  if (focus && $(focus)) $(focus).focus();
}

registerTab("notify", "Уведомления", "M6 9a6 6 0 0 1 12 0c0 6 2 7 2 7H4s2-1 2-7zM10 20a2 2 0 0 0 4 0", renderNotify);
(function () {
  setInterval(function () { if (!document.hidden && ui.tab === "notify") ntPoll(); }, 5000);
  ntPoll();
})();
