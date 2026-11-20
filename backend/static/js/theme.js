// Темы оформления: «Стекло» (по умолчанию), «Графит», «Ночь».
// Скрипт подключён синхронно в <head>, чтобы не было вспышки светлой темы.
(function () {
  "use strict";

  var KEY = "labbooking-theme";
  var THEMES = ["glass", "graphite", "night"];
  var root = document.documentElement;

  function readSaved() {
    try {
      return localStorage.getItem(KEY);
    } catch (e) {
      return null;
    }
  }

  function normalize(value) {
    if (value === "light") {
      return "graphite";
    }
    if (value === "dark") {
      return "night";
    }
    return THEMES.indexOf(value) > -1 ? value : null;
  }

  var params = new URLSearchParams(window.location.search);
  var queryTheme = normalize(params.get("theme"));
  var saved = normalize(readSaved());
  var chosen = queryTheme || saved;
  if (chosen) {
    root.dataset.theme = chosen;
  }

  function prefersDark() {
    return (
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches
    );
  }

  function currentTheme() {
    return root.dataset.theme || (prefersDark() ? "night" : "glass");
  }

  function updateMeta(theme) {
    var meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) {
      return;
    }
    var colors = {
      glass: "#f6f5fb",
      graphite: "#f4f2ed",
      night: "#0c0f11",
    };
    meta.setAttribute("content", colors[theme] || colors.glass);
  }

  function updateButtons() {
    var theme = currentTheme();
    document.querySelectorAll("[data-set-theme]").forEach(function (button) {
      var active = button.dataset.setTheme === theme;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
    updateMeta(theme);
  }

  function applyTheme(theme, persist) {
    root.dataset.theme = theme;
    if (persist) {
      try {
        localStorage.setItem(KEY, theme);
      } catch (e) {
        /* приватный режим — просто не запоминаем */
      }
    }
    updateButtons();
    document.dispatchEvent(
      new CustomEvent("labbooking:themechange", { detail: { theme: theme } })
    );
  }

  function init() {
    document.querySelectorAll("[data-set-theme]").forEach(function (button) {
      button.addEventListener("click", function () {
        applyTheme(button.dataset.setTheme, true);
      });
    });

    if (typeof window.matchMedia === "function") {
      var media = window.matchMedia("(prefers-color-scheme: dark)");
      var onSystemChange = function () {
        if (!root.dataset.theme) {
          updateButtons();
          document.dispatchEvent(
            new CustomEvent("labbooking:themechange", {
              detail: { theme: currentTheme() },
            })
          );
        }
      };
      if (media.addEventListener) {
        media.addEventListener("change", onSystemChange);
      } else if (media.addListener) {
        media.addListener(onSystemChange);
      }
    }

    updateButtons();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
