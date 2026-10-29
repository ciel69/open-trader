"use strict";
// Верхняя полоса «Состояние системы» (на всех вкладках) и красный баннер для KILL и красных узлов. Данные: S.v2.strip (model2.build_strip).
// Полоса компактная (чипы), клик по любому чипу раскрывает подробности. Источники только файлы: runtime/exec_health.json, state/KILL, шина событий.

var sbOpen = false;

function stripItems() {
  var v = V2(), items = v && v.strip ? v.strip.items.filter(function (i) { return i.id !== "mode"; }) : [];   // режим уже в верхнем чипе
  if (S && S.session) {
    var s = S.session;
    items.push({ id: "session", label: "Сессия", level: s.entries_allowed ? "ok" : "warn", value: s.type || "?", left: s.left_s, text: "сессия " + (s.type || "") + (s.entries_allowed ? ", входы разрешены" : ", входы запрещены: " + (s.block_reasons || []).join(", ")) });
  } else if (S) {
    items.push({ id: "session", label: "Сессия", level: "off", value: "нет", text: "активной сессии нет: торговля не идёт" });
  }
  return items;
}
function sbChip(i) {
  var b = el("button", { type: "button", class: "sb-chip lv-" + i.level, "aria-expanded": sbOpen ? "true" : "false", title: i.label + ": " + i.text },
    el("i", { class: "dot", "aria-hidden": "true" }), el("span", { class: "sb-l", text: i.label }), " ",
    i.id === "session" && isn(i.left) ? el("b", { class: "sb-v" }, tk("sbsl", i.left, "left")) : el("b", { class: "sb-v", text: i.value }));
  b.addEventListener("click", function () {
    var idx = Array.prototype.indexOf.call($("sysbar-row").children, b);
    sbOpen = !sbOpen; renderStrip(true);
    var nb = $("sysbar-row").children[idx]; if (nb) nb.focus();   // фокус с клавиатуры не теряется при перерисовке
  });
  return b;
}
function renderStripBanner(v) {
  var box = $("sysbanner"), st = v && v.strip;
  if (!st) { box.hidden = true; return; }
  var bad = st.items.filter(function (i) { return i.level === "bad"; });
  if (!bad.length) { box.hidden = true; box.replaceChildren(); return; }
  var sig = JSON.stringify([st.kill, bad.map(function (i) { return [i.id, i.text]; })]);
  box.hidden = false;
  if (box._sig === sig) return;
  box._sig = sig;
  var nodes = [];
  if (st.kill) {
    nodes.push(el("b", { text: "KILL включён" }), el("span", { text: ": " + (st.kill.reason ? st.kill.reason + ". " : "") + "новые входы запрещены" + (st.kill.flatten ? ", позиции закрываются (flatten)" : "") + ". " }));
    nodes.push(el("span", { class: "kill-how" }, "Как снять: владелец удаляет файл ", el("code", { text: "state/KILL" }), " или выполняет ", el("code", { text: "./tb exec resume" }), " в терминале (из дашборда нельзя)."));
  } else {
    nodes.push(el("b", { text: "Нужно внимание: " }), el("span", { text: bad.map(function (i) { return i.label + " (" + i.value + ")"; }).join(" · ") }));
  }
  box.className = "sysbanner" + (st.kill ? " is-kill" : "");
  put(box, nodes);
}
function renderStrip(force) {
  var v = V2(), host = $("sysbar"), row = $("sysbar-row"), det = $("sysbar-det");
  if (!S) { host.hidden = true; return; }
  var items = stripItems();
  host.hidden = !items.length;
  renderStripBanner(v);
  var sig = JSON.stringify([items.map(function (i) { return [i.id, i.level, i.value, i.text]; }), sbOpen]);
  if (!force && row._sig === sig) { items.forEach(function (i) { if (i.id === "session" && isn(i.left)) tkReg("sbsl", i.left, "left"); }); return; }
  row._sig = sig;
  put(row, items.map(sbChip));
  items.forEach(function (i) { if (i.id === "session" && isn(i.left)) tkReg("sbsl", i.left, "left"); });
  det.hidden = !sbOpen;
  if (sbOpen) {
    var ex = v && v.strip && v.strip.exec;
    put(det, [el("ul", { class: "lst" }, items.map(function (i) {
      return el("li", null, stIcon(i.level), el("div", null, el("b", { text: i.label }), el("div", { class: "muted small", text: i.text })));
    })), ex && ex.present ? el("div", { class: "muted small", text: "uptime " + dur(ex.uptime_s) + " · версия " + (ex.version || "?") + " · задержка цикла " + (isn(ex.loop_lag_ms) ? ex.loop_lag_ms + " мс" : "–") }) : null,
      tabLink("Подробнее в разделе «Система»", "system", {}, "pop-a")]);
  }
}
