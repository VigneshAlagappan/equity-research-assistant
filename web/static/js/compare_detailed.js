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
    const searchUrl = panel.dataset.searchUrl;
    const chartsUrlTemplate = panel.dataset.chartsUrlTemplate;
    const MAX_COMPANIES = C.MAX_COMPARISONS || 4;

    const pillsRoot = document.getElementById("cmp-pills");
    const addInput = document.getElementById("cmp-add-input");
    const addResults = document.getElementById("cmp-add-results");
    const attrsBtn = document.getElementById("cmp-attrs-btn");
    const attrsPanel = document.getElementById("cmp-attrs-panel");
    const attrsCount = document.getElementById("cmp-attrs-count");
    const periodBtns = Array.prototype.slice.call(document.querySelectorAll("[data-cmp-period]"));

    const state = {
      companies: [], // [{id, name}], insertion order
      periodType: "annual",
      selectedAttrs: new Set(), // attrId strings, e.g. "incomeStatement:netProfit"
      cache: {}, // cacheKey(companyId, periodType) -> ds {PERIODS, PERIOD_KEYS, CURRENCY, attributes, byId}
      attrsOpen: false,
      search: { items: [], activeIndex: -1, requestId: 0 },
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

    function renderPills() {
      pillsRoot.innerHTML = state.companies.map((c) => (
        '<span class="cmp-pill">' + escapeHtml(c.name) +
          ' <button type="button" data-remove-company="' + escapeHtml(c.id) + '" title="Remove ' + escapeHtml(c.name) + '">&times;</button>' +
        "</span>"
      )).join("");
      pillsRoot.querySelectorAll("[data-remove-company]").forEach((btn) => {
        btn.addEventListener("click", () => {
          state.companies = state.companies.filter((c) => c.id !== btn.dataset.removeCompany);
          renderPills();
          renderAttrsPanel();
          renderTable();
        });
      });
      addInput.placeholder = state.companies.length >= MAX_COMPANIES
        ? "Maximum " + MAX_COMPANIES + " companies"
        : "+ Add company from NSE 500…";
      addInput.disabled = state.companies.length >= MAX_COMPANIES;
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
        return '<span class="cmp-not-reported">Not reported</span>';
      }
      const V = window.SignalsValuation;
      return V ? escapeHtml(V.fmt(value, unit, currency)) : escapeHtml(String(value));
    }

    function renderTable() {
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

      const headerCells = union.PERIODS.map((p) => "<th>" + escapeHtml(p) + "</th>").join("");
      let bodyRows = "", lastSection = null;
      selected.forEach((attr) => {
        if (attr.section !== lastSection) {
          bodyRows += '<tr class="cmp-section-row"><th colspan="' + (union.PERIODS.length + 2) + '">' +
            escapeHtml(C.SECTION_TITLES[attr.section] || attr.section) + "</th></tr>";
          lastSection = attr.section;
        }
        loaded.forEach((entry, i) => {
          const a = entry.ds.byId[C.attrId(attr.section, attr.key)];
          const labelCell = i === 0 ? "<td>" + escapeHtml(attr.label) + "</td>" : "<td></td>";
          const cells = union.PERIOD_KEYS.map((pk) => {
            if (!a) return "<td>" + fmtValue(null) + "</td>";
            const idx = entry.ds.PERIOD_KEYS.findIndex((k) => k[0] === pk[0] && k[1] === pk[1]);
            const value = idx === -1 ? null : a.values[idx];
            return "<td>" + fmtValue(value, attr.unit, entry.ds.CURRENCY) + "</td>";
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

      tableRoot.innerHTML =
        '<div class="compare-table-wrap"><table class="cmp-detailed-table">' +
          "<thead><tr><th>Attribute</th><th>Company</th>" + headerCells + "</tr></thead>" +
          "<tbody>" + bodyRows + "</tbody>" +
        "</table></div>" +
        '<p class="cmp-detailed-footnote">Full recorded history per company, not a fixed window. ' +
        '"Not reported" cells reflect a real gap in the source filings, never an estimate.</p>';
    }

    function addCompany(companyId, displayName) {
      if (state.companies.some((c) => c.id === companyId) || state.companies.length >= MAX_COMPANIES) return;
      state.companies.push({ id: companyId, name: displayName });
      renderPills();
      loadCompany(companyId, state.periodType)
        .then((ds) => {
          pickDefaultAttrs(ds);
          renderAttrsPanel();
          renderTable();
        })
        .catch(() => {
          state.companies = state.companies.filter((c) => c.id !== companyId);
          renderPills();
        });
    }

    // — Search (same debounced-typeahead shape as header_search.js /
    // web/static/js/compare.js's slot search, single instance here since
    // there's one shared add-box rather than N independent slots). —
    let searchDebounce = null;
    addInput.addEventListener("input", () => {
      const q = addInput.value.trim();
      clearTimeout(searchDebounce);
      if (!q) { addResults.hidden = true; addResults.innerHTML = ""; return; }
      searchDebounce = setTimeout(() => runSearch(q), 150);
    });
    function runSearch(query) {
      const thisRequest = ++state.search.requestId;
      fetch(searchUrl + "?q=" + encodeURIComponent(query))
        .then((r) => r.json())
        .then((data) => {
          if (thisRequest !== state.search.requestId) return;
          const usedIds = state.companies.map((c) => c.id);
          state.search.items = (data.results || []).filter((c) => usedIds.indexOf(c.company_id) === -1);
          renderSearchResults();
        })
        .catch(() => {});
    }
    function renderSearchResults() {
      if (state.search.items.length === 0) {
        addResults.innerHTML = '<div class="site-search-empty">No matching companies.</div>';
      } else {
        addResults.innerHTML = state.search.items.map((c) => (
          '<button type="button" class="site-search-result" data-select-company="' + escapeHtml(c.company_id) + '" data-select-name="' + escapeHtml(c.display_name) + '">' +
            '<div class="site-search-result-name">' + escapeHtml(c.display_name) + "</div>" +
            '<div class="site-search-result-meta">' + escapeHtml(c.company_id) + (c.sector ? " &middot; " + escapeHtml(c.sector) : "") + "</div>" +
          "</button>"
        )).join("");
      }
      addResults.hidden = false;
      addResults.querySelectorAll("[data-select-company]").forEach((btn) => {
        btn.addEventListener("click", () => {
          addCompany(btn.dataset.selectCompany, btn.dataset.selectName);
          addInput.value = "";
          addResults.hidden = true;
          addResults.innerHTML = "";
        });
      });
    }
    document.addEventListener("click", (e) => {
      if (!addInput.contains(e.target) && !addResults.contains(e.target)) {
        addResults.hidden = true;
      }
    });

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
  }

  document.addEventListener("DOMContentLoaded", init);
})();
