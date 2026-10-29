"use strict";
// Графики на чистом SVG/HTML: кривая накопленного PnL, столбики PnL по сделкам, горизонтальные распределения.
// Цвета только через CSS-классы (темы переключаются без перерисовки); зелёный/красный только для знака PnL.

// рисует график при смене данных или ширины блока (ResizeObserver), без анимаций
function chartHost(box, sig, draw) {
  box._draw = draw; box._csig = sig;
  if (!box._ro && typeof ResizeObserver === "function") {
    box._ro = new ResizeObserver(function () {
      if (box._w !== box.clientWidth && box._draw) { box._w = box.clientWidth; box._draw(box); }
    });
    box._ro.observe(box);
  }
  var w = box.clientWidth;
  if (box._drawn === sig + "|" + w) return;
  box._drawn = sig + "|" + w; box._w = w;
  draw(box);
}

var gradSeq = 0;
// Монотонная кубическая кривая (без выбросов выше/ниже данных): pts = [[x, y], ...] -> строка path
function smoothPath(pts) {
  var n = pts.length;
  if (n < 3) return pts.map(function (p, i) { return (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ");
  var dx = [], m = [], t = [];
  for (var i = 0; i < n - 1; i++) { dx[i] = pts[i + 1][0] - pts[i][0]; m[i] = (pts[i + 1][1] - pts[i][1]) / (dx[i] || 1); }
  t[0] = m[0]; t[n - 1] = m[n - 2];
  for (i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
  for (i = 0; i < n - 1; i++) {
    if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    var a = t[i] / m[i], b = t[i + 1] / m[i], h = a * a + b * b;
    if (h > 9) { var k = 3 / Math.sqrt(h); t[i] = k * a * m[i]; t[i + 1] = k * b * m[i]; }
  }
  var d = "M" + pts[0][0].toFixed(1) + "," + pts[0][1].toFixed(1);
  for (i = 0; i < n - 1; i++) {
    var x0 = pts[i][0], y0 = pts[i][1], x1 = pts[i + 1][0], y1 = pts[i + 1][1], h3 = dx[i] / 3;
    d += " C" + (x0 + h3).toFixed(1) + "," + (y0 + t[i] * h3).toFixed(1) + " " + (x1 - h3).toFixed(1) + "," + (y1 - t[i + 1] * h3).toFixed(1) + " " + x1.toFixed(1) + "," + y1.toFixed(1);
  }
  return d;
}
function niceStep(range, n) {
  var raw = range / n, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}
function axisY(svg, lo, hi, Y, L, W, R) {
  var step = niceStep(hi - lo, 3), v = Math.ceil(lo / step) * step;
  for (; v <= hi + 1e-9; v += step) {
    var y = Y(v);
    svg.appendChild(sv("line", { x1: L, x2: W - R, y1: y, y2: y, class: Math.abs(v) < step / 1000 ? "ax-zero" : "ax-grid" }));
    var t = sv("text", { x: L - 6, y: y + 4, "text-anchor": "end", class: "ax-t" }); t.textContent = (Math.abs(v) < step / 1000 ? 0 : v).toFixed(step < 0.1 ? 2 : step < 1 ? 1 : 0 === step % 1 ? 0 : 1);
    svg.appendChild(t);
  }
}

// Кривая накопленного PnL: pts = [{cum, pnl, sym, id, ts}], старт с нуля
function lineChart(box, pts, label) {
  var sig = JSON.stringify(pts.map(function (p) { return [p.id, p.cum]; }));
  chartHost(box, sig, function (b) {
    b.replaceChildren();
    if (!pts.length) { emptyState(b.appendChild(el("div", { class: "empty" })), "Закрытых сделок пока нет", "Кривая появится после первой закрытой сделки."); return; }
    var W = Math.max(240, b.clientWidth), H = W < 420 ? 170 : 200, L = 42, R = 14, T = 12, B = 22;
    var ys = [0].concat(pts.map(function (p) { return p.cum; }));
    var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
    if (hi - lo < 1e-9) { hi += 0.05; lo -= 0.05; }
    var pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
    var n = pts.length, X = function (i) { return L + (n ? i / n : 0) * (W - L - R); };
    var Y = function (v) { return T + (hi - v) / (hi - lo) * (H - T - B); };
    var svg = sv("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img", "aria-label": label || ("Накопленный PnL по " + n + " сделкам, итого " + sg(pts[n - 1].cum, 3) + " USDT") });
    axisY(svg, lo, hi, Y, L, W, R);
    var id = "g" + (++gradSeq), P = [[X(0), Y(0)]];
    pts.forEach(function (p, i) { P.push([X(i + 1), Y(p.cum)]); });
    var y0 = Y(0), d = smoothPath(P), yMax = Y(Math.max.apply(null, ys)), yMin = Y(Math.min.apply(null, ys));
    var defs = sv("defs", {});
    // плюс выше нуля, минус ниже: заливка гуще там, где дальше от нуля
    [["p", yMax, "gs-pos"], ["n", yMin, "gs-neg"]].forEach(function (g) {
      var far = Math.abs(g[1] - y0) < 1 ? (g[0] === "p" ? y0 - 1 : y0 + 1) : g[1];
      var lg = sv("linearGradient", { id: id + g[0], gradientUnits: "userSpaceOnUse", x1: 0, x2: 0, y1: y0.toFixed(1), y2: far.toFixed(1) });
      lg.appendChild(sv("stop", { offset: "0%", class: g[2] + " gs-0" })); lg.appendChild(sv("stop", { offset: "100%", class: g[2] + " gs-1" }));
      defs.appendChild(lg);
    });
    defs.appendChild(sv("clipPath", { id: id + "cp" }, sv("rect", { x: 0, y: 0, width: W, height: y0.toFixed(1) })));
    defs.appendChild(sv("clipPath", { id: id + "cn" }, sv("rect", { x: 0, y: y0.toFixed(1), width: W, height: Math.max(0, H - y0).toFixed(1) })));
    svg.appendChild(defs);
    var area = d + " L" + X(n).toFixed(1) + "," + y0.toFixed(1) + " L" + X(0).toFixed(1) + "," + y0.toFixed(1) + " Z";
    [["p", "pos"], ["n", "neg"]].forEach(function (g) {
      svg.appendChild(sv("path", { d: area, class: "ln-area", fill: "url(#" + id + g[0] + ")", "clip-path": "url(#" + id + "c" + g[0] + ")" }));
      svg.appendChild(sv("path", { d: d, class: "ln ln-" + g[1], "clip-path": "url(#" + id + "c" + g[0] + ")" }));
    });
    var last = P[n], sgn = pts[n - 1].cum >= 0 ? "pos" : "neg";
    svg.appendChild(sv("circle", { cx: last[0].toFixed(1), cy: last[1].toFixed(1), r: 9, class: "ln-halo ln-" + sgn }));
    svg.appendChild(sv("circle", { cx: last[0].toFixed(1), cy: last[1].toFixed(1), r: 4, class: "ln-end ln-" + sgn }));
    var cross = sv("line", { class: "xh", y1: T, y2: H - B, visibility: "hidden" });
    var dot = sv("circle", { class: "xh-dot", r: 4, visibility: "hidden" });
    svg.appendChild(cross); svg.appendChild(dot);
    var hit = sv("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" });
    function at(ev) {
      var r = svg.getBoundingClientRect(), x = (ev.clientX - r.left) * (W / r.width);
      var i = Math.max(1, Math.min(n, Math.round((x - L) / (W - L - R) * n)));
      var p = pts[i - 1];
      cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", X(i)); dot.setAttribute("cy", Y(p.cum)); dot.setAttribute("visibility", "visible");
      showTip(ev, "#" + i + " " + (p.sym || "") + " " + sg(p.pnl, 3) + " USDT · итого " + sg(p.cum, 3) + (p.ts ? " · " + dt(p.ts) : ""));
    }
    hit.addEventListener("pointermove", at);
    hit.addEventListener("pointerleave", function () { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip(); });
    svg.appendChild(hit);
    b.appendChild(svg);
  });
}

// Столбики PnL по сделкам (последние N): вверх плюс, вниз минус
function barChart(box, pts, maxN) {
  var shown = pts.slice(-maxN);
  var sig = JSON.stringify(shown.map(function (p) { return [p.id, p.pnl]; }));
  chartHost(box, sig, function (b) {
    b.replaceChildren();
    if (!shown.length) { emptyState(b.appendChild(el("div", { class: "empty" })), "Нет сделок для столбиков"); return; }
    var W = Math.max(240, b.clientWidth), H = W < 420 ? 170 : 200, L = 42, R = 10, T = 10, B = 22;
    var vals = shown.map(function (p) { return p.pnl || 0; });
    var lo = Math.min(0, Math.min.apply(null, vals)), hi = Math.max(0, Math.max.apply(null, vals));
    if (hi - lo < 1e-9) hi = lo + 0.1;
    var pad = (hi - lo) * 0.08; if (lo < 0) lo -= pad; hi += pad;
    var n = shown.length, slot = (W - L - R) / n, bw = Math.max(2, Math.min(22, slot - 2));
    var Y = function (v) { return T + (hi - v) / (hi - lo) * (H - T - B); };
    var svg = sv("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img", "aria-label": "PnL по последним " + n + " сделкам" });
    axisY(svg, lo, hi, Y, L, W, R);
    shown.forEach(function (p, i) {
      var v = p.pnl || 0, x = L + slot * i + (slot - bw) / 2, y0 = Y(0), y1 = Y(v);
      var r = sv("rect", { x: x.toFixed(1), y: Math.min(y0, y1).toFixed(1), width: bw.toFixed(1), height: Math.max(1, Math.abs(y1 - y0)).toFixed(1), rx: Math.min(4, bw / 2).toFixed(1), class: v > 0 ? "bar-pos" : v < 0 ? "bar-neg" : "bar-zero" });
      svg.appendChild(r);
      var hit = sv("rect", { x: (L + slot * i).toFixed(1), y: T, width: slot.toFixed(1), height: H - T - B, fill: "transparent" });
      hit.addEventListener("pointermove", function (ev) { showTip(ev, (p.sym || "") + " " + sg(v, 3) + " USDT · " + dt(p.ts)); });
      hit.addEventListener("pointerleave", hideTip);
      svg.appendChild(hit);
    });
    b.appendChild(svg);
  });
}

// Горизонтальное распределение: rows = [{name, n, pnl}]; длина полоски = число сделок (нейтральный цвет), PnL текстом со знаком
function hbars(rows, unit) {
  if (!rows.length) return null;
  var mx = Math.max.apply(null, rows.map(function (r) { return r.n; })) || 1;
  return el("ul", { class: "hb" }, rows.map(function (r) {
    return el("li", null,
      el("span", { class: "hb-n", text: r.name, title: r.name }),
      el("span", { class: "hb-track", "aria-hidden": "true" }, el("i", { style: "width:" + Math.max(4, r.n / mx * 100).toFixed(1) + "%" })),
      el("span", { class: "hb-v", text: r.n + NB + (unit || "") }),
      el("span", { class: "hb-p " + cls(r.pnl), text: sg(r.pnl, 2) }));
  }));
}
