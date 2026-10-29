"use strict";
// Блок «Эксперименты» во вкладке «Сделки»: аналог `./tb exp stats` по журналу сделок (live и paper), без вердиктов.
// n < 30 помечается звёздочкой; для набора групп показывается число сравнений K и порог значимости с поправкой Бонферрони.

var EXP_BY = [["exp", "Эксперимент"], ["variant", "Вариант"], ["setup", "Сетап"], ["hyp", "Гипотеза"], ["symbol", "Монета"], ["mode", "Режим"]];
var expOpen = false;

function renderExp() {
  var fold = $("exp-fold");
  if (fold.open !== expOpen) fold.open = expOpen;
  segment($("exp-seg"), EXP_BY, ui.eb, function (v) { ui.eb = v; saveUi(); renderExp(); });
  fetchTab("exp", "/api/v2/exp?" + qs({ by: ui.eb, mode: ui.mode }), applyExp);
}
function expCell(g, key, f) { return isn(g[key]) ? f(g[key]) : "–"; }
function applyExp(j, fresh, err) {
  if (ui.tab !== "trades") return;
  var host = $("exp-body");
  if (err && !j) { put(host, el("div", { class: "muted small", text: "Не удалось загрузить: " + err })); return; }
  if (!j || !changed(host, j)) return;
  put("exp-sub", j.rows.length ? "· групп " + j.rows.length : "");
  if (!j.rows.length) {
    put(host, el("div", { class: "empty" }, el("b", { text: "Нет сделок с тегами эксперимента" }),
      el("span", { text: "Теги exp/var/setup/hyp ставит план v2 при открытии сделки. Закрытых сделок в выборке: " + j.total_closed + ", из них по планам: " + j.with_plan + "." })));
    return;
  }
  var head = el("tr", null, [["Группа", 0], ["n", 1], ["Плюсовых", 1], ["Ср. R ± se", 1, "r"], ["PF", 1, "pf"], ["PnL", 1], ["Комиссии", 1], ["MAE/MFE (плюс | минус), R", 1], ["Закрыто правилами", 1]].map(function (h) {
    return el("th", { class: h[1] ? "num" : "" }, h[2] ? tip(h[0], h[2]) : h[0]);
  }));
  var body = j.rows.map(function (g) {
    var meta = j.meta && j.meta[g.name.split("/")[0]];
    return el("tr", { class: g.star ? "star" : "" }, el("td", null, el("b", { text: g.name }), g.modes.length ? " " : null, g.modes.map(function (m) { return chip(m, m === "paper" ? "paper" : ""); }),
        meta ? el("div", { class: "muted small exp-meta", text: "набрано " + g.n + " из " + meta.min_n + (meta.hypothesis ? " · " + meta.hypothesis : "") }) : null),
      el("td", { class: "num" }, String(g.n), g.star ? el("span", { class: "starmark", title: "n < " + j.min_n + ": вывода нет, только описание", text: " *" }) : null),
      el("td", { class: "num", text: isn(g.winrate) ? pc(g.winrate, 0) : "–" }),
      el("td", { class: "num " + cls(g.avg_r), text: isn(g.avg_r) ? sg(g.avg_r, 2) + (isn(g.r_se) ? " ± " + g.r_se.toFixed(2) : "") : "–" }),
      el("td", { class: "num", text: g.pf_inf ? "∞" : expCell(g, "pf", function (x) { return x.toFixed(2); }) }),
      el("td", { class: "num " + cls(g.pnl), text: sg(g.pnl, 2) }),
      el("td", { class: "num", text: fx(g.fees, 2) + (isn(g.fees_share) ? " (" + Math.round(g.fees_share * 100) + "%)" : "") }),
      el("td", { class: "num", text: (isn(g.mae_win) ? fx(g.mae_win, 2) : "–") + "/" + (isn(g.mfe_win) ? fx(g.mfe_win, 2) : "–") + " | " + (isn(g.mae_loss) ? fx(g.mae_loss, 2) : "–") + "/" + (isn(g.mfe_loss) ? fx(g.mfe_loss, 2) : "–") }),
      el("td", { class: "num", text: expCell(g, "by_rule_share", function (x) { return Math.round(x * 100) + "%"; }) }));
  });
  put(host, [el("div", { class: "scroll" }, el("table", { class: "tbl" }, el("thead", null, head), el("tbody", null, body))),
    el("div", { class: "muted small exp-note" }, "* n < " + j.min_n + ": выводов нет. Сравнений в этом разрезе K=" + j.k + (isn(j.alpha) ? ", порог значимости с поправкой Бонферрони α=0.05/K=" + j.alpha.toFixed(4) : "") +
      ". Разные ревизии правил считаются разными вариантами; вердикты и сравнение A/B даёт ./tb exp compare."),
    Object.keys(j.meta || {}).length ? el("details", { class: "fold-in" }, el("summary", { text: "Предрегистрация гипотез (config/experiments.json)" }), el("ul", { class: "lst" }, Object.keys(j.meta).map(function (k) {
      var m = j.meta[k];
      return el("li", null, el("b", { text: k }), el("div", { class: "small muted" }, (m.hypothesis ? m.hypothesis + ". " : "") + (m.prereg ? "Проверяем: " + m.prereg : "") + (m.started ? " Старт " + m.started + "." : "")));
    }))) : null]);
}
