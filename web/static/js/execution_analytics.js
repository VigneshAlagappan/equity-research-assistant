/* Admin > Settings > Execution Analytics -- primary line chart (one series
   per Complexity Level, X=time/Y=avg execution time) with a scatter toggle
   (one point per run, X=timestamp/Y=duration, for spotting slow outliers).
   Hand-rolled SVG built from one embedded JSON blob (#execution-analytics-
   root's data-exa-data), same approach as charts_overlay.js/
   valuation_dashboard.js -- no charting library in this codebase.

   The period/granularity/level filters (top of the panel) are plain <select
   onchange="this.form.submit()"> controls that reload the page server-side
   (web/execution_analytics.py does the actual bucketing/percentiles) --
   only the line/scatter view toggle below is client-side, since both views
   render from the exact same already-filtered dataset. */
(function () {
  "use strict";

  const root = document.getElementById("execution-analytics-root");
  if (!root) return;

  let data;
  try {
    data = JSON.parse(root.dataset.exaData || "{}");
  } catch (e) {
    return;
  }
  const lineSeries = data.line_series || [];
  const scatter = data.scatter || [];

  const svg = document.getElementById("execution-analytics-svg");
  const legend = document.getElementById("execution-analytics-legend");
  const tooltip = document.getElementById("execution-analytics-tooltip");
  if (!svg || !legend) return;

  const SVG_NS = "http://www.w3.org/2000/svg";
  const WIDTH = 960, HEIGHT = 360;
  const MARGIN = { top: 16, right: 20, bottom: 28, left: 52 };
  const PLOT_W = WIDTH - MARGIN.left - MARGIN.right;
  const PLOT_H = HEIGHT - MARGIN.top - MARGIN.bottom;

  const LEVEL_LABELS = { 1: "Level 1", 2: "Level 2", 3: "Level 3", 4: "Level 4", 5: "Level 5" };

  function levelColor(level) {
    return getComputedStyle(document.documentElement).getPropertyValue("--exa-series-" + level).trim() || "#888";
  }
  function errorColor() {
    return getComputedStyle(document.documentElement).getPropertyValue("--exa-status-error").trim() || "#d03b3b";
  }

  function fmtSeconds(ms) {
    return (ms / 1000).toFixed(1) + "s";
  }
  function fmtDateShort(d) {
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function el(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    return node;
  }

  function showTooltip(evt, lines) {
    if (!tooltip) return;
    tooltip.textContent = lines.join("\n");
    tooltip.style.left = Math.min(evt.clientX + 14, window.innerWidth - 270) + "px";
    tooltip.style.top = Math.max(evt.clientY - 10, 8) + "px";
    tooltip.hidden = false;
  }
  function hideTooltip() {
    if (tooltip) tooltip.hidden = true;
  }

  function clearSvg() {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
  }

  function drawAxes(yMaxMs, xTicks, xScale) {
    svg.appendChild(el("line", {
      class: "exa-axis-line", x1: MARGIN.left, y1: MARGIN.top + PLOT_H, x2: MARGIN.left + PLOT_W, y2: MARGIN.top + PLOT_H,
    }));
    svg.appendChild(el("line", {
      class: "exa-axis-line", x1: MARGIN.left, y1: MARGIN.top, x2: MARGIN.left, y2: MARGIN.top + PLOT_H,
    }));
    const yTickCount = 4;
    for (let i = 0; i <= yTickCount; i++) {
      const value = (yMaxMs / yTickCount) * i;
      const y = MARGIN.top + PLOT_H - (value / yMaxMs) * PLOT_H;
      if (i > 0) {
        svg.appendChild(el("line", { class: "exa-gridline", x1: MARGIN.left, y1: y, x2: MARGIN.left + PLOT_W, y2: y }));
      }
      const label = el("text", { x: MARGIN.left - 8, y: y + 3, "text-anchor": "end" });
      label.textContent = fmtSeconds(value);
      svg.appendChild(label);
    }
    xTicks.forEach((tick) => {
      const x = xScale(tick.t);
      const label = el("text", { x: x, y: MARGIN.top + PLOT_H + 18, "text-anchor": "middle" });
      label.textContent = tick.label;
      svg.appendChild(label);
    });
  }

  function buildXScale(minT, maxT) {
    const span = Math.max(1, maxT - minT);
    return function (t) {
      return MARGIN.left + ((t - minT) / span) * PLOT_W;
    };
  }

  function niceTicks(minT, maxT, count) {
    const ticks = [];
    for (let i = 0; i <= count; i++) {
      const t = minT + ((maxT - minT) * i) / count;
      ticks.push({ t: t, label: fmtDateShort(new Date(t)) });
    }
    return ticks;
  }

  function renderLegend(levelsPresent, showError) {
    legend.innerHTML = "";
    levelsPresent.forEach((lvl) => {
      const item = document.createElement("span");
      item.className = "exa-legend-item";
      const swatch = document.createElement("span");
      swatch.className = "exa-legend-swatch";
      swatch.style.background = levelColor(lvl);
      item.appendChild(swatch);
      item.appendChild(document.createTextNode(LEVEL_LABELS[lvl] || ("Level " + lvl)));
      legend.appendChild(item);
    });
    if (showError) {
      const item = document.createElement("span");
      item.className = "exa-legend-item";
      const swatch = document.createElement("span");
      swatch.className = "exa-legend-swatch";
      swatch.style.background = errorColor();
      item.appendChild(swatch);
      item.appendChild(document.createTextNode("Error"));
      legend.appendChild(item);
    }
  }

  function renderLine() {
    clearSvg();
    if (!lineSeries.length) {
      renderLegend([], false);
      return;
    }
    let minT = Infinity, maxT = -Infinity, maxMs = 0;
    lineSeries.forEach((series) => {
      series.points.forEach((p) => {
        const t = new Date(p.bucket + "T00:00:00Z").getTime();
        minT = Math.min(minT, t);
        maxT = Math.max(maxT, t);
        maxMs = Math.max(maxMs, p.avg_ms);
      });
    });
    if (!isFinite(minT)) return;
    maxMs = maxMs * 1.15 || 1000;
    const xScale = buildXScale(minT, maxT);
    const yScale = (ms) => MARGIN.top + PLOT_H - (ms / maxMs) * PLOT_H;

    drawAxes(maxMs, niceTicks(minT, maxT, Math.min(6, lineSeries[0].points.length - 1 || 1)), xScale);

    lineSeries.forEach((series) => {
      const color = levelColor(series.level);
      const points = series.points.map((p) => ({
        x: xScale(new Date(p.bucket + "T00:00:00Z").getTime()),
        y: yScale(p.avg_ms),
        p: p,
      }));
      const path = points.map((pt, i) => (i === 0 ? "M" : "L") + pt.x.toFixed(1) + "," + pt.y.toFixed(1)).join(" ");
      svg.appendChild(el("path", { class: "exa-line", d: path, stroke: color }));
      points.forEach((pt) => {
        const dot = el("circle", { class: "exa-dot", cx: pt.x, cy: pt.y, r: 3.5, fill: color });
        dot.addEventListener("mouseenter", (e) => showTooltip(e, [
          LEVEL_LABELS[series.level] || ("Level " + series.level),
          pt.p.bucket,
          "Avg: " + fmtSeconds(pt.p.avg_ms) + " (" + pt.p.count + " run" + (pt.p.count === 1 ? "" : "s") + ")",
        ]));
        dot.addEventListener("mousemove", (e) => showTooltip(e, [
          LEVEL_LABELS[series.level] || ("Level " + series.level),
          pt.p.bucket,
          "Avg: " + fmtSeconds(pt.p.avg_ms) + " (" + pt.p.count + " run" + (pt.p.count === 1 ? "" : "s") + ")",
        ]));
        dot.addEventListener("mouseleave", hideTooltip);
        svg.appendChild(dot);
      });
    });
    renderLegend(lineSeries.map((s) => s.level), false);
  }

  function renderScatter() {
    clearSvg();
    if (!scatter.length) {
      renderLegend([], false);
      return;
    }
    let minT = Infinity, maxT = -Infinity, maxMs = 0;
    const parsed = scatter.map((row) => {
      const t = new Date(row.created_at).getTime();
      minT = Math.min(minT, t);
      maxT = Math.max(maxT, t);
      maxMs = Math.max(maxMs, row.duration_ms);
      return { t: t, row: row };
    });
    maxMs = maxMs * 1.1 || 1000;
    const xScale = buildXScale(minT, maxT);
    const yScale = (ms) => MARGIN.top + PLOT_H - (ms / maxMs) * PLOT_H;

    drawAxes(maxMs, niceTicks(minT, maxT, 6), xScale);

    const levelsPresent = new Set();
    let hasError = false;
    parsed.forEach(({ t, row }) => {
      const isError = row.status === "error";
      if (isError) hasError = true;
      if (row.level) levelsPresent.add(row.level);
      const color = isError ? errorColor() : (row.level ? levelColor(row.level) : "#888");
      const dot = el("circle", {
        class: "exa-dot", cx: xScale(t), cy: yScale(row.duration_ms), r: 4, fill: color, "fill-opacity": isError ? 0.9 : 0.65,
      });
      const lines = [
        row.task + (row.level ? " — Level " + row.level : ""),
        "Duration: " + fmtSeconds(row.duration_ms),
        "Model: " + (row.model || "—"),
        "Status: " + row.status + " (" + row.mode + ")",
        "Run: " + row.run_id,
      ];
      dot.addEventListener("mouseenter", (e) => showTooltip(e, lines));
      dot.addEventListener("mousemove", (e) => showTooltip(e, lines));
      dot.addEventListener("mouseleave", hideTooltip);
      svg.appendChild(dot);
    });
    renderLegend(Array.from(levelsPresent).sort(), hasError);
  }

  const viewButtons = document.querySelectorAll(".exa-view-btn");
  function setView(view) {
    viewButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.exaView === view));
    if (view === "scatter") renderScatter();
    else renderLine();
  }
  viewButtons.forEach((btn) => {
    btn.addEventListener("click", () => setView(btn.dataset.exaView));
  });

  setView("line");
})();
