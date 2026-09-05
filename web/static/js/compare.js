/* Compare page (web/templates/compare.html) — car/product-comparison-style
   spec sheet: up to 4 fixed slots, each independently searchable/swappable
   (not a growing "Compare With" list like the Charts tab's own picker —
   here the grid itself is the cap, so "swap slot 2" never disturbs 1/3/4).

   Reuses window.SignalsValuation (valuation_dashboard.js's exported
   fmt/buildRatioContext/RATIO_CATALOG) for the actual metric computation —
   one ratio catalog, one place its math lives, not a second copy here that
   could drift out of sync with the Overview tab's own numbers.

   Search-per-slot follows header_search.js's exact pattern (debounced
   fetch, arrow-key nav, click-outside-to-close) generalized to N
   independent instances via event delegation on the slots container —
   critical detail: only the *results* dropdown re-renders on each
   keystroke, never the <input> itself, since replacing an <input> node
   mid-typing (a naive innerHTML rebuild of the whole slot) would drop
   focus and the cursor position after every character. */
(function () {
  "use strict";

  const MAX_SLOTS = 4;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // catalog key -> {unit, ctxField} for the 9 monetary rows (everything
  // else in OVERVIEW_RATIO_CATALOG is a ratio/percentage/count, already
  // currency-invariant, and is left completely alone). "big" is this app's
  // aggregate-figure convention -- Crore for an INR company, million for a
  // USD one (sources/yfinance_financials.py's own scale); "perShare" is a
  // raw per-share currency amount. See toUsdContext() below for the two
  // conversion formulas these drive.
  const MONETARY_KEYS = {
    marketCap: { unit: "big", ctxField: "marketCap" },
    price: { unit: "perShare", ctxField: "price" },
    bookValue: { unit: "perShare", ctxField: "lastBv" },
    eps: { unit: "perShare", ctxField: "lastEps" },
    netProfit: { unit: "big", ctxField: "lastNetProfit" },
    revenue: { unit: "big", ctxField: "lastRevenue" },
    networth: { unit: "big", ctxField: "lastNetworth" },
    totalAssets: { unit: "big", ctxField: "lastTotalAssets" },
    salesPerShare: { unit: "perShare", ctxField: "lastSalesPerShare" },
  };

  // Every other catalog row (stockPE, priceToBook, dividendYield, ROE,
  // margins, Debt/Equity, CAGRs, ...) is a plain ratio of two same-company
  // figures -- converting both sides of a ratio by the same factor leaves
  // it unchanged, so those need no conversion at all, only the 9 leaf
  // figures above do.
  function toUsdContext(ctx, fxRate) {
    const out = Object.assign({}, ctx, { currency: "USD" });
    Object.keys(MONETARY_KEYS).forEach((key) => {
      const { unit, ctxField } = MONETARY_KEYS[key];
      const val = ctx[ctxField];
      if (val === null || val === undefined) return;
      // "big": value is in INR Crore (1 Cr = 1e7 INR) -> USD million:
      // (val * 1e7) / fxRate / 1e6, simplifies to val * 10 / fxRate.
      // "perShare": plain raw-rupee division.
      out[ctxField] = unit === "big" ? (val * 10) / fxRate : val / fxRate;
    });
    // "shares" (No. Equity Shares) isn't money -- a share count needs no FX
    // conversion at all -- but fmt()'s "sharesCount" case reads `currency`
    // to pick "Cr" vs "M" purely for the *label*, and out.currency just
    // flipped to "USD" above. Left alone, the same raw Crore number would
    // get relabeled "M" without actually being rescaled -- 1 Crore = 10
    // million, an order-of-magnitude display error, not a rounding
    // quibble. Rescale by that fixed 10x (a unit conversion, not FX --
    // fxRate never enters into it) so the label change stays truthful.
    if (out.shares !== null && out.shares !== undefined) out.shares = out.shares * 10;
    return out;
  }

  function init() {
    const slotsRoot = document.getElementById("compare-slots");
    const tableRoot = document.getElementById("compare-table");
    if (!slotsRoot || !tableRoot) return;
    const V = window.SignalsValuation;
    if (!V) {
      tableRoot.innerHTML = '<div class="empty-state">Could not load the ratio catalog (valuation_dashboard.js).</div>';
      return;
    }
    const searchUrl = slotsRoot.dataset.searchUrl;
    const ratioCatalog = window.COMPARE_RATIO_CATALOG || [];

    // slots[i]: null (empty) or { meta: {...compare-meta.json...}, ds: {...charts-feed json, or null on fetch failure...} }
    const slots = new Array(MAX_SLOTS).fill(null);
    // search[i]: transient typeahead state for an empty slot -- discarded once that slot fills.
    const search = new Array(MAX_SLOTS).fill(null).map(() => ({ items: [], activeIndex: -1, requestId: 0 }));
    let fxRate = null; // {rate, as_of} once fetched, or {rate: null} if the fetch failed -- fetched lazily, only once currencies actually mix

    function slotEl(i) {
      return slotsRoot.querySelector('[data-slot="' + i + '"]');
    }
    function resultsEl(i) {
      return slotsRoot.querySelector('[data-slot-results="' + i + '"]');
    }

    function renderSlotContainer(i) {
      const slot = slots[i];
      const el = slotEl(i);
      if (!el) return;
      if (slot) {
        el.className = "compare-slot compare-slot-filled";
        el.innerHTML =
          '<div class="compare-slot-name">' + escapeHtml(slot.meta.display_name) + "</div>" +
          '<div class="compare-slot-meta">' + escapeHtml(slot.meta.company_id) +
            (slot.meta.currency ? " &middot; " + escapeHtml(slot.meta.currency) : "") +
            (slot.ds === null ? ' &middot; <span class="muted">data unavailable</span>' : "") + "</div>" +
          '<div class="compare-slot-actions">' +
            '<button type="button" class="btn btn-ghost" data-change="' + i + '">Change</button>' +
            '<button type="button" class="btn btn-ghost" data-remove="' + i + '">Remove</button>' +
          "</div>";
      } else {
        el.className = "compare-slot compare-slot-empty";
        el.innerHTML =
          '<input type="text" class="site-search-input" data-slot-input="' + i + '" autocomplete="off" placeholder="+ Add company&hellip;">' +
          '<div class="site-search-results" data-slot-results="' + i + '" hidden></div>';
      }
    }

    function renderResults(i) {
      const el = resultsEl(i);
      if (!el) return;
      const s = search[i];
      if (s.items.length === 0) {
        el.innerHTML = '<div class="site-search-empty">No matching companies.</div>';
      } else {
        el.innerHTML = s.items
          .map(
            (c, idx) =>
              '<button type="button" class="site-search-result' + (idx === s.activeIndex ? " is-active" : "") + '" ' +
                'data-slot-select="' + i + '" data-company-id="' + escapeHtml(c.company_id) + '">' +
                '<div class="site-search-result-name">' + escapeHtml(c.display_name) + "</div>" +
                '<div class="site-search-result-meta">' + escapeHtml(c.company_id) + (c.sector ? " &middot; " + escapeHtml(c.sector) : "") + "</div>" +
              "</button>"
          )
          .join("");
      }
      el.hidden = false;
    }

    function closeResults(i) {
      const el = resultsEl(i);
      if (el) { el.hidden = true; el.innerHTML = ""; }
      search[i].items = [];
      search[i].activeIndex = -1;
    }

    async function runSearch(i, query) {
      const s = search[i];
      const thisRequest = ++s.requestId;
      try {
        const resp = await fetch(searchUrl + "?q=" + encodeURIComponent(query));
        const data = await resp.json();
        if (thisRequest !== s.requestId) return; // superseded by a newer keystroke
        const usedIds = slots.filter(Boolean).map((sl) => sl.meta.company_id);
        s.items = (data.results || []).filter((c) => usedIds.indexOf(c.company_id) === -1);
        s.activeIndex = -1;
        renderResults(i);
      } catch (e) {
        // Network hiccup on a typeahead isn't worth surfacing.
      }
    }

    async function fillSlot(i, companyId) {
      closeResults(i);
      slotEl(i).innerHTML = '<p class="muted">Loading&hellip;</p>';
      try {
        const metaResp = await fetch("/companies/" + encodeURIComponent(companyId) + "/compare-meta.json");
        if (!metaResp.ok) throw new Error("HTTP " + metaResp.status);
        const meta = await metaResp.json();
        let ds = null;
        try {
          const dsResp = await fetch(meta.financials_url);
          if (dsResp.ok) ds = await dsResp.json();
        } catch (e) {
          ds = null; // meta loaded fine, financials didn't -- still show the slot, just without numbers
        }
        slots[i] = { meta: meta, ds: ds };
      } catch (e) {
        slots[i] = null;
      }
      renderSlotContainer(i);
      renderTable();
    }

    function removeSlot(i) {
      slots[i] = null;
      search[i] = { items: [], activeIndex: -1, requestId: 0 };
      renderSlotContainer(i);
      renderTable();
    }

    function currentCurrencies() {
      return Array.from(new Set(slots.filter(Boolean).filter((s) => s.ds).map((s) => s.ds.CURRENCY || "INR")));
    }

    async function renderTable() {
      const filled = slots.filter(Boolean);
      if (filled.length === 0) {
        tableRoot.innerHTML = '<p class="muted">Add at least one company above to see its metrics.</p>';
        return;
      }
      const currencies = currentCurrencies();
      const mixed = currencies.length > 1;
      if (mixed && fxRate === null) {
        try {
          const resp = await fetch("/fx/usdinr.json");
          fxRate = resp.ok ? await resp.json() : { rate: null };
        } catch (e) {
          fxRate = { rate: null };
        }
      }
      const conversionUnavailable = mixed && (!fxRate || !fxRate.rate);

      const contexts = slots.map((slot) => {
        if (!slot || !slot.ds) return null;
        const ds = slot.ds;
        let ctx = V.buildRatioContext(
          ds.PERIODS, ds.PERIOD_KEYS, ds.METRICS, ds.CURRENCY,
          slot.meta.price, slot.meta.shares_outstanding, slot.meta.shares_outstanding_fy
        );
        if (mixed && !conversionUnavailable && ds.CURRENCY !== "USD") ctx = toUsdContext(ctx, fxRate.rate);
        return ctx;
      });

      const headerCells = slots.map((slot) =>
        "<th>" + (slot ? escapeHtml(slot.meta.display_name) : '<span class="muted">&mdash;</span>') + "</th>"
      ).join("");

      const bodyRows = ratioCatalog.map((r) => {
        const cells = slots.map((slot, i) => {
          if (!slot) return "<td>&mdash;</td>";
          if (!slot.ds || !contexts[i]) return '<td class="muted">&mdash;</td>';
          return "<td>" + V.RATIO_CATALOG[r.key].value(contexts[i]) + "</td>";
        }).join("");
        return "<tr><th>" + escapeHtml(r.label) + "</th>" + cells + "</tr>";
      }).join("");

      const warning = conversionUnavailable
        ? '<p class="compare-fx-note muted">These companies use different currencies, but a live USD/INR rate isn\'t available right now — figures below are each shown in their own native currency.</p>'
        : "";
      const footnote = mixed && !conversionUnavailable
        ? '<p class="compare-fx-note muted">Monetary figures converted to USD at 1 USD = &#8377;' +
            fxRate.rate.toFixed(2) + " (rate as of " + escapeHtml(fxRate.as_of) + ").</p>"
        : "";

      tableRoot.innerHTML =
        warning +
        '<div class="compare-table-wrap"><table class="table compare-table">' +
          "<thead><tr><th></th>" + headerCells + "</tr></thead>" +
          "<tbody>" + bodyRows + "</tbody>" +
        "</table></div>" +
        footnote;
    }

    // One set of delegated listeners on the whole slots container instead
    // of re-wiring per slot after every partial DOM update (fill/remove) --
    // event delegation means a slot's inner markup can be freely replaced
    // without ever losing its click/input handlers.
    slotsRoot.addEventListener("input", (e) => {
      if (!e.target.matches("[data-slot-input]")) return;
      const i = parseInt(e.target.dataset.slotInput, 10);
      const query = e.target.value.trim();
      clearTimeout(search[i].debounce);
      if (!query) { closeResults(i); return; }
      search[i].debounce = setTimeout(() => runSearch(i, query), 150);
    });

    slotsRoot.addEventListener("keydown", (e) => {
      if (!e.target.matches("[data-slot-input]")) return;
      const i = parseInt(e.target.dataset.slotInput, 10);
      const s = search[i];
      const el = resultsEl(i);
      if (!el || el.hidden || s.items.length === 0) return;
      if (e.key === "ArrowDown") { e.preventDefault(); s.activeIndex = Math.min(s.activeIndex + 1, s.items.length - 1); renderResults(i); }
      else if (e.key === "ArrowUp") { e.preventDefault(); s.activeIndex = Math.max(s.activeIndex - 1, 0); renderResults(i); }
      else if (e.key === "Enter") {
        e.preventDefault();
        const chosen = s.items[s.activeIndex >= 0 ? s.activeIndex : 0];
        if (chosen) fillSlot(i, chosen.company_id);
      } else if (e.key === "Escape") { closeResults(i); }
    });

    slotsRoot.addEventListener("click", (e) => {
      const selectBtn = e.target.closest("[data-slot-select]");
      if (selectBtn) { fillSlot(parseInt(selectBtn.dataset.slotSelect, 10), selectBtn.dataset.companyId); return; }
      const changeBtn = e.target.closest("[data-change]");
      if (changeBtn) { removeSlot(parseInt(changeBtn.dataset.change, 10)); return; }
      const removeBtn = e.target.closest("[data-remove]");
      if (removeBtn) { removeSlot(parseInt(removeBtn.dataset.remove, 10)); }
    });

    document.addEventListener("click", (e) => {
      if (slotsRoot.contains(e.target)) return;
      for (let i = 0; i < MAX_SLOTS; i++) closeResults(i);
    });

    // Initial paint: MAX_SLOTS empty slot containers, one <div data-slot="i">
    // shell per slot (renderSlotContainer fills each in place from here on).
    slotsRoot.innerHTML = Array.from({ length: MAX_SLOTS }, (_, i) => '<div class="compare-slot" data-slot="' + i + '"></div>').join("");
    for (let i = 0; i < MAX_SLOTS; i++) renderSlotContainer(i);
    renderTable();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
