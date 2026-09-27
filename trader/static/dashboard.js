// Dashboard behaviour. No inline scripts (CSP script-src 'self'); all text via textContent.
(function () {
  "use strict";

  // ---- theme toggle -------------------------------------------------------------------
  function currentTheme() {
    var set = document.documentElement.getAttribute("data-theme");
    if (set) return set;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-theme-toggle]");
    if (!b) return;
    var next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("trader-theme", next); } catch (e) {}
    labelTheme();
  });
  function labelTheme() {
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
      b.textContent = currentTheme() === "dark" ? "☀ Light" : "☾ Dark";
      b.setAttribute("aria-label", "Switch to " + (currentTheme() === "dark" ? "light" : "dark") + " theme");
    });
  }

  // ---- "updated X s ago" and the countdown to the next run ---------------------------------
  function fmtDur(ms) {
    var s = Math.max(0, Math.round(ms / 1000)), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    if (h > 0) return h + " h " + m + " m";
    if (m > 0) return m + " m " + (s % 60) + " s";
    return s + " s";
  }
  function tick() {
    var now = Date.now();
    document.querySelectorAll("[data-updated]").forEach(function (el) {
      var t = Date.parse(el.getAttribute("data-updated"));
      if (!isNaN(t)) el.textContent = "updated " + fmtDur(now - t) + " ago";
    });
    document.querySelectorAll("[data-next-run]").forEach(function (el) {
      var t = Date.parse(el.getAttribute("data-next-run"));
      el.textContent = isNaN(t) ? "next run: not scheduled" : (t > now ? "next run in " + fmtDur(t - now) : "next run: due now");
    });
  }

  // ---- chart crosshair + tooltip (same data format as the HTML reports) ----------------------
  function bindCharts(root) {
    (root || document).querySelectorAll(".chart[data-series]").forEach(function (box) {
      if (box.dataset.bound) return;
      box.dataset.bound = "1";
      var d = JSON.parse(box.dataset.series), svg = box.querySelector("svg"), tip = box.querySelector(".tip"),
          hair = svg.querySelector(".hair"), n = d.dates.length;
      function show(ev) {
        var r = svg.getBoundingClientRect(), sx = (ev.clientX - r.left) * (d.w / r.width);
        var t = Math.min(1, Math.max(0, (sx - d.x0) / (d.x1 - d.x0))), i = n > 1 ? Math.round(t * (n - 1)) : 0;
        var x = n > 1 ? d.x0 + (d.x1 - d.x0) * i / (n - 1) : (d.x0 + d.x1) / 2;
        hair.setAttribute("x1", x); hair.setAttribute("x2", x); hair.style.display = "";
        tip.replaceChildren();
        var dd = document.createElement("div"); dd.className = "d"; dd.textContent = d.dates[i]; tip.appendChild(dd);
        d.series.forEach(function (s) {
          var row = document.createElement("div"); row.className = "row";
          var b = document.createElement("b"), v = s.values[i];
          b.textContent = s.fmt === "pct" ? (v * 100).toFixed(2) + "%" : "$" + Math.round(v).toLocaleString("en-US");
          var sp = document.createElement("span"), k = document.createElement("i");
          k.style.borderColor = s.color; sp.appendChild(k); sp.appendChild(document.createTextNode(s.name));
          row.appendChild(b); row.appendChild(sp); tip.appendChild(row);
        });
        tip.style.display = "block";
        var px = (x / d.w) * r.width;
        tip.style.left = Math.max(4, Math.min(px + 12, r.width - tip.offsetWidth - 4)) + "px"; tip.style.top = "8px";
      }
      svg.addEventListener("pointermove", show);
      svg.addEventListener("pointerleave", function () { tip.style.display = "none"; hair.style.display = "none"; });
    });
  }

  // ---- live refresh: keep expanded trades open; flash only what changed ------------------------
  var openTrades = [], sigs = {};
  function snapshot() {
    openTrades = Array.prototype.map.call(document.querySelectorAll("details[data-trade][open]"),
      function (e) { return e.getAttribute("data-trade"); });
    sigs = {};
    document.querySelectorAll("[data-key][data-sig]").forEach(function (e) { sigs[e.getAttribute("data-key")] = e.getAttribute("data-sig"); });
  }
  function restore() {
    openTrades.forEach(function (id) {
      var el = document.querySelector('details[data-trade="' + CSS.escape(id) + '"]');
      if (el) el.open = true;
    });
    document.querySelectorAll("[data-key][data-sig]").forEach(function (e) {
      var k = e.getAttribute("data-key");
      if (!(k in sigs) || sigs[k] !== e.getAttribute("data-sig")) {
        if (Object.keys(sigs).length) e.classList.add("changed");  // not on first load
      }
    });
  }
  document.addEventListener("htmx:beforeSwap", snapshot);
  function stampReceived() {  // "updated X ago" = since this browser got the data, not server vs phone clock
    var t = new Date().toISOString();
    document.querySelectorAll("[data-updated]").forEach(function (el) { el.setAttribute("data-updated", t); });
  }
  document.addEventListener("htmx:afterSwap", function (ev) { stampReceived(); restore(); bindCharts(ev.target); labelTheme(); tick(); });

  document.addEventListener("DOMContentLoaded", function () {
    stampReceived(); snapshot(); bindCharts(); labelTheme(); tick(); setInterval(tick, 1000);
  });
})();
