/* Valuation Model tab — the assumptions-driven counterpart to the
   facts-only dashboard on Financials (web/static/js/valuation_dashboard.js).
   Same data feed (valuation_data_url — a ported dataset or the live
   canonical_financials-derived one, see web/valuation_feed.py), but this
   tab lets you adjust required return / growth / price assumptions and see
   intrinsic value, margin of safety, and a 10-year projection recompute —
   the "play around and see the impact" tool. Originally ported from the
   "HDFC Bank Equity Dashboard" Claude Design project. */
(function () {
  "use strict";

  function fmt(val, unit) {
    if (val === null || val === undefined || Number.isNaN(val) || !Number.isFinite(val)) return "—";
    switch (unit) {
      case "crore": return "₹" + Math.round(val).toLocaleString("en-IN") + " Cr";
      case "rupee": return "₹" + val.toFixed(2);
      case "pct": return (val * 100).toFixed(1) + "%";
      case "pctAbs": return (Math.abs(val) * 100).toFixed(1) + "%";
      case "x": return val.toFixed(2) + "x";
      case "crShares": return val.toFixed(1) + " Cr";
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

  // Shared scaling for the Trend column's two chart sizes (small sparkline,
  // big expanded view) — one min/max/coordinate system so both agree on
  // where a given year's point sits.
  function chartGeometry(historicalValues, forecastValues, w, h, pad) {
    const all = historicalValues.concat(forecastValues);
    const n = all.length;
    const validPts = all.map((v, i) => ({ v: v, i: i })).filter((p) => p.v !== null && Number.isFinite(p.v));
    const histLen = historicalValues.length;
    if (validPts.length < 2) return { ok: false, all: all, n: n, histLen: histLen };
    const vs = validPts.map((p) => p.v);
    const min = Math.min.apply(null, vs);
    const max = Math.max.apply(null, vs);
    const range = max - min || 1;
    function coordFor(i) {
      const v = all[i];
      const x = pad + (i / (n - 1)) * (w - 2 * pad);
      const y = h - pad - ((v - min) / range) * (h - 2 * pad);
      return { x: x, y: y, v: v };
    }
    return { ok: true, all: all, n: n, histLen: histLen, min: min, max: max, coordFor: coordFor };
  }

  function pathFromGeometry(geom, indices) {
    const coords = indices
      .filter((i) => geom.all[i] !== null && Number.isFinite(geom.all[i]))
      .map((i) => { const c = geom.coordFor(i); return c.x.toFixed(1) + "," + c.y.toFixed(1); });
    return coords.length < 2 ? "" : "M" + coords.join(" L");
  }

  function actualForecastIndices(geom) {
    const actualIndices = [];
    for (let i = 0; i < geom.histLen; i++) actualIndices.push(i);
    // Forecast segment starts one index early (the last actual point) so
    // the two paths visually connect instead of leaving a gap.
    const forecastIndices = [];
    for (let i = Math.max(geom.histLen - 1, 0); i < geom.n; i++) forecastIndices.push(i);
    return { actualIndices: actualIndices, forecastIndices: forecastIndices };
  }

  // Trend column (Growth Projection): one continuous line spanning actual
  // + forecast, both segments sharing one scale so they're comparable, but
  // rendered as two separate paths so the forecast half can be styled
  // differently (dashed/tinted) — same actual-vs-forecast distinction as
  // the table's own columns, just as a shape instead of numbers.
  function combinedSparkPaths(historicalValues, forecastValues, w, h, pad) {
    w = w || 100; h = h || 28; pad = pad === undefined ? 2 : pad;
    const geom = chartGeometry(historicalValues, forecastValues, w, h, pad);
    if (!geom.ok) return { actual: "", forecast: "" };
    const idx = actualForecastIndices(geom);
    return { actual: pathFromGeometry(geom, idx.actualIndices), forecast: pathFromGeometry(geom, idx.forecastIndices) };
  }

  // Expanded chart: bigger, plus year/value markers — a hover (native SVG
  // <title>, no JS needed) shows "FYxxxx: value" per point, and sparse axis
  // labels give orientation without needing to scroll the wide table to
  // find what year a given point on the sparkline corresponds to.
  function detailedChartSvg(historicalValues, forecastValues, years, forecastYears, unit, w, h) {
    const padLeft = 44, padRight = 12, padTop = 12, padBottom = 28;
    const geom = chartGeometry(historicalValues, forecastValues, w - padLeft - padRight, h - padTop - padBottom, 0);
    if (!geom.ok) {
      return '<svg viewBox="0 0 ' + w + " " + h + '" class="vm-trend-expanded-svg"><text x="' + (w / 2) + '" y="' + (h / 2) + '" text-anchor="middle" class="vm-chart-empty-text">Not enough data</text></svg>';
    }
    // Shift every coordinate by the axis gutter, since chartGeometry itself
    // plots into a (w - gutters) x (h - gutters) box starting at (0,0).
    function coord(i) {
      const c = geom.coordFor(i);
      return { x: c.x + padLeft, y: c.y + padTop, v: c.v };
    }
    const idx = actualForecastIndices(geom);
    function pathFor(indices) {
      const coords = indices
        .filter((i) => geom.all[i] !== null && Number.isFinite(geom.all[i]))
        .map((i) => { const c = coord(i); return c.x.toFixed(1) + "," + c.y.toFixed(1); });
      return coords.length < 2 ? "" : "M" + coords.join(" L");
    }
    const actualPath = pathFor(idx.actualIndices);
    const forecastPath = pathFor(idx.forecastIndices);

    const allYears = years.concat(forecastYears);
    // A dot + native tooltip at every valid point — exact year/value on
    // hover without cluttering the chart with permanent text everywhere.
    let dots = "";
    for (let i = 0; i < geom.n; i++) {
      const v = geom.all[i];
      if (v === null || !Number.isFinite(v)) continue;
      const c = coord(i);
      const dotClass = i < geom.histLen ? "vm-chart-dot" : "vm-chart-dot vm-chart-dot-forecast";
      dots +=
        '<circle cx="' + c.x.toFixed(1) + '" cy="' + c.y.toFixed(1) + '" r="2.6" class="' + dotClass + '">' +
          "<title>FY" + allYears[i] + ": " + escapeHtml(fmt(v, unit)) + "</title>" +
        "</circle>";
    }

    // X-axis labels: first, last, and evenly-spaced years in between —
    // showing every year would overlap once there are 20-30 of them.
    const maxLabels = Math.max(2, Math.floor((w - padLeft - padRight) / 46));
    const lastIdx = geom.n - 1;
    // Evenly-spaced indices that always land exactly on 0 and lastIdx —
    // avoids the crowding a fixed step can cause when the last point falls
    // just short of a step boundary and a separately force-added label
    // ends up overlapping the one right before it.
    const labelCount = Math.min(maxLabels, geom.n);
    const labelIndices = [];
    for (let k = 0; k < labelCount; k++) {
      const i = labelCount === 1 ? 0 : Math.round((k * lastIdx) / (labelCount - 1));
      if (labelIndices[labelIndices.length - 1] !== i) labelIndices.push(i);
    }
    // First/last labels anchor to start/end rather than middle, so they
    // hug the axis edge instead of overflowing the viewBox around it.
    function anchorFor(i) { return i === 0 ? "start" : i === lastIdx ? "end" : "middle"; }
    let xLabels = "";
    labelIndices.forEach(function (i) {
      const c = coord(i);
      xLabels += '<text x="' + c.x.toFixed(1) + '" y="' + (h - padBottom + 16) + '" text-anchor="' + anchorFor(i) + '" class="vm-chart-axis-label">FY' + allYears[i] + "</text>";
    });

    // Y-axis: just the min and max actually plotted — enough to read scale
    // without a full tick ladder.
    const yTop = '<text x="' + (padLeft - 6) + '" y="' + (padTop + 4) + '" text-anchor="end" class="vm-chart-axis-label">' + escapeHtml(fmt(geom.max, unit)) + "</text>";
    const yBottom = '<text x="' + (padLeft - 6) + '" y="' + (h - padBottom) + '" text-anchor="end" class="vm-chart-axis-label">' + escapeHtml(fmt(geom.min, unit)) + "</text>";
    const boundaryX = coord(Math.max(geom.histLen - 1, 0)).x;
    const boundaryLine =
      '<line x1="' + boundaryX.toFixed(1) + '" y1="' + padTop + '" x2="' + boundaryX.toFixed(1) + '" y2="' + (h - padBottom) + '" class="vm-chart-boundary"></line>';

    return (
      '<svg viewBox="0 0 ' + w + " " + h + '" class="vm-trend-expanded-svg">' +
        boundaryLine +
        '<path d="' + actualPath + '" fill="none" class="vm-chart-line-actual"></path>' +
        '<path d="' + forecastPath + '" fill="none" class="vm-chart-line-forecast"></path>' +
        dots +
        xLabels + yTop + yBottom +
      "</svg>"
    );
  }

  const SECTION_META = {
    balanceSheet: { title: "Balance Sheet", desc: function (y) { return "Core balance-sheet lines, FY" + y[0] + "–FY" + y[y.length - 1] + ", in ₹ Crore."; } },
    incomeStatement: { title: "Income Statement", desc: function (y) { return "Revenue, expenses and profit, FY" + y[0] + "–FY" + y[y.length - 1] + ", in ₹ Crore."; } },
    perShare: { title: "Per-Share Metrics", desc: function () { return "EPS, book value and dividend on a per-share basis."; } },
    profitability: { title: "Profitability Ratios", desc: function (y) { return "Margins and returns on capital, FY" + y[0] + "–FY" + y[y.length - 1] + "."; } },
    bankRatios: { title: "Bank-Specific Ratios", desc: function () { return "Credit-deposit and coverage ratios specific to a banking balance sheet."; } },
    valuation: { title: "Valuation", desc: function () { return "Historical price multiples from the model, plus live multiples at the current price above."; } },
  };

  function buildRow(metric, years, startYear, endYear, projGrowth) {
    const idxStart = years.indexOf(startYear);
    const idxEnd = years.indexOf(endYear);
    const startVal = idxStart >= 0 ? metric.values[idxStart] : null;
    const endVal = idxEnd >= 0 ? metric.values[idxEnd] : null;
    const cagrVal = cagr(startVal, endVal, endYear - startYear);
    const last = lastNonNull(metric.values);
    const showProj = metric.unit === "crore" || metric.unit === "rupee";
    const proj10 = showProj && last.val !== null ? last.val * Math.pow(1 + projGrowth, 10) : null;
    return {
      label: metric.label,
      startValFmt: fmt(startVal, metric.unit),
      endValFmt: fmt(endVal, metric.unit),
      cagrFmt: cagrVal === null ? "—" : (cagrVal * 100).toFixed(1) + "%",
      sparkPath: sparkPath(metric.values, 100, 28, 2),
      proj10Fmt: proj10 === null ? "—" : fmt(proj10, metric.unit),
    };
  }

  function computeKpis(a, METRICS) {
    const eps = METRICS.perShare.find((m) => m.key === "eps");
    const bv = METRICS.perShare.find((m) => m.key === "bookValue");
    const dividend = METRICS.perShare.find((m) => m.key === "dividend");
    const netProfit = METRICS.incomeStatement.find((m) => m.key === "netProfit");
    const roe = METRICS.profitability.find((m) => m.key === "roe");

    const lastEps = lastNonNull(eps.values).val;
    const lastBv = lastNonNull(bv.values).val;
    const lastDividend = lastNonNull(dividend.values).val;
    const lastNetProfit = lastNonNull(netProfit.values).val;
    const lastRoe = lastNonNull(roe.values).val;

    const futureBv10 = lastBv !== null ? lastBv * Math.pow(1 + a.projGrowth, 10) : null;
    const intrinsicValue = futureBv10 !== null ? futureBv10 / Math.pow(1 + a.requiredRoR, 10) : null;
    const mos = intrinsicValue !== null && intrinsicValue > 0 ? (intrinsicValue - a.currentPrice) / intrinsicValue : null;
    const mosLabel = mos === null ? "—" : (mos * 100).toFixed(1) + "%" + (mos > 0.15 ? " · undervalued" : mos < -0.15 ? " · overvalued" : " · near fair value");
    const mosColor = mos === null ? "var(--ink)" : mos > 0.15 ? "var(--color-accent-700)" : mos < -0.15 ? "#8a3b2b" : "var(--ink)";

    return {
      lastEps: lastEps, lastBv: lastBv, lastDividend: lastDividend, lastNetProfit: lastNetProfit, lastRoe: lastRoe,
      futureBv10: futureBv10, intrinsicValue: intrinsicValue,
      kpiNetProfit: fmt(lastNetProfit, "crore"),
      kpiEps: fmt(lastEps, "rupee"),
      kpiBookValue: fmt(lastBv, "rupee"),
      kpiRoe: fmt(lastRoe, "pct"),
      kpiPrice: fmt(a.currentPrice, "rupee"),
      kpiLivePe: fmt(lastEps ? a.currentPrice / lastEps : null, "x"),
      kpiIntrinsic: fmt(intrinsicValue, "rupee"),
      kpiMos: mosLabel,
      kpiMosColor: mosColor,
    };
  }

  function renderOverview(a, YEARS, METRICS, latestYear) {
    const k = computeKpis(a, METRICS);
    const eps = METRICS.perShare.find((m) => m.key === "eps");
    const epsChartPath = sparkPath(eps.values, 600, 140, 6);
    const epsChartBody = epsChartPath
      ? '<svg viewBox="0 0 600 140" class="vm-eps-chart" preserveAspectRatio="none"><path d="' + epsChartPath + '" fill="none" stroke="var(--color-accent)" stroke-width="2"></path></svg>'
      : '<div class="vm-eps-chart vm-eps-chart-empty muted">—</div>';
    return (
      '<h2>Overview</h2>' +
      '<p class="muted vm-section-desc">Snapshot from the latest actuals (FY' + latestYear + '), and a present-value estimate of intrinsic value using the assumptions above.</p>' +
      '<div class="vm-kpi-grid">' +
        '<div class="card elev-sm"><div class="card-kicker">Net Profit, FY' + latestYear + '</div><div class="card-title vm-num">' + k.kpiNetProfit + '</div></div>' +
        '<div class="card elev-sm"><div class="card-kicker">EPS, FY' + latestYear + '</div><div class="card-title vm-num">' + k.kpiEps + '</div></div>' +
        '<div class="card elev-sm"><div class="card-kicker">Book Value / share</div><div class="card-title vm-num">' + k.kpiBookValue + '</div></div>' +
        '<div class="card elev-sm"><div class="card-kicker">RONW / ROE</div><div class="card-title vm-num">' + k.kpiRoe + '</div></div>' +
        '<div class="card elev-sm"><div class="card-kicker">Current price</div><div class="card-title vm-num">' + k.kpiPrice + '</div></div>' +
        '<div class="card elev-sm"><div class="card-kicker">P/E at current price</div><div class="card-title vm-num">' + k.kpiLivePe + '</div></div>' +
        '<div class="card elev-sm"><div class="card-kicker">Intrinsic value / share</div><div class="card-title vm-num">' + k.kpiIntrinsic + '</div></div>' +
        '<div class="card elev-sm"><div class="card-kicker">Margin of safety</div><div class="card-title vm-num" style="color:' + k.kpiMosColor + '">' + k.kpiMos + '</div></div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-kicker">EPS trend, FY' + YEARS[0] + '–FY' + latestYear + '</div>' +
        epsChartBody +
      '</div>'
    );
  }

  function renderGrowth(a, YEARS, METRICS, latestYear) {
    const k = computeKpis(a, METRICS);
    const bv = METRICS.perShare.find((m) => m.key === "bookValue");
    const lastBv = lastNonNull(bv.values).val;
    const lastEps = k.lastEps;

    const terminalYear = latestYear + 11;
    const years10 = [];
    for (let n = 1; n <= 10; n++) years10.push(latestYear + n);
    // +1 for the CAGR column sitting between the last actual and first
    // forecast column.
    const totalCols = YEARS.length + 1 + years10.length;

    // CAGR over the actual data only (the CAGR-window picker in Assumptions
    // above), marking the seam between "what's recorded" and "what's
    // projected" — same computation buildRow() below uses for every other
    // table's CAGR column.
    function cagrOfHistorical(values) {
      const idxStart = YEARS.indexOf(a.evalStartYear);
      const idxEnd = YEARS.indexOf(a.evalEndYear);
      const startVal = idxStart >= 0 ? values[idxStart] : null;
      const endVal = idxEnd >= 0 ? values[idxEnd] : null;
      const cagrVal = cagr(startVal, endVal, a.evalEndYear - a.evalStartYear);
      return cagrVal === null ? "—" : (cagrVal * 100).toFixed(1) + "%";
    }

    // Every Balance Sheet and Income Statement parameter, actuals through
    // FY{{latestYear}} plus 10 years projected forward at the same
    // projected-growth-rate assumption — not just the curated handful
    // (Advances/Total Income/Net Profit) the original design used.
    function projectMetric(metric) {
      const last = lastNonNull(metric.values).val;
      const rawForecast = last !== null
        ? years10.map((y, i) => last * Math.pow(1 + a.projGrowth, i + 1))
        : years10.map(() => null);
      return {
        label: metric.label,
        unit: metric.unit,
        historical: metric.values.map((v) => fmt(v, metric.unit)),
        rawHistorical: metric.values,
        cagr: cagrOfHistorical(metric.values),
        forecast: rawForecast.map((v) => fmt(v, metric.unit)),
        rawForecast: rawForecast,
      };
    }

    const balanceSheetRows = METRICS.balanceSheet.map(projectMetric);
    const incomeStatementRows = METRICS.incomeStatement.map(projectMetric);
    const perShareRows = METRICS.perShare.map(projectMetric);
    const profitabilityRows = METRICS.profitability.map(projectMetric);
    const bankRatiosRows = METRICS.bankRatios.map(projectMetric);

    // Valuation: Price and P/E are driven by the "Current stock price" /
    // "Projected annualized price growth" assumptions above, not the
    // financial-statement projected growth rate every other row here uses —
    // they get the same special-cased forecast as before. P/BV and Dividend
    // Yield have no such market-price-specific assumption, so they fall
    // back to the generic projected-growth projection like everything else.
    const priceMetric = METRICS.valuation.find((m) => m.key === "price");
    const peMetric = METRICS.valuation.find((m) => m.key === "pe");
    const pbvMetric = METRICS.valuation.find((m) => m.key === "pbv");
    const divYieldMetric = METRICS.valuation.find((m) => m.key === "divYield");
    const projEpsByYear = years10.map((y, i) => lastEps !== null ? lastEps * Math.pow(1 + a.projGrowth, i + 1) : null);
    const projPriceByYear = years10.map((y, i) => a.currentPrice * Math.pow(1 + a.priceGrowth, i + 1));
    const projPeByYear = projPriceByYear.map((p, i) => (projEpsByYear[i] ? p / projEpsByYear[i] : null));

    const valuationRows = [
      { label: priceMetric.label, unit: "rupee", historical: priceMetric.values.map((v) => fmt(v, "rupee")), rawHistorical: priceMetric.values,
        cagr: cagrOfHistorical(priceMetric.values), forecast: projPriceByYear.map((v) => fmt(v, "rupee")), rawForecast: projPriceByYear },
      { label: peMetric.label, unit: "x", historical: peMetric.values.map((v) => fmt(v, "x")), rawHistorical: peMetric.values,
        cagr: cagrOfHistorical(peMetric.values),
        forecast: projPeByYear.map((v) => fmt(v, "x")), rawForecast: projPeByYear },
      projectMetric(pbvMetric),
      projectMetric(divYieldMetric),
    ];

    const historicalHeaderCells = YEARS.map((y) => '<th class="vm-num">FY' + y + "</th>").join("");
    const forecastHeaderCells = years10.map((y) => '<th class="vm-num vm-forecast-col">FY' + y + "</th>").join("");
    function sectionRow(label) {
      // +2 for the Parameter and Trend columns ahead of the year/CAGR columns.
      return '<tr class="vm-growth-section"><td colspan="' + (totalCols + 2) + '">' + escapeHtml(label) + "</td></tr>";
    }
    // Small sparkline by default; click to swap in a bigger chart with
    // year labels and a hover tooltip per point — same actual-vs-forecast
    // split as the rest of the row, just as a shape.
    function trendCell(r) {
      const small = combinedSparkPaths(r.rawHistorical, r.rawForecast, 100, 28, 2);
      const detailed = detailedChartSvg(r.rawHistorical, r.rawForecast, YEARS, years10, r.unit, 640, 260);
      return (
        '<td class="vm-trend-cell">' +
          '<details class="vm-trend-details">' +
            '<summary aria-label="Expand trend for ' + escapeHtml(r.label) + '">' +
              '<svg viewBox="0 0 100 28" class="vm-spark">' +
                '<path d="' + small.actual + '" fill="none" stroke="var(--color-accent-700)" stroke-width="1.6"></path>' +
                '<path d="' + small.forecast + '" fill="none" stroke="var(--color-accent)" stroke-width="1.6" stroke-dasharray="3,2"></path>' +
              '</svg>' +
            '</summary>' +
            '<div class="vm-trend-expanded">' +
              '<div class="vm-trend-expanded-title">' + escapeHtml(r.label) + '</div>' +
              detailed +
            '</div>' +
          '</details>' +
        "</td>"
      );
    }
    function dataRows(rows) {
      return rows
        .map(
          (r) =>
            "<tr><td>" + escapeHtml(r.label) + "</td>" +
            trendCell(r) +
            r.historical.map((v) => '<td class="vm-num">' + v + "</td>").join("") +
            '<td class="vm-num vm-cagr-cell">' + r.cagr + "</td>" +
            r.forecast.map((v) => '<td class="vm-num vm-forecast-cell">' + v + "</td>").join("") +
            "</tr>"
        )
        .join("");
    }
    const bodyRows =
      sectionRow("Balance Sheet") + dataRows(balanceSheetRows) +
      sectionRow("Income Statement") + dataRows(incomeStatementRows) +
      sectionRow("Per-Share Metrics") + dataRows(perShareRows) +
      sectionRow("Profitability Ratios") + dataRows(profitabilityRows) +
      sectionRow("Bank Ratios") + dataRows(bankRatiosRows) +
      sectionRow("Valuation") + dataRows(valuationRows);

    const walkFutureBv = lastBv !== null ? lastBv * Math.pow(1 + a.projGrowth, 10) : null;
    const walkIntrinsic = walkFutureBv !== null ? walkFutureBv / Math.pow(1 + a.requiredRoR, 10) : null;
    const futureGrowthPct = (a.futureGrowth * 100).toFixed(1) + "%";

    const table =
      '<h2>10-Year Growth Projection</h2>' +
      '<p class="muted vm-section-desc">Actuals through FY' + latestYear + ' (CAGR over the window above), then 10 years projected forward at the assumptions above (shaded). Beyond FY' + terminalYear + ', growth is assumed to settle at the terminal rate. Parameter, Trend, and the year headers stay fixed as you scroll the table.</p>' +
      '<div class="vm-table-scroll"><table class="table"><thead>' +
        '<tr><th></th><th></th><th colspan="' + YEARS.length + '" class="vm-group-header">Actual</th><th class="vm-cagr-col"></th><th colspan="' + years10.length + '" class="vm-group-header vm-forecast-col">Forecast</th></tr>' +
        '<tr><th>Parameter</th><th>Trend</th>' + historicalHeaderCells + '<th class="vm-num vm-cagr-col">CAGR</th>' + forecastHeaderCells + '</tr>' +
      '</thead><tbody>' +
        bodyRows +
        '<tr><td class="text-muted" colspan="2">FY' + terminalYear + ' onward</td><td class="text-muted" colspan="' + totalCols + '">Grows at the terminal rate, ' + futureGrowthPct + ' / yr, in perpetuity</td></tr>' +
      '</tbody></table></div>' +
      '<p class="text-muted vm-footnote">Price compounds the current price at the annualized price growth rate above for forecast years; P/E divides it by projected EPS.</p>';

    // Rendered into #vmi-walk (next to Assumptions, see the template) by
    // the caller, not inlined here — that's what makes it sit parallel to
    // the assumptions panel instead of trailing after this whole table.
    const walk =
      '<div class="card">' +
        '<div class="card-kicker">Intrinsic value walk</div>' +
        '<div class="vm-walk">' +
          '<div class="vm-walk-label">Book value / share, FY' + latestYear + '</div><div class="vm-num">' + fmt(lastBv, "rupee") + '</div>' +
          '<div class="vm-walk-label">&times; (1 + projected growth)^10</div><div class="vm-num">' + fmt(walkFutureBv, "rupee") + '</div>' +
          '<div class="vm-walk-label">&divide; (1 + required rate of return)^10</div><div class="vm-num vm-walk-strong">' + fmt(walkIntrinsic, "rupee") + '</div>' +
        '</div>' +
        '<div class="hr"></div>' +
        '<div class="vm-walk">' +
          '<div class="vm-walk-label">Current price</div><div class="vm-num">' + fmt(a.currentPrice, "rupee") + '</div>' +
          '<div class="vm-walk-label">Margin of safety</div><div class="vm-num vm-walk-strong" style="color:' + k.kpiMosColor + '">' + k.kpiMos + '</div>' +
        '</div>' +
      '</div>';

    return { content: table, walk: walk };
  }

  function renderTableSection(sectionId, a, YEARS, METRICS) {
    const meta = SECTION_META[sectionId];
    const rows = METRICS[sectionId].map((m) => buildRow(m, YEARS, a.evalStartYear, a.evalEndYear, a.projGrowth));

    let kpiHtml = "";
    if (sectionId === "valuation") {
      const eps = METRICS.perShare.find((m) => m.key === "eps");
      const bv = METRICS.perShare.find((m) => m.key === "bookValue");
      const dividend = METRICS.perShare.find((m) => m.key === "dividend");
      const lastEps = lastNonNull(eps.values).val;
      const lastBv = lastNonNull(bv.values).val;
      const lastDividend = lastNonNull(dividend.values).val;
      kpiHtml =
        '<div class="vm-kpi-grid vm-kpi-grid-3">' +
          '<div class="card elev-sm"><div class="card-kicker">P/E at current price</div><div class="card-title vm-num">' + fmt(lastEps ? a.currentPrice / lastEps : null, "x") + '</div></div>' +
          '<div class="card elev-sm"><div class="card-kicker">P/BV at current price</div><div class="card-title vm-num">' + fmt(lastBv ? a.currentPrice / lastBv : null, "x") + '</div></div>' +
          '<div class="card elev-sm"><div class="card-kicker">Dividend yield at current price</div><div class="card-title vm-num">' + fmt(lastDividend !== null ? lastDividend / a.currentPrice : null, "pct") + '</div></div>' +
        '</div>';
    }

    const bodyRows = rows
      .map(
        (r) =>
          "<tr><td>" + escapeHtml(r.label) + '</td><td class="text-muted vm-num">' + r.startValFmt + '</td><td class="vm-num">' + r.endValFmt +
          '</td><td class="vm-num">' + r.cagrFmt + '</td><td><svg viewBox="0 0 100 28" class="vm-spark"><path d="' + r.sparkPath +
          '" fill="none" stroke="var(--color-accent-700)" stroke-width="1.6"></path></svg></td><td class="text-muted vm-num">' + r.proj10Fmt + "</td></tr>"
      )
      .join("");

    return (
      "<h2>" + escapeHtml(meta.title) + "</h2>" +
      '<p class="muted vm-section-desc">' + escapeHtml(meta.desc(YEARS)) + "</p>" +
      kpiHtml +
      '<div class="vm-table-scroll"><table class="table"><thead><tr><th>Metric</th><th>FY' + a.evalStartYear + "</th><th>FY" + a.evalEndYear +
        "</th><th>CAGR</th><th>Trend</th><th>+10yr @ proj. growth</th></tr></thead><tbody>" +
        bodyRows +
      "</tbody></table></div>"
    );
  }

  // Every section returns { content, walk } — walk is only non-empty for
  // Growth Projection (see renderGrowth); it's what gets shown in the
  // #vmi-walk slot next to Assumptions, cleared everywhere else.
  function renderSection(section, a, YEARS, METRICS, latestYear) {
    if (section === "overview") return { content: renderOverview(a, YEARS, METRICS, latestYear), walk: "" };
    if (section === "growth") return renderGrowth(a, YEARS, METRICS, latestYear);
    return { content: renderTableSection(section, a, YEARS, METRICS), walk: "" };
  }

  function init(root) {
    const dataUrl = root.dataset.url;
    const contentEl = root.querySelector("#vmi-content");
    const walkEl = root.querySelector("#vmi-walk");
    const navButtons = Array.prototype.slice.call(root.querySelectorAll(".vm-nav-btn"));
    const rorInput = root.querySelector("#vmi-ror");
    const projGrowthInput = root.querySelector("#vmi-proj-growth");
    const terminalGrowthInput = root.querySelector("#vmi-terminal-growth");
    const priceGrowthInput = root.querySelector("#vmi-price-growth");
    const priceInput = root.querySelector("#vmi-price");
    const startYearSelect = root.querySelector("#vmi-start-year");
    const endYearSelect = root.querySelector("#vmi-end-year");

    // Growth Projection is the only section left in this tab's sidebar
    // (Overview lives at the top-level Overview tab instead) — default
    // straight to it rather than an "overview" state with no matching button.
    const state = { activeSection: "growth", data: null };

    function currentAssumptions() {
      return {
        requiredRoR: (parseFloat(rorInput.value) || 0) / 100,
        projGrowth: (parseFloat(projGrowthInput.value) || 0) / 100,
        futureGrowth: (parseFloat(terminalGrowthInput.value) || 0) / 100,
        priceGrowth: (parseFloat(priceGrowthInput.value) || 0) / 100,
        currentPrice: parseFloat(priceInput.value) || 0,
        evalStartYear: parseInt(startYearSelect.value, 10),
        evalEndYear: parseInt(endYearSelect.value, 10),
      };
    }

    function render() {
      if (!state.data) return;
      const a = currentAssumptions();
      const YEARS = state.data.YEARS;
      const METRICS = state.data.METRICS;
      const latestYear = YEARS[YEARS.length - 1];
      const result = renderSection(state.activeSection, a, YEARS, METRICS, latestYear);
      contentEl.innerHTML = result.content;
      walkEl.innerHTML = result.walk;
    }

    navButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        state.activeSection = btn.dataset.section;
        navButtons.forEach((b) => b.classList.toggle("active", b === btn));
        render();
      });
    });
    [rorInput, projGrowthInput, terminalGrowthInput, priceGrowthInput, priceInput].forEach((el) => {
      el.addEventListener("input", render);
    });
    startYearSelect.addEventListener("change", render);
    endYearSelect.addEventListener("change", render);

    fetch(dataUrl)
      .then((r) => {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then((data) => {
        state.data = data;
        const years = data.YEARS;
        if (years.length === 0) {
          contentEl.innerHTML = '<div class="empty-state">No years of data available for this company yet.</div>';
          return;
        }
        const optionsHtml = years.map((y) => '<option value="' + y + '">' + y + "</option>").join("");
        startYearSelect.innerHTML = optionsHtml;
        endYearSelect.innerHTML = optionsHtml;
        startYearSelect.value = years[0];
        endYearSelect.value = years[years.length - 1];

        const priceMetric = data.METRICS.valuation.find((m) => m.key === "price");
        const lastPrice = priceMetric ? lastNonNull(priceMetric.values).val : null;
        if (lastPrice !== null) priceInput.value = lastPrice;

        render();
      })
      .catch(() => {
        contentEl.innerHTML = '<div class="empty-state">Could not load the valuation model data.</div>';
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    const root = document.getElementById("valuation-dashboard-interactive");
    if (root) init(root);
  });
})();
