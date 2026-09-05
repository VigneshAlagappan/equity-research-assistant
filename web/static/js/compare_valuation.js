/* Compare page, Valuation Model tab (web/templates/compare.html) -- the
   exact same assumptions-driven Growth Projection / intrinsic-value widget
   as a single company's own Valuation Model tab
   (web/static/js/valuation_dashboard_interactive.js), just picking which
   ONE company to show it for from whichever companies are currently
   selected in Quick Comparison (window.CompareShared) rather than a second
   company search -- same one-directional "Quick Comparison drives who's
   available here" convention compare_detailed.js already established.

   The model itself is inherently single-company (one set of assumptions,
   one intrinsic-value walk), so this tab doesn't attempt a side-by-side
   multi-company version of it -- a segmented company picker swaps which
   company's live feed is loaded into the same widget. */
(function () {
  "use strict";

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Mirrors the #valuation-dashboard-interactive markup web/templates/
  // company.html renders server-side (same ids -- valuation_dashboard_
  // interactive.js's init() reads/writes them by id, scoped to whatever
  // root element is passed in). Built as a string here, not shared via a
  // Jinja include, since this copy is rebuilt fresh on every company
  // switch (see renderShellFor below) rather than rendered once at page
  // load.
  function shellHtml() {
    return (
      '<div id="valuation-dashboard-interactive" class="vm-layout">' +
        '<aside class="vm-sidebar">' +
          '<nav class="vm-nav">' +
            '<button type="button" class="vm-nav-btn active" data-section="growth">Growth Projection</button>' +
          '</nav>' +
        '</aside>' +
        '<div class="vm-main">' +
          '<div class="vm-top-row">' +
            '<div class="card vm-assumptions-card">' +
              '<div class="card-kicker">Assumptions</div>' +
              '<div class="vm-assumptions-grid">' +
                '<div class="field"><label for="vmi-ror">Required rate of return (%)</label>' +
                  '<input class="input" id="vmi-ror" type="number" step="1" min="5" max="40" value="22"></div>' +
                '<div class="field"><label for="vmi-proj-growth">Projected growth rate (%)</label>' +
                  '<input class="input" id="vmi-proj-growth" type="number" step="1" min="0" max="50" value="15"></div>' +
                '<div class="field"><label for="vmi-terminal-growth">Terminal growth rate (%)</label>' +
                  '<input class="input" id="vmi-terminal-growth" type="number" step="0.5" min="0" max="10" value="1"></div>' +
                '<div class="field"><label for="vmi-price-growth">Projected annualized price growth (%)</label>' +
                  '<input class="input" id="vmi-price-growth" type="number" step="1" min="-20" max="50" value="12"></div>' +
                '<div class="field"><label for="vmi-start-year">CAGR window</label>' +
                  '<div class="vm-year-range">' +
                    '<select class="input" id="vmi-start-year" aria-label="Eval start year"></select>' +
                    '<span class="vm-year-sep">&ndash;</span>' +
                    '<select class="input" id="vmi-end-year" aria-label="Eval end year"></select>' +
                  '</div>' +
                '</div>' +
                '<div class="field"><label for="vmi-price">Current stock price</label>' +
                  '<input class="input" id="vmi-price" type="number" step="1" value="0"></div>' +
              '</div>' +
            '</div>' +
            '<div id="vmi-walk" class="vm-walk-card"></div>' +
          '</div>' +
          '<div id="vmi-content" class="vm-content"><p class="muted">Loading model&hellip;</p></div>' +
        '</div>' +
      '</div>'
    );
  }

  function init() {
    const panel = document.getElementById("compare-panel-valuation");
    const contentRoot = document.getElementById("cmp-valuation-root");
    const toggleRoot = document.getElementById("cmp-val-company-toggle");
    if (!panel || !contentRoot || !toggleRoot) return;
    const urlTemplate = panel.dataset.valuationUrlTemplate;

    const state = { companies: [], selectedId: null };

    // Full rebuild (not a data-url swap on the existing DOM) -- valuation_
    // dashboard_interactive.js's init() binds its input/nav listeners once
    // per call and closes over that specific root's elements; calling it a
    // second time on the SAME nodes would double-bind every listener and
    // risk an older, slower-to-resolve fetch overwriting a newer one's
    // result. Fresh nodes each time sidesteps both: old listeners die with
    // the old DOM, and a late-arriving stale fetch just writes into
    // detached elements nobody sees.
    function renderShellFor(companyId) {
      contentRoot.innerHTML = shellHtml();
      const shellRoot = contentRoot.querySelector("#valuation-dashboard-interactive");
      shellRoot.dataset.url = urlTemplate.replace("__ID__", encodeURIComponent(companyId));
      if (window.SignalsValuationInteractive) window.SignalsValuationInteractive.init(shellRoot);
    }

    function renderToggle() {
      if (state.companies.length === 0) {
        toggleRoot.innerHTML = "";
        contentRoot.innerHTML = '<p class="muted">Select companies in Quick Comparison to see their valuation model here.</p>';
        return;
      }
      // Reuses the Detailed tab's own period-toggle pill styling
      // (.cmp-period-toggle/.cmp-period-btn) -- a segmented control of up
      // to 4 buttons is the same shape either way, no new CSS needed.
      toggleRoot.innerHTML = state.companies.map((c) => (
        '<button type="button" class="cmp-period-btn' + (c.id === state.selectedId ? " active" : "") + '" data-val-company="' + escapeHtml(c.id) + '">' +
          escapeHtml(c.name) +
        "</button>"
      )).join("");
      toggleRoot.querySelectorAll("[data-val-company]").forEach((btn) => {
        btn.addEventListener("click", () => {
          if (btn.dataset.valCompany === state.selectedId) return;
          state.selectedId = btn.dataset.valCompany;
          renderToggle();
          renderShellFor(state.selectedId);
        });
      });
    }

    // window.CompareShared.subscribeQuickCompanies -- same sync convention
    // as compare_detailed.js's syncFromQuick, just picking one company to
    // actively display instead of unioning every selected company's data.
    // The currently-selected company is preserved across a sync as long as
    // it's still present in Quick's list; otherwise the first company
    // becomes the new selection.
    function syncFromQuick(quickList) {
      state.companies = quickList.slice();
      if (state.companies.length === 0) {
        state.selectedId = null;
      } else if (!state.companies.some((c) => c.id === state.selectedId)) {
        state.selectedId = state.companies[0].id;
      }
      renderToggle();
      if (state.selectedId) renderShellFor(state.selectedId);
    }

    window.CompareShared.subscribeQuickCompanies(syncFromQuick);
    syncFromQuick(window.CompareShared.getQuickCompanies());
  }

  document.addEventListener("DOMContentLoaded", init);
})();
