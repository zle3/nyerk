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
    const msg = e.target.dataset.confirm;
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
