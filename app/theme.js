/* Blake2b ASIC Control: light / dark switch.
   Loaded in <head> on every page so the saved choice applies before the page draws.
   Any element with data-theme-toggle becomes the switch. The choice is kept in this
   browser only (localStorage); if storage is blocked it simply resets to dark. */
(function () {
  // Every request this app's pages make to the app itself carries X-B2AC: 1. The server refuses
  // POST / DELETE without it, so a page from another site, or from another app on the same Umbrel,
  // can't make changes through your login (it can't add the header without a CORS preflight).
  if (window.fetch && !window.fetch.__b2ac) {
    var f0 = window.fetch;
    var f1 = function (input, init) {
      try {
        var url = typeof input === "string" ? input : (input && input.url) || "";
        var same = !/^[a-z][a-z0-9+.-]*:/i.test(url) || url.indexOf(location.origin + "/") === 0;
        if (same) {
          init = Object.assign({}, init || {});
          var h = new Headers(init.headers || (typeof input !== "string" && input && input.headers) || {});
          h.set("X-B2AC", "1");
          init.headers = h;
        }
      } catch (e) { /* fall back to a plain request */ }
      return f0.call(this, input, init);
    };
    f1.__b2ac = true;
    window.fetch = f1;
  }

  var KEY = "gc-theme";
  function load() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function save(v) { try { localStorage.setItem(KEY, v); } catch (e) { /* storage blocked: fine */ } }
  var theme = load() === "light" ? "light" : "dark";

  function label() {
    var btns = document.querySelectorAll("[data-theme-toggle]");
    for (var i = 0; i < btns.length; i++) {
      btns[i].innerHTML = theme === "light" ? '☾<span class="tl"> Dark</span>' : '☀<span class="tl"> Light</span>';
      btns[i].setAttribute("title", "Switch to " + (theme === "light" ? "black" : "white") + " background");
      btns[i].setAttribute("aria-pressed", theme === "light" ? "true" : "false");
    }
  }
  function apply() {
    document.documentElement.setAttribute("data-theme", theme);
    label();
  }
  apply();
  document.addEventListener("DOMContentLoaded", label);
  document.addEventListener("click", function (e) {
    var b = e.target && e.target.closest ? e.target.closest("[data-theme-toggle]") : null;
    if (!b) return;
    theme = theme === "light" ? "dark" : "light";
    save(theme);
    apply();
    document.dispatchEvent(new CustomEvent("themechange", { detail: theme }));
  });
  window.gcTheme = function () { return theme; };
})();
