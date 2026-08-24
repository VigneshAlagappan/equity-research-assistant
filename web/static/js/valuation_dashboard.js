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

  function renderOverview(YEARS, METRICS, latestYear, currency) {
    const eps = METRICS.perShare.find((m) => m.key === "eps");
    const bv = METRICS.perShare.find((m) => m.key === "bookValue");
    const netProfit = METRICS.incomeStatement.find((m) => m.key === "netProfit");
    const roe = METRICS.profitability.find((m) => m.key === "roe");
    const price = METRICS.valuation.find((m) => m.key === "price");

    const lastEps = lastNonNull(eps.values).val;
    const lastBv = lastNonNull(bv.values).val;
    const lastNetProfit = lastNonNull(netProfit.values).val;
    const lastRoe = lastNonNull(roe.values).val;
    const lastPrice = price ? lastNonNull(price.values).val : null;

    return (
      '<h2>Overview</h2>' +
      '<p class="muted vm-section-desc">Latest recorded actuals, FY' + latestYear + '.</p>' +
      '<div class="vm-kpi-grid">' +
        '<div class="card elev-sm"><div class="card-kicker">Net Profit, FY' + latestYear + '</div><div class="card-title vm-num">' + fmt(lastNetProfit, "big", currency) + '</div></div>' +
        '<div class="card elev-sm"><div class="card-kicker">EPS, FY' + latestYear + '</div><div class="card-title vm-num">' + fmt(lastEps, "perShare", currency) + '</div></div>' +
        '<div class="card elev-sm"><div class="card-kicker">Book Value / share</div><div class="card-title vm-num">' + fmt(lastBv, "perShare", currency) + '</div></div>' +
        '<div class="card elev-sm"><div class="card-kicker">RONW / ROE</div><div class="card-title vm-num">' + fmt(lastRoe, "pct") + '</div></div>' +
        '<div class="card elev-sm"><div class="card-kicker">Latest recorded price</div><div class="card-title vm-num">' + fmt(lastPrice, "perShare", currency) + '</div></div>' +
        '<div class="card elev-sm"><div class="card-kicker">P/E at latest price</div><div class="card-title vm-num">' + fmt(lastPrice / lastEps, "x") + '</div></div>' +
      '</div>'
    );
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

  function renderSection(section, YEARS, METRICS, latestYear, currency) {
    if (section === "overview") return renderOverview(YEARS, METRICS, latestYear, currency);
    return renderTableSection(section, YEARS, METRICS, currency);
  }

  function init(root) {
    const dataUrl = root.dataset.url;
    const contentEl = root.querySelector(".vm-content");
    const navButtons = Array.prototype.slice.call(root.querySelectorAll(".vm-nav-btn"));

    const state = { activeSection: "overview", data: null };

    function render() {
      if (!state.data) return;
      const YEARS = state.data.YEARS;
      const METRICS = state.data.METRICS;
      const currency = state.data.CURRENCY || "INR";
      const latestYear = YEARS[YEARS.length - 1];
      contentEl.innerHTML = renderSection(state.activeSection, YEARS, METRICS, latestYear, currency);
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
