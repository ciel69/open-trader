"use strict";
// Вкладка «Новости» (WP13b): очищенные заголовки для ЧЕЛОВЕКА + структурная разметка карантинного читателя, статус источников и календаря, расход читателя.
// Только чтение (/api/intel/news). Любой текст третьих лиц вставляется через textContent (el/add из core.js); ссылки не кликабельны (показан только домен);
// заголовок не интерпретируется как HTML или markdown. Торговый мозг этот текст не видит: он получает только enum и числа (digest.json -> brief).

var NEWS_TYPE = { listing: "листинг", delisting: "делистинг", maintenance: "обслуживание", hack: "взлом", regulation: "регулятор", macro: "макро", etf: "ETF",
  unlock: "разблокировка", partnership: "партнёрство", price_move: "движение цены", rumor: "слух", upgrade: "обновление", outage: "сбой", other: "другое", suspicious: "подозрительно" };
var NEWS_DIR = { up: "↑ вверх", down: "↓ вниз", neutral: "→ нейтрально", unknown: "? неизвестно" };
var NEWS_STATUS = { ok: "работает", idle: "новых нет", off: "выключен", limit: "лимит расхода", paused: "пауза после ошибок формата", llm_down: "LLM недоступен" };
var NEWS_LIMIT = 60;
var newsHost = null;

function newsImpChip(c) {
  if (!c || c.st === "fail") return chip("не размечено", "");
  if (c.st === "susp") return chip("подозрение", "bad", "Текст не передавался модели: сработал фильтр инъекций");
  var t = "важность " + c.imp + (c.ir !== c.imp ? " (читатель: " + c.ir + ", понижено правилом подтверждения)" : "");
  return chip("важн. " + c.imp, c.imp >= 4 ? "bad" : c.imp >= 3 ? "warn" : "", t);
}
function newsClsCell(it) {
  var c = it.cls;
  if (it.struct) return el("span", { class: "muted small", text: "структурно (facts)" });
  if (!c) return el("span", { class: "muted small", text: it.susp ? "без модели" : "ждёт читателя" });
  if (c.st === "fail") return el("span", { class: "muted small", text: "формат не принят" });
  if (c.st === "susp") return chip("подозрение", "bad", "Текст не передавался модели: сработал фильтр инъекций");
  return el("span", null, chip(NEWS_TYPE[c.type] || c.type, c.st === "susp" ? "bad" : ""), " ", newsImpChip(c), c.st === "ok" ? [" ", el("span", { class: "small", text: NEWS_DIR[c.dir] || "" })] : null,
    c.syms && c.syms.length ? [" ", el("b", { class: "small", text: c.syms.join(" ") })] : null);
}
function newsDetail(it) {
  var c = it.cls, d = el("div", { class: "hd-det" });
  if (it.snip) d.appendChild(el("div", { class: "note-box", text: it.snip }));
  var pairs = [["Источник", it.src + (it.domain ? " · " + it.domain : "")], ["Возраст", ago(it.age_s)], ["Подтверждений с других доменов", String(it.seen || 0)], ["Идентификатор", it.ref]];
  if (c && c.st === "ok") {
    pairs.push(["Разметка читателя", (NEWS_TYPE[c.type] || c.type) + ", важность " + c.imp + (c.ir !== c.imp ? " (читатель дал " + c.ir + ")" : "") + ", " + (NEWS_DIR[c.dir] || c.dir)]);
    pairs.push(["Актуально", dur(c.ttl_min * 60) + ", уверенность " + fx(c.conf, 2)]);
  }
  if (it.susp) pairs.push(["Фильтр инъекций", "сработал: текст не передавался модели, важность 1"]);
  d.appendChild(kvgrid(pairs));
  return d;
}
function newsReaderBlock(R) {
  var st = R.stats || {}, s = R.status || (R.enabled ? "ждёт первого цикла" : "off");
  var label = NEWS_STATUS[s] || s;
  var chips = [chip(R.enabled ? label : "выключен", !R.enabled ? "" : (s === "ok" || s === "idle") ? "ok" : "warn")];
  if (R.stale) chips.push(chip("разметка устарела", "warn"));
  var pairs = [["Профиль", R.profile || "–"], ["Вызовов за час", (st.calls_h != null ? st.calls_h : "–") + " из " + ((R.limits && R.limits.calls_h) || "–")],
    ["Расход за час / сутки", (isn(st.usd_h) ? fx(st.usd_h, 4) : "–") + " / " + (isn(st.usd_day) ? fx(st.usd_day, 4) : "–") + " USD (потолок суток " + ((R.limits && R.limits.usd_day) || "–") + ")"],
    ["Ждут разметки", st.pending != null ? String(st.pending) : "–"], ["Размечено / не принято / подозрительных", (st.ok_n || 0) + " / " + (st.fail_n || 0) + " / " + (st.susp_n || 0)]];
  if (st.err) pairs.push(["Последняя ошибка LLM", st.err]);
  if (st.pause_until) pairs.push(["Пауза до", dt(st.pause_until)]);
  return el("section", { class: "block" }, el("h2", null, "Карантинный читатель ", el("span", { class: "sub", text: "модель видит только очищенный текст и отвечает JSON по схеме; мозг получает лишь перечисления и числа" })),
    el("div", { class: "chips" }, chips),
    R.enabled ? null : el("p", { class: "muted small", text: "Читатель выключен: config/intel.json, reader.enabled. Заголовки ниже показаны без разметки." }),
    kvgrid(pairs));
}
function newsSourcesBlock(J) {
  var kids = J.sources.map(function (s) {
    var bad = s.degraded || !s.ok;
    var t = s.id + " · " + (s.degraded ? "деградирован" : s.ok ? "работает" : "ошибка") + " · " + (s.verified ? "проверен" : "не проверен") + (s.age_s != null ? " · " + dur(s.age_s) : "");
    return chip(t, bad ? "bad" : s.verified ? "ok" : "warn");
  });
  var cal = J.calendar || {}, evs = (cal.next || []).map(function (e) {
    return el("li", null, chip(e.type, e.verified ? "ok" : "warn"), el("span", null, " через ", dur((e.in_min || 0) * 60), " · влияние ", e.impact, e.verified ? " · дата проверена" : " · дата НЕ проверена"));
  });
  return el("section", { class: "block" }, el("h2", { text: "Источники и календарь" }),
    kids.length ? el("div", { class: "chips" }, kids) : el("div", { class: "muted", text: "Сервис intel ещё не публиковал статусы (./tb up --services ...,intel)." }),
    cal.blackout ? el("p", { class: "small" }, chip("blackout", "warn"), " окно вокруг ", el("b", { text: cal.blackout.name || "события" })) : null,
    evs.length ? el("ul", { class: "lst" }, evs) : el("div", { class: "muted small", text: "Ближайших событий календаря нет." }));
}
function newsTable(J) {
  if (!J.items.length) return el("div", { class: "empty" }, el("b", { text: "Заголовков пока нет" }), el("span", { text: "Сервис intel собирает их из RSS; запуск: ./tb up --services exec,scan,dispatch,dash,intel" }));
  var tbl = el("table", { class: "rt" }, el("thead", null, el("tr", null, ["Возраст", "Источник", "Заголовок", "Разметка читателя"].map(function (h) { return el("th", { text: h }); }))));
  J.items.forEach(function (it) {
    tbl.appendChild(rowGroup("news:" + it.ref, [
      { t: ago(it.age_s), c: "muted-c", l: "Возраст" },
      { t: it.src, l: "Источник" },
      { t: el("span", { class: "clamp", text: it.title }), c: "wrapc", l: "Заголовок" },
      { t: newsClsCell(it), l: "Разметка" }
    ], 4, function (td) { td.appendChild(newsDetail(it)); }));
  });
  return el("div", { class: "scroll-x" }, tbl);
}
function newsApply(J, fresh, err) {
  if (!newsHost) return;
  if (!J) {
    if (!newsHost.firstChild || err) put(newsHost, el("div", { class: "empty" }, el("b", { text: err ? "Не удалось получить новости" : "Загрузка…" }), err ? el("span", { text: err }) : null));
    return;
  }
  var sig = JSON.stringify(J, function (k, v) { return k === "age_s" || k === "now" || k === "age" ? undefined : v; });
  var now = performance.now();
  if (newsHost._sig === sig && (!fresh || now - (newsHost._at || 0) < 15000)) return;     // возраст в подписи не участвует: перерисовка не чаще раза в 15 с
  newsHost._sig = sig; newsHost._at = now;
  put(newsHost, [newsReaderBlock(J.reader || {}), newsSourcesBlock(J),
    el("section", { class: "block" }, el("h2", null, "Заголовки ", el("span", { class: "sub", text: "очищены от разметки, ссылок и невидимых символов; только для чтения" })), newsTable(J))]);
}
function renderIntel() {
  var tab = $("tab-intel");
  if (!newsHost || !newsHost.isConnected) { newsHost = el("div", { id: "intel-body" }); tab.appendChild(newsHost); }
  fetchTab("intel", "/api/intel/news?limit=" + NEWS_LIMIT, newsApply);
}

registerTab("intel", "Новости", "M5 4h11l3 3v13H5zM8 9h8M8 13h8M8 17h5", renderIntel);
