"use strict";
// Тема до отрисовки страницы (подключён в <head> синхронно), чтобы не было вспышки не той темы.
// Значение: "light" | "dark" | "auto" (следовать системе). По умолчанию тёмная. localStorage может быть недоступен: тогда тёмная.
(function () {
  var t = "dark";
  try { var v = localStorage.getItem("tbdash.theme"); if (v === "light" || v === "auto") t = v; } catch (e) { /* приватный режим: тёмная */ }
  if (t !== "auto") document.documentElement.setAttribute("data-theme", t);
})();
