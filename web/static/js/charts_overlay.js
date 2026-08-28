/* Charts tab — pick any number of attributes from web/charts_feed.py and
   overlay them on one time series. X-axis is always time, but is itself
   configurable two ways: period granularity (annual vs quarterly — a real
   refetch, since they're genuinely different series, not a client-side
   reshape of one dataset) and a trailing-window range (last N periods —
   a pure client-side slice of whichever granularity is loaded, no refetch).
   There are only 2 physical y-axes (left/right), so every selected
   attribute carries an explicit side; attributes sharing a side share one
   linear scale. Hand-rolled SVG, no charting library — same approach as
   valuation_dashboard.js/valuation_dashboard_interactive.js. */
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

  const SECTION_ORDER = ["incomeStatement", "balanceSheet", "perShare", "profitability", "bankRatios", "valuation"];
  const SECTION_TITLES = {
    incomeStatement: "Income Statement",
    balanceSheet: "Balance Sheet",
    perShare: "Per-Share Metrics",
    profitability: "Profitability Ratios",
    bankRatios: "Bank-Specific Ratios",
    valuation: "Valuation",
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

  const RANGE_OPTIONS = {
    annual: [{ v: "2", l: "Last 2 FY" }, { v: "5", l: "Last 5 FY" }, { v: "10", l: "Last 10 FY" }, { v: "all", l: "All" }],
    quarterly: [{ v: "4", l: "Last 4Q" }, { v: "8", l: "Last 8Q" }, { v: "12", l: "Last 12Q" }, { v: "all", l: "All" }],
  };
  const DEFAULT_RANGE = { annual: "10", quarterly: "8" };

  function attrId(section, key) { return section + ":" + key; }
  function colorVar(idx) { return "var(--chart-series-" + ((idx % PALETTE_SIZE) + 1) + ")"; }

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
    const rangeOpts = RANGE_OPTIONS[periodType].map((o) => {
      return '<option value="' + o.v + '"' + (o.v === range ? " selected" : "") + ">" + escapeHtml(o.l) + "</option>";
    }).join("");
    return (
      '<div class="chart-overlay-controls">' +
        '<span class="chart-overlay-side-toggle">' + periodBtns + "</span>" +
        '<select class="chart-overlay-range-select" data-range-select>' + rangeOpts + "</select>" +
      "</div>"
    );
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
        '<p>Pick any number of attributes. There are only 2 y-axes, so each one carries an L/R side — attributes sharing a side share one scale. Auto-assigned by matching units; use L/R to move one yourself.</p>' +
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

  // One shared linear scale for every attribute on a side — the actual
  // "2 y-axes total" constraint. min/max span every valid value across all
  // of that side's attributes, not each attribute independently, rounded
  // out to a nice tick ladder rather than the raw data's own min/max.
  function sideGeometry(attrs, n, x0, x1, y0, y1, maxTicks) {
    const allVals = [];
    attrs.forEach((a) => a.values.forEach((v) => { if (v !== null && Number.isFinite(v)) allVals.push(v); }));
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
  // attribute on it agrees on a unit; otherwise a plain number, since a
  // unit-specific format (₹, %, x) would misrepresent whichever attributes
  // don't use it.
  function sideUnit(attrs) {
    const units = new Set(attrs.map((a) => a.unit));
    return units.size === 1 ? attrs[0].unit : null;
  }

  function renderChart(PERIODS, leftAttrs, rightAttrs, colorOf, currency) {
    if (leftAttrs.length === 0 && rightAttrs.length === 0) {
      return '<div class="chart-overlay-empty">Select any number of attributes on the left to see them plotted over time.</div>';
    }
    const w = 720, h = 320;
    const padLeft = leftAttrs.length ? 62 : 16;
    const padRight = rightAttrs.length ? 62 : 16;
    const padTop = 16, padBottom = 32;
    const x0 = padLeft, x1 = w - padRight, y0 = padTop, y1 = h - padBottom;
    const n = PERIODS.length;

    // A 320px-tall chart doesn't have room for the reference screenshot's
    // 9-11 ticks without the labels colliding — 6 keeps every number legible.
    const maxTicks = 6;
    const leftGeom = leftAttrs.length ? sideGeometry(leftAttrs, n, x0, x1, y0, y1, maxTicks) : null;
    const rightGeom = rightAttrs.length ? sideGeometry(rightAttrs, n, x0, x1, y0, y1, maxTicks) : null;
    if (!leftGeom && !rightGeom) {
      const names = leftAttrs.concat(rightAttrs).map((a) => a.label).join(" / ");
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

    function drawSide(attrs, geom, axisX, anchor) {
      if (!geom) return;
      attrs.forEach((attr) => {
        const id = attrId(attr.section, attr.key);
        const color = colorOf(id);
        lines += '<path d="' + pathFor(attr.values, geom) + '" class="chart-overlay-line" style="stroke: ' + color + '"></path>';
        attr.values.forEach((v, i) => {
          if (v === null || !Number.isFinite(v)) return;
          const c = geom.coordFor(attr.values, i);
          dots +=
            '<circle cx="' + c.x.toFixed(1) + '" cy="' + c.y.toFixed(1) + '" r="2.8" style="fill: ' + color + '">' +
              "<title>" + escapeHtml(attr.label) + " · " + PERIODS[i] + ": " + escapeHtml(fmt(v, attr.unit, currency)) + "</title>" +
            "</circle>";
        });
      });
      const unit = sideUnit(attrs);
      geom.ticks.forEach((t) => {
        const y = geom.yFor(t);
        if (y < y0 - 0.5 || y > y1 + 0.5) return;
        axisLabels += '<text x="' + axisX + '" y="' + (y + 3) + '" text-anchor="' + anchor + '" class="chart-overlay-axis-label">' + escapeHtml(fmtAxis(t, unit, currency)) + "</text>";
      });
      if (!unit && attrs.length > 1) {
        axisLabels += '<text x="' + axisX + '" y="' + (y1 + (anchor === "end" ? -8 : 12)) + '" text-anchor="' + anchor + '" class="chart-overlay-axis-note">mixed units</text>';
      }
    }
    drawSide(leftAttrs, leftGeom, padLeft - 8, "end");
    drawSide(rightAttrs, rightGeom, x1 + 8, "start");

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

    const legend = leftAttrs.concat(rightAttrs).map((attr) => {
      const id = attrId(attr.section, attr.key);
      const side = leftAttrs.indexOf(attr) !== -1 ? "L" : "R";
      return (
        '<span class="chart-overlay-legend-item">' +
          '<span class="chart-overlay-swatch" style="background: ' + colorOf(id) + '"></span>' +
          escapeHtml(attr.label) + ' <span class="chart-overlay-legend-side">(' + side + ")</span>" +
        "</span>"
      );
    }).join("");

    return (
      '<svg viewBox="0 0 ' + w + " " + h + '" class="chart-overlay-svg" preserveAspectRatio="xMidYMid meet">' +
        grid + lines + dots + axisLabels + xLabels +
      "</svg>" +
      '<div class="chart-overlay-legend">' + legend + "</div>"
    );
  }

  function init(root) {
    const baseUrl = root.dataset.url;
    if (!baseUrl) return;
    const state = {
      cache: {},              // periodType -> {PERIODS, CURRENCY, METRICS, attributes, byId}
      periodType: "annual",
      range: DEFAULT_RANGE.annual,
      order: [],               // selected attribute ids, in selection order (drives color)
      sides: {},                // id -> "left" | "right"
      loading: false,
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

    function render() {
      const ds = state.cache[state.periodType];
      if (!ds) return;
      // Drop selections that don't exist for this period type (e.g. ROE,
      // annual-only) rather than silently rendering a phantom attribute.
      state.order = state.order.filter((id) => ds.byId[id]);
      Object.keys(state.sides).forEach((id) => { if (!ds.byId[id]) delete state.sides[id]; });

      const PERIODS = sliceLast(ds.PERIODS, state.range);
      const offset = ds.PERIODS.length - PERIODS.length;
      const selectedAttrs = state.order.map((id) => {
        const full = ds.byId[id];
        return { section: full.section, key: full.key, label: full.label, unit: full.unit, values: full.values.slice(offset) };
      });
      const leftAttrs = selectedAttrs.filter((a) => (state.sides[attrId(a.section, a.key)] || "left") === "left");
      const rightAttrs = selectedAttrs.filter((a) => (state.sides[attrId(a.section, a.key)] || "left") === "right");

      root.innerHTML =
        renderControls(state.periodType, state.range) +
        '<div class="chart-overlay-layout">' +
          '<div class="chart-overlay-picker">' + renderPicker(ds.attributes, state.order, state.sides, colorOf) + "</div>" +
          '<div class="chart-overlay-chart">' + renderChart(PERIODS, leftAttrs, rightAttrs, colorOf, ds.CURRENCY) + "</div>" +
        "</div>";

      root.querySelectorAll("[data-attr-id]").forEach((checkbox) => {
        checkbox.addEventListener("change", () => {
          const id = checkbox.dataset.attrId;
          const at = state.order.indexOf(id);
          if (checkbox.checked) {
            if (at === -1) {
              state.sides[id] = autoSideFor(ds.byId[id], state.order, state.sides, ds.byId);
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

      const rangeSelect = root.querySelector("[data-range-select]");
      if (rangeSelect) {
        rangeSelect.addEventListener("change", () => {
          state.range = rangeSelect.value;
          render();
        });
      }
    }

    function loadPeriodType(pt) {
      if (state.cache[pt]) return Promise.resolve(state.cache[pt]);
      const sep = baseUrl.indexOf("?") === -1 ? "?" : "&";
      return fetch(baseUrl + sep + "period_type=" + pt)
        .then((r) => {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then((data) => {
          const attributes = flattenAttributes(data.METRICS || {});
          const byId = {};
          attributes.forEach((a) => { byId[attrId(a.section, a.key)] = a; });
          const ds = { PERIODS: data.PERIODS || [], CURRENCY: data.CURRENCY || "INR", attributes: attributes, byId: byId };
          state.cache[pt] = ds;
          return ds;
        });
    }

    function switchPeriodType(pt) {
      state.periodType = pt;
      state.range = DEFAULT_RANGE[pt];
      root.innerHTML = '<p class="muted">Loading chart&hellip;</p>';
      loadPeriodType(pt)
        .then((ds) => {
          pickDefaults(ds);
          render();
        })
        .catch(() => {
          root.innerHTML = '<div class="chart-overlay-empty">Could not load chart data.</div>';
        });
    }

    loadPeriodType("annual")
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
