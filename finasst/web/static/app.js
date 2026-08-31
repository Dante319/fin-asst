/* fin-asst front end. No framework, no CDN, no build step -- this file is
   loaded as-is and works with the machine offline.

   Everything here is progressive enhancement. The pages render and are fully
   readable with JavaScript disabled: the theme follows the OS, the charts are
   server-rendered SVG with <title> tooltips, and every chart has a table view.
   This file adds the toggle, the count-up, and the richer hover layer. */

(function () {
  "use strict";

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------------------------------------------------------------- theme --
  // Applied by an inline script in <head> before first paint so there is no
  // flash; this half only handles the click.
  const toggle = document.querySelector(".theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      const root = document.documentElement;
      const current =
        root.dataset.theme ||
        (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = current === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try {
        localStorage.setItem("finasst-theme", next);
      } catch (err) {
        /* private browsing; the toggle still works for this page view */
      }
    });
  }

  // ------------------------------------------------------------- count-up --
  // Runs on the big stat numbers only. The final text is already in the DOM,
  // so a failure here leaves the correct number on screen.
  function countUp(el) {
    const target = parseFloat(el.dataset.count);
    if (!isFinite(target)) return;
    const finalText = el.textContent;
    const prefix = el.dataset.prefix || "";
    const suffix = el.dataset.suffix || "";
    const start = performance.now();
    const duration = 780;
    function frame(now) {
      const t = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - t, 3);
      if (t >= 1) {
        el.textContent = finalText;
        return;
      }
      const value = target * eased;
      el.textContent =
        prefix +
        Math.round(Math.abs(value)).toLocaleString("en-CA") +
        suffix;
      requestAnimationFrame(frame);
    }
    if (Math.abs(target) >= 1) requestAnimationFrame(frame);
  }
  if (!reduceMotion) document.querySelectorAll("[data-count]").forEach(countUp);

  // -------------------------------------------------------- chart tooltip --
  const wrap = document.querySelector(".chart-wrap");
  const tip = document.querySelector(".chart-wrap .tip");
  if (wrap && tip) {
    const money = (v) =>
      (v < 0 ? "-$" : "$") +
      Math.abs(v).toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

    wrap.querySelectorAll(".month-group").forEach((group) => {
      const show = () => {
        let rows = "";
        group.querySelectorAll(".bar").forEach((bar) => {
          const cls = bar.classList.contains("s1")
            ? "var(--series-1)"
            : bar.classList.contains("s2")
            ? "var(--series-2)"
            : "var(--series-3)";
          rows +=
            '<div class="tip-row"><span class="k"><i style="background:' +
            cls +
            '"></i>' +
            bar.dataset.label +
            "</span><span>" +
            money(parseFloat(bar.dataset.value)) +
            "</span></div>";
        });
        tip.innerHTML =
          '<span class="tip-month">' +
          group.dataset.month +
          (group.dataset.partial === "1" ? " (part month)" : "") +
          "</span>" +
          rows;
        const band = group.querySelector(".band");
        const wrapBox = wrap.getBoundingClientRect();
        const bandBox = band.getBoundingClientRect();
        tip.style.left = bandBox.left - wrapBox.left + bandBox.width / 2 + "px";
        tip.style.top = Math.max(bandBox.top - wrapBox.top, 0) + 4 + "px";
        tip.classList.add("show");
      };
      const hide = () => tip.classList.remove("show");
      group.addEventListener("mouseenter", show);
      group.addEventListener("mouseleave", hide);
      group.addEventListener("focusin", show);
      group.addEventListener("focusout", hide);
    });
  }

  // ------------------------------------------------ inline recategorising --
  document.addEventListener("change", async (event) => {
    const select = event.target;
    if (!select.classList || !select.classList.contains("cat-select")) return;
    const category = select.value;
    if (!category) return;

    const teachBox = document.getElementById("teach");
    const body = new FormData();
    body.append("category", category);
    body.append("teach", teachBox && teachBox.checked ? "true" : "false");

    select.disabled = true;
    try {
      const response = await fetch(`/transactions/${select.dataset.tx}/category`, {
        method: "POST",
        body,
      });
      if (!response.ok) throw new Error(await response.text());
      const row = document.getElementById(`tx-${select.dataset.tx}`);
      row.outerHTML = await response.text();
    } catch (err) {
      select.disabled = false;
      const row = document.getElementById(`tx-${select.dataset.tx}`);
      if (row) row.classList.add("save-failed");
      console.error("Could not save that category:", err);
    }
  });
})();
