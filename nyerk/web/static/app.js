// Nyerk admin: Discord timestamp preview, schedule fields, confirmations.
(function () {
  "use strict";

  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const FORMATS = {
    t: { timeStyle: "short" },
    T: { timeStyle: "medium" },
    d: { dateStyle: "short" },
    D: { dateStyle: "long" },
    f: { dateStyle: "long", timeStyle: "short" },
    F: { weekday: "long", year: "numeric", month: "long", day: "numeric", hour: "numeric", minute: "2-digit" },
  };

  function relative(seconds) {
    const abs = Math.abs(seconds);
    const units = [["year", 31536000], ["month", 2592000], ["day", 86400], ["hour", 3600], ["minute", 60], ["second", 1]];
    for (const [unit, size] of units) {
      if (abs >= size || unit === "second") return rtf.format(Math.round(seconds / size), unit);
    }
  }

  // Renders like Discord does, in the viewer's own time zone.
  function renderTimes() {
    const now = Date.now() / 1000;
    document.querySelectorAll("time[data-ts]").forEach((el) => {
      const ts = Number(el.dataset.ts);
      const style = el.dataset.style || "f";
      el.textContent = style === "R"
        ? relative(ts - now)
        : new Intl.DateTimeFormat(undefined, FORMATS[style] || FORMATS.f).format(new Date(ts * 1000));
      el.title = new Date(ts * 1000).toString();
    });
  }
  renderTimes();
  setInterval(renderTimes, 15000);

  // Show only the parameter fieldset for the chosen schedule type.
  const typeSelect = document.querySelector("[data-schedule-type]");
  if (typeSelect) {
    const sync = () => {
      document.querySelectorAll("[data-params-for]").forEach((fs) => {
        const on = fs.dataset.paramsFor === typeSelect.value;
        fs.hidden = !on;
        fs.disabled = !on;
      });
      document.querySelectorAll("[data-boss-only]").forEach((el) => { el.hidden = typeSelect.value !== "kill_report"; });
    };
    typeSelect.addEventListener("change", sync);
    sync();
  }

  // Destructive actions ask once. Everything else acts immediately.
  document.addEventListener("submit", (e) => {
    const msg = (e.submitter && e.submitter.dataset.confirm) || e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Maintenance override: hide time fields when cancelling a week.
  document.querySelectorAll("[data-override-action]").forEach((sel) => {
    const form = sel.closest("form");
    const sync = () => form.querySelectorAll("[data-times]").forEach((el) => { el.hidden = sel.value === "cancel"; });
    sel.addEventListener("change", sync);
    sync();
  });
})();

// Grouped navigation: one menu open at a time; click outside or Escape closes it.
(function () {
  "use strict";
  const menus = Array.from(document.querySelectorAll("details.menu"));
  if (!menus.length) return;
  const closeAll = (except) => menus.forEach((m) => { if (m !== except) m.open = false; });
  menus.forEach((m) => m.addEventListener("toggle", () => { if (m.open) closeAll(m); }));
  document.addEventListener("click", (e) => { if (!e.target.closest("details.menu")) closeAll(null); });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const open = menus.find((m) => m.open);
    if (open) { open.open = false; open.querySelector("summary").focus(); }
  });
})();

// Search box in the top bar: live suggestions as you type.
(function () {
  "use strict";
  const input = document.getElementById("site-search");
  const list = document.getElementById("site-search-list");
  if (!input || !list) return;
  const cache = new Map();
  let timer = 0, active = -1, items = [], seq = 0;

  function close() {
    list.hidden = true; active = -1;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function select(i) {
    const rows = list.querySelectorAll("li[role=option]");
    rows.forEach((li, n) => li.setAttribute("aria-selected", n === i ? "true" : "false"));
    active = i;
    if (i >= 0 && rows[i]) {
      input.setAttribute("aria-activedescendant", rows[i].id);
      rows[i].scrollIntoView({ block: "nearest" });
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  function render(q, results) {
    items = results;
    list.replaceChildren();
    results.forEach((r, n) => {
      const li = document.createElement("li");
      li.id = "site-search-" + n; li.setAttribute("role", "option");
      const a = document.createElement("a");
      a.href = r.url; a.tabIndex = -1;
      const page = document.createElement("span"); page.className = "s-page"; page.textContent = r.page;
      const label = document.createElement("span"); label.className = "s-label"; label.textContent = r.label;
      a.append(page, label);
      if (r.detail) {
        const d = document.createElement("span"); d.className = "s-detail"; d.textContent = r.detail;
        a.append(d);
      }
      li.append(a); list.append(li);
    });
    if (!results.length) {
      const li = document.createElement("li"); li.className = "s-empty";
      li.textContent = "Nothing matches. Press Enter to search anyway.";
      list.append(li);
    }
    const all = document.createElement("li"); all.className = "s-all";
    const a = document.createElement("a"); a.href = "/search?q=" + encodeURIComponent(q); a.tabIndex = -1;
    a.textContent = "See all results for “" + q + "”";
    all.append(a); list.append(all);
    list.hidden = false; input.setAttribute("aria-expanded", "true"); select(-1);
  }

  async function suggest() {
    const q = input.value.trim().slice(0, 80);
    if (q.length < 2) { close(); return; }
    if (cache.has(q)) { render(q, cache.get(q)); return; }
    const mine = ++seq;
    try {
      const resp = await fetch("/search/suggest?q=" + encodeURIComponent(q), {
        headers: { Accept: "application/json" }, credentials: "same-origin",
      });
      if (!resp.ok) return;  // rate limited or signed out: keep the box usable, just no list
      const data = await resp.json();
      if (cache.size > 100) cache.clear();
      cache.set(q, data);
      if (mine === seq) render(q, data);
    } catch (e) { /* offline: no suggestions */ }
  }

  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(suggest, 180); });
  input.addEventListener("focus", () => { if (input.value.trim().length >= 2) suggest(); });
  input.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    if (e.key === "ArrowDown") { e.preventDefault(); select(Math.min(active + 1, items.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); select(Math.max(active - 1, -1)); }
    else if (e.key === "Escape") { close(); }
    else if (e.key === "Enter" && active >= 0 && items[active]) { e.preventDefault(); location.href = items[active].url; }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".site-search")) close(); });
})();

// Copy buttons: data-copy="#input" copies that field's value.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const field = document.querySelector(btn.dataset.copy);
  if (!field) return;
  try { await navigator.clipboard.writeText(field.value); }
  catch (err) { field.select(); document.execCommand("copy"); }
  const label = btn.textContent;
  btn.textContent = "Copied";
  setTimeout(() => { btn.textContent = label; }, 1500);
});
