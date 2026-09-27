// Runs before first paint: apply the saved theme choice (dark is the default; the OS setting
// is respected when nothing was chosen). Storage can be blocked, so never let it throw.
(function () {
  try {
    var t = localStorage.getItem("trader-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) {}
})();
