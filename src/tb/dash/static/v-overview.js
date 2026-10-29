"use strict";
// Вкладка «Обзор»: капитал и PnL, активные позиции, ждущие лимиты, кривая PnL, последние сделки.

// ---------- главные числа
function kpiTile(id, lab, val, valCls, subs, help) {
  return el("div", { class: "kpi" },
    el("div", { class: "lab" }, help ? tip(lab, help) : lab),
    el("div", { class: "val " + (valCls || "") }, val),
    el("div", { class: "det" }, subs));
}
function renderKpis() {
  var a = S.account, sm = S.summary || { today: {}, total: {} }, td = sm.today, tt = sm.total;
  var vs = Object.keys(a.venues || {}).filter(function (n) { return a.venues[n].equity; }).map(function (n) { return venueLabel(n) + " " + fx(a.venues[n].equity); });
  var free = Object.keys(a.venues || {}).reduce(function (s, n) { return s + (a.venues[n].free || 0); }, 0);
  var items = [
    { id: "risk", sig: [S.risk_room], build: function () {
      var R = S.risk_room, d = R && R.rows.filter(function (r) { return r.id === "daily"; })[0];
      var orisk = d && isn(d.open_risk) ? d.open_risk : 0, lim = d && isn(d.limit) && d.limit > 0 ? d.limit : null;
      return kpiTile("risk", "Под риском", fx(orisk, 2) + NB + "USDT", orisk > 0 ? "" : "muted", [
        lim ? pc(orisk / lim * 100, 0) + " лимита дня " + fx(lim, 2) : "открытых позиций и ждущих лимитов", R && R.allowed && R.next_risk ? el("span", { title: "Столько можно потерять на новой сделке (убыток до стопа)" }, " · новая сделка до " + fx(R.next_risk.usdt, 2)) : null, R && !R.allowed ? el("span", { class: "neg", text: " · входы запрещены" }) : null]);
    } },
    { id: "eq", sig: [a.equity, free, vs.join(), a.sync], build: function () {
      return kpiTile("eq", "Капитал", fx(a.equity) + NB + "USDT", "", [
        el("span", { text: "свободно " + fx(free) }), " · ", isn(a.snapshot_age_s) ? [el("span", { class: a.snapshot_age_s > 900 ? "warn-t" : "", title: "время снимка баланса: " + vs.join(" · ") }, "баланс ", tk("snap", a.snapshot_age_s, "ago"), a.sync && a.sync.stale ? " · устарело" : a.sync && a.sync.status === "ok" ? " · авто" : "")] : "нет снимка баланса",
        a.sync && a.sync.status !== "ok" ? el("span", { class: "warn-t", title: a.sync.reason || "" }, " · синхронизация: " + ({ no_key: "нет ключа read", mismatch: "ждёт подтверждения", error: "ошибка" }[a.sync.status] || a.sync.status)) : null]);
    } },
    { id: "today", sig: [td.pnl, td.n, td.wins, a.equity, tt.pnl], build: function () {
      var pct = isn(a.equity) && a.equity > 0 && td.n ? td.pnl / a.equity * 100 : null;
      return kpiTile("today", "PnL сегодня", td.n ? usd(td.pnl, 2) : "0.00" + NB + "USDT", td.n ? cls(td.pnl) : "muted", [
        (td.n ? td.n + " " + plural(td.n, "сделка", "сделки", "сделок") + (pct != null ? " · " + spc(pct, 1) + " капитала" : "") : "сегодня сделок не закрыто") + " · всего ", el("span", { class: cls(tt.pnl), text: usd(tt.pnl, 2) })]);
    } },
    { id: "dd", sig: [a.drawdown, a.hwm, a.ladder_mult], build: function () {
      return kpiTile("dd", "Просадка", pc(a.drawdown * 100, 1), "", [
        "от пика " + fx(a.hwm), " · ", tip("риск ×" + (isn(a.ladder_mult) ? fx(a.ladder_mult) : "–"), "ladder")], "dd");
    } }
  ];
  syncList($("kpis"), items, { key: function (i) { return i.id; }, sig: function (i) { return JSON.stringify(i.sig); }, build: function (i) { return i.build(); }, after: function (i) {
    if (i.id === "eq" && isn(a.snapshot_age_s)) tkReg("snap", a.snapshot_age_s, "ago");
  } });
  $("kpis").removeAttribute("aria-busy");
  $("kpis").querySelectorAll(".skel").forEach(function (n) { n.remove(); });
}

// ---------- позиции
function pathText(p) {
  if (!p) return "–";
  var s = (p.mae_pct > 0 ? "−" : "") + fx(p.mae_pct, 2) + "% / " + (p.mfe_pct > 0 ? "+" : "") + fx(p.mfe_pct, 2) + "%";
  if (isn(p.mae_r) && isn(p.mfe_r)) s += " (" + fx(p.mae_r, 2) + "R / " + fx(p.mfe_r, 2) + "R)";
  if (isn(p.path_coverage) && p.path_coverage < 0.8) s += " · замеры покрывают " + Math.round(p.path_coverage * 100) + "% времени";
  return s;
}
// Шкала «стоп — вход — цена — тейк»; серая полоса = диапазон цены с момента входа (по замерам сканера), если они есть
function gauge(t, c) {
  if (!c.gauge) return el("div", { class: "muted small", text: c.has_price ? "Нет стопа или тейка: шкалу не построить" : "" });
  var pp = c.gauge.price * 100, ep = c.gauge.entry * 100, lo = Math.min(pp, ep), hi = Math.max(pp, ep), p = t.path_live, band = null;
  if (p && isn(p.mae_pct) && isn(p.mfe_pct) && t.entry && t.sl && t.tp) {
    var sgn = t.side === "long" ? 1 : -1, a = t.entry * (1 - sgn * p.mae_pct / 100), b = t.entry * (1 + sgn * p.mfe_pct / 100), span = t.tp - t.sl;
    var pa = Math.max(0, Math.min(100, (a - t.sl) / span * 100)), pb = Math.max(0, Math.min(100, (b - t.sl) / span * 100));
    band = el("div", { class: "g-band", style: "left:" + Math.min(pa, pb) + "%;width:" + Math.abs(pb - pa) + "%", title: "диапазон цены с входа: " + pathText(p) });
  }
  var money = function (x) { return isn(x) ? " · " + sg(x, 2) : ""; };
  return el("div", { class: "gauge", role: "img", "aria-label": "Маршрут: стоп " + price(t.sl) + ", вход " + price(t.entry) + ", цена " + price(c.price) + ", тейк " + price(t.tp) },
    el("div", { class: "g-track" }, el("i", { class: "g-risk", style: "width:" + ep + "%" }), el("i", { class: "g-reward", style: "left:" + ep + "%;right:0" })), band,
    el("div", { class: "g-fill " + (c.pnl >= 0 ? "pos" : "neg"), style: "left:" + lo + "%;width:" + (hi - lo) + "%" }),
    el("div", { class: "g-entry", style: "left:" + ep + "%", title: "вход " + price(t.entry) }),
    el("div", { class: "g-lbl m", style: "left:" + Math.max(12, Math.min(88, ep)) + "%" }, el("span", { text: "вход " }), price(t.entry)),
    el("div", { class: "g-now", style: "left:" + pp + "%", title: "цена " + price(c.price) }),
    el("div", { class: "g-lbl l" }, el("span", { text: "стоп " }), price(t.sl), el("b", { class: "neg", text: money(c.pnl_at_sl) })),
    el("div", { class: "g-lbl r" }, el("span", { text: "тейк " }), price(t.tp), el("b", { class: "pos", text: money(c.pnl_at_tp) })));
}
function posDetail(t, c, has) {
  var pairs = [
    ["Вход", price(t.entry)],
    ["Цена сейчас", has ? price(c.price) + (c.price_stale ? " (устарела)" : "") : "нет цены"],
    ["Плечо", isn(t.lev) ? t.lev + "×" : "–"], ["Маржа", fx(t.margin, 2) + " USDT"],
    ["Номинал", fx(t.notional, 2) + " USDT"], ["Риск до стопа", fx(t.risk_usdt, 3) + " USDT"],
    [tip("До стопа", "rr"), has && isn(c.to_sl_pct) ? fx(c.to_sl_pct, 2) + "% · итог " + sg(c.pnl_at_sl, 3) + " USDT" : "–"],
    ["До тейка", has && isn(c.to_tp_pct) ? fx(c.to_tp_pct, 2) + "% · итог " + sg(c.pnl_at_tp, 3) + " USDT" : "–"],
    ["Ликвидация (оценка)", isn(c.liq_est) ? price(c.liq_est) : "–"],
    ["Комиссия входа", fx(c.fee_open_usdt, 4) + " USDT" + (c.fee_open_known ? "" : " (оценка)")],
    has ? ["С комиссией выхода", usd(c.pnl_est_exit, 3) + " · " + tpc(c.pnl_pct_margin_est) + " к марже", cls(c.pnl_est_exit)] : null,
    has ? [tip("Движение цены", "move"), tpc(c.move_pct), cls(c.move_pct)] : null,
    t.path_live ? [el("span", null, tip("MAE", "mae"), " / ", tip("MFE", "mfe")), pathText(t.path_live)] : null,
    ["Сессия", (t.stype ? t.stype + " · " : "") + (t.session || "–")],
    ["Площадка", venueLabel(t.venue) + " · " + t.mode],
    ["Цена из", has ? (c.price_src || "?") + ", " + ago(c.price_age_s) : "–"]
  ];
  var d = el("div", { class: "pc-det" }, kvgrid(pairs));
  if (t.tags && t.tags.length) d.appendChild(el("div", { class: "chips" }, t.tags.map(function (g) { return chip(g); })));
  if (t.thesis) d.appendChild(el("div", { class: "thesis", text: t.thesis }));
  d.appendChild(el("div", { class: "muted small mono", text: t.id }));
  return d;
}
function posCard(t) {
  var c = t.calc || {}, has = c.has_price, key = "p:" + t.id, vp = t._vp || null;
  var top = el("div", { class: "pc-top" }, el("span", { class: "sym", text: t.sym || "?" }), chip((t.side || "?").toUpperCase() + (isn(t.lev) ? " " + t.lev + "×" : ""), "side"),
    el("span", { class: "pc-age muted small" }, "в сделке ", tk("held:" + t.id, t.held_s, "dur")));
  var main = has
    ? el("div", { class: "pc-main" },
        el("div", { class: "pc-big " + cls(c.pnl_pct_margin) }, tip(tpc(c.pnl_pct_margin), "margin", true)),
        el("div", { class: "pc-side" }, el("span", { class: "pc-usd " + cls(c.pnl), text: usd(c.pnl, 3) }), el("span", { class: "muted small " + cls(c.r_now) }, tip(rr(c.r_now), "r", true))))
    : el("div", { class: "pc-main" }, el("div", { class: "pc-big muted", text: "нет цены" }),
        el("div", { class: "muted small", text: "ни фид, ни сканер не дают цену " + (t.sym || "") + (S.price_feed && !S.price_feed.running ? ": " + S.price_feed.hint : "") }));
  var foot = has && isn(c.to_sl_pct) ? el("div", { class: "pc-foot small" },
    el("span", { class: "muted" }, "до стопа "), el("span", { text: fx(c.to_sl_pct, 2) + "%" }), el("span", { class: "muted" }, " · до тейка "), el("span", { text: isn(c.to_tp_pct) ? fx(c.to_tp_pct, 2) + "%" : "–" }),
    c.price_stale ? el("span", { class: "chip warn", text: "цена устарела" }) : null) : null;
  var btn = el("button", { type: "button", class: "pc-toggle", "aria-expanded": "false" }, el("span", { class: "cv", "aria-hidden": "true" }), "Подробнее");
  if (vp) top.appendChild(el("span", { class: "chip", title: "позиция ведётся планом " + vp.plan, text: "план " + planShort(vp.plan) }));
  var det = null, card = el("article", { class: "pcard" + (vp ? " has-plan" : "") }, top, main, gauge(t, c), foot, vp ? manageLine(vp) : null, btn);
  function setOpen(on) {
    if (on && !det) { det = posDetail(t, c, has); if (vp) det.insertBefore(managePanel(vp), det.firstChild); card.appendChild(det); }
    if (det) det.hidden = !on;
    card.classList.toggle("open", on);
    btn.setAttribute("aria-expanded", on ? "true" : "false"); btn.lastChild.textContent = on ? "Свернуть" : "Подробнее";
    if (on) ui.open[key] = true; else delete ui.open[key];
  }
  btn.addEventListener("click", function () { setOpen(btn.getAttribute("aria-expanded") !== "true"); });
  if (ui.open[key]) setOpen(true);
  return card;
}
function v2PosFor(t) {
  var v = V2(); if (!v) return null;
  var hit = null;
  v.positions.forEach(function (p) { if ((t.plan_id && p.plan === t.plan_id) || (p.journal_id && p.journal_id === t.id)) hit = p; });
  return hit;
}
function renderActive() {
  var list = S.open_trades.slice(), box = $("active"), v = V2();
  list.forEach(function (t) { t._vp = v2PosFor(t); });
  // позиции под планом, которых нет в журнале открытых сделок (например paper-планы): отдельные карточки, с учётом фильтра режима
  var known = {};
  list.forEach(function (t) { if (t._vp) known[t._vp.plan] = true; });
  var extra = v ? v.positions.filter(function (p) { return !known[p.plan] && (ui.mode === "all" || p.mode === ui.mode); }) : [];
  list = list.concat(extra.map(function (p) { return { id: "v2:" + p.plan, _v2only: true, _vp: p }; }));
  $("open-sum").replaceChildren();
  // плавающий результат считаем по тем же карточкам, что показаны ниже; результат частичных закрытий (сработавшие тейки) показываем отдельно:
  // раньше сервер складывал обе части, и зелёное число в заголовке не сходилось с суммой красных карточек
  var fl = 0, nfl = 0, rz = 0;
  list.forEach(function (t) {
    if (t._v2only) { if (isn(t._vp.upnl)) { fl += t._vp.upnl; nfl++; } return; }
    var c = t.calc || {};
    if (c.has_price && isn(c.pnl)) { fl += c.pnl; nfl++; }
    if (isn(c.realized_parts)) rz += c.realized_parts - (c.fees_parts || 0);
  });
  if (list.length) put("open-sum", [list.length + " · ", el("span", { class: cls(fl), title: "Плавающий результат открытых позиций (остаток после частичных закрытий)", text: nfl ? usd(fl, 3) : "" }),
    Math.abs(rz) >= 5e-4 ? el("span", { class: "muted", title: "Уже зафиксировано частичными закрытиями по тейкам (за вычетом комиссий); в карточках это не показано", text: " · зафиксировано " + usd(rz, 3) }) : null]);
  if (!list.length) put("open-sum", "");
  box.querySelectorAll(".skel,.empty").forEach(function (n) { n.remove(); });
  if (!list.length) {
    box.replaceChildren(el("div", { class: "empty" }, el("b", { text: "Открытых позиций нет" }), el("span", { text: "Когда агент откроет сделку, карточка появится здесь." + (filterIsDefault() ? "" : " Сейчас включён фильтр: " + S.filter.mode + " / " + S.filter.venue + ".") })));
    return;
  }
  syncList(box, list, { key: function (t) { return t.id; }, sig: function (t) { return sigOf(t, ["held_s", "price_age_s", "last_ts", "hold_left_s", "snap_age_s"]); }, build: function (t) { return t._v2only ? posCardV2(t._vp) : posCard(t); },
    after: function (t) { tkReg("held:" + t.id, t._v2only ? t._vp.held_s : t.held_s, "dur"); } });
}
function filterIsDefault() { return ui.mode === "live" && ui.venue === "all"; }

// ---------- ждущие лимиты
function renderPending() {
  var list = S.pending || [], tbl = $("pending"), empty = $("pend-empty");
  put("pend-sum", list.length ? "· " + list.length + " · риск " + fx(list.reduce(function (a, r) { return a + (r.risk_usdt || 0); }, 0), 2) + " USDT" : "");
  if (!list.length) {
    tbl.hidden = true; $("pend-block").classList.add("compact");
    emptyState(empty, "Ждущих лимитных заявок нет", "Лимитный вход ставится командой ./tb bybit trade open --limit-price ...");
    return;
  }
  empty.hidden = true; tbl.hidden = false; $("pend-block").classList.remove("compact");
  if (!tbl.tHead) {
    var th = el("thead", null, el("tr", null, ["Монета", "Лимит", "Сейчас", "До лимита", "Осталось"].map(function (h, i) { return el("th", { text: h, class: i ? "num" : "" }); })));
    tbl.appendChild(th);
  }
  syncList(tbl, list, { key: function (r) { return r.id; }, sig: function (r) { return sigOf(r, ["age_s", "left_s", "price_age_s"]); },
    after: function (r) { tkReg("pl:" + r.id, r.left_s, "left"); tkReg("pa:" + r.id, r.age_s, "dur"); },
    build: function (r) {
      return rowGroup("l:" + r.id, [
        { t: [el("b", { text: r.sym }), " ", chip((r.side || "?").toUpperCase(), "side")], l: "Монета" },
        { t: price(r.price), c: "num", l: "Лимит" },
        { t: isn(r.cur) ? price(r.cur) + (r.price_stale ? " (устар.)" : "") : "нет цены", c: "num", l: "Сейчас" },
        { t: isn(r.dist_pct) ? spc(r.dist_pct, 2) : "–", c: "num", l: "До лимита" },
        { t: tk("pl:" + r.id, r.left_s, "left"), c: "num", l: "Осталось" }
      ], 5, function (td) {
        td.appendChild(kvgrid([["Стоп", price(r.sl)], ["Тейк", price(r.tp)], ["Маржа × плечо", fx(r.margin, 2) + " USDT × " + (isn(r.lev) ? r.lev : "–")],
          ["Риск до стопа", fx(r.risk_usdt, 3) + " USDT"], ["Отмена выше / ниже", (isn(r.cancel_above) ? price(r.cancel_above) : "–") + " / " + (isn(r.cancel_below) ? price(r.cancel_below) : "–")],
          ["Возраст заявки", tk("pa:" + r.id, r.age_s, "dur")], ["Время жизни (ttl)", isn(r.ttl_min) ? r.ttl_min + " мин" : "–"], ["Идея", r.idea || "–"],
          ["Цена из", r.price_src ? r.price_src + ", " + ago(r.price_age_s) : "–"], ["Id", el("span", { class: "mono", text: r.id })]]));
      });
    } });
}

// ---------- кривая и последние сделки
function renderCurve() {
  var pts = S.curve || [];
  put("curve-sum", pts.length ? "· " + pts.length + " " + plural(pts.length, "сделка", "сделки", "сделок") + ", итого " + sg(pts[pts.length - 1].cum, 2) + " USDT" : "");
  lineChart($("chart"), pts);
}
function renderRecent() {
  var h = S.history.slice(0, 5), host = $("recent");
  host.querySelectorAll(".skel,.empty").forEach(function (n) { n.remove(); });
  var ul = host.querySelector("ul");
  if (!h.length) { if (ul) ul.remove(); host.appendChild(el("div", { class: "empty" }, el("b", { text: "Закрытых сделок пока нет" }), el("span", { text: "Здесь будут пять последних." }))); return; }
  if (!ul) { ul = el("ul", { class: "rl" }); host.appendChild(ul); }
  syncList(ul, h, { key: function (t) { return t.id; }, sig: function (t) { return sigOf(t); }, build: function (t) {
    var b = el("button", { type: "button", class: "rl-row", title: "Открыть разбор сделки" },
      el("span", { class: "rl-l" }, el("b", { text: t.sym || "?" }), " ", chip((t.side || "?").toUpperCase(), "side"), " ", el("span", { class: "muted small", text: t.reason || "" })),
      el("span", { class: "rl-r" }, el("b", { class: cls(t.pnl_pct_margin), text: tpc(t.pnl_pct_margin) }), el("span", { class: "small " + cls(t.pnl_usdt), text: sg(t.pnl_usdt, 3) }), el("span", { class: "muted small", text: dt(t.ts_close_epoch) })));
    b.addEventListener("click", function () { ui.open["h:" + t.id] = true; var ix = S.history.findIndex(function (x) { return x.id === t.id; });
      ui.hn = ix >= 15 ? "all" : ui.hn; goTab("trades"); });
    return el("li", null, b);
  } });
}
// ---------- запас до лимитов (числа считает model.risk_room по формулам guard/session/portfolio)
function roomVal(r) {
  var f = r.unit === "USDT" ? function (x) { return fx(Math.max(0, x), 2); } : function (x) { return String(Math.max(0, Math.round(x))); };
  if (r.id === "daily" && isn(r.realized_today)) {      // прибыль дня увеличивает запас: показываем слагаемые, а не «осталось X из Y» (X может быть больше Y)
    return "лимит " + fx(r.limit, 2) + " · сегодня " + sg(r.realized_today, 2) + " · запас " + f(r.room) + NB + "USDT";
  }
  return r.id === "stops" ? "серия " + f(r.used) + " из " + f(r.limit) : "осталось " + f(r.room) + " из " + f(r.limit) + (r.unit === "USDT" ? NB + "USDT" : "");
}
function roomRow(r, detailed) {
  var lab = r.help ? el("span", { class: "tip-h", tabindex: "0", "data-tip": r.help, "aria-label": r.label + ". " + r.help, text: r.label }) : r.label;
  var d = el("div", { class: "room-row" },
    el("div", { class: "room-hd" }, el("span", { class: "room-l" }, lab), el("span", { class: "room-v" }, stIcon(r.level), " ", roomVal(r))),
    el("div", { class: "rbar rb-" + r.level, role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(r.frac * 100)), "aria-label": r.label + ": запас " + Math.round(r.frac * 100) + "%" },
      el("i", { style: "width:" + Math.max(r.frac > 0 ? 2 : 0, r.frac * 100).toFixed(1) + "%" })));
  if (detailed) {
    d.appendChild(el("div", { class: "muted small", text: r.help }));
    var ex = [];
    if (r.id === "daily") ex = ["результат закрытых сегодня " + sg(r.realized_today, 2) + " USDT", "риск открытых и ждущих " + fx(r.open_risk, 2) + " USDT"];
    if (r.id === "session") ex = ["результат сессии " + sg(r.realized, 2) + " USDT", "риск открытых и ждущих " + fx(r.open_risk, 2) + " USDT"];
    if (r.id === "slots") ex = ["открыто " + r.open, "ждущих лимитов " + r.pending];
    if (ex.length) d.appendChild(el("div", { class: "small", text: ex.join(" · ") }));
  }
  return d;
}
// множитель по результатам зависит от типа сессии: без активной сессии показываем тип по умолчанию и честно говорим об этом
function perfNote(n) {
  var bt = n.perf_by_type || {}, ks = Object.keys(bt);
  if (!n.session_type) return "";
  var same = ks.every(function (k) { return bt[k] === bt[ks[0]]; });
  return (n.assumed_type ? " для " + n.session_type + ", сессии нет" : " для " + n.session_type) + (!same ? "; " + ks.map(function (k) { return k + " ×" + fx(bt[k]); }).join(", ") : "");
}
function roomView(R, detailed) {
  var nodes = [];
  nodes.push(el("div", { class: "room-st" }, stIcon(R.allowed ? "ok" : "bad"), el("b", { text: R.allowed ? "Входы разрешены" : "Входы запрещены" }),
    R.allowed ? null : el("span", { class: "muted", text: ": " + R.reasons.join("; ") })));
  R.rows.forEach(function (r) { nodes.push(roomRow(r, detailed)); });
  var n = R.next_risk;
  var head = el("span", null, tip("Риск на следующую сделку", n.help.replace(/^/, "")), ": ", el("b", { text: "до " + fx(n.usdt, 2) + " USDT" }));
  if (!detailed) {
    nodes.push(el("div", { class: "room-next" }, head, el("span", { class: "muted small", text: n.limited_by ? " (упирается в " + n.limited_by + ")" : " (множители: просадка ×" + fx(n.dd_mult) + ", результаты ×" + fx(n.perf_mult) + perfNote(n) + ")" })));
  } else {
    nodes.push(el("div", { class: "room-next" }, head));
    nodes.push(kvgrid([["Капитал", fx(n.equity, 2) + " USDT"], ["Доля риска на сделку", pc(n.frac * 100, 1)], ["Множитель просадки", "×" + fx(n.dd_mult)], ["Тип сессии для расчёта", n.session_type ? n.session_type + (n.assumed_type ? " (по умолчанию: активной сессии нет)" : "") : "–"],
      [tip("Множитель по результатам", "Пока закрыто меньше 30 сделок этого типа сессии, риск урезан до ×0.5 (испытательный срок); дальше ×0.25…×1.0 по профит-фактору и среднему R."), "×" + fx(n.perf_mult) + (n.perf_n != null ? " (закрыто сделок типа: " + n.perf_n + ")" : "")],
      ["Полный бюджет риска", fx(n.full, 3) + " USDT"], ["Разрешено сейчас", fx(n.usdt, 3) + " USDT" + (n.limited_by ? " (ограничено: " + n.limited_by + ")" : "")]]));
  }
  return nodes;
}
// Компактный вид для главного экрана: плитки лимитов в один ряд; галочки только когда запас не в порядке
var ROOM_SHORT = { session: "Бюджет сессии", daily: "Лимит дня", slots: "Слоты", stops: "Стопы подряд", trades: "Сделки", learn_trades: "Учёба" };
function roomTile(r) {
  var f = function (x) { return r.unit === "USDT" ? fx(Math.max(0, x), 2) : String(Math.max(0, Math.round(x))); };
  var main, sub;
  if (r.id === "daily") { main = f(r.room); sub = "из " + fx(r.limit, 2) + " USDT"; }
  else if (r.id === "stops") { main = f(r.used) + " / " + f(r.limit); sub = "стопов подряд"; }
  else { main = f(r.room); sub = "из " + f(r.limit) + (r.unit === "USDT" ? NB + "USDT" : ""); }
  var lab = r.help ? el("span", { class: "tip-h quiet", tabindex: "0", "data-tip": r.help, "aria-label": r.label + ". " + r.help, text: ROOM_SHORT[r.id] || r.label }) : (ROOM_SHORT[r.id] || r.label);
  return el("div", { class: "rt-tile rt-" + r.level, title: roomVal(r) },
    el("div", { class: "rt-l" }, lab, r.level === "ok" ? null : stIcon(r.level)),
    el("div", { class: "rt-v", text: main }), el("div", { class: "rt-s muted", text: sub }),
    el("div", { class: "rbar rb-" + r.level, role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(r.frac * 100)), "aria-label": r.label + ": запас " + Math.round(r.frac * 100) + "%" },
      el("i", { style: "width:" + Math.max(r.frac > 0 ? 2 : 0, r.frac * 100).toFixed(1) + "%" })));
}
function roomCompact(R) {
  return [R.allowed ? null : el("div", { class: "room-why neg small", text: R.reasons.join("; ") }), el("div", { class: "room-tiles" }, R.rows.map(roomTile))];
}
function renderRoom() {
  var R = S.risk_room, host = $("room");
  if (!R) { put(host, el("div", { class: "empty" }, el("b", { text: "Нет данных о лимитах" }), el("span", { text: "Не найден config/risk_profile.json." }))); put("room-sum", ""); return; }
  var min = R.rows.reduce(function (a, r) { return r.frac < a.frac ? r : a; }, R.rows[0]);
  put("room-sum", min && min.level !== "ok" ? "· теснее всего: " + min.label.toLowerCase() : "");
  var st = $("room-state");
  st.className = "room-allow " + (R.allowed ? "ok" : "bad");
  put(st, [el("i", { class: "dot", "aria-hidden": "true" }), R.allowed ? "Входы разрешены" : "Входы запрещены"]);
  if (changed(host, R)) put(host, roomCompact(R));
  renderPauses();
}
function renderOverview() { renderKpis(); renderPlansBlock(); renderRoom(); renderActive(); renderPending(); renderWake(); renderCurve(); renderRecent(); }

// ---------- сопровождение позиции плана (S.v2.positions): лесенка цен, правила, история действий исполнителя
var RULE_ST_RU = { waiting: "ждёт", done: "выполнено", skipped: "пропущено" };
function manageLine(vp) {
  var n = { waiting: 0, done: 0, skipped: 0 };
  vp.rules.forEach(function (r) { n[r.state] = (n[r.state] || 0) + 1; });
  var bits = [el("span", null, el("span", { class: "muted", text: "стоп " }), el("b", { text: price(vp.cur_stop) }), el("span", { class: "muted small", text: " (" + vp.stop_src + ")" }))];
  if (vp.next_stop) bits.push(el("span", { title: "правило " + vp.next_stop.rule + ": " + vp.next_stop.why }, "→ ", el("b", { class: "next-t", text: price(vp.next_stop.to) })));
  bits.push(el("span", { class: "muted small", text: "правила: " + n.done + " выполнено · " + n.waiting + " ждут" + (n.skipped ? " · " + n.skipped + " пропущено" : "") }));
  if (isn(vp.hold_left_s)) bits.push(el("span", { class: "muted small" }, "макс. срок: ", tk("hl:" + vp.plan, vp.hold_left_s, "left")));
  return el("div", { class: "pc-manage small" }, bits);
}
// Вертикальная шкала цен: тейки, вход, текущая цена, стоп (начальный и текущий), куда сдвинется стоп (пунктир)
function ladder(vp) {
  var sgn = vp.side === "long" ? 1 : -1, lv = [];
  function add(name, p, k) { if (isn(p) && p > 0) lv.push({ n: name, p: p, k: k }); }
  (vp.targets || []).forEach(function (t) { add(t.id + (t.state === "filled" ? " ✓" : t.state === "resting" ? " (лимит)" : ""), t.price, "tp" + (t.state === "filled" ? " done" : "")); });
  add("вход", vp.avg, "entry");
  add("стоп", vp.cur_stop, "stop");
  if (isn(vp.initial_stop) && vp.initial_stop !== vp.cur_stop) add("начальный стоп", vp.initial_stop, "stop0");
  if (vp.next_stop) add("стоп →", vp.next_stop.to, "next");
  add("цена", vp.price, "px");
  if (lv.length < 2) return el("div", { class: "muted small", text: "Нет цен для шкалы" });
  var hi = Math.max.apply(null, lv.map(function (x) { return x.p; })), lo = Math.min.apply(null, lv.map(function (x) { return x.p; })), pad = (hi - lo) * 0.08 || hi * 0.01;
  hi += pad; lo -= pad;
  var H = 250, AX = 62, kids = [];
  function y(p) { return 8 + (hi - p) / (hi - lo) * (H - 16); }
  // зона между стопом и входом (риск или зафиксированная прибыль) и между входом и ценой
  if (isn(vp.cur_stop) && isn(vp.avg)) {
    var locked = sgn * (vp.cur_stop - vp.avg) >= 0;
    kids.push(sv("rect", { x: AX - 6, width: 12, y: Math.min(y(vp.cur_stop), y(vp.avg)), height: Math.abs(y(vp.cur_stop) - y(vp.avg)), class: locked ? "z-lock" : "z-risk" }));
  }
  if (isn(vp.price) && isn(vp.avg)) kids.push(sv("rect", { x: AX - 3, width: 6, y: Math.min(y(vp.price), y(vp.avg)), height: Math.abs(y(vp.price) - y(vp.avg)), class: sgn * (vp.price - vp.avg) >= 0 ? "z-pos" : "z-neg" }));
  kids.push(sv("line", { x1: AX, x2: AX, y1: 4, y2: H - 4, class: "ld-axis" }));
  if (vp.next_stop && isn(vp.cur_stop)) kids.push(sv("line", { x1: AX + 14, x2: AX + 14, y1: y(vp.cur_stop), y2: y(vp.next_stop.to), class: "ld-next", "marker-end": "none" }));
  var sorted = lv.slice().sort(function (a, b) { return y(a.p) - y(b.p); }), last = -99;
  sorted.forEach(function (l) {
    var yy = y(l.p), ly = Math.max(yy, last + 13);
    last = ly;
    kids.push(sv("line", { x1: AX - 10, x2: AX + 10, y1: yy, y2: yy, class: "ld-t k-" + l.k.replace(" ", "-") }));
    if (Math.abs(ly - yy) > 1) kids.push(sv("line", { x1: AX + 10, x2: AX + 22, y1: yy, y2: ly, class: "ld-lead" }));
    var txt = sv("text", { x: AX + 26, y: ly + 4, class: "ld-l k-" + l.k.replace(" ", "-") });
    txt.appendChild(document.createTextNode(l.n + " " + price(l.p)));
    kids.push(txt);
    if (l.k === "px") kids.push(sv("circle", { cx: AX, cy: yy, r: 5, class: "ld-px" }));
  });
  return sv.apply(null, ["svg", { viewBox: "0 0 300 " + H, class: "ladder", role: "img",
    "aria-label": "Лесенка цен: " + sorted.map(function (l) { return l.n + " " + price(l.p); }).join(", ") }].concat(kids));
}
function rulesList(vp) {
  if (!vp.rules.length) return el("div", { class: "muted small", text: "Правил сопровождения в плане нет." });
  return el("ul", { class: "lst rules" }, vp.rules.map(function (r) {
    return el("li", null, el("span", { class: "chip rs-" + r.state, text: RULE_ST_RU[r.state] || r.state }),
      el("div", null, el("div", null, el("b", { class: "mono", text: r.id }), el("span", { class: "muted", text: " · " + r.do }), r.fired > 1 ? el("span", { class: "muted small", text: " ×" + r.fired }) : null),
        r.state === "waiting" && r.need ? el("div", { class: "small muted", text: r.need }) : null,
        r.state === "done" ? el("div", { class: "small muted", text: (r.detail ? r.detail + " · " : "") + (isn(r.ts) ? dts(r.ts) : "") }) : null,
        r.code ? el("div", { class: "small" }, (r.state === "skipped" ? "причина: " : "последний пропуск: "), el("span", { class: "chip warn", text: r.code })) : null));
  }));
}
function historyList(vp) {
  if (!vp.history.length) return el("div", { class: "muted small", text: "Действий пока нет." });
  return el("ul", { class: "lst" }, vp.history.map(function (h) {
    return el("li", { class: "ev" }, el("span", { class: "t", text: isn(h.t) ? dts(h.t) : "" }), el("span", null, actorChip(h.actor), " ", el("b", { text: h.action }), h.why ? el("span", { class: "muted", text: " · " + h.why }) : null, " ", resultChip(h)));
  }));
}
function managePanel(vp) {
  var pairs = [
    ["R сейчас", isn(vp.r_now) ? rr(vp.r_now) : "–", cls(vp.r_now)],
    [el("span", null, tip("MAE", "mae"), " / ", tip("MFE", "mfe")), (isn(vp.mae_r) ? fx(vp.mae_r, 2) : "–") + "R / " + (isn(vp.mfe_r) ? fx(vp.mfe_r, 2) : "–") + "R"],
    ["Единица риска (1R)", isn(vp.r_unit) ? price(vp.r_unit) : "–"],
    ["В сделке", tk("mh:" + vp.plan, vp.held_s, "dur")],
    isn(vp.hold_left_s) ? ["До макс. срока", tk("mhl:" + vp.plan, vp.hold_left_s, "left")] : null,
    ["Размер · средняя", (isn(vp.qty) ? vp.qty : "–") + " @ " + price(vp.avg)],
    isn(vp.liq) ? ["Ликвидация (биржа)", price(vp.liq)] : null,
    vp.snap_age_s != null ? ["Снимок биржи", ago(vp.snap_age_s)] : ["Снимок биржи", "нет: данные runtime плана"],
    isn(vp.realized_pnl) && vp.realized_pnl ? ["Реализовано по частям", usd(vp.realized_pnl, 3), cls(vp.realized_pnl)] : null
  ];
  return el("div", { class: "mg" },
    el("div", { class: "mg-l" }, el("h3", { text: "Лесенка цен" }), ladder(vp), el("div", { class: "muted small ld-note" }, "пунктир: куда сдвинется стоп по правилу, когда условие выполнится")),
    el("div", { class: "mg-r" }, el("h3", { text: "Правила сопровождения" }), rulesList(vp), el("h3", { text: "Действия исполнителя и мозга" }), historyList(vp), el("h3", { text: "Параметры" }), kvgrid(pairs),
      el("div", { class: "pl-links" }, tabLink("хронология решений", "decisions", { dp: vp.plan }, "pl-a"), tabLink("план на доске", "plans", {}, "pl-a"))));
}
// Карточка позиции без записи в журнале открытых сделок (например paper-план): данные только из плана и снимка исполнителя
function posCardV2(vp) {
  var key = "p:v2:" + vp.plan;
  var top = el("div", { class: "pc-top" }, el("span", { class: "sym", text: vp.sym || "?" }), sideChip(vp.side), vp.mode === "paper" ? chip("paper", "paper") : null,
    vp.exp ? chip(vp.exp + (vp.var ? "/" + vp.var : ""), "exp") : null, el("span", { class: "pc-age muted small" }, "в сделке ", tk("held:v2:" + vp.plan, vp.held_s, "dur")));
  var main = el("div", { class: "pc-main" }, el("div", { class: "pc-big " + cls(vp.r_now) }, isn(vp.r_now) ? rr(vp.r_now) : "–"),
    el("div", { class: "pc-side" }, isn(vp.upnl) ? el("span", { class: "pc-usd " + cls(vp.upnl), text: usd(vp.upnl, 3) }) : null, el("span", { class: "muted small", text: price(vp.avg) + " → " + price(vp.price) + (vp.price_stale ? " (устарела)" : "") })));
  var btn = el("button", { type: "button", class: "pc-toggle", "aria-expanded": "false" }, el("span", { class: "cv", "aria-hidden": "true" }), "Подробнее");
  var det = null, card = el("article", { class: "pcard has-plan" + (vp.mode === "paper" ? " is-paper" : "") }, top, main, vp.thesis ? el("div", { class: "thesis clamp", text: vp.thesis }) : null, manageLine(vp), btn);
  function setOpen(on) {
    if (on && !det) { det = managePanel(vp); card.appendChild(det); }
    if (det) det.hidden = !on;
    card.classList.toggle("open", on);
    btn.setAttribute("aria-expanded", on ? "true" : "false"); btn.lastChild.textContent = on ? "Свернуть" : "Подробнее";
    if (on) ui.open[key] = true; else delete ui.open[key];
  }
  btn.addEventListener("click", function () { setOpen(btn.getAttribute("aria-expanded") !== "true"); });
  if (ui.open[key]) setOpen(true);
  return card;
}

// ---------- блок «Планы в работе»: счётчики доски, ближайшие триггеры, дисциплина за сутки
function renderPlansBlock() {
  var v = V2(), host = $("plans-sum");
  if (!v) return;
  var C = v.plans.counts, day = v.decisions_day, live = v.plans.cards;
  $("plans-block").classList.toggle("compact", !v.plans.total);
  put("plans-h-sum", v.plans.total ? "· всего " + v.plans.total : "");
  if (!v.plans.total) {
    var eh = v.strip.exec;
    put(host, el("div", { class: "empty" }, el("b", { text: "Планов нет" }), el("span", { text: eh && eh.present ? "Исполнитель работает, активных и свежих планов нет." : "Слой планов (v2) не используется: исполнитель не запущен." })));
    return;
  }
  var near = live.filter(function (p) { return (p.col === "waiting" || p.col === "watching") && p.trigger && isn(p.trigger.dist_pct); })
    .sort(function (a, b) { return a.trigger.dist_pct - b.trigger.dist_pct; }).slice(0, 3);
  var sig = JSON.stringify([C, day, near.map(function (p) { return [p.id, p.trigger.dist_pct, p.trigger.text]; })]);
  if (host._s === sig) { near.forEach(function (p) { if (isn(p.trigger.eta_s)) tkReg("ov-eta:" + p.id, p.trigger.eta_s, "left"); }); return; }
  host._s = sig;
  var cnt = el("div", { class: "pl-counts" }, COLS.map(function (c) {
    return el("a", { class: "pl-cnt c-" + c[0] + (C[c[0]] ? "" : " zero"), href: hashFor("plans", {}), title: c[2] }, el("b", { text: String(C[c[0]] || 0) }), el("span", { text: c[1] }));
  }));
  var list = near.length ? el("ul", { class: "lst" }, near.map(function (p) {
    return el("li", null, el("div", { class: "grow" }, el("b", { text: p.sym }), " ", sideChip(p.side), " ", p.mode === "paper" ? chip("paper", "paper") : null,
      el("div", { class: "small muted clamp", text: p.trigger.text })), el("div", { class: "small nowrap", style: "text-align:right" }, el("b", { text: pc(p.trigger.dist_pct, 2) }),
      isn(p.trigger.eta_s) ? el("div", { class: "muted" }, "свеча ", tk("ov-eta:" + p.id, p.trigger.eta_s, "left")) : null));
  })) : null;
  var disc = day && day.n ? el("div", { class: "small disc" }, el("span", { class: "muted", text: "Сутки: " }), "мозг " + day.brain + " · отказов Gate ",
    el("b", { class: day.rejects ? "neg" : "", text: String(day.rejects) }), (day.reject_codes || []).length ? el("span", { class: "muted", text: " (" + day.reject_codes.map(function (c) { return c.code + " ×" + c.n; }).join(", ") + ")" }) : null,
    " · авто-действий " + day.auto + " ", tabLink("лента решений", "decisions", {}, "more-link-i")) : null;
  put(host, [cnt, list, disc]);
}
function renderPauses() {
  var v = V2(), box = $("room-pauses");
  var p = v && v.pauses ? v.pauses : [];
  var hold = v && v.strip && v.strip.exec && v.strip.exec.hold_reasons ? v.strip.exec.hold_reasons : [];
  box.hidden = !(p.length || hold.length);
  if (box.hidden) return;
  if (!changed(box, [p, hold])) return;
  put(box, [el("b", { text: "Паузы: " }), p.map(function (x) { return chip((x.sym ? x.sym + " " : "") + x.which + " ещё " + dur(x.left_s), "warn"); }), hold.length ? chip("hold: " + hold.join(", "), "warn") : null]);
}
function renderWake() {
  var v = V2(), block = $("wake-block"), ul = $("wake-list");
  var ev = v && v.wake_events ? v.wake_events : [];
  block.hidden = !ev.length;
  if (!ev.length) return;
  if (!changed(ul, ev)) return;
  put(ul, ev.slice(0, 4).map(function (e) {
    var d = e.d || {}, dir = d.dir === "up" ? "вверх" : d.dir === "down" ? "вниз" : "";
    var what = e.type === "scanner_signal" ? "сигнал сканера" + (dir ? ", " + dir : "") + (isn(+d.score) ? " · скор " + fx(+d.score, 1) : "") : (e.type || "событие");
    return el("li", { class: "ev", title: kvList(d) }, el("span", { class: "t", text: dts(e.t) }), el("span", { class: "ev-w" }, el("b", { text: e.sym || "" }), " ", what));
  }));
}
