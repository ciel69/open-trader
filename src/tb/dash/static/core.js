"use strict";
// Ядро дашборда: DOM-помощники, форматирование, состояние интерфейса (хэш URL + localStorage),
// «тикающие» значения (возраст, остаток времени) и инкрементальное обновление списков по ключам.
// Любой текст из данных вставляется через textContent (el/add), разметка строкой не используется.

var S = null;          // последний ответ /api/state
var fetchedAt = 0;     // performance.now() получения ответа
var FAIL = 0;          // подряд неудачных опросов

// ---------- хранилище (localStorage может быть недоступен)
function store(k, v) {
  try {
    if (v === undefined) return localStorage.getItem("tbdash." + k);
    localStorage.setItem("tbdash." + k, v);
  } catch (e) { /* приватный режим */ }
  return null;
}

// ---------- DOM
function el(tag, attrs) {
  var n = document.createElement(tag);
  if (attrs) for (var k in attrs) {
    var v = attrs[k];
    if (v == null || v === false) continue;
    if (k === "class") n.className = v; else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (var i = 2; i < arguments.length; i++) add(n, arguments[i]);
  return n;
}
function add(n, c) {
  if (c == null || c === false) return;
  if (Array.isArray(c)) { c.forEach(function (x) { add(n, x); }); return; }
  n.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
}
function $(id) { return document.getElementById(id); }
function put(target, kids) {
  var n = typeof target === "string" ? $(target) : target;
  n.replaceChildren(); add(n, kids); n.removeAttribute("aria-busy"); return n;
}
var NS = "http://www.w3.org/2000/svg";
function sv(tag, attrs) {
  var n = document.createElementNS(NS, tag);
  for (var k in attrs || {}) n.setAttribute(k, attrs[k]);
  for (var i = 2; i < arguments.length; i++) if (arguments[i]) n.appendChild(arguments[i]);
  return n;
}

// ---------- форматирование (единые форматы чисел и процентов)
var NB = " ";
function isn(x) { return typeof x === "number" && isFinite(x); }
function fx(x, d) { return isn(x) ? x.toFixed(d == null ? 2 : d) : "–"; }
function sg(x, d) { return isn(x) ? (x > 0 ? "+" : x < 0 ? "−" : "") + Math.abs(x).toFixed(d == null ? 2 : d) : "–"; }
function usd(x, d) { return isn(x) ? sg(x, d == null ? 2 : d) + NB + "USDT" : "–"; }
function pc(x, d) { return isn(x) ? x.toFixed(d == null ? 1 : d) + "%" : "–"; }
function spc(x, d) { return isn(x) ? sg(x, d == null ? 2 : d) + "%" : "–"; }
// Проценты сделки: знак всегда, 2 знака, для |x|<0.01 три знака
function tpc(x) { return isn(x) ? sg(x, Math.abs(x) < 0.01 && x !== 0 ? 3 : 2) + "%" : "—"; }
function rr(x) { return isn(x) ? sg(x, 2) + "R" : "–"; }
function cls(x) { return !isn(x) || Math.abs(x) < 5e-7 ? "" : (x > 0 ? "pos" : "neg"); }
// Цена по величине (значащие цифры); эталон в Python: tb.dash.model.fmt_price, держи их одинаковыми.
function price(x) {
  if (!isn(x)) return "–";
  var a = Math.abs(x), d;
  if (a >= 1000) d = 1; else if (a >= 100) d = 2; else if (a >= 10) d = 3; else if (a >= 1) d = 4; else if (a === 0) d = 2;
  else d = Math.min(10, (a < 0.01 ? 4 : 5) - 1 - Math.floor(Math.log10(a)));
  return x.toFixed(d);
}
function dur(s) {
  if (!isn(s)) return "–";
  var neg = s < 0; s = Math.abs(Math.round(s));
  var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60, t;
  if (h >= 48) t = Math.floor(h / 24) + "д " + (h % 24) + "ч";
  else t = h ? h + "ч " + (m < 10 ? "0" : "") + m + "м" : m ? m + "м " + (r < 10 ? "0" : "") + r + "с" : r + "с";
  return (neg ? "−" : "") + t;
}
function ago(s) { return isn(s) ? (s < 1.5 ? "только что" : dur(s) + " назад") : "нет данных"; }
function p2(n) { return (n < 10 ? "0" : "") + n; }
function dt(epoch) {
  if (!isn(epoch)) return "–";
  var d = new Date(epoch * 1000);
  return p2(d.getDate()) + "." + p2(d.getMonth() + 1) + " " + p2(d.getHours()) + ":" + p2(d.getMinutes());
}
// epoch -> «ДД.ММ ЧЧ:ММ:СС» по локальному времени; ISO-строка -> то же
function dts(x) {
  var d = typeof x === "string" ? new Date(x) : (isn(x) ? new Date(x * 1000) : null);
  if (!d || isNaN(d.getTime())) return "–";
  return p2(d.getDate()) + "." + p2(d.getMonth() + 1) + " " + p2(d.getHours()) + ":" + p2(d.getMinutes()) + ":" + p2(d.getSeconds());
}
function hms(d) { return p2(d.getHours()) + ":" + p2(d.getMinutes()) + ":" + p2(d.getSeconds()); }
function pf(m) { return m.pf_inf ? "∞" : isn(m.pf) ? m.pf.toFixed(2) : "–"; }
function elapsed() { return (performance.now() - fetchedAt) / 1000; }
function plural(n, a, b, c) { var m = n % 100, k = n % 10; return (m > 10 && m < 15) ? c : k === 1 ? a : (k >= 2 && k <= 4) ? b : c; }

// ---------- подсказки и расшифровка сокращений
var GL = {
  mae: "MAE: худшее движение цены против позиции за время сделки, % от входа. Чем меньше, тем аккуратнее был вход.",
  mfe: "MFE: лучшее движение цены в пользу позиции, % от входа. Показывает, сколько можно было забрать.",
  r: "R: результат в долях риска. 1R = потеря при срабатывании стопа; +2R = прибыль вдвое больше риска.",
  rr: "RR: отношение прибыли к риску по плану: расстояние до тейка делить на расстояние до стопа.",
  pf: "PF (профит-фактор): сумма прибылей делить на сумму убытков. Выше 1 значит в плюсе.",
  margin: "PnL после комиссий в процентах от маржи (с учётом плеча). Это доходность на вложенные в сделку деньги.",
  move: "Изменение цены монеты от входа с учётом стороны, без плеча и комиссий.",
  dd: "Просадка: на сколько процентов капитал ниже своего максимума (высшей отметки).",
  ladder: "Множитель риска по лестнице просадки: при просадке размер риска на сделку уменьшается."
};
// Подчёркнутая метка с подсказкой, доступная с клавиатуры (фокус показывает подсказку)
function tip(text, key, quiet) {
  return el("span", { class: "tip-h" + (quiet ? " quiet" : ""), tabindex: "0", "data-tip": GL[key] || key, text: text, "aria-label": text + ". " + (GL[key] || key) });
}
(function () {
  var box = null;
  function show(t, x, y) {
    box = box || $("tip");
    box.textContent = t; box.hidden = false;
    var w = box.offsetWidth, h = box.offsetHeight;
    box.style.left = Math.max(6, Math.min(window.innerWidth - w - 6, x)) + "px";
    box.style.top = (y + h + 14 > window.innerHeight ? Math.max(6, y - h - 12) : y + 14) + "px";
  }
  function hide() { box = box || $("tip"); box.hidden = true; }
  window.showTip = function (ev, text) { show(text, ev.clientX + 12, ev.clientY); };
  window.hideTip = hide;
  function target(e) { return e.target && e.target.closest ? e.target.closest("[data-tip]") : null; }
  document.addEventListener("mouseover", function (e) { var t = target(e); if (t) { var r = t.getBoundingClientRect(); show(t.getAttribute("data-tip"), r.left, r.bottom - 8); } });
  document.addEventListener("mouseout", function (e) { if (target(e)) hide(); });
  document.addEventListener("focusin", function (e) { var t = target(e); if (t) { var r = t.getBoundingClientRect(); show(t.getAttribute("data-tip"), r.left, r.bottom - 8); } });
  document.addEventListener("focusout", function (e) { if (target(e)) hide(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
})();

// ---------- «тикающие» значения: возраст и остаток времени идут между опросами сами
// tk(ключ, секунды_на_момент_ответа, вид) -> span; вид: dur (растёт), ago (растёт, «N назад»), left (убывает до «истёк»)
var TK = {};
function tkText(kind, secs) {
  if (kind === "ago") return ago(secs);
  if (kind === "left") return secs < 0 ? "истёк" : dur(secs);
  return dur(secs);
}
function tkReg(key, base, kind) { TK[key] = { b: base, at: performance.now(), kind: kind }; }
function tkVal(r) { var d = (performance.now() - r.at) / 1000; return r.kind === "left" ? r.b - d : r.b + d; }
function tk(key, base, kind) {
  if (!isn(base)) return el("span", { text: kind === "ago" ? "нет данных" : "–" });
  tkReg(key, base, kind);
  return el("span", { "data-tk": key, text: tkText(kind, base) });
}
function tickAll() {
  var nodes = document.querySelectorAll("[data-tk]");
  for (var i = 0; i < nodes.length; i++) {
    var r = TK[nodes[i].getAttribute("data-tk")];
    if (!r || !isn(r.b)) continue;
    var t = tkText(r.kind, tkVal(r));
    if (nodes[i].textContent !== t) nodes[i].textContent = t;
  }
  if (window.onTick) window.onTick();
}
setInterval(function () { if (!document.hidden) tickAll(); }, 1000);

// ---------- состояние интерфейса: вкладка и фильтры живут в хэше URL (#trades?res=loss) и в localStorage
var TABS = [["overview", "Обзор"], ["plans", "Планы"], ["decisions", "Решения"], ["trades", "Сделки"], ["brief", "Брифинг"], ["ideas", "Идеи"], ["market", "Рынок"], ["system", "Система"]];
// Вкладки-расширения (WP12): скрипт расширения вызывает registerTab(id, title, iconPath, render) до app.js; секцию страницы создаёт app.js.
var EXT = {};
function registerTab(id, title, icon, render) { TABS.push([id, title]); EXT[id] = { render: render, icon: icon }; }
// pm: режим планов (all|live|paper); da/df/dm/dp: фильтры ленты решений (актор, только отказы, режим, план); b: id досье; eb: разрез экспериментов
var UI_DEF = { tab: "overview", mode: "live", venue: "all", res: "all", bd: "reason", it: "", hn: "15", pm: "all", da: "all", df: "all", dm: "all", dp: "", b: "", eb: "exp" };
var ui = {};
for (var _k in UI_DEF) ui[_k] = UI_DEF[_k];
ui.open = {};          // раскрытые строки: ключ -> true (не сохраняется)
ui.bdo = false;
// Разделы боковой рейки: каждый собирает несколько страниц (вкладок TABS); не вошедшие в список вкладки-расширения попадают в «Систему».
var GROUPS = [["now", "Сейчас", ["overview"]], ["brain", "Мозг", ["plans", "decisions", "brief", "ideas"]], ["trades", "Сделки", ["trades", "day"]],
  ["market", "Рынок", ["market", "intel"]], ["system", "Система", ["system", "settings", "control", "notify"]]];
function groupOf(tab) {
  for (var i = 0; i < GROUPS.length; i++) if (GROUPS[i][2].indexOf(tab) >= 0) return GROUPS[i];
  return GROUPS[GROUPS.length - 1];
}
function groupTabs(g) {   // вкладки раздела в порядке GROUPS, плюс расширения «Системы»
  var own = g[2].filter(validTab);
  if (g[0] === "system") TABS.forEach(function (t) { if (!GROUPS.some(function (x) { return x[2].indexOf(t[0]) >= 0; })) own.push(t[0]); });
  return own;
}
function tabTitle(id) { var t = TABS.filter(function (x) { return x[0] === id; })[0]; return t ? t[1] : id; }
function validTab(t) { return TABS.some(function (x) { return x[0] === t; }); }
function uiHash() {
  var q = [];
  Object.keys(UI_DEF).forEach(function (k) { if (k !== "tab" && ui[k] !== UI_DEF[k]) q.push(k + "=" + encodeURIComponent(ui[k])); });
  return "#" + ui.tab + (q.length ? "?" + q.join("&") : "");
}
function applyUi(obj) {
  Object.keys(UI_DEF).forEach(function (k) { if (obj && typeof obj[k] === "string" && (obj[k] || k === "it" || k === "dp" || k === "b")) ui[k] = obj[k]; });
  if (!validTab(ui.tab)) ui.tab = UI_DEF.tab;
}
function parseHash(h) {
  var m = /^#([a-z]+)(?:\?(.*))?$/.exec(h || "");
  if (!m) return null;
  var o = { tab: m[1] };
  (m[2] || "").split("&").forEach(function (p) {
    var kv = p.split("=");
    if (kv[0] && kv[0] in UI_DEF && kv[0] !== "tab") { try { o[kv[0]] = decodeURIComponent(kv[1] || ""); } catch (e) { /* битый хэш */ } }
  });
  return o;
}
function saveUi(push) {
  var h = uiHash();
  try { if (push) { if (location.hash !== h) location.hash = h; } else history.replaceState(null, "", h); } catch (e) { /* file:// и т.п. */ }
  var o = {}; Object.keys(UI_DEF).forEach(function (k) { o[k] = ui[k]; });
  store("ui", JSON.stringify(o));
}
function loadUi() {
  var saved = null;
  try { saved = JSON.parse(store("ui") || "null"); } catch (e) { saved = null; }
  applyUi(saved);
  var h = parseHash(location.hash);
  if (h) { for (var k in UI_DEF) if (k !== "tab" && !(k in h)) h[k] = UI_DEF[k]; applyUi(h); }  // хэш главнее сохранённого
}

// ---------- инкрементальный список: узлы переиспользуются по ключу, пересобираются только изменившиеся
// o.key(item), o.sig(item) (строка: изменилась -> пересобрать), o.build(item) -> узел, o.after(item, node) (необязательно, на каждый проход)
// Статические дети родителя допускаются только ПЕРЕД элементами списка (например, thead).
function syncList(parent, items, o) {
  var kids = Array.prototype.slice.call(parent.children), old = {}, lead = 0, seen = false, want = {};
  kids.forEach(function (c) {
    if (c.hasAttribute("data-key")) { seen = true; old[c.getAttribute("data-key")] = c; } else if (!seen) lead++;
  });
  items.forEach(function (it) { want[String(o.key(it))] = true; });
  Object.keys(old).forEach(function (k) { if (!want[k]) parent.removeChild(old[k]); });
  items.forEach(function (it, i) {
    var k = String(o.key(it)), sig = o.sig(it), n = old[k];
    if (!n || n._sig !== sig) {
      var nn = o.build(it);
      nn.setAttribute("data-key", k); nn._sig = sig;
      if (n && n.parentNode === parent) parent.replaceChild(nn, n);
      n = nn;
    }
    var exp = parent.children[lead + i] || null;
    if (n !== exp) parent.insertBefore(n, exp);
    if (o.after) o.after(it, n);
  });
}
function sigOf(obj, skip) {
  return JSON.stringify(obj, function (k, v) { return skip && skip.indexOf(k) >= 0 ? undefined : v; });
}

// ---------- раскрываемая строка таблицы: <tbody class="rg"> = строка-сводка + строка-деталь
// cells: [{t: узел|текст, c: класс, l: подпись для мобильной карточки}]
// fill(detailTd): заполняет деталь при первом раскрытии
function rowGroup(id, cells, ncols, fill, rowCls) {
  var tb = el("tbody", { class: "rg" });
  var sum = el("tr", { class: "sum" + (rowCls ? " " + rowCls : ""), tabindex: "0", role: "button", "aria-expanded": "false" });
  cells.forEach(function (c, i) {
    var td = el("td", { class: (c.c || "") + (i === 0 ? " first" : ""), "data-l": c.l || null });
    if (i === 0) td.appendChild(el("span", { class: "cv", "aria-hidden": "true" }));
    add(td, c.t);
    sum.appendChild(td);
  });
  var det = el("tr", { class: "det", hidden: true }), dtd = el("td", { colspan: ncols });
  det.appendChild(dtd);
  tb.appendChild(sum); tb.appendChild(det);
  function setOpen(on) {
    if (on && !dtd.firstChild && fill) fill(dtd);
    det.hidden = !on; sum.setAttribute("aria-expanded", on ? "true" : "false");
    if (on) ui.open[id] = true; else delete ui.open[id];
  }
  sum.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("a,button")) return;
    var sel = window.getSelection && String(window.getSelection());
    if (sel) return;                                            // выделяли текст: не сворачиваем
    setOpen(det.hidden);
  });
  sum.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen(det.hidden); } });
  if (ui.open[id]) setOpen(true);
  return tb;
}
// Пары «подпись: значение» для деталей
function kvgrid(pairs) {
  return el("dl", { class: "kv" }, pairs.filter(Boolean).map(function (p) {
    return [el("dt", null, p[0]), el("dd", { class: p[2] || "" }, p[1])];
  }));
}
function chip(text, kind, title) { return el("span", { class: "chip" + (kind ? " " + kind : ""), text: text, title: title || null }); }
function emptyState(node, title, hint) {
  node.hidden = false; node.replaceChildren();
  node.appendChild(el("b", { text: title }));
  if (hint) node.appendChild(el("span", { text: hint }));
}
// true, если содержимое узла нужно пересобрать (данные изменились); так опрос не трогает неизменившиеся блоки
function changed(node, data) {
  var sig = typeof data === "string" ? data : JSON.stringify(data);
  if (node._s === sig) return false;
  node._s = sig; return true;
}
// true, пока пользователь вводит текст внутри узла: опрос не должен перерисовывать форму под курсором.
// После ухода фокуса откладенная перерисовка выполняется сама (onIdle).
function typing(host, onIdle) {
  var a = document.activeElement;
  if (!(a && host.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))) return false;
  if (!host._idleHook) {
    host._idleHook = true;
    host.addEventListener("focusout", function () { setTimeout(function () { if (!typing(host)) onIdle(); }, 120); });
  }
  return true;
}
// Переключатель-сегмент: кнопки переиспользуются, поэтому фокус с клавиатуры не теряется при обновлении
function segment(host, options, current, onPick, tabs) {
  var btns = host.children;
  var same = btns.length === options.length && options.every(function (o, i) { return btns[i].getAttribute("data-v") === String(o[0]); });
  if (!same) {
    host.replaceChildren();
    options.forEach(function (o) {
      var b = el("button", { type: "button", "data-v": String(o[0]) }, el("span", { class: "sl" }), el("span", { class: "cnt" }));
      if (tabs) b.setAttribute("role", "tab");
      host.appendChild(b);
    });
  }
  options.forEach(function (o, i) {
    var b = btns[i], on = o[0] === current;
    b.onclick = function () { onPick(o[0]); };
    b.className = on ? "on" : "";
    if (tabs) { b.setAttribute("aria-selected", on ? "true" : "false"); b.tabIndex = on ? 0 : -1; } else b.setAttribute("aria-pressed", on ? "true" : "false");
    if (b.firstChild.textContent !== o[1]) b.firstChild.textContent = o[1];
    var c = o[2] != null ? String(o[2]) : "";
    if (b.lastChild.textContent !== c) b.lastChild.textContent = c;
  });
}

// ---------- вторичные запросы (/api/v2/*): один запрос на ключ одновременно, не чаще раза в 1.5 с; ответ кэшируется для мгновенной отрисовки
var TF = {};
function fetchTab(key, url, apply, force) {
  var st = TF[key] || (TF[key] = { busy: false, at: 0, data: null, url: null, dataUrl: null });
  if (st.data && st.dataUrl === url) apply(st.data, false);
  if ((st.busy && st.url === url) || (!force && st.url === url && performance.now() - st.at < 1500)) return;
  st.busy = true; st.url = url;
  var ctl = typeof AbortController === "function" ? new AbortController() : null;
  var to = setTimeout(function () { if (ctl) ctl.abort(); }, 6000);
  fetch(url, { cache: "no-store", signal: ctl ? ctl.signal : undefined })
    .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(function (j) { clearTimeout(to); if (st.url !== url) return; st.busy = false; st.at = performance.now(); st.data = j; st.dataUrl = url; apply(j, true); })
    .catch(function (e) { clearTimeout(to); if (st.url !== url) return; st.busy = false; st.at = performance.now(); apply(null, true, String((e && e.message) || e)); });
}
function qs(o) {
  return Object.keys(o).filter(function (k) { return o[k] !== "" && o[k] != null; }).map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(o[k]); }).join("&");
}
// «PL-1028-SOL-01» -> «SOL-01» (короткая подпись плана)
function planShort(id) { var m = /^PL-\d{4}-(.+)$/.exec(id || ""); return m ? m[1] : (id || "–"); }
function V2() { return S && S.v2 ? S.v2 : null; }
// ссылка на вкладку с фильтрами: текущие настройки + переопределения (так переход не сбрасывает режим и площадку)
function hashFor(tab, over) {
  var q = [];
  Object.keys(UI_DEF).forEach(function (k) {
    if (k === "tab") return;
    var v = over && k in over ? over[k] : ui[k];
    if (v !== UI_DEF[k]) q.push(k + "=" + encodeURIComponent(v));
  });
  return "#" + tab + (q.length ? "?" + q.join("&") : "");
}
function tabLink(text, tab, over, cls_) { return el("a", { href: hashFor(tab, over), class: cls_ || "more-link-i", text: text }); }
