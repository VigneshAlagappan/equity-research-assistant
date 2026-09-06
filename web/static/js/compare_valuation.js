/* Compare page, Valuation Model tab (web/templates/compare.html) -- a
   side-by-side Growth Projection comparison across every company selected
   in Quick Comparison, structured like the Detailed Comparison tab (one
   collapsible section per statement group, companies nested under each
   parameter) rather than the single-company assumptions dashboard this tab
   used to show (that experience still lives on each company's own
   Valuation Model tab; see web/static/js/valuation_dashboard_interactive.js).
   Reads the same feed that dashboard does (web/valuation_feed.py's
   build_valuation_feed, {"YEARS": [...], "METRICS": {section: [{key,
   label, unit, type, values}]}}) but for every selected company at once,
   unioned onto a shared FY axis and projected 3 years forward.

   Same 5 assumptions as the single-company tab (required return, projected/
   terminal growth, price growth, CAGR window) drive this table, but ONE
   shared set applied to every company -- a per-company assumptions panel
   doesn't fit a side-by-side table. "Current stock price" is the one
   exception: a single shared price is meaningless across companies trading
   at wildly different scales, so it's a per-company input instead (see the
   Intrinsic Value Walk card), same as every company's own Overview/
   Valuation Model tab already treats price as company-specific. */
(function () {
  "use strict";

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  const FORECAST_YEARS_AHEAD = 3;
  // Trailing window of actual years shown alongside the forecast -- this
  // tab is about "where these companies are headed from here", not a full
  // history (Detailed Comparison already owns that job); matches the
  // reference design's own 3 actual + 3 forecast column layout.
  const ACTUAL_YEARS_SHOWN = 3;
  // Same fixed 10-year horizon web/static/js/valuation_dashboard_
  // interactive.js's computeKpis() uses for its Book Value -> Intrinsic
  // Value walk, independent of how many forecast columns the table itself
  // shows -- the walk has always been a longer-horizon DCF-ish estimate,
  // not tied to the table's own display window.
  const WALK_YEARS = 10;

  const SECTION_ORDER = ["balanceSheet", "incomeStatement", "perShare", "profitability", "bankRatios", "valuation"];

  const DEFAULT_ATTR_IDS = [
    "balanceSheet:networth", "balanceSheet:she", "balanceSheet:deposits",
    "incomeStatement:netProfit", "profitability:roe",
  ];

  function attrId(section, key) { return section + ":" + key; }

  // Every other section only surfaces a row with at least one real actual
  // value (same reasoning as charts_overlay.js's flattenAttributes). The
  // "valuation" section is the one exception: web/valuation_feed.py's
  // Price/P/E/P-BV/Div-Yield rows are ALWAYS empty (no market-data source),
  // which is exactly why the single-company tab special-cases Price/P/E
  // with an assumptions-driven forecast instead of the generic "compound
  // the last actual" rule -- with a per-company price input now available
  // here too, Price/P/E become worth surfacing even with zero recorded
  // history, so this section is exempted from the filter.
  function flattenAttributes(METRICS) {
    const out = [];
    SECTION_ORDER.forEach((section) => {
      const rows = METRICS[section];
      if (!rows) return;
      rows.forEach((m) => {
        const hasData = m.values.some((v) => v !== null && Number.isFinite(v));
        if (hasData || section === "valuation") {
          out.push({ section: section, key: m.key, label: m.label, unit: m.unit, type: m.type || "fact", values: m.values });
        }
      });
    });
    return out;
  }

  function unionAttributes(datasets) {
    const seen = {};
    const bySection = {};
    SECTION_ORDER.forEach((s) => { bySection[s] = []; });
    datasets.forEach((ds) => {
      ds.attributes.forEach((a) => {
        const id = attrId(a.section, a.key);
        if (seen[id]) return;
        seen[id] = true;
        bySection[a.section].push(a);
      });
    });
    const out = [];
    SECTION_ORDER.forEach((s) => { out.push.apply(out, bySection[s]); });
    return out;
  }

  function unionYears(datasets) {
    const years = new Set();
    datasets.forEach((ds) => ds.YEARS.forEach((y) => years.add(y)));
    return Array.from(years).sort((a, b) => a - b);
  }

  function lastNonNull(values) {
    for (let i = values.length - 1; i >= 0; i--) {
      if (values[i] !== null && Number.isFinite(values[i])) return values[i];
    }
    return null;
  }

  function cagr(startVal, endVal, years) {
    if (startVal === null || endVal === null || !Number.isFinite(startVal) || !Number.isFinite(endVal)) return null;
    if (startVal <= 0 || endVal <= 0 || years <= 0) return null;
    return Math.pow(endVal / startVal, 1 / years) - 1;
  }

  function init() {
    const panel = document.getElementById("compare-panel-valuation");
    const tableRoot = document.getElementById("cmp-valuation-root");
    const assumptionsRoot = document.getElementById("cmp-val-assumptions");
    const walkRoot = document.getElementById("cmp-val-walk");
    if (!panel || !tableRoot || !assumptionsRoot || !walkRoot) return;
    const urlTemplate = panel.dataset.valuationUrlTemplate;

    const pillsRoot = document.getElementById("cmp-val-pills");
    const attrsBtn = document.getElementById("cmp-val-attrs-btn");
    const attrsPanel = document.getElementById("cmp-val-attrs-panel");
    const attrsCount = document.getElementById("cmp-val-attrs-count");

    const state = {
      companies: [], // [{id, name}], mirrors Quick Comparison, same as compare_detailed.js
      cache: {}, // companyId -> {YEARS, CURRENCY, attributes, byId}
      selectedAttrs: new Set(),
      assumptions: { requiredRoR: 0.22, projGrowth: 0.15, terminalGrowth: 0.01, priceGrowth: 0.12, evalStartYear: null, evalEndYear: null },
      companyPrices: {}, // companyId -> number, per-company (see module comment)
      attrsOpen: false,
      collapsedSections: new Set(),
      defaultsApplied: false,
    };

    function loadCompany(companyId) {
      if (state.cache[companyId]) return Promise.resolve(state.cache[companyId]);
      const url = urlTemplate.replace("__ID__", encodeURIComponent(companyId));
      return fetch(url)
        .then((r) => {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then((data) => {
          const attributes = flattenAttributes(data.METRICS || {});
          const byId = {};
          attributes.forEach((a) => { byId[attrId(a.section, a.key)] = a; });
          const ds = { YEARS: data.YEARS || [], CURRENCY: data.CURRENCY || "INR", attributes: attributes, byId: byId };
          state.cache[companyId] = ds;
          return ds;
        });
    }

    function loadedDatasets() {
      return state.companies
        .map((c) => ({ company: c, ds: state.cache[c.id] }))
        .filter((x) => x.ds);
    }

    function renderPills() {
      pillsRoot.innerHTML = state.companies.length
        ? state.companies.map((c) => '<span class="cmp-pill">' + escapeHtml(c.name) + "</span>").join("")
        : '<span class="muted">Select companies in Quick Comparison to see them here.</span>';
    }

    function renderAttrsPanel() {
      const loaded = loadedDatasets();
      const unionAttrs = unionAttributes(loaded.map((x) => x.ds));
      const validIds = new Set(unionAttrs.map((a) => attrId(a.section, a.key)));
      state.selectedAttrs.forEach((id) => { if (!validIds.has(id)) state.selectedAttrs.delete(id); });

      attrsCount.textContent = String(state.selectedAttrs.size);

      if (unionAttrs.length === 0) {
        attrsPanel.innerHTML = '<p class="muted" style="padding:8px;margin:0;">Add a company to see its parameters.</p>';
        return;
      }
      let html = "", lastSection = null;
      unionAttrs.forEach((a) => {
        if (a.section !== lastSection) {
          html += '<div class="cmp-attrs-section-title">' + escapeHtml((window.SignalsCharts && window.SignalsCharts.SECTION_TITLES[a.section]) || a.section) + "</div>";
          lastSection = a.section;
        }
        const id = attrId(a.section, a.key);
        const checked = state.selectedAttrs.has(id) ? " checked" : "";
        html += (
          '<label class="cmp-attr-item">' +
            '<input type="checkbox" data-val-attr-id="' + escapeHtml(id) + '"' + checked + ">" +
            escapeHtml(a.label) +
          "</label>"
        );
      });
      attrsPanel.innerHTML = html;
      attrsPanel.querySelectorAll("[data-val-attr-id]").forEach((cb) => {
        cb.addEventListener("change", () => {
          if (cb.checked) state.selectedAttrs.add(cb.dataset.valAttrId);
          else state.selectedAttrs.delete(cb.dataset.valAttrId);
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

    // — Assumptions card: rendered ONCE (its number inputs must never be
    // rebuilt while the user might be mid-keystroke in one of them — same
    // "assumptions live outside the re-rendered area" split valuation_
    // dashboard_interactive.js's own init() already relies on). Only the
    // CAGR-window <select> options get refreshed in place when the set of
    // loaded companies changes, since that's the one part of this card
    // driven by fetched data rather than a fixed default. —
    function renderAssumptionsShell() {
      const a = state.assumptions;
      assumptionsRoot.innerHTML =
        '<div class="card-kicker">Assumptions</div>' +
        '<div class="vm-assumptions-grid">' +
          '<div class="field"><label for="cmp-val-ror">Required rate of return (%)</label>' +
            '<input class="input" id="cmp-val-ror" type="number" step="1" min="5" max="40" value="' + (a.requiredRoR * 100) + '"></div>' +
          '<div class="field"><label for="cmp-val-proj-growth">Projected growth rate (%)</label>' +
            '<input class="input" id="cmp-val-proj-growth" type="number" step="1" min="0" max="50" value="' + (a.projGrowth * 100) + '"></div>' +
          '<div class="field"><label for="cmp-val-terminal-growth">Terminal growth rate (%)</label>' +
            '<input class="input" id="cmp-val-terminal-growth" type="number" step="0.5" min="0" max="10" value="' + (a.terminalGrowth * 100) + '"></div>' +
          '<div class="field"><label for="cmp-val-price-growth">Projected annualized price growth (%)</label>' +
            '<input class="input" id="cmp-val-price-growth" type="number" step="1" min="-20" max="50" value="' + (a.priceGrowth * 100) + '"></div>' +
          '<div class="field"><label for="cmp-val-start-year">CAGR window</label>' +
            '<div class="vm-year-range">' +
              '<select class="input" id="cmp-val-start-year" aria-label="Eval start year"></select>' +
              '<span class="vm-year-sep">&ndash;</span>' +
              '<select class="input" id="cmp-val-end-year" aria-label="Eval end year"></select>' +
            '</div></div>' +
        "</div>";

      function bindNumber(id, field, scale) {
        const el = assumptionsRoot.querySelector("#" + id);
        el.addEventListener("input", () => {
          const v = parseFloat(el.value);
          state.assumptions[field] = Number.isFinite(v) ? v / (scale || 100) : 0;
          renderTable();
          if (field === "requiredRoR" || field === "projGrowth") updateWalkOutputs();
        });
      }
      bindNumber("cmp-val-ror", "requiredRoR");
      bindNumber("cmp-val-proj-growth", "projGrowth");
      bindNumber("cmp-val-terminal-growth", "terminalGrowth");
      bindNumber("cmp-val-price-growth", "priceGrowth");

      const startSelect = assumptionsRoot.querySelector("#cmp-val-start-year");
      const endSelect = assumptionsRoot.querySelector("#cmp-val-end-year");
      startSelect.addEventListener("change", () => { state.assumptions.evalStartYear = parseInt(startSelect.value, 10); renderTable(); });
      endSelect.addEventListener("change", () => { state.assumptions.evalEndYear = parseInt(endSelect.value, 10); renderTable(); });
    }

    // Only touches the two <select>s -- never re-renders the number inputs
    // above them, so a keystroke in progress there is never disturbed by a
    // company load completing in the background.
    function populateYearSelects() {
      const loaded = loadedDatasets();
      const years = unionYears(loaded.map((x) => x.ds));
      const startSelect = assumptionsRoot.querySelector("#cmp-val-start-year");
      const endSelect = assumptionsRoot.querySelector("#cmp-val-end-year");
      if (!startSelect || !endSelect) return;
      if (years.length === 0) {
        startSelect.innerHTML = "";
        endSelect.innerHTML = "";
        return;
      }
      const optionsHtml = years.map((y) => '<option value="' + y + '">' + y + "</option>").join("");
      startSelect.innerHTML = optionsHtml;
      endSelect.innerHTML = optionsHtml;
      const validStart = state.assumptions.evalStartYear !== null && years.indexOf(state.assumptions.evalStartYear) !== -1;
      const validEnd = state.assumptions.evalEndYear !== null && years.indexOf(state.assumptions.evalEndYear) !== -1;
      state.assumptions.evalStartYear = validStart ? state.assumptions.evalStartYear : years[0];
      state.assumptions.evalEndYear = validEnd ? state.assumptions.evalEndYear : years[years.length - 1];
      startSelect.value = state.assumptions.evalStartYear;
      endSelect.value = state.assumptions.evalEndYear;
    }

    // — Intrinsic Value Walk: Book Value/share -> (1+projected growth)^10
    // -> discounted at (1+required return)^10 -> Intrinsic Value/share,
    // same formula as valuation_dashboard_interactive.js's computeKpis(),
    // just once per company here. Rendered once per company-list change
    // (renderWalkTable) so the per-company price <input> nodes are never
    // torn down mid-typing; every recompute after that (an assumption
    // change or a price edit) only touches the output cells in place
    // (updateWalkOutputs), same "static inputs, live outputs" split as the
    // assumptions card above. —
    function renderWalkTable() {
      const loaded = loadedDatasets();
      if (loaded.length === 0) {
        walkRoot.innerHTML = "";
        return;
      }
      const rows = loaded.map((entry) => {
        const price = state.companyPrices[entry.company.id] || 0;
        return (
          "<tr>" +
            "<td>" + escapeHtml(entry.company.name) + "</td>" +
            '<td><input class="input cmp-val-price-input" type="number" step="1" min="0" ' +
              'data-price-company="' + escapeHtml(entry.company.id) + '" value="' + price + '"></td>' +
            '<td class="vm-num" data-walk-cell="bv" data-walk-company="' + escapeHtml(entry.company.id) + '">—</td>' +
            '<td class="vm-num" data-walk-cell="iv" data-walk-company="' + escapeHtml(entry.company.id) + '">—</td>' +
            '<td class="vm-num" data-walk-cell="mos" data-walk-company="' + escapeHtml(entry.company.id) + '">—</td>' +
          "</tr>"
        );
      }).join("");
      walkRoot.innerHTML =
        '<div class="card">' +
          '<div class="card-kicker">Intrinsic Value Walk, per company</div>' +
          '<p class="muted vm-section-desc">Book value/share &times; (1 + projected growth)<sup>10</sup> &divide; (1 + required rate of return)<sup>10</sup>, against each company’s own current price.</p>' +
          '<div class="vm-table-scroll"><table class="table">' +
            "<thead><tr><th>Company</th><th>Current price</th><th>Book value/share</th><th>Intrinsic value/share</th><th>Margin of safety</th></tr></thead>" +
            "<tbody>" + rows + "</tbody>" +
          "</table></div>" +
        "</div>";
      walkRoot.querySelectorAll("[data-price-company]").forEach((inp) => {
        inp.addEventListener("input", () => {
          const v = parseFloat(inp.value);
          state.companyPrices[inp.dataset.priceCompany] = Number.isFinite(v) ? v : 0;
          updateWalkOutputs();
          renderTable(); // safe: rebuilds #cmp-valuation-root, a different subtree from this input's own
        });
      });
      updateWalkOutputs();
    }

    function updateWalkOutputs() {
      loadedDatasets().forEach((entry) => {
        const bvRow = entry.ds.byId[attrId("perShare", "bookValue")];
        const lastBv = bvRow ? lastNonNull(bvRow.values) : null;
        const futureBv = lastBv !== null ? lastBv * Math.pow(1 + state.assumptions.projGrowth, WALK_YEARS) : null;
        const iv = futureBv !== null ? futureBv / Math.pow(1 + state.assumptions.requiredRoR, WALK_YEARS) : null;
        const price = state.companyPrices[entry.company.id] || 0;
        const mos = iv !== null && iv > 0 ? (iv - price) / iv : null;
        const mosLabel = mos === null ? "—" : (mos * 100).toFixed(1) + "%" + (mos > 0.15 ? " · undervalued" : mos < -0.15 ? " · overvalued" : " · near fair value");
        const mosColor = mos === null ? "" : mos > 0.15 ? "var(--color-accent-700)" : mos < -0.15 ? "#8a3b2b" : "";

        const bvCell = walkRoot.querySelector('[data-walk-cell="bv"][data-walk-company="' + CSS.escape(entry.company.id) + '"]');
        const ivCell = walkRoot.querySelector('[data-walk-cell="iv"][data-walk-company="' + CSS.escape(entry.company.id) + '"]');
        const mosCell = walkRoot.querySelector('[data-walk-cell="mos"][data-walk-company="' + CSS.escape(entry.company.id) + '"]');
        if (bvCell) bvCell.innerHTML = fmtValue(lastBv, "perShare", entry.ds.CURRENCY);
        if (ivCell) ivCell.innerHTML = fmtValue(iv, "perShare", entry.ds.CURRENCY);
        if (mosCell) {
          mosCell.textContent = mosLabel;
          mosCell.style.color = mosColor;
          mosCell.style.fontWeight = mos === null ? "" : "600";
        }
      });
    }

    function renderTable() {
      const loaded = loadedDatasets();
      if (loaded.length === 0) {
        tableRoot.innerHTML = '<p class="muted">Add at least one company in Quick Comparison to see its Growth Projection.</p>';
        return;
      }
      const allActualYears = unionYears(loaded.map((x) => x.ds));
      const actualYears = allActualYears.slice(-ACTUAL_YEARS_SHOWN);
      const unionAttrs = unionAttributes(loaded.map((x) => x.ds));
      const selected = unionAttrs.filter((a) => state.selectedAttrs.has(attrId(a.section, a.key)));
      if (selected.length === 0) {
        tableRoot.innerHTML = '<p class="muted">Pick at least one parameter above.</p>';
        return;
      }

      const lastActualYear = actualYears.length ? actualYears[actualYears.length - 1] : null;
      const forecastYears = [];
      for (let n = 1; n <= FORECAST_YEARS_AHEAD && lastActualYear !== null; n++) forecastYears.push(lastActualYear + n);

      const a = state.assumptions;
      const hasCagrWindow = a.evalStartYear !== null && a.evalEndYear !== null && a.evalEndYear > a.evalStartYear;

      const actualHeaderCells = actualYears.map((y) => "<th>FY" + y + "</th>").join("");
      const cagrHeaderCell = hasCagrWindow ? '<th class="vm-cagr-col">CAGR</th>' : "";
      const forecastHeaderCells = forecastYears.map((y) => '<th class="vm-forecast-col vm-group-header">FY' + y + "E</th>").join("");

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
            const rowAttr = entry.ds.byId[attrId(attr.section, attr.key)];
            const labelCell = i === 0
              ? "<td>" + escapeHtml(attr.label) + "</td>" +
                '<td><span class="tag ' + (attr.type === "calc" ? "tag-calculation" : "tag-fact") + '">' +
                (attr.type === "calc" ? "CALC" : "FACT") + "</span></td>"
              : "<td></td><td></td>";

            const actualCells = actualYears.map((y) => {
              if (!rowAttr) return "<td>" + fmtValue(null) + "</td>";
              const idx = entry.ds.YEARS.indexOf(y);
              const value = idx === -1 ? null : rowAttr.values[idx];
              return "<td>" + fmtValue(value, attr.unit, entry.ds.CURRENCY) + "</td>";
            }).join("");

            let cagrCell = "";
            if (hasCagrWindow) {
              let cagrVal = null;
              if (rowAttr) {
                const idxStart = entry.ds.YEARS.indexOf(a.evalStartYear);
                const idxEnd = entry.ds.YEARS.indexOf(a.evalEndYear);
                const startVal = idxStart !== -1 ? rowAttr.values[idxStart] : null;
                const endVal = idxEnd !== -1 ? rowAttr.values[idxEnd] : null;
                cagrVal = cagr(startVal, endVal, a.evalEndYear - a.evalStartYear);
              }
              cagrCell = '<td class="vm-cagr-cell">' + (cagrVal === null ? "—" : (cagrVal * 100).toFixed(1) + "%") + "</td>";
            }

            // Price/P/E get the same assumptions-driven special case
            // web/static/js/valuation_dashboard_interactive.js's
            // renderGrowth() applies -- everything else compounds its own
            // last actual value at the shared projected growth rate.
            let forecastCells;
            if (attr.section === "valuation" && attr.key === "price") {
              const price = state.companyPrices[entry.company.id] || 0;
              forecastCells = forecastYears.map((y, idx) => {
                const value = price > 0 ? price * Math.pow(1 + a.priceGrowth, idx + 1) : null;
                return '<td class="vm-forecast-cell">' + fmtValue(value, "perShare", entry.ds.CURRENCY) + "</td>";
              }).join("");
            } else if (attr.section === "valuation" && attr.key === "pe") {
              const price = state.companyPrices[entry.company.id] || 0;
              const epsRow = entry.ds.byId[attrId("perShare", "eps")];
              const lastEps = epsRow ? lastNonNull(epsRow.values) : null;
              forecastCells = forecastYears.map((y, idx) => {
                const projPrice = price > 0 ? price * Math.pow(1 + a.priceGrowth, idx + 1) : null;
                const projEps = lastEps !== null ? lastEps * Math.pow(1 + a.projGrowth, idx + 1) : null;
                const value = projPrice !== null && projEps ? projPrice / projEps : null;
                return '<td class="vm-forecast-cell">' + fmtValue(value, "x", entry.ds.CURRENCY) + "</td>";
              }).join("");
            } else {
              const last = rowAttr ? lastNonNull(rowAttr.values) : null;
              forecastCells = forecastYears.map((y, idx) => {
                const value = last !== null ? last * Math.pow(1 + a.projGrowth, idx + 1) : null;
                return '<td class="vm-forecast-cell">' + fmtValue(value, rowAttr ? rowAttr.unit : null, entry.ds.CURRENCY) + "</td>";
              }).join("");
            }

            bodyRows += (
              '<tr class="' + (i === 0 ? "cmp-attr-label-row" : "") + '">' +
                labelCell +
                '<td class="cmp-company-cell">' + escapeHtml(entry.company.name) + "</td>" +
                actualCells + cagrCell + forecastCells +
              "</tr>"
            );
          });
        });
        const isOpen = !state.collapsedSections.has(group.section);
        const title = escapeHtml((window.SignalsCharts && window.SignalsCharts.SECTION_TITLES[group.section]) || group.section);
        const metricWord = group.attrs.length === 1 ? "parameter" : "parameters";
        return (
          '<details class="cmp-section" data-section="' + escapeHtml(group.section) + '"' + (isOpen ? " open" : "") + ">" +
            '<summary class="cmp-section-summary">' +
              '<span class="cmp-section-title">' + title + "</span>" +
              '<span class="cmp-section-meta" data-section-meta>' + group.attrs.length + " " + metricWord + " &middot; " +
                (isOpen ? "expanded" : "collapsed") + "</span>" +
            "</summary>" +
            '<div class="compare-table-wrap"><table class="cmp-detailed-table">' +
              "<thead>" +
                '<tr><th></th><th></th><th></th><th colspan="' + actualYears.length + '" class="vm-group-header">Actual</th>' +
                  (hasCagrWindow ? "<th></th>" : "") +
                  '<th colspan="' + forecastYears.length + '" class="vm-forecast-col vm-group-header">Forecast</th></tr>' +
                "<tr><th>Parameter</th><th>Type</th><th>Company</th>" + actualHeaderCells + cagrHeaderCell + forecastHeaderCells + "</tr>" +
              "</thead>" +
              "<tbody>" + bodyRows + "</tbody>" +
            "</table></div>" +
          "</details>"
        );
      }).join("");

      const terminalYear = lastActualYear !== null ? lastActualYear + FORECAST_YEARS_AHEAD + 1 : null;
      const terminalNote = terminalYear !== null
        ? " Beyond FY" + terminalYear + ", growth is assumed to settle at the terminal rate, " + (a.terminalGrowth * 100).toFixed(1) + "%/yr, in perpetuity."
        : "";

      tableRoot.innerHTML =
        '<div class="cmp-sections-card">' + sectionsHtml + "</div>" +
        '<p class="cmp-detailed-footnote">Last ' + ACTUAL_YEARS_SHOWN + ' actual years' +
        (hasCagrWindow ? ", CAGR over FY" + a.evalStartYear + "–FY" + a.evalEndYear : "") +
        ", then " + FORECAST_YEARS_AHEAD + " years projected forward (shaded) at the projected growth rate above." + terminalNote + " " +
        '"-" reflects a real gap in the source filings, never an estimate.</p>';

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

    function applyDefaultsIfNeeded() {
      if (state.defaultsApplied || state.selectedAttrs.size > 0) return;
      const loaded = loadedDatasets();
      if (loaded.length === 0) return;
      const availableIds = new Set(unionAttributes(loaded.map((x) => x.ds)).map((a) => attrId(a.section, a.key)));
      const anyDefaultAvailable = DEFAULT_ATTR_IDS.some((id) => availableIds.has(id));
      if (!anyDefaultAvailable) return;
      DEFAULT_ATTR_IDS.forEach((id) => { if (availableIds.has(id)) state.selectedAttrs.add(id); });
      state.defaultsApplied = true;
    }

    function syncFromQuick(quickList) {
      state.companies = quickList.slice();
      renderPills();
      quickList.forEach((c) => {
        loadCompany(c.id)
          .then(() => {
            applyDefaultsIfNeeded();
            renderAttrsPanel();
            populateYearSelects();
            renderWalkTable();
            renderTable();
          })
          .catch(() => {
            state.companies = state.companies.filter((x) => x.id !== c.id);
            renderPills();
            renderAttrsPanel();
            populateYearSelects();
            renderWalkTable();
            renderTable();
          });
      });
      renderAttrsPanel();
      populateYearSelects();
      renderWalkTable();
      renderTable();
    }

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

    renderAssumptionsShell();
    renderPills();
    renderAttrsPanel();
    renderTable();

    window.CompareShared.subscribeQuickCompanies(syncFromQuick);
    syncFromQuick(window.CompareShared.getQuickCompanies());
  }

  document.addEventListener("DOMContentLoaded", init);
})();
