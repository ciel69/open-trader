"use strict";
// Раздел «Настройки» (контракт: docs/spec_v2/SETTINGS_API.md). Одна страница из четырёх блоков: «Кто за что отвечает», «Подключения ИИ»,
// «Биржа», «Расход»; редкое спрятано в «Дополнительно». Детали и формы открываются в боковой панели, подтверждения идут в окне простым языком.
// Чтение: GET /api/settings/*. Запись: ctl-сценарий из v-control.js (challenge -> подтверждение -> команда); ключ провайдера: write-only ctlSendKey.
// Торговые ключи бирж здесь не принимаются никогда. Тексты данных вставляются через textContent.

var ST = { data: {}, err: {}, at: {}, drawer: null, days: "7", keyMsg: "", loaded: false };
var ST_THINK = [["off", "выключено"], ["low", "немного"], ["medium", "средне"], ["high", "глубоко"]];
var ST_ROLE = { morning_analyst: ["Утренний аналитик", "готовит план на день"], position_manager: ["Управление позицией", "решает по открытым сделкам"], day_auditor: ["Аудитор дня", "проверяет день по итогам"],
  reviewer: ["Разбор сделок", "пишет разбор после закрытия"], entry_scout: ["Поиск входов", "высматривает места для входа"], news_reader: ["Чтение новостей", "читает ленту и заголовки"] };
var ST_NAMES = ["overview", "providers", "presets", "profiles", "roles", "exchanges", "revisions", "usage"];

// ---------- загрузка
function stUrl(n) { return "/api/settings/" + (n === "usage" ? "usage?days=" + ST.days + "&by=model" : n); }
function stFetch(force) {
  var now = performance.now();
  return Promise.all(ST_NAMES.map(function (n) {
    if (!force && ST.at[n] && now - ST.at[n] < 12000) return null;
    return ctlGet(stUrl(n)).then(function (j) {
      ST.at[n] = performance.now();
      if (j && j.error) ST.err[n] = j.error; else { ST.data[n] = j; delete ST.err[n]; }
    }).catch(function () { ST.err[n] = "network"; });
  })).then(function () { ST.loaded = true; renderSettings("data"); });
}
function stReload() { ST.at = {}; ST.drawer = null; return stFetch(true); }

// ---------- справочники и подписи
function stD(n) { return ST.data[n] || null; }
function stProvName(id) { var p = ((stD("providers") || {}).providers || []).filter(function (x) { return x.id === id; })[0]; return p ? (p.name || p.id) : id; }
function stProf(id) { return ((stD("profiles") || {}).profiles || []).filter(function (x) { return x.id === id; })[0] || null; }
function stProfLabel(p) { return p ? p.model_name + " · " + stProvName(p.provider) : "—"; }
function stRoleName(r) { return (ST_ROLE[r] || [r])[0]; }
function stAuthed() { return typeof ctlAuthed === "function" && ctlAuthed(); }
function stNum(v) { var n = parseFloat(String(v).replace(",", ".")); return isFinite(n) ? n : null; }
function stThinkName(v) { var t = ST_THINK.filter(function (x) { return x[0] === v; })[0]; return t ? t[1] : v; }
function stLocked(r) { var rl = stD("roles") || {}; return rl.mode === "live" && r.live_risk && r.team === rl.live_team; }

// ---------- мелкие кирпичи
function stBtn(text, onclick, kind) { var b = el("button", { type: "button", class: "btn" + (kind ? " " + kind : ""), text: text }); b.addEventListener("click", onclick); return b; }
function stField(label, input, hint) { return el("label", { class: "st-f" }, el("span", { class: "st-fl", text: label }), input, hint ? el("span", { class: "muted small", text: hint }) : null); }
function stInput(value, ph, id) { var i = el("input", { type: "text", id: id || null, autocomplete: "off", spellcheck: "false", placeholder: ph || null }); if (value != null) i.value = String(value); return i; }
function stSelect(opts, value, id) {
  var s = el("select", { id: id || null });
  opts.forEach(function (o) { var op = el("option", { value: o[0], text: o[1] }); if (String(o[0]) === String(value)) op.selected = true; if (o[2] && String(o[0]) !== String(value)) op.disabled = true; s.appendChild(op); });
  return s;
}
// варианты профилей для роли: [id, подпись, недоступен?]; профиль с выключенным провайдером выбрать нельзя (сервер отклонит E_SWAP_PROFILE), но если он уже стоит, показывается
function stProfileOpts() {
  var profs = (stD("profiles") || {}).profiles || [], provs = {};
  ((stD("providers") || {}).providers || []).forEach(function (p) { provs[p.id] = p; });
  return profs.map(function (x) { var pv = provs[x.provider], off = pv ? !pv.enabled : false; return [x.id, x.model_name + " · " + stProvName(x.provider) + (off ? " · провайдер выключен" : ""), off]; });
}
function stDot(level) { return el("i", { class: "st-dot lv-" + level, "aria-hidden": "true" }); }
function stRow(main, side) { return el("div", { class: "st-row" }, el("div", { class: "st-main" }, main), side ? el("div", { class: "st-side" }, side) : null); }
function stTitle(t, sub) { return [el("div", { class: "st-t", text: t }), sub ? el("div", { class: "st-s", text: sub }) : null]; }
function stBlock(title, sub, kids, right) {
  return el("section", { class: "block st-block" }, el("div", { class: "st-bh" }, el("h2", null, title, sub ? el("span", { class: "sub", text: sub }) : null), right || null), kids);
}
function stNote(text, kind) { return el("div", { class: "st-note " + (kind || ""), text: text }); }
function stSkel() { return el("div", { class: "skel", style: "height:120px" }); }
function stLoadErr(n) { return ST.err[n] ? stNote("Не удалось загрузить данные (" + n + "): " + (ST.err[n] === "network" ? "нет связи с дашбордом" : ST.err[n]), "bad") : null; }

// запустить действие: окно подтверждения откроется само
function stRun(action, args, opts) { ST.drawer = null; flowStart(action, args, "", opts || {}); renderSettings("user"); }

// ---------- боковая панель
function stDrawerShell(title, sub, body, foot) {
  return el("div", { class: "st-ov st-dr-ov", id: "st-dov" }, el("aside", { class: "st-drawer", role: "dialog", "aria-modal": "true", "aria-labelledby": "st-dt" },
    el("header", { class: "st-dh" }, el("div", null, el("h2", { id: "st-dt", text: title }), sub ? el("div", { class: "muted small", text: sub }) : null),
      el("button", { type: "button", class: "icon-btn", "aria-label": "Закрыть", onclick: null, id: "st-dx" }, sv("svg", { viewBox: "0 0 24 24", width: 16, height: 16, "aria-hidden": "true" }, sv("path", { d: "M6 6l12 12M18 6L6 18", fill: "none", stroke: "currentColor", "stroke-width": 2.4, "stroke-linecap": "round" })))),
    el("div", { class: "st-db" }, body), foot && foot.length ? el("footer", { class: "st-df" }, foot) : null));
}
function stDrawerOpen(kind, id) { ST.drawer = { kind: kind, id: id }; renderSettings("user"); }
function stDrawerClose() { ST.drawer = null; renderSettings("user"); }
function stDrawerDraw() {
  var host = $("st-drawer"), dr = ST.drawer;
  if (!dr) { put(host, []); host.hidden = true; return; }
  var B = ({ provider: stDrProvider, addprov: stDrAddProv, role: stDrRole, exchange: stDrExchange, profile: stDrProfile })[dr.kind];
  var built = B ? B(dr.id) : null;
  if (!built) { ST.drawer = null; put(host, []); host.hidden = true; return; }
  put(host, [stDrawerShell(built.title, built.sub, built.body, built.foot)]);
  host.hidden = false;
  $("st-dx").addEventListener("click", stDrawerClose);
  $("st-dov").addEventListener("mousedown", function (e) { if (e.target.id === "st-dov") stDrawerClose(); });
  if (!dr._focused) { dr._focused = true; var fi = host.querySelector("input, select"); if (fi) fi.focus(); }
}

// --- панель провайдера
function stDrProvider(id) {
  var p = ((stD("providers") || {}).providers || []).filter(function (x) { return x.id === id; })[0];
  if (!p) return null;
  var c = p.cred || {}, lim = p.limits || {}, body = [];
  if (c.needed) {
    var inp = el("input", { type: "password", id: "st-key", autocomplete: "new-password", spellcheck: "false", placeholder: c.present ? "новый ключ (заменит текущий)" : "вставьте ключ", "aria-label": "Ключ провайдера" });
    var msg = el("div", { class: "small", id: "st-keymsg", role: "status", text: ST.keyMsg });
    var go = stBtn(c.present ? "Заменить ключ" : "Сохранить ключ", function () {
      var v = inp.value; inp.value = "";
      if (!v) { msg.textContent = "Вставьте ключ в поле выше."; msg.className = "small neg"; return; }
      msg.textContent = "Сохраняю…"; msg.className = "small muted";
      ctlSendKey(p.id, v).then(function (j) {
        v = null;
        if (j.ok) { ST.keyMsg = "Ключ сохранён."; ST.at = {}; return stFetch(true).then(function () { renderSettings("user"); }); }
        msg.className = "small neg";
        msg.textContent = (j.code === "E_ELEVATE" || j._http === 401) ? "Нужен токен: любое действие здесь запросит его, например «Проверить настройки»." : (j.msg || "Ключ не принят.");
      }).catch(function () { msg.textContent = "Нет связи с дашбордом"; msg.className = "small neg"; });
    }, "primary");
    inp.addEventListener("keydown", function (e) { if (e.key === "Enter") go.click(); });
    body.push(el("div", { class: "st-sec" }, el("h3", { text: "Ключ" }), el("p", { class: "muted small", text: c.present ? "Сейчас сохранён ключ " + (c.mask || "…") + (c.perms_ok === false ? ". Файл читается другими пользователями — ограничьте права." : ".") : "Ключа нет: без него провайдера нельзя включить." }),
      inp, el("div", { class: "st-inl" }, go, c.present ? stBtn("Удалить ключ", function () { stRun("llm.provider", { op: "key_clear", id: p.id }); }) : null), msg,
      el("p", { class: "muted small", text: "Ключ нигде не показывается и не хранится в браузере. Ключи бирж здесь не вводятся." })));
  } else body.push(el("p", { class: "muted", text: "Локальный сервер: ключ не нужен." }));
  body.push(el("div", { class: "st-sec" }, el("h3", { text: "Модели" }), (p.models || []).length ? el("ul", { class: "st-list" }, p.models.map(function (m) {
    var pr = m.price || {};
    return el("li", null, el("b", { text: m.name }), el("span", { class: "muted small", text: "контекст " + (m.ctx ? Math.round(m.ctx / 1000) + "k" : "—") + " · $" + fx(pr.in_miss != null ? pr.in_miss : pr.in, 2) + " вход / $" + fx(pr.out, 2) + " выход за 1M токенов" }));
  })) : el("p", { class: "muted", text: "Моделей нет." })));
  body.push(el("div", { class: "st-sec" }, el("h3", { text: "Лимиты расхода" }), el("p", { class: "muted", text: "до $" + fx(lim.day_usd, 2) + " в день и $" + fx(lim.hour_usd, 2) + " в час · запросов в минуту: " + (lim.rpm != null ? lim.rpm : "—") })));
  if (!p.verified) body.push(stNote("Не проверен живым ключом. После ввода ключа проверьте в терминале: ./tb llm check --provider " + p.id + " --live", "warn"));
  var foot = [stBtn("Проверить настройки", function () { stRun("llm.provider", { op: "check", id: p.id }); })];
  if (!p.builtin) foot.push(stBtn("Удалить провайдера", function () { stRun("llm.provider", { op: "remove", id: p.id }); }));
  return { title: p.name || p.id, sub: p.base_url, body: body, foot: foot };
}
function stDrAddProv() {
  var free = ((stD("presets") || {}).presets || []).filter(function (x) { return !x.added; });
  if (!free.length) return { title: "Добавить провайдера", body: [el("p", { class: "muted", text: "Все шаблоны уже добавлены." })], foot: [] };
  var sel = stSelect(free.map(function (x) { return [x.id, x.name]; }), free[0].id, "st-preset"), note = el("p", { class: "muted small" });
  var upd = function () { var x = free.filter(function (q) { return q.id === sel.value; })[0]; note.textContent = x ? (x.note || "") : ""; };
  sel.addEventListener("change", upd); upd();
  return { title: "Добавить провайдера", sub: "из готовых шаблонов", body: [stField("Шаблон", sel), note, el("p", { class: "muted small", text: "Провайдер появится выключенным. Дальше: ввести ключ и включить." })],
    foot: [stBtn("Добавить", function () { stRun("llm.provider", { op: "add", id: sel.value, preset: sel.value }); }, "primary"), stBtn("Отмена", stDrawerClose)] };
}

// --- панель роли
function stDrRole(role) {
  var r = ((stD("roles") || {}).roles || []).filter(function (x) { return x.role === role; })[0];
  if (!r) return null;
  var opts = stProfileOpts();
  var locked = stLocked(r), rid = (r.scope === "team" ? r.team + "." : "shared.") + r.role, po = r.params_override || {};
  var fbs = [0, 1].map(function (i) { return stSelect([["", "— нет —"]].concat(opts), (r.fallback[i] || {}).id || "", "st-fb" + i); });
  var tmp = stInput(po.temperature != null ? po.temperature : "", "как в модели", "st-temp"), mx = stInput(po.max_tokens != null ? po.max_tokens : "", "как в модели", "st-max");
  var th = stSelect([["", "как в модели"]].concat(ST_THINK), po.thinking || "", "st-think"), bd = stInput(r.budget && r.budget.day_usd != null ? r.budget.day_usd : "", "без лимита", "st-bud");
  var err = el("p", { class: "neg small", role: "alert" });
  [fbs[0], fbs[1], tmp, mx, th, bd].forEach(function (x) { x.disabled = locked; });
  var body = [];
  if (locked) body.push(stNote("Идёт live: у этой роли можно менять только основную модель. Остальное — после остановки дня.", "warn"));
  body.push(el("div", { class: "st-sec" }, el("h3", { text: "Запасные модели" }), el("p", { class: "muted small", text: "Если основная не ответила, мозг пробует их по очереди." }), stField("Первая запасная", fbs[0]), stField("Вторая запасная", fbs[1])));
  body.push(el("div", { class: "st-sec" }, el("h3", { text: "Особые параметры роли" }), el("p", { class: "muted small", text: "Пусто — берётся из модели." }),
    el("div", { class: "st-two" }, stField("Температура", tmp, "0–2"), stField("Токенов ответа", mx, "16–65536")), stField("Глубина размышления", th), stField("Бюджет в день, $", bd), err));
  var foot = [stBtn("Сохранить", function () {
    var seen = {}, fb = [], bad = null, prim = (r.primary || {}).id;
    fbs.forEach(function (s) { if (s.value) { if (seen[s.value] || s.value === prim) bad = "Модели в цепочке не должны повторяться."; seen[s.value] = 1; fb.push(s.value); } });
    var t = tmp.value.trim() === "" ? null : stNum(tmp.value), m = mx.value.trim() === "" ? null : stNum(mx.value), b = bd.value.trim() === "" ? null : stNum(bd.value);
    if (bad) { err.textContent = bad; return; }
    if (t != null && (t < 0 || t > 2)) { err.textContent = "Температура от 0 до 2."; return; }
    if (m != null && (m < 16 || m > 65536)) { err.textContent = "Токенов ответа от 16 до 65536."; return; }
    if (b != null && b < 0) { err.textContent = "Бюджет не может быть отрицательным."; return; }
    var chainChanged = fb.join() !== (r.fallback || []).map(function (x) { return x.id; }).join();
    var optsChanged = JSON.stringify([t, m == null ? null : Math.round(m), th.value || null, b]) !== JSON.stringify([po.temperature != null ? po.temperature : null, po.max_tokens != null ? po.max_tokens : null, po.thinking || null, r.budget && r.budget.day_usd != null ? r.budget.day_usd : null]);
    if (!chainChanged && !optsChanged) { stDrawerClose(); return; }
    if (chainChanged) stRun("brain.role", { op: "chain", role: rid, primary: prim, fallback: fb });
    else stRun("brain.role", { op: "opts", role: rid, params: { temperature: t, max_tokens: m == null ? null : Math.round(m), thinking: th.value || null }, budget: { day_usd: b } });
  }, "primary"), stBtn("Отмена", stDrawerClose)];
  if (!locked && Object.keys(po).length) foot.push(stBtn("Сбросить параметры", function () { stRun("brain.role", { op: "opts_clear", role: rid }); }));
  return { title: stRoleName(r.role), sub: (ST_ROLE[r.role] || [])[1] || "", body: body, foot: foot };
}

// --- панель биржи
function stDrExchange(id) {
  var d = stD("exchanges") || {}, x = (d.exchanges || []).filter(function (q) { return q.id === id; })[0];
  if (!x) return null;
  var dis = !d.editable, mode = stSelect([["paper", "Бумага (paper)"], ["testnet", "Тестовая сеть (testnet)"], ["live", "Реальные деньги (LIVE)"]], x.mode, "st-mode"), dis2 = dis;
  var lim = stInput(x.limits && x.limits.max_open_positions != null ? x.limits.max_open_positions : "", "без лимита", "st-lim"), err = el("p", { class: "neg small", role: "alert" });
  mode.disabled = dis2; lim.disabled = dis;
  var body = [];
  if (dis) body.push(stNote("Платформа запущена: биржи меняются только при остановке. Остановите день в «Управлении».", "warn"));
  body.push(stField("Режим исполнителя", mode), stField("Максимум открытых позиций на этой бирже", lim, "пусто — без лимита, до 50"), err);
  body.push(el("p", { class: "muted small", text: "Ключи бирж вводятся только в терминале: ./tb setup. Изменения вступят в силу при следующем запуске платформы." }));
  var foot = [];
  var ops = function () {
    var v = lim.value.trim() === "" ? null : stNum(lim.value);
    if (v != null && (v < 0 || v > 50 || Math.round(v) !== v)) { err.textContent = "Лимит — целое число от 0 до 50."; return null; }
    return v;
  };
  foot.push(stBtn("Применить режим", function () { if (mode.value !== x.mode) stRun("exchange.set", { op: "mode", id: x.id, mode: mode.value }); else stDrawerClose(); }, "primary"));
  foot.push(stBtn("Применить лимит", function () { var v = ops(); if (v === null && lim.value.trim() !== "") return; stRun("exchange.set", { op: "limits", id: x.id, max_open_positions: v }); }));
  if (!x.active) foot.push(stBtn("Сделать основной", function () { stRun("exchange.set", { op: "activate", id: x.id }); }));
  foot.forEach(function (b) { b.disabled = dis; });
  return { title: x.name || x.id, sub: x.active ? "основная биржа" : "", body: body, foot: foot };
}

// --- панель профиля
function stDrProfile(id) {
  var pf = id === "new" ? null : stProf(id), provs = (stD("providers") || {}).providers || [];
  if (id !== "new" && !pf) return null;
  var pa = (pf && pf.params) || {}, idIn = stInput(pf ? pf.id : "", "например, ds-flash", "st-pf-id");
  if (pf) idIn.disabled = true;
  var ps = stSelect(provs.map(function (x) { return [x.id, x.name || x.id]; }), pf ? pf.provider : (provs[0] || {}).id, "st-pf-prov");
  var fill = function () { var p = provs.filter(function (x) { return x.id === ps.value; })[0]; ms.replaceChildren(); ((p && p.models) || []).forEach(function (m) { var o = el("option", { value: m.id, text: m.name }); if (pf && m.id === pf.model) o.selected = true; ms.appendChild(o); }); };
  var ms = el("select", { id: "st-pf-model" }); fill(); ps.addEventListener("change", fill);
  var t = stInput(pa.temperature != null ? pa.temperature : 0.1, "0–2", "st-pf-temp"), mt = stInput(pa.max_out_tok != null ? pa.max_out_tok : 2500, "16–65536", "st-pf-max");
  var th = stSelect(ST_THINK, pa.thinking || "off", "st-pf-think"), err = el("p", { class: "neg small", role: "alert" });
  var foot = [stBtn(pf ? "Сохранить" : "Создать", function () {
    var nid = idIn.value.trim().toLowerCase(), temp = stNum(t.value), mx = stNum(mt.value);
    if (!/^[a-z0-9][a-z0-9_-]{0,29}$/.test(nid)) { err.textContent = "Название: латиница, цифры, «-» и «_», до 30 символов."; return; }
    if (temp == null || temp < 0 || temp > 2) { err.textContent = "Температура от 0 до 2."; return; }
    if (mx == null || mx < 16 || mx > 65536) { err.textContent = "Токенов ответа от 16 до 65536."; return; }
    if (!ms.value) { err.textContent = "Выберите модель."; return; }
    stRun("brain.profile", { op: "set", id: nid, provider: ps.value, model: ms.value, params: { temperature: temp, max_tokens: Math.round(mx), thinking: th.value } });
  }, "primary"), stBtn("Отмена", stDrawerClose)];
  if (pf && !pf.builtin) foot.push(stBtn("Удалить", function () { stRun("brain.profile", { op: "delete", id: pf.id }); }));
  return { title: pf ? "Профиль «" + pf.id + "»" : "Новый профиль", sub: "набор: провайдер, модель и параметры", foot: foot,
    body: [stField("Название", idIn), stField("Провайдер", ps), stField("Модель", ms), el("div", { class: "st-two" }, stField("Температура", t, "0–2"), stField("Токенов ответа", mt)), stField("Глубина размышления", th), err] };
}

// ---------- страница
function stSummary() {
  var o = stD("overview");
  if (!o) return stSkel();
  var kids = [];
  var p = o.providers || {};
  if ((p.no_cred || []).length) kids.push(stNote("Включены без ключа: " + p.no_cred.map(stProvName).join(", ") + ". Введите ключ в блоке «Подключения ИИ».", "warn"));
  if (o.source === "untrusted") kids.push(stNote("Подпись настроек мозга не прошла проверку: действует файл config/brain.json.", "bad"));
  var errs = (o.errors || []).filter(function (e, i, a) { return a.indexOf(e) === i; });
  if (errs.length) kids.push(el("details", { class: "st-note warn st-errs" }, el("summary", { text: "Замечания к конфигурации: " + errs.length + " (нажмите, чтобы посмотреть)" }), el("ul", { class: "st-why" }, errs.map(function (e) { return el("li", { text: String(e) }); }))));
  if (!stAuthed()) kids.push(stNote("Смотреть можно без входа. Для изменений понадобится токен: окно с запросом появится само.", ""));
  return kids;
}
function stRolesBlock() {
  var d = stD("roles"), profs = stD("profiles");
  if (stLoadErr("roles")) return stBlock("Кто за что отвечает", null, stLoadErr("roles"));
  if (!d || !profs) return stBlock("Кто за что отвечает", null, stSkel());
  var opts = stProfileOpts();
  var rows = d.roles.map(function (r) {
    var sel = stSelect(opts, (r.primary || {}).id, "st-sel-" + r.role), locked = stLocked(r), nm = ST_ROLE[r.role] || [r.role, ""];
    sel.setAttribute("aria-label", "Основная модель роли " + nm[0]);
    sel.disabled = false;
    sel.addEventListener("change", function () {
      var rid = (r.scope === "team" ? r.team + "." : "shared.") + r.role;
      stRun("brain.role", { op: "chain", role: rid, primary: sel.value });
    });
    var fb = (r.fallback || []).map(function (f) { return f.model_name; });
    var t = r.today || {};
    return el("div", { class: "st-row st-role" },
      el("div", { class: "st-main" }, el("div", { class: "st-t" }, nm[0], locked ? chip("заблокирована в live", "bad") : null), el("div", { class: "st-s", text: nm[1] + (fb.length ? " · запасная: " + fb.join(", ") : "") + (t.calls ? " · сегодня $" + fx(t.usd, 3) : "") })),
      el("div", { class: "st-side" }, sel, el("button", { type: "button", class: "btn", text: "Ещё", onclick: null, "data-role": r.role })));
  });
  var blk = stBlock("Кто за что отвечает", "какая модель решает каждую задачу", el("div", { class: "st-rows" }, rows));
  blk.querySelectorAll("button[data-role]").forEach(function (b) { b.addEventListener("click", function () { stDrawerOpen("role", b.getAttribute("data-role")); }); });
  return blk;
}
function stProvStatus(p) {
  var c = p.cred || {};
  if (p.error) return ["bad", "Ошибка настройки"];
  if (c.needed && !c.present) return ["warn", "Нет ключа"];
  return p.enabled ? ["ok", "Работает"] : ["off", "Выключен"];
}
function stProvidersBlock() {
  var d = stD("providers");
  if (stLoadErr("providers")) return stBlock("Подключения ИИ", null, stLoadErr("providers"));
  if (!d) return stBlock("Подключения ИИ", null, stSkel());
  var rows = d.providers.map(function (p) {
    var s = stProvStatus(p), c = p.cred || {}, act;
    if (c.needed && !c.present) act = stBtn("Ввести ключ", function () { stDrawerOpen("provider", p.id); }, "primary");
    else if (!p.enabled) act = stBtn("Включить", function () { stRun("llm.provider", { op: "enable", id: p.id }); }, "primary");
    else act = stBtn("Выключить", function () { stRun("llm.provider", { op: "disable", id: p.id }); });
    return el("div", { class: "st-row" }, el("div", { class: "st-main" }, el("div", { class: "st-t" }, stDot(s[0]), p.name || p.id), el("div", { class: "st-s", text: s[1] + " · " + (p.models || []).length + " " + plural((p.models || []).length, "модель", "модели", "моделей") + (p.verified ? "" : " · не проверен живым ключом") })),
      el("div", { class: "st-side" }, act, stBtn("Подробнее", function () { stDrawerOpen("provider", p.id); })));
  });
  var free = ((stD("presets") || {}).presets || []).filter(function (x) { return !x.added; });
  var right = free.length ? stBtn("Добавить", function () { stDrawerOpen("addprov"); }) : null;
  return stBlock("Подключения ИИ", "откуда мозг берёт модели", el("div", { class: "st-rows" }, rows), right);
}
function stExchangeBlock() {
  var d = stD("exchanges");
  if (stLoadErr("exchanges")) return stBlock("Биржа", null, stLoadErr("exchanges"));
  if (!d) return stBlock("Биржа", null, stSkel());
  var x = d.exchanges.filter(function (q) { return q.active; })[0] || d.exchanges.filter(function (q) { return q.implemented; })[0];
  if (!x) return stBlock("Биржа", null, stNote("Нет доступных бирж."));
  var ex = x.exec || {}, modeName = ({ paper: "бумага", testnet: "тестовая сеть", live: "реальные деньги" })[x.mode] || x.mode;
  var row = el("div", { class: "st-row" }, el("div", { class: "st-main" }, el("div", { class: "st-t" }, stDot(ex.alive ? "ok" : "off"), x.name || x.id, chip(modeName, x.mode === "live" ? "bad" : "")),
    el("div", { class: "st-s", text: "исполнитель " + (ex.alive ? "работает" : "не запущен") + (x.limits && x.limits.max_open_positions != null ? " · позиций до " + x.limits.max_open_positions : "") })),
    el("div", { class: "st-side" }, stBtn("Изменить", function () { stDrawerOpen("exchange", x.id); })));
  return stBlock("Биржа", null, [d.editable ? null : stNote("Платформа запущена: менять биржу можно только после остановки.", ""), row]);
}
function stUsageBlock() {
  var d = stD("usage");
  var seg = el("div", { class: "seg", role: "group", "aria-label": "Период" });
  segment(seg, [["1", "Сутки"], ["7", "7 дней"], ["30", "30 дней"]], ST.days, function (v) { ST.days = v; ST.at.usage = 0; stFetch(false); });
  if (stLoadErr("usage")) return stBlock("Расход на ИИ", null, stLoadErr("usage"), seg);
  if (!d) return stBlock("Расход на ИИ", null, stSkel(), seg);
  var t = d.total || {}, b = d.budget || {}, groups = d.groups || [], mx = Math.max.apply(null, groups.map(function (g) { return g.usd; }).concat([1e-9]));
  var bm = { ok: ["ok", "в рамках лимитов"], warn: ["warn", "лимит почти исчерпан"], exhausted: ["bad", "лимит исчерпан"] }[b.state] || ["off", "—"];
  var head = el("div", { class: "st-use" }, el("div", null, el("div", { class: "st-big", text: "$" + fx(t.usd, 3) }), el("div", { class: "muted small", text: "за период · " + (t.calls || 0) + " " + plural(t.calls || 0, "вызов", "вызова", "вызовов") })),
    el("div", { class: "st-s" }, stDot(bm[0]), "Бюджет: " + bm[1]));
  var list = groups.length ? el("ul", { class: "hb st-hb" }, groups.map(function (g) {
    return el("li", null, el("span", { class: "hb-n", text: g.id }), el("span", { class: "hb-track", "aria-hidden": "true" }, el("i", { style: "width:" + Math.max(3, g.usd / mx * 100).toFixed(1) + "%" })),
      el("span", { class: "hb-v", text: g.calls + " " + plural(g.calls, "вызов", "вызова", "вызовов") }), el("span", { class: "hb-p", text: "$" + fx(g.usd, 3) }));
  })) : el("p", { class: "muted", text: "За период запросов не было." });
  return stBlock("Расход на ИИ", "по моделям", [head, list], seg);
}
function stAdvancedBlock() {
  var det = el("details", { class: "block st-block st-adv" }, el("summary", null, el("h2", null, "Дополнительно", el("span", { class: "sub", text: "профили моделей и история изменений" }))));
  det.open = !!ST.advOpen;
  det.addEventListener("toggle", function () { ST.advOpen = det.open; });
  var pr = stD("profiles"), rv = stD("revisions"), kids = [];
  if (pr) {
    kids.push(el("h3", { text: "Профили моделей" }), el("p", { class: "muted small", text: "Профиль — это модель с настройками. Его выбирают для ролей выше." }));
    kids.push(el("div", { class: "st-rows" }, pr.profiles.map(function (x) {
      var uses = (x.used_by || []).filter(function (q, i, a) { return a.indexOf(q) === i; }).length;
      return el("div", { class: "st-row" }, el("div", { class: "st-main" }, el("div", { class: "st-t", text: x.id }), el("div", { class: "st-s", text: x.model_name + " · " + stProvName(x.provider) + (uses ? " · используется в ролях: " + uses : " · не используется") })),
        el("div", { class: "st-side" }, stBtn("Изменить", function () { stDrawerOpen("profile", x.id); }), stBtn("Всем ролям", function () { stRun("brain.use", { profile: x.id, all: true }); })));
    })), el("div", { class: "st-addrow" }, stBtn("Новый профиль", function () { stDrawerOpen("profile", "new"); })));
  }
  if (rv) {
    var older = (rv.revisions || []).filter(function (n) { return n !== rv.rev; });
    kids.push(el("h3", { text: "История изменений" }), el("p", { class: "muted small", text: "Сейчас действует ревизия r" + rv.rev + (rv.chain_ok ? "" : ". Журнал изменений повреждён.") }));
    if (older.length) {
      var sel = stSelect(older.map(function (n) { return [n, "ревизия r" + n]; }), older[0], "st-rb");
      kids.push(el("div", { class: "st-inl" }, sel, stBtn("Откатить", function () { stRun("brain.rollback", { to: parseInt(sel.value, 10) }); })));
    }
    var sw = (rv.swaps || []).slice().reverse().slice(0, 6);
    if (sw.length) kids.push(el("ul", { class: "lst st-log" }, sw.map(function (s) {
      var what = s.kind === "reload" ? "настройки перечитаны" : s.kind === "use" ? "модель роли: " + (s.target || []).join(", ") + (s.from !== "-" ? " (" + s.from + " → " + s.to + ")" : "") : s.kind + " " + (s.target || []).join(", ");
      return el("li", { class: "ev" }, el("span", { class: "t", text: dt(s.ts) }), el("span", { text: what }));
    })));
  }
  det.appendChild(el("div", { class: "st-adv-in" }, kids.length ? kids : stSkel()));
  return det;
}

var _stSig = "";
function renderSettings(mode) {
  var host = $("tab-settings");
  if (!host) return;
  if (!host._built) {
    host._built = true;
    put(host, [el("div", { id: "st-top" }), el("div", { id: "st-page" }), el("div", { id: "st-drawer", hidden: true }), el("div", { id: "st-modal", hidden: true })]);
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || ui.tab !== "settings" || e.stDone || CTL.flow || CTL.pick) return;
      if (ST.drawer) stDrawerClose();
    });
    stFetch(true);
  }
  if (mode === undefined) stFetch(false);
  var calm = mode !== "user";
  if (calm && (ST.drawer || CTL.flow || typing(host, function () { renderSettings("user"); }))) { stFlowDraw(); return; }
  if (calm) { var sig = JSON.stringify([ST.data, ST.err, stAuthed(), ST.days]); if (sig === _stSig && host._drawn) { stFlowDraw(); return; } _stSig = sig; }
  host._drawn = true;
  put("st-top", stSummary());
  put("st-page", [stRolesBlock(), stProvidersBlock(), stExchangeBlock(), stUsageBlock(), stAdvancedBlock()]);
  stDrawerDraw();
  stFlowDraw();
}
registerTab("settings", "Настройки", "M4 7h10M18 7h2M4 17h2M10 17h10", renderSettings);
