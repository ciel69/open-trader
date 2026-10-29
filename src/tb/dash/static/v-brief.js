"use strict";
// Вкладка «Брифинг рынка»: то, на что смотрел мозг перед решением (state/briefs/B-*.json). Только числа и перечисления из белого списка;
// единственный текст мозга: own_thesis (свой прошлый тезис, обрезан) и чек-лист владельца. Всё вставляется через textContent.

function bkv(pairs) { return kvgrid(pairs.filter(function (p) { return p && p[1] != null && p[1] !== ""; })); }
function bBlock(title, kids, sub) {
  return el("section", { class: "block bb" }, el("h2", null, title, sub ? el("span", { class: "sub", text: sub }) : null), kids);
}
// значение или «–»: досье бывает неполным (scope=event), «undefined» на странице недопустимо
function u(x) { return x == null || (typeof x === "number" && !isFinite(x)) ? "–" : String(x); }
function yn(x) { return x === true ? "да" : x === false ? "нет" : "–"; }
// Мини-диаграмма уровней символа относительно цены: недельный диапазон, суточный, вчерашний, свинги, цена
function levelsChart(sym, L) {
  var px = L.px, rows = [["неделя", L.week, 0.3, "w"], ["день", L.day, 0.8, "d"], ["вчера", L.prev_day, 0.5, "p"]].filter(function (r) { return r[1] && isn(r[1].lo) && isn(r[1].hi); });
  var vals = [px].concat(L.swing_5m || []);
  rows.forEach(function (r) { vals.push(r[1].lo, r[1].hi); });
  vals = vals.filter(isn);
  if (vals.length < 2) return el("div", { class: "muted small", text: "Недостаточно уровней" });
  var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals), pad = (hi - lo) * 0.04 || 1, W = 320, X0 = 54, X1 = W - 8;
  lo -= pad; hi += pad;
  function x(v) { return X0 + (v - lo) / (hi - lo) * (X1 - X0); }
  var H = 22 + rows.length * 22 + 20, kids = [];
  rows.forEach(function (r, i) {
    var y = 16 + i * 22, a = r[1];
    kids.push(sv("text", { x: 0, y: y + 4, class: "ax-t" }, document.createTextNode(r[0])));
    kids.push(sv("line", { x1: x(a.lo), x2: x(a.hi), y1: y, y2: y, class: "lv-rng lv-" + r[3], "stroke-width": 5 + r[2] * 6 }, sv("title", {}, document.createTextNode(r[0] + ": " + price(a.lo) + " – " + price(a.hi)))));
  });
  (L.swing_5m || []).forEach(function (v) { if (isn(v)) kids.push(sv("line", { x1: x(v), x2: x(v), y1: 6, y2: H - 18, class: "lv-sw" })); });
  if (isn(px)) {
    kids.push(sv("line", { x1: x(px), x2: x(px), y1: 2, y2: H - 18, class: "lv-px" }));
    kids.push(sv("text", { x: Math.min(X1 - 30, Math.max(X0, x(px) - 14)), y: H - 4, class: "ax-t lv-pxt" }, document.createTextNode(price(px))));
  }
  var svg = sv.apply(null, ["svg", { viewBox: "0 0 " + W + " " + H, class: "lv-svg", role: "img", "aria-label": sym + ": цена " + price(px) + " относительно уровней дня и недели" }].concat(kids));
  return svg;
}
function renderBriefView(j) {
  var host = $("br-body");
  var b = j.brief;
  if (!j.available || !b) {
    put(host, el("div", { class: "empty" }, el("b", { text: j.missing ? "Досье " + j.missing + " не найдено" : "Сохранённых досье пока нет" }),
      el("span", { text: "Команда ./tb brief сохраняет каждый вызов в state/briefs/; решения ссылаются на brief_id." })));
    return;
  }
  var blocks = [];
  var ts = briefTs(b.ts);
  blocks.push(el("div", { class: "br-head" }, el("b", { class: "mono", text: b.brief_id || j.id }), chip(b.scope || "?"), el("span", { class: "muted small", text: ts }),
    b.trunc ? chip("обрезано по бюджету", "warn") : null));
  var sy = b.system || {};
  if (sy.exec || sy.kill != null) {
    var ex = sy.exec || {};
    blocks.push(bBlock("Система", bkv([["Исполнитель", ex.alive === undefined ? null : (ex.alive ? "жив" : "не отвечает") + (isn(ex.age_s) ? ", heartbeat " + fx(ex.age_s, 1) + " с" : "")],
      ["Среда · режим", [ex.env, ex.mode].filter(Boolean).join(" · ") + (ex.observe ? " · observe" : "") || null], ["Hold", ex.hold ? String(ex.hold) : "нет"], ["WS", ex.ws],
      ["Сверка с биржей", isn(ex.reconcile_age_s) ? ex.reconcile_age_s + " с назад" : null], ["KILL", yn(sy.kill)],
      ["Сканер", sy.scanner ? (sy.scanner.alive ? "жив" : "стоит") + (isn(sy.scanner.age_s) ? ", " + sy.scanner.age_s + " с" : "") : null], ["Не подтверждено событий", sy.unacked == null ? null : String(sy.unacked)]])));
  }
  var ac = b.account, ss = b.session;
  if (ac || ss) {
    blocks.push(bBlock("Счёт и сессия", bkv([ac ? ["Капитал · свободно", fx(ac.equity) + " · " + fx(ac.free) + " USDT"] : null, ac && ac.dd_pct != null ? ["Просадка · множитель", fx(ac.dd_pct, 1) + "% · ×" + fx(ac.dd_mult)] : null,
      ac && ac.day_pnl != null ? ["Сегодня", "PnL " + sg(ac.day_pnl, 2) + " · сделок " + u(ac.day_trades) + " · стопов " + u(ac.stops_today) + " (серия " + u(ac.stop_streak) + ")"] : null,
      ss ? ["Сессия", u(ss.type) + " · осталось " + u(ss.min_left) + " мин · бюджет " + fx(ss.budget, 2) + " USDT · сделок " + u(ss.trades) + "/" + u(ss.max_trades)] : (b.scope === "start" || ac && ac.day_pnl != null ? ["Сессия", "нет активной"] : null),
      ss ? ["Реализовано · риск открытых", sg(ss.realized, 2) + " · " + fx(ss.open_risk, 2) + " USDT"] : null])));
  }
  var rm = b.room;
  if (rm) {
    blocks.push(bBlock("Запас риска", [el("div", { class: "room-st" }, stIcon(rm.allowed === true ? "ok" : rm.allowed === false ? "bad" : "off"), el("b", { text: rm.allowed === true ? "Входы разрешены" : rm.allowed === false ? "Входы запрещены" : "Запас не определён" }),
      (rm.reasons || []).length ? el("span", { class: "muted", text: ": " + rm.reasons.join("; ") }) : null),
      bkv([["Риск на следующую сделку", isn(rm.next_risk_usdt) ? "до " + fx(rm.next_risk_usdt, 2) + " USDT" + (rm.limited_by ? " (упирается в " + rm.limited_by + ")" : "") : null],
        ["Свободных слотов", rm.slots_free == null ? null : String(rm.slots_free)], ["Дневной запас", isn(rm.daily_room) ? fx(rm.daily_room, 2) + " USDT" : null],
        ["Паузы по монетам", (rm.cooldowns || []).length ? rm.cooldowns.map(function (c) { return c.sym + " ещё " + c.min_left + " мин"; }).join(", ") : "нет"],
        ["Общая пауза", rm.pause_min_left ? "ещё " + rm.pause_min_left + " мин" : "нет"]])]));
  }
  if ((b.positions || []).length) {
    blocks.push(bBlock("Позиции", el("div", { class: "br-list" }, b.positions.map(function (p) {
      return el("div", { class: "br-item" }, el("div", null, el("b", { text: p.sym }), " ", sideChip(p.side), " ", el("span", { class: "muted small mono", text: planShort(p.plan || p.trade) }), "  ",
        el("b", { class: cls(p.r), text: rr(p.r) }), el("span", { class: "muted small", text: "  MAE " + fx(p.mae_r, 2) + "R · MFE " + fx(p.mfe_r, 2) + "R · " + p.age_min + " мин" })),
        el("div", { class: "small" }, "вход " + price(p.avg) + " · цена " + price(p.px) + " · стоп " + price(p.sl) + (isn(p.tp) ? " · тейк " + price(p.tp) : "") + " · ликвидация " + price(p.liq)),
        el("div", { class: "small muted" }, (p.targets || []).map(function (t) { return t.id + " " + price(t.px) + " (" + t.state + ")"; }).join(" · ")),
        (p.rules || []).length ? el("div", { class: "small muted" }, p.rules.map(function (r) { return r.id + ": " + r.state + (r.need ? " (" + r.need + ")" : ""); }).join(" · ")) : null);
    }))));
  }
  if ((b.plans || []).length) {
    blocks.push(bBlock("Планы", el("div", { class: "br-list" }, b.plans.map(function (p) {
      return el("div", { class: "br-item" }, el("div", null, el("b", { text: p.sym }), " ", sideChip(p.side), " ", chip(p.status || "?"), " ", el("span", { class: "muted small mono", text: planShort(p.id) + " r" + p.rev }), p.exp ? " " : null, p.exp ? chip(p.exp, "exp") : null),
        el("div", { class: "small" }, u(p.entry === undefined ? "" : p.entry).replace(/^–$/, "") + (isn(p.dist_pct) ? " · до уровня " + fx(p.dist_pct, 2) + "%" : "") + (isn(p.ttl_left_min) ? " · TTL " + p.ttl_left_min + " мин" : "")),
        p.own_thesis ? el("div", { class: "small muted" }, el("span", { class: "chip" }, "свой тезис"), " ", p.own_thesis) : null);
    }))));
  }
  var mk = b.market, stt = (b.struct && b.struct.s) || {};
  // Поля рынка в досье: market.btc|eth {px, ret_5m_pct, ret_15m_pct, rv_5m_pct, spread_bps, funding_8h_pct, regime, regime_src}; 1ч/24ч и ATR только из struct.s.<SYM> (r1h, r24h, a5_pct, reg).
  function coinLine(key, name) {
    var c = (mk && mk[key]) || {}, s = stt[name] || {}, parts = [];
    var r5 = isn(c.ret_5m_pct) ? c.ret_5m_pct : null, r1 = isn(c.ret_1h_pct) ? c.ret_1h_pct : isn(s.r1h) ? s.r1h : null, r24 = isn(c.ret_24h_pct) ? c.ret_24h_pct : isn(s.r24h) ? s.r24h : null;
    var a5 = isn(c.range_5m_pct) ? c.range_5m_pct : isn(s.a5_pct) ? s.a5_pct : null, fu = isn(c.funding_8h_pct) ? c.funding_8h_pct : isn(s.fu) ? s.fu : null;
    var px = isn(c.px) ? c.px : s.px, reg = c.regime && c.regime !== "unknown" ? c.regime : s.reg;
    if (isn(px)) parts.push(price(px));
    if (r5 != null) parts.push("5м " + spc(r5, 2));
    if (r1 != null) parts.push("1ч " + spc(r1, 2));
    if (r24 != null) parts.push("24ч " + spc(r24, 2));
    if (a5 != null) parts.push("ATR 5м " + fx(a5, 2) + "%");
    if (fu != null) parts.push("funding 8ч " + fx(fu, 4) + "%");
    if (isn(c.spread_bps)) parts.push("спред " + fx(c.spread_bps, 2) + " бп");
    if (reg && reg !== "unknown") parts.push("режим " + reg + (c.regime_src && (!c.regime || c.regime === "unknown" || c.regime_src) ? " (по свечам " + String(c.regime_src).replace("1h", "1ч") + ")" : ""));
    return parts.length ? parts.join(" · ") : null;
  }
  if (mk) {
    var lb = coinLine("btc", "BTC"), le = coinLine("eth", "ETH");
    var noData = b.scope === "event" ? "нет в событийном досье (только risk_off и окно новостей)" : "нет данных: фид и структура рынка ещё не прогреты";
    blocks.push(bBlock("Рынок", bkv([["BTC", lb || noData], ["ETH", le || noData], ["Сессия", mk.utc_session],
      ["risk_off", yn(mk.risk_off)], ["Окно новостей", mk.blackout ? mk.blackout.event + " через " + mk.blackout.in_min + " мин" + (mk.blackout.mode ? " (" + mk.blackout.mode + ")" : "") : "нет"],
      ["Контекст", isn(mk.context_stale_s) ? "устарел на " + mk.context_stale_s + " с" : null]])));
  }
  var srows = Object.keys(stt);
  if (srows.length) {
    blocks.push(bBlock("Структура рынка", el("table", { class: "tbl" }, el("thead", null, el("tr", null, ["Монета", "Режим", "ATR 5м %", "ATR 1ч %", "1ч %", "24ч %", "Позиция в дне", "Спред бп", "Funding 8ч %"].map(function (h, i) { return el("th", { class: i ? "num" : "", text: h }); }))),
      el("tbody", null, srows.map(function (sym) {
        var s = stt[sym];
        return el("tr", null, el("td", null, el("b", { text: sym })), el("td", { class: "num", text: u(s.reg) }), el("td", { class: "num", text: fx(s.a5_pct, 2) }), el("td", { class: "num", text: fx(s.a1h_pct, 2) }),
          el("td", { class: "num " + cls(s.r1h), text: spc(s.r1h, 2) }), el("td", { class: "num " + cls(s.r24h), text: spc(s.r24h, 2) }), el("td", { class: "num", text: fx(s.dpos, 2) }),
          el("td", { class: "num", text: fx(s.sp, 2) }), el("td", { class: "num", text: fx(s.fu, 4) }));
      }))), isn(b.struct.stale_s) ? "устарело на " + b.struct.stale_s + " с" : "ATR в % цены; позиция в дне 0 = низ, 1 = верх"));
  }
  var lim = b.limits, rl = b.risk_level;
  if (lim || rl) {
    blocks.push(bBlock("Лимиты и уровень риска", bkv([lim ? ["Риск на сделку", fx(lim.risk_per_trade_usdt, 2) + " USDT"] : null, lim ? ["Дневной лимит · осталось", fx(lim.daily_loss_usdt, 2) + " · " + fx(lim.daily_left_usdt, 2) + " USDT"] : null,
      lim ? ["Позиции · новые планы", u(lim.max_open_positions) + " · " + u(lim.max_new_plans)] : null, lim ? ["Множитель размера", "×" + fx(lim.size_mult, 2)] : null, rl ? ["Уровень риска", u(rl.level)] : null,
      rl && rl.carry ? ["Перенос позиций", u(rl.carry.policy) + ", макс. " + u(rl.carry.max_positions)] : null])));
  }
  var hotRows = Array.isArray(b.hot) ? b.hot : (b.hot && b.hot.top) || [];
  if (b.hot && !Array.isArray(b.hot) && !hotRows.length) blocks.push(bBlock("Горячие монеты", el("div", { class: "muted small", text: "Сканер не отдал горячих монет" + (isn(b.hot.stale_s) ? " (данные устарели на " + b.hot.stale_s + " с)" : "") })));
  if (hotRows.length) {
    var hs = !Array.isArray(b.hot) && b.hot && isn(b.hot.stale_s) ? "устарело на " + b.hot.stale_s + " с" : (hotRows.every(function (h) { return h.ret_5m_pct == null; }) ? "5м-метрики появятся, когда сканер наберёт историю" : null);
    blocks.push(bBlock("Горячие монеты", el("table", { class: "tbl" }, el("thead", null, el("tr", null, ["Монета", "Скор", "Причины", "Цена", "5м", "Спред бп", "Funding %", "Лот"].map(function (h, i) { return el("th", { class: i && i !== 2 ? "num" : "", text: h }); }))),
      el("tbody", null, hotRows.map(function (h) {
        return el("tr", null, el("td", null, el("b", { text: h.sym }), h.tradable === false ? chip("нельзя", "warn") : null), el("td", { class: "num", text: fx(h.score, 1) }), el("td", { class: "wrapc", text: (h.reasons || []).join(", ") }),
          el("td", { class: "num", text: price(h.px) }), el("td", { class: "num " + cls(h.ret_5m_pct), text: spc(h.ret_5m_pct, 1) }), el("td", { class: "num", text: fx(h.spread_bps, 1) }), el("td", { class: "num", text: fx(h.funding_pct, 3) }), el("td", { class: "num", text: fx(h.lot_usdt, 1) }));
      }))), hs));
  }
  var it = b.intel;
  if (it) {
    var sn = it.sentiment || {}, fng = sn.fng || {}, cal = (it.calendar && it.calendar.next) || [], dv = sn.dvol || {};
    blocks.push(bBlock("Макро и новости", bkv([["Fear & Greed", isn(fng.v) ? fng.v + " (" + u(fng.cls) + ", вчера " + u(fng.prev) + ")" : null], ["DVOL BTC · ETH", isn(dv.BTC) || isn(dv.ETH) ? u(dv.BTC) + " · " + u(dv.ETH) : null],
      ["Доминация BTC", sn.dom && isn(sn.dom.v) ? fx(sn.dom.v, 1) + "%" : null], ["Календарь", cal.length ? cal.slice(0, 3).map(function (c) { return c.type + " через " + (c.in_min >= 120 ? Math.round(c.in_min / 60) + " ч" : c.in_min + " мин"); }).join(", ") : "нет ближайших событий"],
      ["Новостей в дайджесте", b.news_digest && (b.news_digest.events || []).length != null ? String((b.news_digest.events || []).length) : null],
      ["Источники с ошибкой", (it.degraded || []).length ? it.degraded.join(", ") : null]]), isn(it.age_s) ? "возраст " + it.age_s + " с" : null));
  }
  var lv = b.levels || {}, syms = Object.keys(lv);
  if (syms.length) {
    blocks.push(bBlock("Уровни", el("div", { class: "br-levels" }, syms.map(function (s) {
      var L = lv[s];
      return el("div", { class: "br-lv" }, el("div", null, el("b", { text: s }), el("span", { class: "muted small", text: "  ATR 5м " + fx(L.atr_5m, 3) + " · медиана объёма " + (isn(L.vol_med_5m) ? Math.round(L.vol_med_5m) : "–") })), levelsChart(s, L),
        el("div", { class: "muted small", text: "свинги 5м: " + (L.swing_5m || []).map(price).join(", ") }));
    })), "серым: диапазоны недели, дня и вчера; тонкие линии: свинги; тёмная: цена"));
  }
  var id = b.ideas;
  if (id && (id.active || []).length) {
    blocks.push(bBlock("Идеи", el("ul", { class: "lst" }, id.active.map(function (i) {
      return el("li", null, el("b", { text: u(i.id) }), el("span", { text: u(i.sym) + " " + u(i.side) + " уровень " + price(i.level) + " (" + fx(i.dist_pct, 1) + "%) · " + u(i.status) + " · " + fx(i.age_h, 1) + " ч" }));
    })), id.stats ? "закрыто " + id.stats.n_closed + (id.stats.note ? ", " + id.stats.note : "") : null));
  }
  if ((b.recent || []).length) {
    blocks.push(bBlock("Последние события", el("ul", { class: "lst" }, b.recent.map(function (e) {
      return el("li", { class: "ev" }, el("span", { class: "t", text: "#" + e.seq }), el("span", { text: [e.type, e.sym, kvList(e.d)].filter(Boolean).join(" · ") }));
    }))));
  }
  var extra = [];
  if ((b.lessons || []).length) extra.push(el("div", null, el("h3", { text: "Уроки" }), el("ul", { class: "lst" }, b.lessons.map(function (t) { return el("li", { text: t }); }))));
  if ((b.checklist || []).length) extra.push(el("div", null, el("h3", { text: "Чек-лист владельца" }), el("ul", { class: "lst" }, b.checklist.map(function (c) { return el("li", null, el("b", { class: "mono", text: c.id }), el("span", { text: c.text })); }))));
  if (extra.length) blocks.push(el("section", { class: "block bb" }, extra));
  put(host, blocks);
}
function briefTs(iso) {
  if (typeof iso !== "string") return "";
  var d = new Date(iso);
  return isNaN(d.getTime()) ? iso : dt(d.getTime() / 1000) + " (" + ago((Date.now() - d.getTime()) / 1000) + ")";
}
function renderBrief() {
  fetchTab("brief", "/api/v2/brief?" + qs({ id: ui.b || "last" }), applyBrief);
}
function applyBrief(j, fresh, err) {
  if (ui.tab !== "brief") return;
  var sel = $("br-sel");
  if (err && !j) { put("br-body", el("div", { class: "empty" }, el("b", { text: "Не удалось загрузить досье" }), el("span", { text: err }))); return; }
  if (!j) return;
  var ids = j.ids || [];
  if (sel.options.length !== ids.length + 1 || !ids.every(function (v, i) { return sel.options[i + 1].value === v; })) {
    sel.replaceChildren(el("option", { value: "", text: "последнее" }));
    ids.forEach(function (v) { sel.appendChild(el("option", { value: v, text: v })); });
  }
  sel.value = ui.b && ids.indexOf(ui.b) >= 0 ? ui.b : "";
  put("br-sub", j.total ? "· сохранено " + j.total : "");
  var sig = JSON.stringify(j);
  if ($("br-body")._sig === sig) return;
  $("br-body")._sig = sig;
  renderBriefView(j);
}
