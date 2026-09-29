/* Settings > Administration > Eval Analytics — hand-rolled SVG charts, same
   approach as charts_overlay.js/valuation_dashboard.js (no charting
   library). Data is embedded server-side (web/app.py's
   _eval_analytics_panel_context, rendered into settings.html's
   #eval-analytics-data JSON script tag) rather than fetched — this panel
   has no filters that need a fresh payload without a full page reload
   (the one filter it has, the time-period picker, is a normal query-param
   link), so there's nothing here client-side data-fetching would buy.

   Three charts, three different jobs (dataviz skill: "pick the form the
   data's job needs" — see references/choosing-a-form.md):
   - Volume by complexity level -- ORDINAL (position in an ordered
     sequence: Level 1..5), one hue, monotone light->dark. Color is
     redundant with the x-axis label here on purpose (the axis is the
     real identity channel); the ramp just makes "higher level" visually
     read as "further along," same as a funnel chart's own stepped shade.
   - Golden-eval accuracy by level -- STATUS (matched/mismatched is a
     pass/fail state, not a series identity), so it takes the fixed
     good/critical pair, stacked per level, always with a legend (2
     series) since color is never the only signal for state.
   - Eval pass-rate trend -- single-series magnitude over time, one hue,
     with a hover crosshair (line charts get one by default per the
     skill's interaction step).

   Palette source: dataviz skill's reference palette.md, validated via its
   validate_palette.js (ordinal ramp, both --mode light and --mode dark
   against this app's own dark surfaces #1c1b1a/#12171e) -- see the CSS
   custom properties this file reads (--eval-level-1..5, --eval-status-
   good/critical) for the actual hex, themed the same
   ":root[data-theme=\"dark\"], :root[data-theme=\"signals\"]" way
   base.html's own --indicator-* tokens already are. */
(function () {
  "use strict";

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtPct(v) {
    return v === null || v === undefined ? "—" : Math.round(v * 100) + "%";
  }

  function levelColor(level) {
    return "var(--eval-level-" + level + ")";
  }

  // Thin marks, 4px rounded data-ends anchored to the baseline (dataviz
  // skill's mark spec) -- a plain <rect> with ry can't round only the top
  // two corners, so bars are drawn as a path: two straight sides, a flat
  // bottom on the baseline, and an arc-rounded top.
  function barPath(x, yTop, width, yBase, radius) {
    const r = Math.min(radius, width / 2, Math.max(0, yBase - yTop));
    if (yBase - yTop <= 0) return "";
    return (
      "M " + x + " " + yBase +
      " L " + x + " " + (yTop + r) +
      " Q " + x + " " + yTop + " " + (x + r) + " " + yTop +
      " L " + (x + width - r) + " " + yTop +
      " Q " + (x + width) + " " + yTop + " " + (x + width) + " " + (yTop + r) +
      " L " + (x + width) + " " + yBase +
      " Z"
    );
  }

  // Single-series ordinal bar chart -- question volume by complexity level.
  function buildLevelVolumeChart(rows) {
    const w = 640, h = 220, padLeft = 36, padRight = 16, padTop = 16, padBottom = 28;
    const x0 = padLeft, x1 = w - padRight, y0 = padTop, y1 = h - padBottom;
    const maxCount = Math.max(1, ...rows.map((r) => r.count));
    const band = (x1 - x0) / rows.length;
    const barWidth = Math.min(56, band * 0.6);

    let bars = "", axisLabels = "", grid = "";
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const y = y1 - (i / ticks) * (y1 - y0);
      grid += '<line x1="' + x0 + '" y1="' + y.toFixed(1) + '" x2="' + x1 + '" y2="' + y.toFixed(1) + '" class="eval-chart-gridline"></line>';
    }

    rows.forEach((row, i) => {
      const cx = x0 + band * (i + 0.5);
      const barH = (row.count / maxCount) * (y1 - y0);
      const barX = cx - barWidth / 2;
      const barY = y1 - barH;
      const tooltip = escapeHtml(row.label) + ": " + row.count.toLocaleString() + " routed question" + (row.count === 1 ? "" : "s");
      bars +=
        '<path d="' + barPath(barX, barY, barWidth, y1, 4) + '" class="eval-chart-bar" style="fill: ' + levelColor(row.level) +
        '" data-tooltip="' + tooltip + '"><title>' + tooltip + "</title></path>" +
        (row.count > 0
          ? '<text x="' + cx.toFixed(1) + '" y="' + (barY - 6).toFixed(1) + '" class="eval-chart-bar-label" text-anchor="middle">' + row.count.toLocaleString() + "</text>"
          : "") +
        '<text x="' + cx.toFixed(1) + '" y="' + (y1 + 18) + '" class="eval-chart-axis-label" text-anchor="middle">L' + row.level + "</text>";
    });

    return (
      '<svg viewBox="0 0 ' + w + " " + h + '" class="eval-chart-svg" role="img" aria-label="Routed question volume by complexity level">' +
      '<line x1="' + x0 + '" y1="' + y1 + '" x2="' + x1 + '" y2="' + y1 + '" class="eval-chart-axis"></line>' +
      grid + bars + axisLabels + "</svg>"
    );
  }

  // Two-series (status) stacked bar chart -- matched (good) vs mismatched
  // (critical) count per level, for one eval run.
  function buildAccuracyChart(rows) {
    const w = 640, h = 220, padLeft = 36, padRight = 16, padTop = 16, padBottom = 28;
    const x0 = padLeft, x1 = w - padRight, y0 = padTop, y1 = h - padBottom;
    const maxTotal = Math.max(1, ...rows.map((r) => r.total));
    const band = (x1 - x0) / rows.length;
    const barWidth = Math.min(56, band * 0.6);
    const gap = 2; // surface gap between stacked segments (dataviz mark spec)

    let bars = "", labels = "";
    const ticks = 4;
    let grid = "";
    for (let i = 0; i <= ticks; i++) {
      const y = y1 - (i / ticks) * (y1 - y0);
      grid += '<line x1="' + x0 + '" y1="' + y.toFixed(1) + '" x2="' + x1 + '" y2="' + y.toFixed(1) + '" class="eval-chart-gridline"></line>';
    }

    rows.forEach((row, i) => {
      const cx = x0 + band * (i + 0.5);
      const barX = cx - barWidth / 2;
      const mismatched = row.total - row.matched;
      const matchedH = row.total ? (row.matched / maxTotal) * (y1 - y0) : 0;
      const mismatchedH = row.total ? (mismatched / maxTotal) * (y1 - y0) : 0;
      const matchedY = y1 - matchedH;
      const mismatchedY = matchedH > 0 ? matchedY - gap - mismatchedH : y1 - mismatchedH;

      if (row.matched > 0) {
        const tip = escapeHtml(row.label) + " — matched: " + row.matched + "/" + row.total;
        bars += '<path d="' + barPath(barX, matchedY, barWidth, y1, 4) + '" class="eval-chart-bar eval-chart-status-good" data-tooltip="' + tip + '"><title>' + tip + "</title></path>";
      }
      if (mismatched > 0) {
        const tip = escapeHtml(row.label) + " — mismatched: " + mismatched + "/" + row.total;
        bars += '<path d="' + barPath(barX, mismatchedY, barWidth, matchedH > 0 ? matchedY - gap : y1, 4) + '" class="eval-chart-bar eval-chart-status-critical" data-tooltip="' + tip + '"><title>' + tip + "</title></path>";
      }
      const rateLabel = row.total ? fmtPct(row.matched / row.total) : "—";
      const topY = row.total ? Math.min(matchedY, mismatchedY) : y1;
      labels +=
        '<text x="' + cx.toFixed(1) + '" y="' + (topY - 6).toFixed(1) + '" class="eval-chart-bar-label" text-anchor="middle">' + rateLabel + "</text>" +
        '<text x="' + cx.toFixed(1) + '" y="' + (y1 + 18) + '" class="eval-chart-axis-label" text-anchor="middle">L' + row.level + "</text>";
    });

    return (
      '<svg viewBox="0 0 ' + w + " " + h + '" class="eval-chart-svg" role="img" aria-label="Golden-eval accuracy by complexity level, latest run">' +
      '<line x1="' + x0 + '" y1="' + y1 + '" x2="' + x1 + '" y2="' + y1 + '" class="eval-chart-axis"></line>' +
      grid + bars + labels + "</svg>"
    );
  }

  // Single-series line chart -- eval pass-rate trend across recent runs.
  function buildTrendChart(points) {
    const w = 640, h = 200, padLeft = 40, padRight = 16, padTop = 16, padBottom = 28;
    const x0 = padLeft, x1 = w - padRight, y0 = padTop, y1 = h - padBottom;
    if (points.length === 0) {
      return '<div class="eval-chart-empty">No eval runs recorded yet.</div>';
    }
    const n = points.length;
    const xFor = (i) => (n === 1 ? (x0 + x1) / 2 : x0 + (i / (n - 1)) * (x1 - x0));
    const yFor = (rate) => y1 - (rate === null ? 0 : rate) * (y1 - y0);

    let grid = "", gridLabels = "";
    [0, 0.5, 1].forEach((frac) => {
      const y = y1 - frac * (y1 - y0);
      grid += '<line x1="' + x0 + '" y1="' + y.toFixed(1) + '" x2="' + x1 + '" y2="' + y.toFixed(1) + '" class="eval-chart-gridline"></line>';
      gridLabels += '<text x="' + (x0 - 8) + '" y="' + (y + 4).toFixed(1) + '" class="eval-chart-axis-label" text-anchor="end">' + Math.round(frac * 100) + "%</text>";
    });

    let path = "";
    let dots = "";
    points.forEach((p, i) => {
      const x = xFor(i);
      const y = yFor(p.pass_rate === null ? 0 : p.pass_rate);
      path += (i === 0 ? "M " : "L ") + x.toFixed(1) + " " + y.toFixed(1) + " ";
      const tip = "Run #" + p.run_id + " (" + escapeHtml(p.started_at.slice(0, 10)) + "): " + fmtPct(p.pass_rate) + " (" + p.matched + "/" + p.total + ")";
      // >=8px hit target (invisible, larger) around the visible 3px dot,
      // same click-to-pin hover pattern charts_overlay.js uses.
      dots +=
        '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="8" class="eval-chart-hit" data-tooltip="' + tip + '"><title>' + tip + "</title></circle>" +
        '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="3" class="eval-chart-dot"></circle>';
    });

    return (
      '<svg viewBox="0 0 ' + w + " " + h + '" class="eval-chart-svg" role="img" aria-label="Golden-eval pass rate trend across recent runs">' +
      '<line x1="' + x0 + '" y1="' + y1 + '" x2="' + x1 + '" y2="' + y1 + '" class="eval-chart-axis"></line>' +
      grid + gridLabels +
      '<path d="' + path.trim() + '" class="eval-chart-line"></path>' +
      dots + "</svg>"
    );
  }

  // Click-to-pin tooltip, same mechanism as charts_overlay.js's own
  // .chart-overlay-tooltip (an SVG <title> is the hover/keyboard fallback;
  // a fixed-position hover popover is unreliable on touch, so a click pins
  // a small label instead).
  function wireTooltip(container) {
    const tooltip = container.querySelector(".eval-chart-tooltip");
    if (!tooltip) return;
    container.querySelectorAll("[data-tooltip]").forEach((hit) => {
      hit.addEventListener("click", (e) => {
        e.stopPropagation();
        const rect = container.getBoundingClientRect();
        tooltip.textContent = hit.dataset.tooltip;
        tooltip.style.left = (e.clientX - rect.left) + "px";
        tooltip.style.top = (e.clientY - rect.top) + "px";
        tooltip.hidden = false;
      });
    });
    container.addEventListener("click", () => { tooltip.hidden = true; });
  }

  function renderInto(id, svgHtml) {
    const container = document.getElementById(id);
    if (!container) return;
    const wrap = container.querySelector(".eval-chart-wrap");
    if (!wrap) return;
    wrap.innerHTML = svgHtml + '<div class="eval-chart-tooltip" hidden></div>';
    wireTooltip(wrap);
  }

  function init() {
    const dataEl = document.getElementById("eval-analytics-data");
    if (!dataEl) return;
    let data;
    try {
      data = JSON.parse(dataEl.textContent);
    } catch (e) {
      return;
    }
    if (data.level_breakdown && data.level_breakdown.length) {
      renderInto("eval-chart-volume", buildLevelVolumeChart(data.level_breakdown));
    }
    if (data.latest_eval_by_level && data.latest_eval_by_level.length) {
      renderInto("eval-chart-accuracy", buildAccuracyChart(data.latest_eval_by_level));
    }
    renderInto("eval-chart-trend", buildTrendChart(data.eval_history || []));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
