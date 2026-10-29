"use strict";
// Вкладка «День» (WP14): состояние дня, шаги старта и закрытия, вопросы о переносе, итог дня. Только чтение (/api/day/*);
// кнопки («Перенести/Закрыть», «Прочитал») идут через сценарий подтверждения вкладки «Управление» (flowStart, WP12) и сервис ctl.
// Любой текст из данных вставляется через textContent (el/add из core.js).
var DAY = { st: null, list: null, rep: null, repId: null, err: null, timer: null };
var DAY_STATE = { stopped: "остановлен", starting: "запускается", running: "идёт", closing: "закрывается", halted: "аварийно остановлен" };
var DAY_VERDICT = { ok: "ok", attention: "warn", alarm: "bad" };

function dayGet(path) { return fetch(path, { cache: "no-store", credentials: "same-origin" }).then(function (r) { return r.json(); }); }
function dayPoll() {
  if (document.hidden) return Promise.resolve();
  return Promise.all([dayGet("/api/day/status"), dayGet("/api/day/list")]).then(function (a) {
    DAY.st = a[0]; DAY.list = a[1]; DAY.err = null;
    if (ui.tab === "day") renderDay();
  }).catch(function () { DAY.err = "Нет связи с дашбордом"; if (ui.tab === "day") renderDay(); });
}
function dayLoadReport(id) {
  DAY.repId = id; DAY.rep = null; renderDay();
  dayGet("/api/day/report?day=" + encodeURIComponent(id)).then(function (j) { DAY.rep = j && j.report ? j : { none: true }; renderDay(); }).catch(function () { DAY.rep = { none: true }; renderDay(); });
}
function dayCtl(action, args, title, opts) {
  if (typeof flowStart !== "function") return;
  goTab("control");
  flowStart(action, args, title, opts);
}
function dayStepRow(s) {
  var mark = s.ok === true ? "ok" : s.ok === false ? "bad" : "warn";
  var amber = s.detail && typeof s.detail === "object" && s.detail.amber;
  return el("li", null, chip(s.name, mark === "ok" && !amber ? "ok" : mark === "bad" ? "bad" : "warn"), " ",
    el("span", { class: "muted small", text: s.ok === null ? "выполняется…" : (s.ok === false ? "сбой " + (s.code || "") : (amber ? "с замечанием" : "готово")) + (isn(s.dur_s) ? " · " + dur(s.dur_s) : "") }));
}
function dayRender(c, banner) {
  var kids = [];
  if (banner) kids.push(el("div", { class: "ctl-banner " + (banner.level || "info"), role: "status", text: banner.text }));
  kids.push(el("div", { class: "tiles" },
    el("div", { class: "tile" }, el("div", { class: "lab", text: "День" }), el("div", { class: "val", text: c.day_id }), el("div", { class: "det", text: DAY_STATE[c.state] || c.state })),
    el("div", { class: "tile" }, el("div", { class: "lab", text: "Режим" }), el("div", { class: "val", text: (c.mode || "–").toUpperCase() }), el("div", { class: "det", text: "агент " + (c.agent || "–") })),
    el("div", { class: "tile" }, el("div", { class: "lab", text: "Итог" }), el("div", { class: "val " + cls(c.net), text: c.net != null ? usd(c.net) : "–" }), el("div", { class: "det", text: c.verdict ? "оценка: " + c.verdict : "отчёта ещё нет" })),
    el("div", { class: "tile" }, el("div", { class: "lab", text: "План дня" }), el("div", { class: "val", text: c.dayplan ? (c.dayplan.regime || "–") + " · " + (c.dayplan.bias || "–") : "–" }),
      el("div", { class: "det", text: c.dayplan ? "источник: " + c.dayplan.source + ", новых планов до " + c.dayplan.max_new_plans : "" }))));
  if (c.needs_resume) kids.push(el("div", { class: "ctl-banner warn", role: "alert", text: "Платформа перезапущена: торговля выключена." }),
    el("button", { class: "btn primary", type: "button", id: "day-resume", text: "Возобновить день", onclick: function () { dayCtl("day.resume", {}, "Возобновить день (торговый флаг снова включится; live: только стоп и новый старт)", { go: "Возобновить" }); } }));
  if (c.abort) kids.push(el("div", { class: "ctl-banner alarm", role: "alert", text: "Старт отклонён на шаге " + c.abort.step + " (" + c.abort.code + ")" }));
  if (c.closing && c.closing.failed) kids.push(el("div", { class: "ctl-banner alarm", role: "alert", text: "Закрытие не подтверждено исполнителем: " + (c.closing.failed.items || []).join(", ") + ". Позиции под стопом на бирже." }));
  var qs = (c.questions || []).filter(function (q) { return q.answer == null; });
  if (qs.length) {
    kids.push(el("section", { class: "block" }, el("h2", { text: "Перенос через сутки: решение владельца" }),
      el("div", { class: "muted small", text: "Без ответа" + (c.deadline_ts ? " до " + dts(c.deadline_ts) : "") + " позиция будет закрыта." }),
      el("ul", { class: "lst" }, qs.map(function (q) {
        return el("li", null, el("b", { text: q.plan + " " }), el("span", { text: q.text }), " ",
          el("button", { class: "btn", type: "button", text: "Перенести", onclick: null, "data-act": "carry", "data-plan": q.plan }),
          " ", el("button", { class: "btn", type: "button", text: "Закрыть", "data-act": "close", "data-plan": q.plan }));
      }))));
  }
  var steps = el("ul", { class: "lst" }, (c.steps || []).map(dayStepRow));
  kids.push(el("section", { class: "block" }, el("h2", { text: "Шаги " }, el("span", { class: "sub", text: "S1–S9 старт, C1–C8 закрытие" })), (c.steps || []).length ? steps : el("div", { class: "muted", text: "Шагов ещё не было." })));
  if ((c.carry || []).length) {
    kids.push(el("section", { class: "block" }, el("h2", { text: "Перенесено" }), el("ul", { class: "lst" }, c.carry.map(function (x) {
      return el("li", null, el("b", { text: x.plan + " " + x.sym + " " + x.side }), " ", el("span", { class: "muted small", text: "стоп " + price(x.stop) + ", риск " + usd(x.risk_usdt) + ", решил: " + x.decided_by }),
        el("div", { class: "small", text: x.why || "" }));
    }))));
  }
  if ((c.checklist || []).length) {
    kids.push(el("section", { class: "block" }, el("h2", { text: "Чек-лист закрытия" }), el("ul", { class: "lst" }, c.checklist.map(function (r) {
      return el("li", null, chip(r.rule || "–", r.decision === "carry" ? "ok" : r.decision === "close" || r.decision === "cancel" ? "warn" : ""), " ", el("b", { text: r.item + " " }),
        el("span", { class: "muted small", text: r.decision + (r.codes && r.codes.length ? " · " + r.codes.join(", ") : "") }));
    }))));
  }
  return kids;
}
function dayReportBlock() {
  if (!DAY.repId) return null;
  var r = DAY.rep;
  if (!r) return el("section", { class: "block" }, el("div", { class: "muted", text: "Загружаю отчёт…" }));
  if (r.none) return el("section", { class: "block" }, el("div", { class: "muted", text: "Отчёта по этому дню нет." }));
  var rep = r.report, p = rep.pnl || {};
  var kids = [el("h2", { text: "Итог дня " + rep.day_id + " " }, chip(rep.verdict, DAY_VERDICT[rep.verdict] || "")),
    el("div", { class: "tiles" },
      el("div", { class: "tile" }, el("div", { class: "lab", text: "PnL чистый" }), el("div", { class: "val " + cls(p.net), text: usd(p.net, 4) }), el("div", { class: "det", text: "комиссии " + fx(p.fees, 4) + ", funding " + fx(p.funding, 4) })),
      el("div", { class: "tile" }, el("div", { class: "lab", text: "Сделок" }), el("div", { class: "val", text: String(rep.trades_n) }), el("div", { class: "det", text: "R " + sg(p.r_sum) })),
      el("div", { class: "tile" }, el("div", { class: "lab", text: "LLM" }), el("div", { class: "val", text: usd(-(rep.cost || {}).usd, 4).replace("−", "") }), el("div", { class: "det", text: ((rep.cost || {}).calls || 0) + " вызовов" })),
      el("div", { class: "tile" }, el("div", { class: "lab", text: "Перенесено" }), el("div", { class: "val", text: String((rep.carried || []).length) }), el("div", { class: "det", text: "вопросов " + (rep.open_questions || []).length })))];
  kids.push(el("pre", { class: "ctl-md", text: r.md || "" }));
  return el("section", { class: "block" }, kids);
}
function renderDay() {
  var host = $("tab-day");
  if (!host) return;
  var st = DAY.st, kids = [el("h2", { text: "День" })];
  if (DAY.err && !st) { put(host, [kids[0], el("div", { class: "empty" }, el("b", { text: DAY.err }))]); return; }
  if (!st) { put(host, [kids[0], el("div", { class: "muted", text: "Загружаю…" })]); return; }
  var c = st.current;
  if (st.banner && st.banner.day_report) {
    kids.push(el("div", { class: "ctl-banner " + (st.banner.level || "info"), role: "status" }, el("span", { text: st.banner.text + " " }),
      el("button", { class: "btn", type: "button", text: "Открыть отчёт", "data-act": "open", "data-day": st.banner.day_report }),
      " ", el("button", { class: "btn primary", type: "button", text: "Прочитал", "data-act": "ack", "data-day": st.banner.day_report })));
  }
  kids.push(c ? dayRender(c, st.banner && !st.banner.day_report ? st.banner : null) : el("div", { class: "empty" }, el("b", { text: "Дней ещё не было" }), el("span", { text: "Старт дня: вкладка «Управление»." })));
  var rb = dayReportBlock();
  if (rb) kids.push(rb);
  var ds = (DAY.list && DAY.list.days) || [];
  kids.push(el("section", { class: "block" }, el("h2", { text: "Дни" }), ds.length ? el("ul", { class: "lst" }, ds.map(function (d) {
    return el("li", null, el("b", { text: d.day_id + " " }), chip(DAY_STATE[d.state] || d.state, d.state === "running" ? "ok" : ""), " ", el("span", { class: "muted small", text: d.mode + " · " + (d.net != null ? usd(d.net) : "–") + (d.verdict ? " · " + d.verdict : "") }),
      " ", d.report_ready ? el("button", { class: "btn", type: "button", text: "Отчёт", "data-act": "open", "data-day": d.day_id }) : null);
  })) : el("div", { class: "muted", text: "Список пуст." })));
  put(host, kids);
  host.querySelectorAll("button[data-act]").forEach(function (b) {
    b.addEventListener("click", function () {
      var a = b.getAttribute("data-act");
      if (a === "open") dayLoadReport(b.getAttribute("data-day"));
      else if (a === "ack") dayCtl("report.ack", { day_id: b.getAttribute("data-day") }, "Итог дня прочитан", { go: "Прочитал" });
      else dayCtl("carry.decide", { plan: b.getAttribute("data-plan"), decision: a }, a === "carry" ? "Перенести позицию через сутки" : "Закрыть позицию", { go: a === "carry" ? "Перенести" : "Закрыть" });
    });
  });
}

registerTab("day", "День", "M7 3v3M17 3v3M4 8h16M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zM8 13h3v3H8z", renderDay);
(function () {
  setInterval(function () { if (!document.hidden && ui.tab === "day") dayPoll(); }, 4000);
  dayPoll();
})();
