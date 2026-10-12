(() => {
  // ── Tooltips: every data mark carries data-tip ───────────────────────────────────────────
  const tip = document.getElementById("tip");
  const show = (el, x, y) => {
    tip.textContent = el.getAttribute("data-tip");
    tip.style.opacity = 1;
    const r = tip.getBoundingClientRect();
    tip.style.left = Math.max(8, Math.min(x + 14, innerWidth - r.width - 8)) + "px";
    tip.style.top = Math.max(8, y - r.height - 12) + "px";
  };
  const hide = () => { tip.style.opacity = 0; };
  document.addEventListener("pointermove", e => {
    const el = e.target.closest("[data-tip]");
    el ? show(el, e.clientX, e.clientY) : hide();
  });
  document.addEventListener("focusin", e => {
    const el = e.target.closest("[data-tip]");
    if (el) { const r = el.getBoundingClientRect(); show(el, r.left + r.width / 2, r.top); }
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("scroll", hide, { passive: true });

  // ── Live indicator: how old the data is; amber once a scheduled refresh looks missed ─────
  const live = document.querySelector(".live[data-ts]");
  if (live) {
    const ts = Date.parse(live.dataset.ts);
    const text = live.querySelector(".live-text");
    const ago = ms => {
      const m = Math.round(ms / 60000);
      if (m < 1) return "just now";
      if (m < 60) return m + " min ago";
      const h = Math.round(m / 60);
      if (h < 48) return h + " h ago";
      return Math.round(h / 24) + " days ago";
    };
    const tick = () => {
      if (isNaN(ts)) return;
      const age = Date.now() - ts;
      const stale = age > 6 * 3600 * 1000;
      live.classList.toggle("stale", stale);
      text.textContent = (stale ? "Last updated " : "Live · updated ") + ago(age);
    };
    tick();
    setInterval(tick, 60000);
  }

  // ── Theme: automatic → light → dark, remembered on this device when storage allows ───────
  const btn = document.querySelector(".theme-btn");
  const root = document.documentElement;
  const icons = { auto: "◐", light: "☀", dark: "☾" };
  const names = { auto: "automatic", light: "light", dark: "dark" };
  const apply = mode => {
    if (mode === "auto") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", mode);
    if (btn) { btn.textContent = icons[mode]; btn.setAttribute("aria-label", "Colour theme: " + names[mode]); }
  };
  let mode = "auto";
  try { mode = localStorage.getItem("ratings-theme") || "auto"; } catch (e) { /* storage blocked */ }
  apply(mode);
  if (btn) btn.addEventListener("click", () => {
    mode = { auto: "light", light: "dark", dark: "auto" }[mode];
    apply(mode);
    try { localStorage.setItem("ratings-theme", mode); } catch (e) { /* storage blocked */ }
  });

  // ── Long tables: show the first rows, expand on request (all rows print) ─────────────────
  document.querySelectorAll("[data-collapse]").forEach(tbody => {
    const keep = +tbody.dataset.collapse;
    const rows = [...tbody.rows].slice(keep);
    if (!rows.length) return;
    rows.forEach(r => r.classList.add("folded"));
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "more-btn";
    btn.textContent = "Show all " + (keep + rows.length);
    btn.setAttribute("aria-expanded", "false");
    btn.addEventListener("click", () => {
      const open = btn.getAttribute("aria-expanded") !== "true";
      rows.forEach(r => r.classList.toggle("folded", !open));
      btn.setAttribute("aria-expanded", String(open));
      btn.textContent = open ? "Show fewer" : "Show all " + (keep + rows.length);
    });
    tbody.closest(".table-scroll, table").after(btn);
  });

  // Print every collapsed detail.
  addEventListener("beforeprint", () => document.querySelectorAll("details").forEach(d => (d.open = true)));
})();
