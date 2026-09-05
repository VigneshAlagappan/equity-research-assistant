/* Compare page, Detailed Comparison tab (web/templates/compare.html) --
   pick any number of attributes and up to MAX_COMPANIES companies, see the
   FULL recorded period history side by side, companies nested under each
   attribute rather than as columns (the transpose of the Charts tab's own
   multi-company overlay -- same METRICS feed, same union-of-periods/
   union-of-attributes problem, rendered as a table instead of an SVG
   chart). Reuses window.SignalsCharts (charts_overlay.js's exported
   attribute/period-union helpers) rather than a second implementation of
   that merge logic.

   Deliberately no range selector (unlike Charts' Last N / Max toggle) --
   this view's whole point is "full recorded history per company, not a
   fixed window" (see the footnote this file renders), so it always shows
   everything the union of loaded companies has. */
(function () {
  "use strict";

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function init() {
    const panel = document.getElementById("compare-panel-detailed");
    const tableRoot = document.getElementById("cmp-detailed-table");
    if (!panel || !tableRoot) return;
    const C = window.SignalsCharts;
    if (!C) {
      tableRoot.innerHTML = '<div class="empty-state">Could not load the comparison engine (charts_overlay.js).</div>';
      return;
    }
    const chartsUrlTemplate = panel.dataset.chartsUrlTemplate;
    const MAX_COMPANIES = C.MAX_COMPARISONS || 4;

    const pillsRoot = document.getElementById("cmp-pills");
    const attrsBtn = document.getElementById("cmp-attrs-btn");
    const attrsPanel = document.getElementById("cmp-attrs-panel");
    const attrsCount = document.getElementById("cmp-attrs-count");
    const periodBtns = Array.prototype.slice.call(document.querySelectorAll("[data-cmp-period]"));

    const state = {
      // [{id, name}], driven entirely by window.CompareShared (Quick
      // Comparison's own selection, see syncFromQuick below) -- this tab
      // has no add/remove of its own; capped to MAX_COMPANIES defensively,
      // though Quick Comparison's own slot count already enforces that.
      companies: [],
      periodType: "annual",
      selectedAttrs: new Set(), // attrId strings, e.g. "incomeStatement:netProfit"
      cache: {}, // cacheKey(companyId, periodType) -> ds {PERIODS, PERIOD_KEYS, CURRENCY, attributes, byId}
      attrsOpen: false,
      fxRate: null, // {rate, as_of} once fetched via window.CompareShared, or {rate: null} if unavailable
      // Sections the user has explicitly collapsed -- tracked separately
      // from the <details> DOM nodes themselves because renderTable()
      // rebuilds the whole table's innerHTML on every state change (a new
      // company, a toggled attribute); without this, every rebuild would
      // silently re-expand anything the user had just collapsed. A
      // section not in this set defaults open -- every section shown here
      // has at least one attribute the user explicitly checked, so
      // "hidden until proven interesting" would be backwards.
      collapsedSections: new Set(),
    };

    function cacheKey(companyId, periodType) {
      return companyId + "|" + periodType;
    }

    function loadCompany(companyId, periodType) {
      const key = cacheKey(companyId, periodType);
      if (state.cache[key]) return Promise.resolve(state.cache[key]);
      const url = chartsUrlTemplate.replace("__ID__", encodeURIComponent(companyId)) + "&period_type=" + periodType;
      return fetch(url)
        .then((r) => {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then((data) => {
          const attributes = C.flattenAttributes(data.METRICS || {});
          const byId = {};
          attributes.forEach((a) => { byId[C.attrId(a.section, a.key)] = a; });
          const ds = {
            PERIODS: data.PERIODS || [], PERIOD_KEYS: data.PERIOD_KEYS || [],
            CURRENCY: data.CURRENCY || "INR", attributes: attributes, byId: byId,
          };
          state.cache[key] = ds;
          return ds;
        });
    }

    function loadedDatasets() {
      return state.companies
        .map((c) => ({ company: c, ds: state.cache[cacheKey(c.id, state.periodType)] }))
        .filter((x) => x.ds);
    }

    // First add with nothing picked yet gets a sensible starting point
    // (Net Profit + Revenue, if the company reports them) instead of an
    // empty table -- same "don't land on a blank view" reasoning
    // charts_overlay.js's own pickDefaults()/DEFAULT_PICK_PAIRS follows,
    // just picking rows instead of a chart's L/R pair.
    function pickDefaultAttrs(ds) {
      if (state.selectedAttrs.size > 0) return;
      ["incomeStatement:netProfit", "incomeStatement:earnings"].forEach((id) => {
        if (ds.byId[id]) state.selectedAttrs.add(id);
      });
    }

    // Read-only labels -- company selection lives entirely in Quick
    // Comparison now (see syncFromQuick), so there's nothing to remove here.
    function renderPills() {
      pillsRoot.innerHTML = state.companies.length
        ? state.companies.map((c) => '<span class="cmp-pill">' + escapeHtml(c.name) + "</span>").join("")
        : '<span class="muted">Select companies in Quick Comparison to see them here.</span>';
    }

    function renderAttrsPanel() {
      const loaded = loadedDatasets();
      const unionAttrs = C.unionAttributes(loaded.map((x) => x.ds));
      // Drop a selection that no longer exists for any loaded company
      // (e.g. the one company reporting it just got removed) rather than
      // silently keeping a phantom count.
      const validIds = new Set(unionAttrs.map((a) => C.attrId(a.section, a.key)));
      state.selectedAttrs.forEach((id) => { if (!validIds.has(id)) state.selectedAttrs.delete(id); });

      attrsCount.textContent = String(state.selectedAttrs.size);

      if (unionAttrs.length === 0) {
        attrsPanel.innerHTML = '<p class="muted" style="padding:8px;margin:0;">Add a company to see its attributes.</p>';
        return;
      }
      let html = "", lastSection = null;
      unionAttrs.forEach((a) => {
        if (a.section !== lastSection) {
          html += '<div class="cmp-attrs-section-title">' + escapeHtml(C.SECTION_TITLES[a.section] || a.section) + "</div>";
          lastSection = a.section;
        }
        const id = C.attrId(a.section, a.key);
        const checked = state.selectedAttrs.has(id) ? " checked" : "";
        html += (
          '<label class="cmp-attr-item">' +
            '<input type="checkbox" data-attr-id="' + escapeHtml(id) + '"' + checked + ">" +
            escapeHtml(a.label) +
          "</label>"
        );
      });
      attrsPanel.innerHTML = html;
      attrsPanel.querySelectorAll("[data-attr-id]").forEach((cb) => {
        cb.addEventListener("change", () => {
          if (cb.checked) state.selectedAttrs.add(cb.dataset.attrId);
          else state.selectedAttrs.delete(cb.dataset.attrId);
          attrsCount.textContent = String(state.selectedAttrs.size);
          renderTable();
        });
      });
    }

    function fmtValue(value, unit, currency) {
      if (value === null || value === undefined || Number.isNaN(value) || !Number.isFinite(value)) {
        return '<span class="cmp-not-reported">-</span>';
      }
      const V = window.SignalsValuation;
      return V ? escapeHtml(V.fmt(value, unit, currency)) : escapeHtml(String(value));
    }

    // unit -> which of the two conversion formulas applies (see
    // compare.js's toUsdContext(), where both were first built and
    // verified against real HDFC-Bank-vs-Apple figures). "rupee" (Price &
    // Volume's Close Price row) is deliberately excluded: valuation_
    // dashboard.js's fmt() treats "rupee" as a legacy case that always
    // renders "₹" regardless of the currency argument, so converting the
    // number without also fixing that formatter would show a USD-scaled
    // figure under a rupee sign -- worse than leaving it unconverted. Left
    // as a known gap, not silently patched around here.
    const CONVERTIBLE_UNITS = { big: true, perShare: true, sharesCount: true };

    function convertToUsd(value, unit, fxRate) {
      if (value === null || value === undefined || !CONVERTIBLE_UNITS[unit]) return value;
      if (unit === "big") return (value * 10) / fxRate; // Crore -> USD million
      if (unit === "sharesCount") return value * 10; // Crore -> Million, no fx division -- a share count isn't money
      return value / fxRate; // perShare: raw rupees -> dollars
    }

    async function renderTable() {
      const loaded = loadedDatasets();
      if (loaded.length === 0) {
        tableRoot.innerHTML = '<p class="muted">Add at least one company above to see its history.</p>';
        return;
      }
      const union = C.unionPeriods(loaded.map((x) => x.ds));
      const unionAttrs = C.unionAttributes(loaded.map((x) => x.ds));
      const selected = unionAttrs.filter((a) => state.selectedAttrs.has(C.attrId(a.section, a.key)));
      if (selected.length === 0) {
        tableRoot.innerHTML = '<p class="muted">Pick at least one attribute above.</p>';
        return;
      }

      const currencies = Array.from(new Set(loaded.map((x) => x.ds.CURRENCY || "INR")));
      const mixed = currencies.length > 1;
      if (mixed && state.fxRate === null) state.fxRate = await window.CompareShared.getUsdInrRate();
      const conversionUnavailable = mixed && (!state.fxRate || !state.fxRate.rate);

      const headerCells = union.PERIODS.map((p) => "<th>" + escapeHtml(p) + "</th>").join("");

      // Group the selected attributes by section (still SECTION_ORDER'd,
      // since `selected` is a filter over `unionAttrs`) -- one <details>
      // per section instead of one continuous table mixing every
      // section's rows, so P&L/Balance Sheet/Cash Flow can each be
      // skimmed as a self-contained grid or collapsed out of the way.
      const bySection = [];
      selected.forEach((attr) => {
        let group = bySection[bySection.length - 1];
        if (!group || group.section !== attr.section) {
          group = { section: attr.section, attrs: [] };
          bySection.push(group);
        }
        group.attrs.push(attr);
      });

      const sectionsHtml = bySection.map((group) => {
        let bodyRows = "";
        group.attrs.forEach((attr) => {
          loaded.forEach((entry, i) => {
            const a = entry.ds.byId[C.attrId(attr.section, attr.key)];
            const labelCell = i === 0 ? "<td>" + escapeHtml(attr.label) + "</td>" : "<td></td>";
            const needsConversion = mixed && !conversionUnavailable && entry.ds.CURRENCY !== "USD";
            const displayCurrency = needsConversion ? "USD" : entry.ds.CURRENCY;
            const cells = union.PERIOD_KEYS.map((pk) => {
              if (!a) return "<td>" + fmtValue(null) + "</td>";
              const idx = entry.ds.PERIOD_KEYS.findIndex((k) => k[0] === pk[0] && k[1] === pk[1]);
              let value = idx === -1 ? null : a.values[idx];
              if (needsConversion) value = convertToUsd(value, attr.unit, state.fxRate.rate);
              return "<td>" + fmtValue(value, attr.unit, displayCurrency) + "</td>";
            }).join("");
            bodyRows += (
              '<tr class="' + (i === 0 ? "cmp-attr-label-row" : "") + '">' +
                labelCell +
                '<td class="cmp-company-cell">' + escapeHtml(entry.company.name) + "</td>" +
                cells +
              "</tr>"
            );
          });
        });
        const isOpen = !state.collapsedSections.has(group.section);
        const title = escapeHtml(C.SECTION_TITLES[group.section] || group.section);
        const metricWord = group.attrs.length === 1 ? "metric" : "metrics";
        return (
          '<details class="cmp-section" data-section="' + escapeHtml(group.section) + '"' + (isOpen ? " open" : "") + ">" +
            '<summary class="cmp-section-summary">' +
              '<span class="cmp-section-title">' + title + "</span>" +
              '<span class="cmp-section-meta" data-section-meta>' + group.attrs.length + " " + metricWord + " &middot; " +
                (isOpen ? "expanded" : "collapsed") + "</span>" +
            "</summary>" +
            '<div class="compare-table-wrap"><table class="cmp-detailed-table">' +
              "<thead><tr><th>Metric</th><th>Company</th>" + headerCells + "</tr></thead>" +
              "<tbody>" + bodyRows + "</tbody>" +
            "</table></div>" +
          "</details>"
        );
      }).join("");

      const warning = conversionUnavailable
        ? '<p class="cmp-detailed-footnote">These companies use different currencies, but a live USD/INR rate ' +
          "isn't available right now — monetary figures below are each shown in their own native currency.</p>"
        : "";
      const fxNote = mixed && !conversionUnavailable
        ? '<p class="cmp-detailed-footnote">Monetary figures converted to USD at 1 USD = &#8377;' +
          state.fxRate.rate.toFixed(2) + " (rate as of " + escapeHtml(state.fxRate.as_of) + "). Ratios and " +
          "percentages need no conversion and are shown as-is.</p>"
        : "";

      tableRoot.innerHTML =
        '<div class="cmp-sections-card">' + sectionsHtml + "</div>" +
        warning + fxNote +
        '<p class="cmp-detailed-footnote">Full recorded history per company, not a fixed window. ' +
        '"-" reflects a real gap in the source filings, never an estimate.</p>';

      // Native <details> already toggles its own open/closed visuals; this
      // only needs to (a) remember the choice past the next full
      // re-render (state.collapsedSections) and (b) flip the "expanded" /
      // "collapsed" word in that section's own summary line to match.
      tableRoot.querySelectorAll(".cmp-section").forEach((el) => {
        el.addEventListener("toggle", () => {
          const section = el.dataset.section;
          if (el.open) state.collapsedSections.delete(section);
          else state.collapsedSections.add(section);
          const meta = el.querySelector("[data-section-meta]");
          meta.textContent = meta.textContent.replace(/expanded|collapsed/, el.open ? "expanded" : "collapsed");
        });
      });
    }

    // window.CompareShared.subscribeQuickCompanies -- this tab has no
    // company selection of its own; it always mirrors Quick Comparison's
    // current slots (capped defensively to MAX_COMPANIES), loading/caching
    // each company's dataset and dropping any that fails to load.
    function syncFromQuick(quickList) {
      const capped = quickList.slice(0, MAX_COMPANIES);
      state.companies = capped.map((c) => ({ id: c.id, name: c.name }));
      renderPills();
      capped.forEach((c) => {
        loadCompany(c.id, state.periodType)
          .then((ds) => {
            pickDefaultAttrs(ds);
            renderAttrsPanel();
            renderTable();
          })
          .catch(() => {
            state.companies = state.companies.filter((x) => x.id !== c.id);
            renderPills();
            renderAttrsPanel();
            renderTable();
          });
      });
      renderAttrsPanel();
      renderTable();
    }

    // — Attributes dropdown open/close —
    attrsBtn.addEventListener("click", () => {
      state.attrsOpen = !state.attrsOpen;
      attrsPanel.hidden = !state.attrsOpen;
      attrsBtn.setAttribute("aria-expanded", String(state.attrsOpen));
    });
    document.addEventListener("click", (e) => {
      if (state.attrsOpen && !attrsBtn.contains(e.target) && !attrsPanel.contains(e.target)) {
        state.attrsOpen = false;
        attrsPanel.hidden = true;
        attrsBtn.setAttribute("aria-expanded", "false");
      }
    });

    // — Annual/Quarterly toggle —
    periodBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        const periodType = btn.dataset.cmpPeriod;
        if (periodType === state.periodType) return;
        state.periodType = periodType;
        periodBtns.forEach((b) => b.classList.toggle("active", b === btn));
        Promise.all(state.companies.map((c) => loadCompany(c.id, periodType))).then(() => {
          renderAttrsPanel();
          renderTable();
        });
      });
    });

    renderPills();
    renderAttrsPanel();
    renderTable();

    // Pick up whatever's already selected in Quick Comparison (e.g. the
    // user picked companies there first, then switched tabs) and keep
    // following it from here on.
    window.CompareShared.subscribeQuickCompanies(syncFromQuick);
    syncFromQuick(window.CompareShared.getQuickCompanies());
  }

  document.addEventListener("DOMContentLoaded", init);
})();
