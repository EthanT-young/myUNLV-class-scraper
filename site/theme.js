// Light/dark theme. Loaded without defer so the saved choice is applied before
// first paint (no flash). "system" means no data-theme attribute, so the CSS
// follows prefers-color-scheme. The choice is saved only in this browser.
(() => {
  const KEY = "unlv:theme"; // namespaced: other apps share this origin
  const root = document.documentElement;
  const valid = (v) => (v === "light" || v === "dark" ? v : "system");
  const load = () => { try { return valid(localStorage.getItem(KEY)); } catch { return "system"; } };
  const save = (v) => {
    try {
      if (v === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, v);
    } catch { /* storage unavailable: the choice lasts for this page only */ }
  };
  const apply = (v) => {
    if (v === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", v);
  };

  apply(load());

  const sync = () => {
    const picker = document.getElementById("theme-picker");
    if (picker) picker.value = root.getAttribute("data-theme") || "system";
  };

  document.addEventListener("DOMContentLoaded", () => {
    const picker = document.getElementById("theme-picker");
    if (!picker) return;
    sync();
    picker.addEventListener("change", () => {
      const v = valid(picker.value);
      apply(v);
      save(v);
    });
  });

  // Back/forward (and bfcache restores) can bring back a stale picker value or
  // a theme changed in another tab meanwhile.
  window.addEventListener("pageshow", () => { apply(load()); sync(); });

  // Keep other open tabs in step.
  window.addEventListener("storage", (e) => {
    if (e.key === KEY || e.key === null) { apply(load()); sync(); }
  });
})();
