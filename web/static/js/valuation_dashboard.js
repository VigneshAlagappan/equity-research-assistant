/* Valuation tables under the Financials tab — ported from the "HDFC Bank
   Equity Dashboard" Claude Design project (claude.ai/design, project
   "Excel to web format"). The design tool's own template used a React-like
   `<x-dc>` runtime that only runs inside claude.ai/design; this is a plain-JS
   port against the same data. Deliberately facts-only: no assumption inputs
   (required return, projected/terminal growth, price growth) — everything
   here is a historical actual or a ratio/CAGR/sparkline computed directly
   from recorded data, over the full recorded period range. The original
   design's projection-dependent content (Growth Projection section,
   intrinsic value, margin of safety, +10yr @ proj. growth column) is
   intentionally not ported here for that reason — that lives in
   valuation_dashboard_interactive.js instead (Valuation Model tab), reading
   its own separate, annual-only feed (web/valuation_feed.py).

   Two feed shapes are handled here:
   - The live feed, web/charts_feed.py's build_charts_feed() (also what
     drives the Charts tab) — {"PERIODS": [...], "PERIOD_KEYS": [[year,
     quarter_num], ...], "CURRENCY", "METRICS": {section: [{key, label,
     unit, values}]}}, period_type-aware (annual/quarterly, see the
     Annual/Quarterly toggle in init()).
   - A ported valuation_model_file JSON (web/static/data/*.json) — the older
     {"YEARS": [...], "METRICS": {...}} shape, annual-only, no period_type
     concept, detected by the absence of PERIODS. */
(function () {
  "use strict";

  // currency defaults to "INR" everywhere below — ported valuation_model_file
  // JSON has no CURRENCY field at all and is always an Indian company
  // anyway, so the omitted 4th argument naturally preserves the original ₹
  // formatting for that path.
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
      // Currency-agnostic category names — used by both live feeds,
      // scale/symbol chosen from `currency`.
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
      case "volume": return Math.round(val).toLocaleString("en-IN");
      default: return String(val);
    }
  }

  function cagr(startVal, endVal, years) {
    if (startVal === null || endVal === null || !Number.isFinite(startVal) || !Number.isFinite(endVal)) return null;
    if (startVal <= 0 || endVal <= 0 || years <= 0) return null;
    return Math.pow(endVal / startVal, 1 / years) - 1;
  }

  // Elapsed years between two period_keys ([year, quarter_num], quarter_num
  // 0 for an annual period) — fractional for a quarterly span (e.g. Q1 FY24
  // to Q3 FY25 is 1.5 years), reduces to a plain integer year difference
  // when quarter_num is 0 on both sides (annual mode), so one formula
  // serves both period types without a branch.
  function elapsedYears(startKey, endKey) {
    if (!startKey || !endKey) return 0;
    return (endKey[0] * 4 + endKey[1] - (startKey[0] * 4 + startKey[1])) / 4;
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

  function growthCagr(section, key, periodKeys) {
    const row = findRow(section, key);
    if (!row) return null;
    const first = firstNonNull(row.values);
    const last = lastNonNull(row.values);
    if (first.idx < 0 || last.idx < 0 || first.idx === last.idx) return null;
    return cagr(first.val, last.val, elapsedYears(periodKeys[first.idx], periodKeys[last.idx]));
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

  // "valuation" is the sidebar/nav-facing section id everywhere (unchanged
  // markup, unchanged label) — internally it maps to whichever METRICS key
  // the feed actually provided: the live feed's real, price-history-backed
  // "priceVolume" (closePrice/volume/peRatio/pbRatio) if present, else the
  // ported feed's older "valuation" (price/pe/pbv/divYield, always-empty on
  // the now-retired live path that used to produce it).
  function valuationSection(METRICS) {
    return METRICS.priceVolume || METRICS.valuation || [];
  }

  // A company can genuinely have zero periods for the section being viewed
  // (verified against real Infosys data: no annual balance-sheet figures
  // were ever ingested for it from any source, only quarterly XBRL) — bare
  // p[0]/p[p.length-1] would render the literal string "undefined–undefined"
  // in that case instead of gracefully dropping the range, same "guard
  // against an empty array" fix already applied to the table's own column
  // headers below.
  function periodRange(p) {
    return p.length ? p[0] + "–" + p[p.length - 1] : "no data yet";
  }

  const SECTION_META = {
    balanceSheet: { title: "Balance Sheet", desc: function (p, c) { return "Core balance-sheet lines, " + periodRange(p) + ", in " + (c === "USD" ? "USD millions" : "₹ Crore") + "."; } },
    incomeStatement: { title: "Income Statement", desc: function (p, c) { return "Revenue, expenses and profit, " + periodRange(p) + ", in " + (c === "USD" ? "USD millions" : "₹ Crore") + "."; } },
    perShare: { title: "Per-Share Metrics", desc: function () { return "EPS, book value and dividend on a per-share basis."; } },
    profitability: { title: "Profitability Ratios", desc: function (p) { return "Margins and returns on capital, " + periodRange(p) + "."; } },
    bankRatios: { title: "Bank-Specific Ratios", desc: function () { return "Credit-deposit and coverage ratios specific to a banking balance sheet."; } },
    valuation: { title: "Valuation", desc: function () { return "Historical price and valuation multiples."; } },
  };

  // Every period as its own column (not just first/last) — same "Parameter
  // | Trend | one column per period | Growth" shape as the Valuation
  // Model tab's Growth Projection table, minus the forecast columns:
  // Financials is facts-only by design (this file's own header comment),
  // so there's nothing to project here, just the actuals that table's own
  // "Actual" column group already shows.
  function buildRow(metric, periodKeys, currency) {
    const startVal = metric.values[0];
    const endVal = metric.values[metric.values.length - 1];
    const cagrVal = cagr(startVal, endVal, elapsedYears(periodKeys[0], periodKeys[periodKeys.length - 1]));
    return {
      label: metric.label,
      type: metric.type || "fact",
      valuesFmt: metric.values.map((v) => fmt(v, metric.unit, currency)),
      cagrFmt: cagrVal === null ? "—" : (cagrVal * 100).toFixed(1) + "%",
      sparkPath: sparkPath(metric.values, 100, 28, 2),
    };
  }

  // Every value the ratio catalog below might need, computed once per
  // render. `price` comes from the page itself (a data attribute on the
  // root element, see init() below) — a live/latest quote, not a
  // period-end close, so it's independent of the Annual/Quarterly toggle.
  function buildRatioContext(periods, periodKeys, METRICS, currency, price, sharesOutstanding, sharesOutstandingFy) {
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
    const salesCagr = growthCagr(METRICS.incomeStatement, "earnings", periodKeys);
    const profitCagr = growthCagr(METRICS.incomeStatement, "netProfit", periodKeys);

    // Whatever period is latest in the currently-selected view (Annual or
    // Quarterly) — e.g. "FY2024" or "Q3 FY2025" — used to label Net
    // Profit/Revenue/Shares/Market Cap below, so it's always clear which
    // period's figure is on screen (a single quarter's Net Profit is not
    // the same thing as a full year's).
    const currentPeriodLabel = periods.length ? periods[periods.length - 1] : "";

    // Prefer the page's own server-resolved shares-outstanding (annual,
    // always consolidated — storage/repositories.py's
    // list_latest_shares_outstanding()) over the feed's own per-share row;
    // falls back to the feed's row only when there's no shares_outstanding
    // data ingested for this company at all under that source.
    const shares = sharesOutstanding !== null ? sharesOutstanding : lastShares;
    const sharesFy = sharesOutstanding !== null
      ? (sharesOutstandingFy !== null && sharesOutstandingFy !== undefined ? "FY" + sharesOutstandingFy : "")
      : currentPeriodLabel;

    return {
      periods: periods, currentPeriodLabel: currentPeriodLabel, currency: currency, price: price,
      shares: shares, sharesFy: sharesFy,
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
  // A company with zero ingested financial data has an empty periods array,
  // so c.currentPeriodLabel is "" — fmt()'s own null handling already turns
  // that into a plain unsuffixed label rather than a broken one.
  // type: "fact" (a raw ingested figure, at most relabeled/rescaled — price,
  // shares outstanding, net profit, revenue, networth, total assets) vs
  // "calc" (built from arithmetic or an independent ratio computation on
  // top of one or more raw figures — everything else here, including a row
  // whose own underlying series (lastEps/lastBv/lastSalesPerShare) already
  // carries a fill_missing fallback formula upstream in charts_feed.py).
  // Same FACT/CALC badge as the Financials tab (web/static/js/
  // valuation_dashboard.js's own renderTableSection) and base.html's
  // [FACT]/[CALCULATION] insight tags — one design, reused a third time.
  const RATIO_CATALOG = {
    marketCap: {
      label: (c) => "Market Cap" + (c.sharesFy ? ", " + c.sharesFy : ""),
      value: (c) => fmt(c.marketCap, "big", c.currency),
      title: (c) => (c.sharesFy ? "Current price × shares outstanding as reported for " + c.sharesFy : undefined),
      type: "calc",
    },
    price: { label: "Current Price", value: (c) => fmt(c.price, "perShare", c.currency), type: "fact" },
    stockPE: { label: "Stock P/E", value: (c) => fmt(c.stockPE, "x"), type: "calc" },
    bookValue: { label: "Book Value", value: (c) => fmt(c.lastBv, "perShare", c.currency), type: "calc" },
    dividendYield: { label: "Dividend Yield", value: (c) => fmt(c.dividendYield, "pct"), type: "calc" },
    roe: { label: "ROE", value: (c) => fmt(c.lastRoe, "pct"), type: "calc" },
    eps: { label: "EPS", value: (c) => fmt(c.lastEps, "perShare", c.currency), type: "calc" },
    priceToBook: { label: "Price to Book Value", value: (c) => fmt(c.priceToBook, "x"), type: "calc" },
    debtToEquity: { label: "Debt to Equity", value: (c) => fmt(c.debtToEquity, "x"), type: "calc" },
    payout: { label: "Dividend Payout", value: (c) => fmt(c.lastPayout, "pct"), type: "calc" },
    shares: {
      label: (c) => "No. Equity Shares" + (c.sharesFy ? ", " + c.sharesFy : ""),
      value: (c) => fmt(c.shares, "sharesCount", c.currency),
      title: (c) => (c.sharesFy ? "Shares outstanding as reported for " + c.sharesFy : undefined),
      type: "fact",
    },
    netProfit: {
      label: (c) => "Net Profit" + (c.currentPeriodLabel ? ", " + c.currentPeriodLabel : ""),
      value: (c) => fmt(c.lastNetProfit, "big", c.currency),
      type: "fact",
    },
    revenue: {
      label: (c) => "Revenue" + (c.currentPeriodLabel ? ", " + c.currentPeriodLabel : ""),
      value: (c) => fmt(c.lastRevenue, "big", c.currency),
      type: "fact",
    },
    salesCagr: { label: (c) => "Sales Growth" + (c.periods.length ? ", " + c.periods[0] + "–" + c.currentPeriodLabel : ""), value: (c) => fmt(c.salesCagr, "pct"), type: "calc" },
    profitCagr: { label: (c) => "Profit Growth" + (c.periods.length ? ", " + c.periods[0] + "–" + c.currentPeriodLabel : ""), value: (c) => fmt(c.profitCagr, "pct"), type: "calc" },
    netMargin: { label: "Net Profit Margin", value: (c) => fmt(c.lastNetMargin, "pct"), type: "calc" },
    taxRate: { label: "Tax Rate", value: (c) => fmt(c.lastTaxRate, "pctAbs"), type: "calc" },
    retention: { label: "Retention Ratio", value: (c) => fmt(c.lastRetention, "pct"), type: "calc" },
    roa: { label: "Return on Assets", value: (c) => fmt(c.lastRoa, "pct"), type: "calc" },
    cdRatio: { label: "Credit-Deposit Ratio", value: (c) => fmt(c.lastCdRatio, "pct"), type: "calc" },
    intCoverage: { label: "Interest Coverage", value: (c) => fmt(c.lastIntCoverage, "x"), type: "calc" },
    networth: { label: "Net Worth", value: (c) => fmt(c.lastNetworth, "big", c.currency), type: "fact" },
    totalAssets: { label: "Total Assets", value: (c) => fmt(c.lastTotalAssets, "big", c.currency), type: "fact" },
    salesPerShare: { label: "Sales per Share", value: (c) => fmt(c.lastSalesPerShare, "perShare", c.currency), type: "calc" },
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
  function renderOverview(periods, periodKeys, METRICS, currency, price, sharesOutstanding, sharesOutstandingFy, ratioKeys) {
    const ctx = buildRatioContext(periods, periodKeys, METRICS, currency, price, sharesOutstanding, sharesOutstandingFy);
    const keys = ratioKeys && ratioKeys.length ? ratioKeys : Object.keys(RATIO_CATALOG);
    const rows = keys
      .map((key) => RATIO_CATALOG[key])
      .filter(Boolean)
      .map((def) => [
        typeof def.label === "function" ? def.label(ctx) : def.label,
        def.value(ctx),
        def.title ? (typeof def.title === "function" ? def.title(ctx) : def.title) : null,
        def.type || "calc",
      ]);

    if (!rows.length || rows.every((r) => r[1] === "—")) {
      return '<h2>Overview</h2><div class="empty-state">Not enough ingested data yet for a ratio snapshot.</div>';
    }

    const cellsHtml = rows
      .map(
        (r) =>
          '<div class="vm-ratio-cell"' + (r[2] ? ' title="' + escapeHtml(r[2]) + '"' : "") +
          '><span class="vm-ratio-label">' + escapeHtml(r[0]) +
          ' <span class="tag ' + (r[3] === "calc" ? "tag-calculation" : "tag-fact") + '">' +
          (r[3] === "calc" ? "CALC" : "FACT") + "</span>" +
          '</span><span class="vm-ratio-value">' + r[1] + '</span></div>'
      )
      .join("");

    return '<h2>Overview</h2><div class="card elev-sm vm-ratio-grid">' + cellsHtml + '</div>';
  }

  function renderTableSection(sectionId, periods, periodKeys, METRICS, currency) {
    const meta = SECTION_META[sectionId];
    if (!periods.length) {
      // A genuine zero, not a slow-loading state — verified against real
      // Infosys data: it has quarterly XBRL but no annual figures for this
      // section from any source, so Annual view here has nothing to show
      // at all, not just gaps within an otherwise-populated table.
      return (
        "<h2>" + escapeHtml(meta.title) + "</h2>" +
        '<div class="empty-state">No data for this view yet — try the other Annual/Quarterly toggle.</div>'
      );
    }
    const sectionRows = sectionId === "valuation" ? valuationSection(METRICS) : METRICS[sectionId];
    const rows = sectionRows.map((m) => buildRow(m, periodKeys, currency));

    let kpiHtml = "";
    if (sectionId === "valuation") {
      const eps = findRow(METRICS.perShare, "eps");
      const bv = findRow(METRICS.perShare, "bookValue");
      const dividend = findRow(METRICS.perShare, "dividend");
      const priceRow = findRow(valuationSection(METRICS), "closePrice") || findRow(valuationSection(METRICS), "price");
      const lastEps = eps ? lastNonNull(eps.values).val : null;
      const lastBv = bv ? lastNonNull(bv.values).val : null;
      const lastDividend = dividend ? lastNonNull(dividend.values).val : null;
      const lastPrice = priceRow ? lastNonNull(priceRow.values).val : null;
      kpiHtml =
        '<div class="vm-kpi-grid vm-kpi-grid-3">' +
          '<div class="card elev-sm"><div class="card-kicker">P/E at latest price</div><div class="card-title vm-num">' + fmt(lastPrice / lastEps, "x") + '</div></div>' +
          '<div class="card elev-sm"><div class="card-kicker">P/BV at latest price</div><div class="card-title vm-num">' + fmt(lastPrice / lastBv, "x") + '</div></div>' +
          '<div class="card elev-sm"><div class="card-kicker">Dividend yield at latest price</div><div class="card-title vm-num">' + fmt(lastDividend / lastPrice, "pct") + '</div></div>' +
        '</div>';
    }

    const periodHeaderCells = periods.map((p) => '<th class="vm-num">' + escapeHtml(p) + "</th>").join("");
    // FACT (a canonical_financials value passed through as-is) vs CALC (built
    // from arithmetic on top of raw values) — same tag-fact/tag-calculation
    // badge already used for [FACT]/[CALCULATION] in AI-generated insights
    // (base.html), reused here rather than a new badge design. Row-level,
    // not per-cell: web/charts_feed.py's _row() docstring explains why a
    // fallback-derived row is "calc" for its whole column.
    const bodyRows = rows
      .map(
        (r) =>
          "<tr><td>" + escapeHtml(r.label) + '</td>' +
          '<td><span class="tag ' + (r.type === "calc" ? "tag-calculation" : "tag-fact") + '">' +
          (r.type === "calc" ? "CALC" : "FACT") + "</span></td>" +
          '<td><svg viewBox="0 0 100 28" class="vm-spark"><path d="' + r.sparkPath +
          '" fill="none" stroke="var(--color-accent-700)" stroke-width="1.6"></path></svg></td>' +
          r.valuesFmt.map((v) => '<td class="vm-num">' + v + "</td>").join("") +
          '<td class="vm-num">' + r.cagrFmt + "</td></tr>"
      )
      .join("");

    return (
      "<h2>" + escapeHtml(meta.title) + "</h2>" +
      '<p class="muted vm-section-desc">' + escapeHtml(meta.desc(periods, currency)) + "</p>" +
      kpiHtml +
      '<div class="vm-table-scroll vm-financials-table"><table class="table"><thead><tr><th>Parameter</th><th>Type</th><th class="vm-trend-header">Trend</th>' +
        periodHeaderCells + "<th>Growth</th></tr></thead><tbody>" +
        bodyRows +
      "</tbody></table></div>"
    );
  }

  function renderSection(section, periods, periodKeys, METRICS, currency, price, sharesOutstanding, sharesOutstandingFy, ratioKeys) {
    if (section === "overview") return renderOverview(periods, periodKeys, METRICS, currency, price, sharesOutstanding, sharesOutstandingFy, ratioKeys);
    return renderTableSection(section, periods, periodKeys, METRICS, currency);
  }

  function init(root, periodToggleEl) {
    const baseUrl = root.dataset.url;
    const price = parseFloatOrNull(root.dataset.price);
    const sharesOutstanding = parseFloatOrNull(root.dataset.sharesOutstanding);
    const sharesOutstandingFy = parseFloatOrNull(root.dataset.sharesOutstandingFy);
    let ratioKeys = [];
    try {
      ratioKeys = root.dataset.ratioKeys ? JSON.parse(root.dataset.ratioKeys) : [];
    } catch (e) {
      ratioKeys = [];
    }
    const contentEl = root.querySelector(".vm-content");
    const navButtons = Array.prototype.slice.call(root.querySelectorAll(".vm-nav-btn"));

    // Whichever section the sidebar's own "active" button already marks
    // (Financials tab: Balance Sheet — no Overview tile grid there any
    // more, kept only on the standalone Overview tab). No sidebar in the
    // DOM at all (the Overview tab's own snapshot widget) means no nav
    // buttons either, so this falls back to "overview" — the ratio grid,
    // its only section.
    const initialButton = navButtons.find((b) => b.classList.contains("active")) || navButtons[0];
    const state = { activeSection: initialButton ? initialButton.dataset.section : "overview", periodType: "annual", data: null };

    function render() {
      if (!state.data) return;
      const data = state.data;
      // Ported valuation_model_file JSON (no PERIODS field) is the older,
      // annual-only {"YEARS": [...]} shape — mapped to the same
      // periods/periodKeys shape the live feed already uses so every
      // render function below is period-shape-agnostic.
      const periods = data.PERIODS || (data.YEARS || []).map((y) => "FY" + y);
      const periodKeys = data.PERIOD_KEYS || (data.YEARS || []).map((y) => [y, 0]);
      const METRICS = data.METRICS;
      const currency = data.CURRENCY || "INR";
      contentEl.innerHTML = renderSection(
        state.activeSection, periods, periodKeys, METRICS, currency, price, sharesOutstanding, sharesOutstandingFy, ratioKeys
      );
    }

    function load() {
      const url = baseUrl + (baseUrl.indexOf("?") >= 0 ? "&" : "?") + "period_type=" + state.periodType;
      contentEl.innerHTML = '<p class="muted">Loading model&hellip;</p>';
      fetch(url)
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

    navButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        state.activeSection = btn.dataset.section;
        navButtons.forEach((b) => b.classList.toggle("active", b === btn));
        render();
      });
    });

    if (periodToggleEl) {
      const periodButtons = Array.prototype.slice.call(periodToggleEl.querySelectorAll(".vm-period-btn"));
      periodButtons.forEach((btn) => {
        btn.addEventListener("click", () => {
          if (btn.dataset.periodType === state.periodType) return;
          state.periodType = btn.dataset.periodType;
          periodButtons.forEach((b) => b.classList.toggle("is-active", b === btn));
          load();
        });
      });
    }

    load();
  }

  document.addEventListener("DOMContentLoaded", function () {
    // Financials tab: full sidebar (all sections) plus the Annual/Quarterly
    // toggle, if the page rendered one (ported-dataset companies don't —
    // see company.html's {% if not has_ported_dataset %} guard, since a
    // ported file has no quarterly data to switch to). Overview tab: same
    // fetch + render, no sidebar and no period toggle in the DOM, so it
    // stays locked on "overview"/"annual" — same KPI snapshot + EPS trend,
    // reachable without clicking into Financials.
    const dashboardRoot = document.getElementById("valuation-dashboard");
    if (dashboardRoot) init(dashboardRoot, document.querySelector("[data-vm-period-toggle]"));
    const overviewRoot = document.getElementById("valuation-overview");
    if (overviewRoot) init(overviewRoot, null);
  });
})();
