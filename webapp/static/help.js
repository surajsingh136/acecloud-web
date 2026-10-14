/* CloudWeb - help page: friendly nudge back to app when signed in */
"use strict";
(async function () {
  try {
    var r = await fetch("/api/me");
    if (!r.ok) return;
    var d = await r.json();
    if (d && d.ok) {
      var a = document.querySelector("a.home");
      if (a) { a.textContent = "\u2190 Back to " + (d.name || "app"); d.href = "/"; }
    }
  } catch (e) {}
})();
