/* Valuation tables under the Financials tab — ported from the "HDFC Bank
   Equity Dashboard" Claude Design project (claude.ai/design, project
   "Excel to web format"). The design tool's own template used a React-like
   `<x-dc>` runtime that only runs inside claude.ai/design; this is a plain-JS
   port against the same data. Deliberately facts-only: no assumption inputs
   (required return, projected/terminal growth, price growth) — everything
   here is a historical actual or a ratio/CAGR/sparkline computed directly
   from recorded data, over the full recorded year range. The original
   design's projection-dependent content (Growth Projection section,
   intrinsic value, margin of safety, +10yr @ proj. growth column) is
   intentionally not ported here for that reason. */
(function () {
  "use strict";

  // currency defaults to "INR" everywhere below — ported valuation_model_file
  // JSON (web/static/data/*.json, from the "HDFC Bank Equity Dashboard"
  // Claude Design import) has no CURRENCY field at all and is always an
  // Indian company anyway, so the omitted 4th argument naturally preserves
  // the original ₹ formatting for that path.
  function fmt(val, unit, currency) {
    if (val === null || val === undefined || Number.isNaN(val) || !Number.isFinite(val)) return "—";
    currency = currency || "INR";
    const isUSD = currency === "USD";
    switch (unit) {
      // Legacy unit names — only ever reached via ported valuation_model_file
      // JSON, which predates currency-aware formatting and is always ₹.
      case "crore": return "₹" + Math.round(val).toLocaleString("en-IN") + " Cr";
      case "rupee": return "₹" + val.toFixed(2);
      case "crShares": return val.toFixed(1) + " Cr";
      // Currency-agnostic category names — used by the live feed
      // (web/valuation_feed.py), scale/symbol chosen from `currency`.
      case "big":
        return isUSD
          ? "$" + Math.round(val).toLocaleString("en-US") + "M"
          : "₹" + Math.round(val).toLocaleString("en-IN") + " Cr";
      case "perShare":
        return (isUSD ? "$" : "₹") + val.toFixed(2);
      case "sharesCount":
        return isUSD ? val.toFixed(1) + "M" : val.toFixed(1) + " Cr";
      case "pct": return (val * 100).toFixed(1) + "%";
      case "pctAbs": return (Math.abs(val) * 100).toFixed(1) + "%";
      case "x": return val.toFixed(2) + "x";
      default: return String(val);
    }
  }

  function cagr(startVal, endVal, years) {
    if (startVal === null || endVal === null || !Number.isFinite(startVal) || !Number.isFinite(endVal)) return null;
    if (startVal <= 0 || endVal <= 0 || years <= 0) return null;
    return Math.pow(endVal / startVal, 1 / years) - 1;
  }

  function lastNonNull(values) {
    for (let i = values.length - 1; i >= 0; i--) {
      if (values[i] !== null && Number.isFinite(values[i])) return { val: values[i], idx: i };
    }
    return { val: null, idx: -1 };
  }

  function firstNonNull(values) {
    for (let i = 0; i < values.length; i++) {
      if (values[i] !== null && Number.isFinite(values[i])) return { val: values[i], idx: i };
    }
    return { val: null, idx: -1 };
  }

  function findRow(section, key) {
    return (section && section.find((m) => m.key === key)) || null;
  }

  function lastVal(section, key) {
    const row = findRow(section, key);
    return row ? lastNonNull(row.values).val : null;
  }

  function growthCagr(section, key, YEARS) {
    const row = findRow(section, key);
    if (!row) return null;
    const first = firstNonNull(row.values);
    const last = lastNonNull(row.values);
    if (first.idx < 0 || last.idx < 0 || first.idx === last.idx) return null;
    return cagr(first.val, last.val, YEARS[last.idx] - YEARS[first.idx]);
  }

  function parseFloatOrNull(s) {
    if (s === undefined || s === null || s === "") return null;
    const n = parseFloat(s);
    return Number.isFinite(n) ? n : null;
  }

  function sparkPath(values, w, h, pad) {
    w = w || 100; h = h || 28; pad = pad === undefined ? 2 : pad;
    const pts = values.map((v, i) => ({ v: v, i: i })).filter((p) => p.v !== null && Number.isFinite(p.v));
    if (pts.length < 2) return "";
    const vs = pts.map((p) => p.v);
    const min = Math.min.apply(null, vs);
    const max = Math.max.apply(null, vs);
    const range = max - min || 1;
    const coords = pts.map((p) => {
      const x = pad + (p.i / (values.length - 1)) * (w - 2 * pad);
      const y = h - pad - ((p.v - min) / range) * (h - 2 * pad);
      return x.toFixed(1) + "," + y.toFixed(1);
    });
    return "M" + coords.join(" L");
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  const SECTION_META = {
    balanceSheet: { title: "Balance Sheet", desc: function (y, c) { return "Core balance-sheet lines, FY" + y[0] + "–FY" + y[y.length - 1] + ", in " + (c === "USD" ? "USD millions" : "₹ Crore") + "."; } },
    incomeStatement: { title: "Income Statement", desc: function (y, c) { return "Revenue, expenses and profit, FY" + y[0] + "–FY" + y[y.length - 1] + ", in " + (c === "USD" ? "USD millions" : "₹ Crore") + "."; } },
    perShare: { title: "Per-Share Metrics", desc: function () { return "EPS, book value and dividend on a per-share basis."; } },
    profitability: { title: "Profitability Ratios", desc: function (y) { return "Margins and returns on capital, FY" + y[0] + "–FY" + y[y.length - 1] + "."; } },
    bankRatios: { title: "Bank-Specific Ratios", desc: function () { return "Credit-deposit and coverage ratios specific to a banking balance sheet."; } },
    valuation: { title: "Valuation", desc: function () { return "Historical price multiples recorded in the source model."; } },
  };

  function buildRow(metric, years, startYear, endYear, currency) {
    const idxStart = years.indexOf(startYear);
    const idxEnd = years.indexOf(endYear);
    const startVal = idxStart >= 0 ? metric.values[idxStart] : null;
    const endVal = idxEnd >= 0 ? metric.values[idxEnd] : null;
    const cagrVal = cagr(startVal, endVal, endYear - startYear);
    return {
      label: metric.label,
      startValFmt: fmt(startVal, metric.unit, currency),
      endValFmt: fmt(endVal, metric.unit, currency),
      cagrFmt: cagrVal === null ? "—" : (cagrVal * 100).toFixed(1) + "%",
      sparkPath: sparkPath(metric.values, 100, 28, 2),
    };
  }

  // Every value the ratio catalog below might need, computed once per
  // render. `price` and `sharesOutstanding` come from the page itself (a
  // data attribute on the root element, see init() below) — not from
  // METRICS.valuation, which the live-computed feed (web/valuation_feed.py)
  // deliberately leaves empty (no market-data pipeline; a guessed price is
  // worse than an honest blank there).
  function buildRatioContext(YEARS, METRICS, latestYear, currency, price, sharesOutstanding) {
    const lastEps = lastVal(METRICS.perShare, "eps");
    const lastBv = lastVal(METRICS.perShare, "bookValue");
    const lastDividend = lastVal(METRICS.perShare, "dividend");
    const lastSalesPerShare = lastVal(METRICS.perShare, "salesPerShare");
    const lastShares = lastVal(METRICS.perShare, "shares");
    const lastNetProfit = lastVal(METRICS.incomeStatement, "netProfit");
    const lastRevenue = lastVal(METRICS.incomeStatement, "earnings");
    const lastRoe = lastVal(METRICS.profitability, "roe");
    const lastPayout = lastVal(METRICS.profitability, "payout");
    const lastNetMargin = lastVal(METRICS.profitability, "netMargin");
    const lastTaxRate = lastVal(METRICS.profitability, "taxRate");
    const lastRetention = lastVal(METRICS.profitability, "retention");
    const lastRoa = lastVal(METRICS.bankRatios, "npAssets");
    const lastCdRatio = lastVal(METRICS.bankRatios, "cdRatio");
    const lastIntCoverage = lastVal(METRICS.bankRatios, "intCoverage");
    const lastBorrowings = lastVal(METRICS.balanceSheet, "borrowings");
    const lastShe = lastVal(METRICS.balanceSheet, "she");
    const lastNetworth = lastVal(METRICS.balanceSheet, "networth");
    const lastTotalAssets = lastVal(METRICS.balanceSheet, "totalAssets");
    const salesCagr = growthCagr(METRICS.incomeStatement, "earnings", YEARS);
    const profitCagr = growthCagr(METRICS.incomeStatement, "netProfit", YEARS);

    // Prefer the page's own server-resolved shares-outstanding (always
    // current) over the feed's own per-share series, which can lag a year
    // or two behind the latest fiscal year the rest of the feed reports.
    const shares = sharesOutstanding !== null ? sharesOutstanding : lastShares;

    return {
      YEARS: YEARS, latestYear: latestYear, currency: currency, price: price, shares: shares,
      lastEps: lastEps, lastBv: lastBv, lastDividend: lastDividend, lastSalesPerShare: lastSalesPerShare,
      lastNetProfit: lastNetProfit, lastRevenue: lastRevenue, lastRoe: lastRoe, lastPayout: lastPayout,
      lastNetMargin: lastNetMargin, lastTaxRate: lastTaxRate, lastRetention: lastRetention,
      lastRoa: lastRoa, lastCdRatio: lastCdRatio, lastIntCoverage: lastIntCoverage,
      lastNetworth: lastNetworth, lastTotalAssets: lastTotalAssets,
      salesCagr: salesCagr, profitCagr: profitCagr,
      marketCap: price !== null && shares !== null ? price * shares : null,
      stockPE: price !== null && lastEps ? price / lastEps : null,
      priceToBook: price !== null && lastBv ? price / lastBv : null,
      dividendYield: price !== null && lastDividend !== null ? lastDividend / price : null,
      debtToEquity: lastBorrowings !== null && lastShe ? lastBorrowings / lastShe : null,
    };
  }

  // The Overview tab's full ratio catalog — one entry per key in
  // storage/repositories.py's OVERVIEW_RATIO_CATALOG (which owns the
  // admin-facing label and the on/off default; this side owns how to
  // actually compute and format the value). Adding a genuinely new ratio
  // is exactly two edits: one entry there, one entry here — never a schema
  // or settings-table change. A key enabled in settings but missing here
  // (or vice versa) is simply skipped, not an error, so the two can be
  // edited independently without a deploy-ordering hazard.
  // A company with zero ingested financial data has an empty YEARS array,
  // so c.latestYear/c.YEARS[0] are undefined — without this guard, the
  // label functions below render as the literal string "FYundefined"
  // instead of gracefully dropping the year suffix, same as fmt() already
  // does for the *values* on a data-less row.
  function fySuffix(latestYear) {
    return latestYear !== undefined && latestYear !== null ? ", FY" + latestYear : "";
  }
  function fyRangeSuffix(years, latestYear) {
    return years && years.length && latestYear !== undefined && latestYear !== null
      ? ", FY" + years[0] + "–" + latestYear
      : "";
  }

  const RATIO_CATALOG = {
    marketCap: { label: "Market Cap", value: (c) => fmt(c.marketCap, "big", c.currency) },
    price: { label: "Current Price", value: (c) => fmt(c.price, "perShare", c.currency) },
    stockPE: { label: "Stock P/E", value: (c) => fmt(c.stockPE, "x") },
    bookValue: { label: "Book Value", value: (c) => fmt(c.lastBv, "perShare", c.currency) },
    dividendYield: { label: "Dividend Yield", value: (c) => fmt(c.dividendYield, "pct") },
    roe: { label: "ROE", value: (c) => fmt(c.lastRoe, "pct") },
    eps: { label: "EPS", value: (c) => fmt(c.lastEps, "perShare", c.currency) },
    priceToBook: { label: "Price to Book Value", value: (c) => fmt(c.priceToBook, "x") },
    debtToEquity: { label: "Debt to Equity", value: (c) => fmt(c.debtToEquity, "x") },
    payout: { label: "Dividend Payout", value: (c) => fmt(c.lastPayout, "pct") },
    shares: { label: "No. Equity Shares", value: (c) => fmt(c.shares, "sharesCount", c.currency) },
    netProfit: { label: (c) => "Net Profit" + fySuffix(c.latestYear), value: (c) => fmt(c.lastNetProfit, "big", c.currency) },
    revenue: { label: (c) => "Revenue" + fySuffix(c.latestYear), value: (c) => fmt(c.lastRevenue, "big", c.currency) },
    salesCagr: { label: (c) => "Sales Growth" + fyRangeSuffix(c.YEARS, c.latestYear), value: (c) => fmt(c.salesCagr, "pct") },
    profitCagr: { label: (c) => "Profit Growth" + fyRangeSuffix(c.YEARS, c.latestYear), value: (c) => fmt(c.profitCagr, "pct") },
    netMargin: { label: "Net Profit Margin", value: (c) => fmt(c.lastNetMargin, "pct") },
    taxRate: { label: "Tax Rate", value: (c) => fmt(c.lastTaxRate, "pctAbs") },
    retention: { label: "Retention Ratio", value: (c) => fmt(c.lastRetention, "pct") },
    roa: { label: "Return on Assets", value: (c) => fmt(c.lastRoa, "pct") },
    cdRatio: { label: "Credit-Deposit Ratio", value: (c) => fmt(c.lastCdRatio, "pct") },
    intCoverage: { label: "Interest Coverage", value: (c) => fmt(c.lastIntCoverage, "x") },
    networth: { label: "Net Worth", value: (c) => fmt(c.lastNetworth, "big", c.currency) },
    totalAssets: { label: "Total Assets", value: (c) => fmt(c.lastTotalAssets, "big", c.currency) },
    salesPerShare: { label: "Sales per Share", value: (c) => fmt(c.lastSalesPerShare, "perShare", c.currency) },
  };

  // Screener.in-style ratio grid, built from whichever catalog keys the
  // page says are enabled (ratioKeys — see init() below, sourced from
  // Admin -> Overview Ratios). Every row is either a directly-ingested
  // figure or a ratio derived from ones that are; a row genuinely
  // unavailable for this company still appears, showing "—" (fmt()'s own
  // null handling) rather than disappearing, same as every other numbers
  // table in this app — but a ratio Screener itself shows that this data
  // model has nowhere to source at all (Face Value, PEG, ROCE,
  // promoter/FII/DII/public holding %, day's High/Low, Cash Conversion
  // Cycle) isn't in the catalog to begin with, not faked.
  function renderOverview(YEARS, METRICS, latestYear, currency, price, sharesOutstanding, ratioKeys) {
    const ctx = buildRatioContext(YEARS, METRICS, latestYear, currency, price, sharesOutstanding);
    const keys = ratioKeys && ratioKeys.length ? ratioKeys : Object.keys(RATIO_CATALOG);
    const rows = keys
      .map((key) => RATIO_CATALOG[key])
      .filter(Boolean)
      .map((def) => [typeof def.label === "function" ? def.label(ctx) : def.label, def.value(ctx)]);

    if (!rows.length || rows.every((r) => r[1] === "—")) {
      return '<h2>Overview</h2><div class="empty-state">Not enough ingested data yet for a ratio snapshot.</div>';
    }

    const cellsHtml = rows
      .map(
        (r) =>
          '<div class="vm-ratio-cell"><span class="vm-ratio-label">' + escapeHtml(r[0]) +
          '</span><span class="vm-ratio-value">' + r[1] + '</span></div>'
      )
      .join("");

    return '<h2>Overview</h2><div class="card elev-sm vm-ratio-grid">' + cellsHtml + '</div>';
  }

  function renderTableSection(sectionId, YEARS, METRICS, currency) {
    const meta = SECTION_META[sectionId];
    const startYear = YEARS[0];
    const endYear = YEARS[YEARS.length - 1];
    const rows = METRICS[sectionId].map((m) => buildRow(m, YEARS, startYear, endYear, currency));

    let kpiHtml = "";
    if (sectionId === "valuation") {
      const eps = METRICS.perShare.find((m) => m.key === "eps");
      const bv = METRICS.perShare.find((m) => m.key === "bookValue");
      const dividend = METRICS.perShare.find((m) => m.key === "dividend");
      const price = METRICS.valuation.find((m) => m.key === "price");
      const lastEps = lastNonNull(eps.values).val;
      const lastBv = lastNonNull(bv.values).val;
      const lastDividend = lastNonNull(dividend.values).val;
      const lastPrice = price ? lastNonNull(price.values).val : null;
      kpiHtml =
        '<div class="vm-kpi-grid vm-kpi-grid-3">' +
          '<div class="card elev-sm"><div class="card-kicker">P/E at latest price</div><div class="card-title vm-num">' + fmt(lastPrice / lastEps, "x") + '</div></div>' +
          '<div class="card elev-sm"><div class="card-kicker">P/BV at latest price</div><div class="card-title vm-num">' + fmt(lastPrice / lastBv, "x") + '</div></div>' +
          '<div class="card elev-sm"><div class="card-kicker">Dividend yield at latest price</div><div class="card-title vm-num">' + fmt(lastDividend / lastPrice, "pct") + '</div></div>' +
        '</div>';
    }

    const bodyRows = rows
      .map(
        (r) =>
          "<tr><td>" + escapeHtml(r.label) + '</td><td class="text-muted vm-num">' + r.startValFmt + '</td><td class="vm-num">' + r.endValFmt +
          '</td><td class="vm-num">' + r.cagrFmt + '</td><td><svg viewBox="0 0 100 28" class="vm-spark"><path d="' + r.sparkPath +
          '" fill="none" stroke="var(--color-accent-700)" stroke-width="1.6"></path></svg></td></tr>'
      )
      .join("");

    return (
      "<h2>" + escapeHtml(meta.title) + "</h2>" +
      '<p class="muted vm-section-desc">' + escapeHtml(meta.desc(YEARS, currency)) + "</p>" +
      kpiHtml +
      '<div class="vm-table-scroll"><table class="table"><thead><tr><th>Metric</th><th>FY' + startYear + "</th><th>FY" + endYear +
        "</th><th>CAGR</th><th>Trend</th></tr></thead><tbody>" +
        bodyRows +
      "</tbody></table></div>"
    );
  }

  function renderSection(section, YEARS, METRICS, latestYear, currency, price, sharesOutstanding, ratioKeys) {
    if (section === "overview") return renderOverview(YEARS, METRICS, latestYear, currency, price, sharesOutstanding, ratioKeys);
    return renderTableSection(section, YEARS, METRICS, currency);
  }

  function init(root) {
    const dataUrl = root.dataset.url;
    const price = parseFloatOrNull(root.dataset.price);
    const sharesOutstanding = parseFloatOrNull(root.dataset.sharesOutstanding);
    let ratioKeys = [];
    try {
      ratioKeys = root.dataset.ratioKeys ? JSON.parse(root.dataset.ratioKeys) : [];
    } catch (e) {
      ratioKeys = [];
    }
    const contentEl = root.querySelector(".vm-content");
    const navButtons = Array.prototype.slice.call(root.querySelectorAll(".vm-nav-btn"));

    const state = { activeSection: "overview", data: null };

    function render() {
      if (!state.data) return;
      const YEARS = state.data.YEARS;
      const METRICS = state.data.METRICS;
      const currency = state.data.CURRENCY || "INR";
      const latestYear = YEARS[YEARS.length - 1];
      contentEl.innerHTML = renderSection(state.activeSection, YEARS, METRICS, latestYear, currency, price, sharesOutstanding, ratioKeys);
    }

    navButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        state.activeSection = btn.dataset.section;
        navButtons.forEach((b) => b.classList.toggle("active", b === btn));
        render();
      });
    });

    fetch(dataUrl)
      .then((r) => {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then((data) => {
        state.data = data;
        render();
      })
      .catch(() => {
        contentEl.innerHTML = '<div class="empty-state">Could not load the valuation model data.</div>';
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    // Financials tab: full sidebar (all sections). Overview tab: same
    // fetch + render, just no sidebar in the DOM, so it stays locked on
    // the "overview" section (state.activeSection's own default) — same
    // KPI snapshot + EPS trend, reachable without clicking into Financials.
    const dashboardRoot = document.getElementById("valuation-dashboard");
    if (dashboardRoot) init(dashboardRoot);
    const overviewRoot = document.getElementById("valuation-overview");
    if (overviewRoot) init(overviewRoot);
  });
})();
