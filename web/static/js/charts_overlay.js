/* Charts tab — pick any number of attributes from web/charts_feed.py and
   overlay them on one time series, for the current company plus up to 4
   "Compare With" peers (Nifty 500 only — the search box's own filter).
   X-axis is always time, but is itself configurable two ways: period
   granularity (annual vs quarterly — a real refetch, since they're
   genuinely different series, not a client-side reshape of one dataset)
   and a trailing-window range (last N periods — a pure client-side slice
   of whichever granularity is loaded, no refetch). There are only 2
   physical y-axes (left/right), so every selected attribute carries an
   explicit side; attributes sharing a side share one linear scale, and
   every company plotting that attribute shares that same side/scale too.
   Hand-rolled SVG, no charting library — same approach as
   valuation_dashboard.js/valuation_dashboard_interactive.js.

   Compare With: every company fetches its own charts-feed.json
   independently (own PERIODS -- companies can have different fiscal-year
   coverage), merged onto one shared union period axis via each company's
   PERIOD_KEYS (added specifically so the client can sort/merge periods
   correctly without re-parsing a formatted label like "Q1 FY2024").
   Attribute *selection* (the checkbox picker) is shared across all active
   companies, per design -- checking "Net Profit" draws one line per active
   company that actually has it, color-coded by attribute (unchanged from
   before) and line-style-coded by company (solid for the primary, a
   distinct dash pattern per comparison) so two dimensions (metric,
   company) stay visually separable without a third dimension of controls.
   With zero comparisons added (the default), every line is solid and the
   legend carries no company suffix -- pixel-identical to before this
   feature existed. */
(function () {
  "use strict";

  function fmt(val, unit, currency) {
    if (val === null || val === undefined || Number.isNaN(val) || !Number.isFinite(val)) return "—";
    currency = currency || "INR";
    const isUSD = currency === "USD";
    switch (unit) {
      case "crore": return "₹" + Math.round(val).toLocaleString("en-IN") + " Cr";
      case "rupee": return "₹" + val.toFixed(2);
      case "crShares": return val.toFixed(1) + " Cr";
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

  // Axis min/max text: attribute-aware formatting only makes sense when
  // every attribute sharing that side agrees on a unit — once they don't,
  // fall back to a plain number so the label isn't quietly wrong for half
  // of what it's labeling.
  function fmtAxis(val, unit, currency) {
    if (unit) return fmt(val, unit, currency);
    return Math.round(val).toLocaleString();
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  const SECTION_ORDER = ["incomeStatement", "balanceSheet", "perShare", "profitability", "bankRatios", "valuation", "priceVolume"];
  const SECTION_TITLES = {
    incomeStatement: "Income Statement",
    balanceSheet: "Balance Sheet",
    perShare: "Per-Share Metrics",
    profitability: "Profitability Ratios",
    bankRatios: "Bank-Specific Ratios",
    valuation: "Valuation",
    priceVolume: "Price & Volume",
  };
  // First pair (in order) where both attributes actually have data wins —
  // deliberately two different units, so a first-time visitor immediately
  // sees what the dual axis is for instead of landing on an empty chart.
  const DEFAULT_PICK_PAIRS = [
    ["incomeStatement:netProfit", "perShare:eps"],
    ["incomeStatement:netProfit", "incomeStatement:earnings"],
    ["perShare:eps", "perShare:bookValue"],
  ];
  const PALETTE_SIZE = 8;
  const MAX_COMPARISONS = 4;
  // Index 0 (the primary company) is always solid; a comparison company
  // gets one of these in order added. Distinct enough to tell apart at a
  // glance without relying on color alone (color already carries the
  // attribute, not the company).
  const DASH_PATTERNS = ["", "6,3", "2,2", "1,3", "8,2,2,2"];

  const RANGE_OPTIONS = {
    annual: [{ v: "2", l: "Last 2 FY" }, { v: "5", l: "Last 5 FY" }, { v: "10", l: "Last 10 FY" }, { v: "all", l: "Max" }],
    quarterly: [{ v: "4", l: "Last 4Q" }, { v: "8", l: "Last 8Q" }, { v: "12", l: "Last 12Q" }, { v: "all", l: "Max" }],
  };
  const DEFAULT_RANGE = { annual: "10", quarterly: "8" };

  function attrId(section, key) { return section + ":" + key; }
  function colorVar(idx) { return "var(--chart-series-" + ((idx % PALETTE_SIZE) + 1) + ")"; }
  function cacheKey(companyId, periodType) { return companyId + "|" + periodType; }
  function periodKeyStr(pk) { return pk[0] + ":" + pk[1]; }

  // Only surface attributes with at least one real value, over the FULL
  // fetched range (not whatever trailing window happens to be selected) —
  // a metric shouldn't wink in/out of the picker just because the visible
  // window shrank past its one data point.
  function flattenAttributes(METRICS) {
    const out = [];
    SECTION_ORDER.forEach((section) => {
      const rows = METRICS[section];
      if (!rows) return;
      rows.forEach((m) => {
        if (m.values.some((v) => v !== null && Number.isFinite(v))) {
          out.push({ section: section, key: m.key, label: m.label, unit: m.unit, values: m.values });
        }
      });
    });
    return out;
  }

  // Union of every active company's attributes (same id = same section+key
  // = the same metric everywhere, so first company to surface it supplies
  // the label/unit) — an attribute is pickable if ANY active company has
  // real data for it, not just the primary, so adding a comparison peer
  // with data the primary lacks still surfaces it. Stays SECTION_ORDER-
  // grouped, same as a single company's flattenAttributes() output, since
  // renderPicker() assumes consecutive same-section entries.
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

  // Merge every active company's PERIOD_KEYS into one deduped, chronologically
  // sorted axis (year, then quarter — quarter 0 is annual, N/A here since a
  // dataset is either all-annual or all-quarterly). First company to
  // introduce a given period_key supplies its label; format is identical
  // across companies for the same (year, quarter), so this never conflicts
  // in practice.
  function unionPeriods(datasets) {
    const seen = {};
    const merged = [];
    datasets.forEach((ds) => {
      ds.PERIOD_KEYS.forEach((pk, i) => {
        const k = periodKeyStr(pk);
        if (seen[k]) return;
        seen[k] = true;
        merged.push({ key: pk, label: ds.PERIODS[i] });
      });
    });
    merged.sort((a, b) => a.key[0] - b.key[0] || a.key[1] - b.key[1]);
    return { PERIODS: merged.map((m) => m.label), PERIOD_KEYS: merged.map((m) => m.key) };
  }

  function sliceLast(arr, n) {
    return n === "all" ? arr : arr.slice(Math.max(0, arr.length - n));
  }

  // Default side for a newly-checked attribute: prefer grouping with
  // whichever existing side already shares its unit (so a shared scale
  // stays numerically meaningful); an attribute with a brand-new unit goes
  // to the emptier side. Always overridable afterwards via the L/R toggle.
  function autoSideFor(attr, order, sides, byId) {
    if (order.length === 0) return "left";
    let leftUnits = new Set(), rightUnits = new Set(), leftCount = 0, rightCount = 0;
    order.forEach((id) => {
      const a = byId[id];
      if (sides[id] === "left") { leftUnits.add(a.unit); leftCount++; }
      else { rightUnits.add(a.unit); rightCount++; }
    });
    if (leftCount === 0) return "left";
    if (rightCount === 0) return leftUnits.has(attr.unit) ? "left" : "right";
    if (leftUnits.has(attr.unit)) return "left";
    if (rightUnits.has(attr.unit)) return "right";
    return leftCount <= rightCount ? "left" : "right";
  }

  function renderControls(periodType, range) {
    const periodBtns = ["annual", "quarterly"].map((pt) => {
      return '<button type="button" class="chart-overlay-side-btn' + (periodType === pt ? " is-active" : "") +
        '" data-period-type="' + pt + '">' + (pt === "annual" ? "Annual" : "Quarterly") + "</button>";
    }).join("");
    // A button row, not a <select> — same .chart-overlay-side-btn pill style
    // as the Annual/Quarterly toggle, so the timeframe is visible/clickable
    // at a glance rather than hidden behind a dropdown.
    const rangeBtns = RANGE_OPTIONS[periodType].map((o) => {
      return '<button type="button" class="chart-overlay-side-btn' + (o.v === range ? " is-active" : "") +
        '" data-range-btn="' + escapeHtml(o.v) + '">' + escapeHtml(o.l) + "</button>";
    }).join("");
    return (
      '<div class="chart-overlay-controls">' +
        '<span class="chart-overlay-side-toggle">' + periodBtns + "</span>" +
        '<span class="chart-overlay-side-toggle">' + rangeBtns + "</span>" +
      "</div>"
    );
  }

  // Compare With bar: pills for every added company (primary excluded —
  // it's the page's own subject, not a removable comparison) plus a search
  // box while under the cap. Search dropdown reuses .site-search-* verbatim
  // (styles.css) — same visual component as the top-nav company search.
  function renderCompareBar(state) {
    const pills = state.companies.slice(1).map((c) => {
      return (
        '<span class="chart-compare-pill">' + escapeHtml(c.name) +
          ' <button type="button" class="chart-compare-pill-remove" data-remove-company="' + escapeHtml(c.id) + '" title="Remove ' + escapeHtml(c.name) + '">&times;</button>' +
        "</span>"
      );
    }).join("");
    const canAddMore = state.companies.length - 1 < MAX_COMPARISONS;
    const searchBox = canAddMore
      ? (
          '<div class="chart-compare-search">' +
            '<input type="text" class="site-search-input" data-compare-input autocomplete="off" ' +
              'placeholder="+ Add company from NSE 500…" value="' + escapeHtml(state.compareQuery) + '">' +
            (state.compareOpen ? renderCompareResults(state) : "") +
          "</div>"
        )
      : '<span class="muted" style="font-size: 12px;">Maximum ' + MAX_COMPARISONS + ' comparison companies</span>';
    return (
      '<div class="chart-compare">' +
        '<div class="card-kicker">Compare With</div>' +
        '<div class="chart-compare-row">' + pills + searchBox + "</div>" +
      "</div>"
    );
  }

  function renderCompareResults(state) {
    if (state.compareResults.length === 0) {
      return '<div class="site-search-results"><div class="site-search-empty">No matching NSE 500 companies.</div></div>';
    }
    const items = state.compareResults.map((c, i) => {
      return (
        '<button type="button" class="site-search-result' + (i === state.compareActiveIndex ? " is-active" : "") + '" ' +
          'data-select-company="' + escapeHtml(c.company_id) + '" data-select-name="' + escapeHtml(c.display_name) + '">' +
          '<div class="site-search-result-name">' + escapeHtml(c.display_name) + '</div>' +
          '<div class="site-search-result-meta">' + escapeHtml(c.company_id) + (c.sector ? " · " + escapeHtml(c.sector) : "") + '</div>' +
        "</button>"
      );
    }).join("");
    return '<div class="site-search-results">' + items + "</div>";
  }

  // The picker used to spend a permanent ~4-line paragraph plus every
  // section always fully expanded — real estate that only pays for itself
  // on a first visit. The hint collapses into a one-line disclosure
  // (matching .insights-history's own <details> pattern), and each section
  // collapses too unless it already has a selection worth surfacing —
  // same picking intent (any number of attributes, grouped by statement
  // section, explicit L/R side), just not all paid for in vertical space
  // on every load regardless of what's actually selected.
  function renderPicker(attributes, order, sides, colorOf) {
    const hint =
      '<details class="chart-overlay-hint">' +
        '<summary>How this works</summary>' +
        '<p>Pick any number of attributes. There are only 2 y-axes, so each one carries an L/R side — attributes sharing a side share one scale. Auto-assigned by matching units; use L/R to move one yourself. With comparison companies added (Compare With, above the chart), each checked attribute draws one line per company.</p>' +
      "</details>";
    if (attributes.length === 0) {
      return hint + '<p class="muted">No data available for this period type yet.</p>';
    }

    const sections = [];
    let current = null;
    attributes.forEach((attr) => {
      if (!current || attr.section !== current.section) {
        current = { section: attr.section, checkedCount: 0, html: "" };
        sections.push(current);
      }
      const id = attrId(attr.section, attr.key);
      const isChecked = order.indexOf(id) !== -1;
      if (isChecked) current.checkedCount += 1;
      const swatch = isChecked
        ? '<span class="chart-overlay-swatch" style="background: ' + colorOf(id) + '"></span>'
        : '<span class="chart-overlay-swatch" style="background: transparent"></span>';
      const side = sides[id] || "left";
      const sideToggle = isChecked
        ? '<span class="chart-overlay-side-toggle">' +
            '<button type="button" class="chart-overlay-side-btn' + (side === "left" ? " is-active" : "") + '" data-side-toggle="' + escapeHtml(id) + '" data-side="left">L</button>' +
            '<button type="button" class="chart-overlay-side-btn' + (side === "right" ? " is-active" : "") + '" data-side-toggle="' + escapeHtml(id) + '" data-side="right">R</button>' +
          "</span>"
        : "";
      current.html +=
        '<div class="chart-overlay-attr">' +
          '<input type="checkbox" id="chart-attr-' + escapeHtml(id) + '" data-attr-id="' + escapeHtml(id) + '"' +
          (isChecked ? " checked" : "") + ">" +
          swatch +
          '<label for="chart-attr-' + escapeHtml(id) + '">' + escapeHtml(attr.label) + "</label>" +
          sideToggle +
        "</div>";
    });

    const sectionsHtml = sections
      .map((s) => {
        const title = escapeHtml(SECTION_TITLES[s.section] || s.section);
        const badge = s.checkedCount > 0 ? ' <span class="chart-overlay-section-count">' + s.checkedCount + "</span>" : "";
        return (
          '<details class="chart-overlay-section"' + (s.checkedCount > 0 ? " open" : "") + ">" +
            "<summary>" + title + badge + "</summary>" +
            '<div class="chart-overlay-section-body">' + s.html + "</div>" +
          "</details>"
        );
      })
      .join("");
    return hint + sectionsHtml;
  }

  // "Nice numbers for graph labels" (Heckbert) — rounds a raw span to a
  // 1/2/5 x 10^n step so axis ticks land on round numbers (0, 1,000,
  // 2,000, ... not 0, 973, 1,946, ...), the way every real charting tool
  // (and the Screener.in reference this was modeled on) labels an axis.
  function niceNum(range, round) {
    if (range === 0) return 1;
    const exponent = Math.floor(Math.log10(range));
    const fraction = range / Math.pow(10, exponent);
    let niceFraction;
    if (round) {
      niceFraction = fraction < 1.5 ? 1 : fraction < 3 ? 2 : fraction < 7 ? 5 : 10;
    } else {
      niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
    }
    return niceFraction * Math.pow(10, exponent);
  }

  function niceScale(rawMin, rawMax, maxTicks) {
    if (rawMin === rawMax) { rawMin -= 1; rawMax += 1; }
    const step = niceNum(niceNum(rawMax - rawMin, false) / (maxTicks - 1), true);
    const min = Math.floor(rawMin / step) * step;
    const max = Math.ceil(rawMax / step) * step;
    const ticks = [];
    for (let v = min; v <= max + step / 2; v += step) ticks.push(Math.round(v / step) * step);
    return { min: min, max: max, ticks: ticks };
  }

  // One shared linear scale for every series on a side — the actual
  // "2 y-axes total" constraint. min/max span every valid value across all
  // of that side's series (every attribute × every company plotting it),
  // not each series independently, rounded out to a nice tick ladder
  // rather than the raw data's own min/max.
  function sideGeometry(seriesList, n, x0, x1, y0, y1, maxTicks) {
    const allVals = [];
    seriesList.forEach((s) => s.values.forEach((v) => { if (v !== null && Number.isFinite(v)) allVals.push(v); }));
    if (allVals.length < 2) return null;
    const scale = niceScale(Math.min.apply(null, allVals), Math.max.apply(null, allVals), maxTicks || 6);
    const range = scale.max - scale.min || 1;
    function coordFor(values, i) {
      const x = n === 1 ? x0 : x0 + (i / (n - 1)) * (x1 - x0);
      const y = y1 - ((values[i] - scale.min) / range) * (y1 - y0);
      return { x: x, y: y };
    }
    function yFor(tickVal) { return y1 - ((tickVal - scale.min) / range) * (y1 - y0); }
    return { min: scale.min, max: scale.max, ticks: scale.ticks, coordFor: coordFor, yFor: yFor };
  }

  function pathFor(values, geom) {
    const coords = values
      .map((v, i) => ({ v: v, i: i }))
      .filter((p) => p.v !== null && Number.isFinite(p.v))
      .map((p) => { const c = geom.coordFor(values, p.i); return c.x.toFixed(1) + "," + c.y.toFixed(1); });
    return coords.length < 2 ? "" : "M" + coords.join(" L");
  }

  // A side's axis label uses that side's shared unit format only when every
  // series on it agrees on a unit; otherwise a plain number, since a
  // unit-specific format (₹, %, x) would misrepresent whichever series
  // don't use it.
  function sideUnit(seriesList) {
    const units = new Set(seriesList.map((s) => s.unit));
    return units.size === 1 ? seriesList[0].unit : null;
  }

  function renderChart(PERIODS, leftSeries, rightSeries, colorOf, currency, showCompanyNames, chartWidth) {
    if (leftSeries.length === 0 && rightSeries.length === 0) {
      return '<div class="chart-overlay-empty">Select any number of attributes on the left to see them plotted over time.</div>';
    }
    // The SVG viewBox width now matches the container's actual measured
    // width (render()'s chartWidth, from #charts-overlay's own
    // getBoundingClientRect() -- company.html's .chart-overlay no longer
    // caps that at 1040px) rather than a fixed 720 -- with an explicit
    // CSS height on <svg> (below) but no explicit width, a viewBox
    // narrower than the rendered box only stretches the *content*
    // pixel-for-pixel via preserveAspectRatio's default "meet" scaling,
    // it doesn't add real horizontal room: the maxTicks/maxLabels math
    // below is all in viewBox units, so a wider *physical* chart with the
    // same 720-unit-wide internal layout would still crowd the same
    // number of x-axis labels into the same relative space. Matching w to
    // the real width means more labels genuinely fit before crowding.
    // 720 remains the floor/fallback (first paint before layout settles,
    // or an unusually narrow viewport) -- never render a chart narrower
    // than the size this was originally designed at.
    const w = Math.max(720, Math.round(chartWidth) || 720), h = 320;
    const padLeft = leftSeries.length ? 62 : 16;
    const padRight = rightSeries.length ? 62 : 16;
    const padTop = 16, padBottom = 32;
    const x0 = padLeft, x1 = w - padRight, y0 = padTop, y1 = h - padBottom;
    const n = PERIODS.length;

    // A 320px-tall chart doesn't have room for the reference screenshot's
    // 9-11 ticks without the labels colliding — 6 keeps every number legible.
    const maxTicks = 6;
    const leftGeom = leftSeries.length ? sideGeometry(leftSeries, n, x0, x1, y0, y1, maxTicks) : null;
    const rightGeom = rightSeries.length ? sideGeometry(rightSeries, n, x0, x1, y0, y1, maxTicks) : null;
    if (!leftGeom && !rightGeom) {
      const names = leftSeries.concat(rightSeries).map((s) => s.label).join(" / ");
      return '<div class="chart-overlay-empty">Not enough data points in this range to chart ' + escapeHtml(names) + ".</div>";
    }

    // Gridlines follow whichever axis is "primary" (left if present, else
    // right) — drawing both sides' tick grids at once would double up into
    // visual noise, and the two scales rarely land on the same y positions
    // anyway (independent nice-number ladders).
    const gridGeom = leftGeom || rightGeom;
    let grid = "";
    gridGeom.ticks.forEach((t) => {
      const y = gridGeom.yFor(t);
      if (y < y0 - 0.5 || y > y1 + 0.5) return;
      grid += '<line x1="' + x0 + '" y1="' + y.toFixed(1) + '" x2="' + x1 + '" y2="' + y.toFixed(1) + '" class="chart-overlay-gridline"></line>';
    });

    let lines = "", dots = "", axisLabels = "";

    function drawSide(seriesList, geom, axisX, anchor) {
      if (!geom) return;
      seriesList.forEach((s) => {
        const id = attrId(s.section, s.key);
        const color = colorOf(id);
        const dash = DASH_PATTERNS[s.companyIndex % DASH_PATTERNS.length];
        const dashAttr = dash ? ' stroke-dasharray="' + dash + '"' : "";
        lines += '<path d="' + pathFor(s.values, geom) + '" class="chart-overlay-line" style="stroke: ' + color + '"' + dashAttr + "></path>";
        const nameSuffix = showCompanyNames ? " (" + escapeHtml(s.companyName) + ")" : "";
        s.values.forEach((v, i) => {
          if (v === null || !Number.isFinite(v)) return;
          const c = geom.coordFor(s.values, i);
          const tooltipText = escapeHtml(s.label) + nameSuffix + "\n" + PERIODS[i] + ": " + escapeHtml(fmt(v, s.unit, currency));
          dots +=
            '<circle cx="' + c.x.toFixed(1) + '" cy="' + c.y.toFixed(1) + '" r="2.8" style="fill: ' + color + '"></circle>' +
            // A visible r=2.8 dot is too small a click/tap target on its own —
            // this invisible, larger circle sits on top purely to catch the
            // click and carries the tooltip text (clicking it selects that
            // point and shows its value; see render()'s click wiring below).
            '<circle cx="' + c.x.toFixed(1) + '" cy="' + c.y.toFixed(1) + '" r="7" class="chart-overlay-hit" data-tooltip="' + tooltipText + '"><title>' + tooltipText + "</title></circle>";
        });
      });
      const unit = sideUnit(seriesList);
      geom.ticks.forEach((t) => {
        const y = geom.yFor(t);
        if (y < y0 - 0.5 || y > y1 + 0.5) return;
        axisLabels += '<text x="' + axisX + '" y="' + (y + 3) + '" text-anchor="' + anchor + '" class="chart-overlay-axis-label">' + escapeHtml(fmtAxis(t, unit, currency)) + "</text>";
      });
      if (!unit && seriesList.length > 1) {
        axisLabels += '<text x="' + axisX + '" y="' + (y1 + (anchor === "end" ? -8 : 12)) + '" text-anchor="' + anchor + '" class="chart-overlay-axis-note">mixed units</text>';
      }
    }
    drawSide(leftSeries, leftGeom, padLeft - 8, "end");
    drawSide(rightSeries, rightGeom, x1 + 8, "start");

    const maxLabels = Math.max(2, Math.floor((x1 - x0) / 46));
    const lastIdx = n - 1;
    const labelCount = Math.min(maxLabels, n);
    const labelIndices = [];
    for (let k = 0; k < labelCount; k++) {
      const i = labelCount === 1 ? 0 : Math.round((k * lastIdx) / (labelCount - 1));
      if (labelIndices[labelIndices.length - 1] !== i) labelIndices.push(i);
    }
    let xLabels = "";
    labelIndices.forEach((i) => {
      const x = lastIdx === 0 ? x0 : x0 + (i / lastIdx) * (x1 - x0);
      const anchor = i === 0 ? "start" : i === lastIdx ? "end" : "middle";
      xLabels += '<text x="' + x.toFixed(1) + '" y="' + (y1 + 16) + '" text-anchor="' + anchor + '" class="chart-overlay-axis-label">' + escapeHtml(PERIODS[i]) + "</text>";
    });

    const legend = leftSeries.concat(rightSeries).map((s) => {
      const id = attrId(s.section, s.key);
      const side = leftSeries.indexOf(s) !== -1 ? "L" : "R";
      const nameSuffix = showCompanyNames ? " · " + escapeHtml(s.companyName) : "";
      return (
        '<span class="chart-overlay-legend-item">' +
          '<span class="chart-overlay-swatch" style="background: ' + colorOf(id) + '"></span>' +
          escapeHtml(s.label) + nameSuffix + ' <span class="chart-overlay-legend-side">(' + side + ")</span>" +
        "</span>"
      );
    }).join("");

    return (
      '<svg viewBox="0 0 ' + w + " " + h + '" class="chart-overlay-svg" preserveAspectRatio="xMidYMid meet">' +
        grid + lines + dots + axisLabels + xLabels +
      "</svg>" +
      '<div class="chart-overlay-legend">' + legend + "</div>" +
      '<div class="chart-overlay-tooltip" hidden></div>'
    );
  }

  function init(root) {
    const urlTemplate = root.dataset.compareUrlTemplate;
    const searchUrl = root.dataset.searchUrl;
    const primaryId = root.dataset.primaryId;
    const primaryName = root.dataset.primaryName;
    if (!urlTemplate || !primaryId) return;

    const state = {
      cache: {},                // "companyId|periodType" -> {PERIODS, PERIOD_KEYS, CURRENCY, attributes, byId}
      companies: [{ id: primaryId, name: primaryName }],  // index 0 = primary, never removed
      periodType: "annual",
      range: DEFAULT_RANGE.annual,
      order: [],                 // selected attribute ids, in selection order (drives color) — shared across companies
      sides: {},                 // id -> "left" | "right" — shared across companies
      compareQuery: "",
      compareResults: [],
      compareOpen: false,
      compareActiveIndex: -1,
      compareFocusPending: false,
      compareDebounce: null,
    };

    function colorOf(id) {
      const idx = state.order.indexOf(id);
      return colorVar(idx === -1 ? 0 : idx);
    }

    function pickDefaults(ds) {
      const available = ds.attributes.map((a) => attrId(a.section, a.key));
      const pair = DEFAULT_PICK_PAIRS.find((p) => p.every((id) => available.indexOf(id) !== -1));
      state.order = [];
      state.sides = {};
      if (pair) {
        pair.forEach((id) => {
          state.sides[id] = autoSideFor(ds.byId[id], state.order, state.sides, ds.byId);
          state.order.push(id);
        });
      }
    }

    function loadCompany(companyId, periodType) {
      const key = cacheKey(companyId, periodType);
      if (state.cache[key]) return Promise.resolve(state.cache[key]);
      const url = urlTemplate.replace("__ID__", encodeURIComponent(companyId)) + "&period_type=" + periodType;
      return fetch(url)
        .then((r) => {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then((data) => {
          const attributes = flattenAttributes(data.METRICS || {});
          const byId = {};
          attributes.forEach((a) => { byId[attrId(a.section, a.key)] = a; });
          const ds = {
            PERIODS: data.PERIODS || [],
            PERIOD_KEYS: data.PERIOD_KEYS || [],
            CURRENCY: data.CURRENCY || "INR",
            attributes: attributes,
            byId: byId,
          };
          state.cache[key] = ds;
          return ds;
        });
    }

    function render() {
      const loaded = state.companies
        .map((c) => ({ company: c, ds: state.cache[cacheKey(c.id, state.periodType)] }))
        .filter((x) => x.ds);
      if (loaded.length === 0) return;

      const union = unionPeriods(loaded.map((x) => x.ds));
      const unionIndexByKey = {};
      union.PERIOD_KEYS.forEach((pk, i) => { unionIndexByKey[periodKeyStr(pk)] = i; });

      // Remap each company's per-attribute values onto the shared union axis.
      loaded.forEach((x) => {
        x.remapped = {};
        x.ds.attributes.forEach((a) => {
          const id = attrId(a.section, a.key);
          const out = new Array(union.PERIODS.length).fill(null);
          a.values.forEach((v, i) => {
            const idx = unionIndexByKey[periodKeyStr(x.ds.PERIOD_KEYS[i])];
            if (idx !== undefined) out[idx] = v;
          });
          x.remapped[id] = out;
        });
      });

      const unionAttrs = unionAttributes(loaded.map((x) => x.ds));
      const byIdUnion = {};
      unionAttrs.forEach((a) => { byIdUnion[attrId(a.section, a.key)] = a; });

      // Drop selections that don't exist for any active company/period type,
      // rather than silently rendering a phantom attribute.
      state.order = state.order.filter((id) => byIdUnion[id]);
      Object.keys(state.sides).forEach((id) => { if (!byIdUnion[id]) delete state.sides[id]; });

      const PERIODS = sliceLast(union.PERIODS, state.range);
      const offset = union.PERIODS.length - PERIODS.length;

      const allSeries = [];
      state.order.forEach((id) => {
        loaded.forEach((x, companyIndex) => {
          if (!x.remapped[id]) return; // this company has no data for this attribute
          const full = byIdUnion[id];
          allSeries.push({
            section: full.section, key: full.key, label: full.label, unit: full.unit,
            values: x.remapped[id].slice(offset),
            companyIndex: companyIndex, companyName: x.company.name,
          });
        });
      });
      const showCompanyNames = state.companies.length > 1;
      const leftSeries = allSeries.filter((s) => (state.sides[attrId(s.section, s.key)] || "left") === "left");
      const rightSeries = allSeries.filter((s) => (state.sides[attrId(s.section, s.key)] || "left") === "right");
      const currency = loaded[0].ds.CURRENCY;

      // Compare With on its own row, then one horizontal toolbar (period/
      // range toggle + attribute-picker pills, each a <details> dropdown
      // rather than an always-visible sidebar list) above the chart --
      // .chart-overlay in company.html now stretches all three to the
      // page-wide container's full breadth (see its own comment for why),
      // so renderChart() needs to know how wide that actually rendered to.
      // Measured on `root` itself (#charts-overlay, not a child of the
      // innerHTML being replaced below) so it reflects the *current*
      // layout width, not whatever the chart happened to render at last
      // time -- root's own width doesn't change as a side effect of
      // overwriting its innerHTML.
      const chartWidth = root.getBoundingClientRect().width;
      root.innerHTML =
        renderCompareBar(state) +
        '<div class="chart-overlay-toolbar">' +
          renderControls(state.periodType, state.range) +
          renderPicker(unionAttrs, state.order, state.sides, colorOf) +
        "</div>" +
        '<div class="chart-overlay-chart">' + renderChart(PERIODS, leftSeries, rightSeries, colorOf, currency, showCompanyNames, chartWidth) + "</div>";

      root.querySelectorAll("[data-attr-id]").forEach((checkbox) => {
        checkbox.addEventListener("change", () => {
          const id = checkbox.dataset.attrId;
          const at = state.order.indexOf(id);
          if (checkbox.checked) {
            if (at === -1) {
              state.sides[id] = autoSideFor(byIdUnion[id], state.order, state.sides, byIdUnion);
              state.order.push(id);
            }
          } else if (at !== -1) {
            state.order.splice(at, 1);
            delete state.sides[id];
          }
          render();
        });
      });

      root.querySelectorAll("[data-side-toggle]").forEach((btn) => {
        btn.addEventListener("click", () => {
          state.sides[btn.dataset.sideToggle] = btn.dataset.side;
          render();
        });
      });

      root.querySelectorAll("[data-period-type]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const pt = btn.dataset.periodType;
          if (pt === state.periodType) return;
          switchPeriodType(pt);
        });
      });

      root.querySelectorAll("[data-range-btn]").forEach((btn) => {
        btn.addEventListener("click", () => {
          state.range = btn.dataset.rangeBtn;
          render();
        });
      });

      // Click a point to select it and show its exact value — the native
      // SVG <title> tooltip (kept as a hover fallback) is slow to appear
      // and unusable on touch, so a click pins a small on-chart label
      // instead. Each dot's larger, invisible .chart-overlay-hit sibling
      // (drawSide(), above) is the actual click target — the visible r=2.8
      // dot alone is too small to tap reliably.
      const chartWrap = root.querySelector(".chart-overlay-chart");
      const tooltip = chartWrap ? chartWrap.querySelector(".chart-overlay-tooltip") : null;
      if (chartWrap && tooltip) {
        chartWrap.querySelectorAll("[data-tooltip]").forEach((hit) => {
          hit.addEventListener("click", (e) => {
            e.stopPropagation();
            const wrapRect = chartWrap.getBoundingClientRect();
            tooltip.textContent = hit.dataset.tooltip;
            tooltip.style.left = (e.clientX - wrapRect.left) + "px";
            tooltip.style.top = (e.clientY - wrapRect.top) + "px";
            tooltip.hidden = false;
          });
        });
      }

      root.querySelectorAll("[data-remove-company]").forEach((btn) => {
        btn.addEventListener("click", () => {
          state.companies = state.companies.filter((c) => c.id !== btn.dataset.removeCompany);
          render();
        });
      });

      root.querySelectorAll("[data-select-company]").forEach((btn) => {
        btn.addEventListener("click", () => {
          addCompany(btn.dataset.selectCompany, btn.dataset.selectName);
        });
      });

      const compareInput = root.querySelector("[data-compare-input]");
      if (compareInput) {
        compareInput.addEventListener("input", () => {
          state.compareQuery = compareInput.value.trim();
          clearTimeout(state.compareDebounce);
          if (!state.compareQuery) {
            state.compareOpen = false;
            state.compareResults = [];
            render();
            return;
          }
          state.compareDebounce = setTimeout(() => runCompareSearch(state.compareQuery), 150);
        });
        compareInput.addEventListener("keydown", (e) => {
          if (!state.compareOpen || state.compareResults.length === 0) return;
          if (e.key === "ArrowDown") {
            e.preventDefault();
            state.compareActiveIndex = Math.min(state.compareActiveIndex + 1, state.compareResults.length - 1);
            state.compareFocusPending = true;
            render();
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            state.compareActiveIndex = Math.max(state.compareActiveIndex - 1, 0);
            state.compareFocusPending = true;
            render();
          } else if (e.key === "Enter") {
            e.preventDefault();
            const chosen = state.compareResults[state.compareActiveIndex >= 0 ? state.compareActiveIndex : 0];
            if (chosen) addCompany(chosen.company_id, chosen.display_name);
          } else if (e.key === "Escape") {
            state.compareOpen = false;
            render();
          }
        });
        if (state.compareFocusPending) {
          state.compareFocusPending = false;
          compareInput.focus();
          const pos = compareInput.value.length;
          compareInput.setSelectionRange(pos, pos);
        }
      }
    }

    function runCompareSearch(query) {
      const activeIds = state.companies.map((c) => c.id);
      fetch(searchUrl + "?q=" + encodeURIComponent(query) + "&index=" + encodeURIComponent("Nifty 500"))
        .then((r) => r.json())
        .then((data) => {
          if (state.compareQuery !== query) return; // a newer keystroke already superseded this request
          state.compareResults = (data.results || []).filter((c) => activeIds.indexOf(c.company_id) === -1);
          state.compareOpen = true;
          state.compareActiveIndex = -1;
          state.compareFocusPending = true;
          render();
        })
        .catch(() => {});
    }

    function addCompany(companyId, displayName) {
      if (state.companies.some((c) => c.id === companyId)) return;
      if (state.companies.length - 1 >= MAX_COMPARISONS) return;
      state.companies.push({ id: companyId, name: displayName });
      state.compareQuery = "";
      state.compareOpen = false;
      state.compareResults = [];
      render();
      const key = cacheKey(companyId, state.periodType);
      if (!state.cache[key]) {
        loadCompany(companyId, state.periodType)
          .then(() => render())
          .catch(() => { /* pill stays, chart just has nothing to draw for it yet */ });
      }
    }

    function switchPeriodType(pt) {
      state.periodType = pt;
      state.range = DEFAULT_RANGE[pt];
      root.innerHTML = '<p class="muted">Loading chart&hellip;</p>';
      Promise.all(state.companies.map((c) => loadCompany(c.id, pt)))
        .then((datasets) => {
          pickDefaults(datasets[0]);
          render();
        })
        .catch(() => {
          root.innerHTML = '<div class="chart-overlay-empty">Could not load chart data.</div>';
        });
    }

    document.addEventListener("click", (e) => {
      if (state.compareOpen && !root.contains(e.target)) {
        state.compareOpen = false;
        render();
      }
      // A dot's own click handler stops propagation, so this only fires for
      // a click elsewhere — dismiss whatever point was previously selected.
      if (!e.target.closest("[data-tooltip]")) {
        const tip = root.querySelector(".chart-overlay-tooltip");
        if (tip) tip.hidden = true;
      }
    });

    loadCompany(primaryId, "annual")
      .then((ds) => {
        pickDefaults(ds);
        render();
      })
      .catch(() => {
        root.innerHTML = '<div class="chart-overlay-empty">Could not load chart data.</div>';
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    const root = document.getElementById("charts-overlay");
    if (root) init(root);
  });
})();
