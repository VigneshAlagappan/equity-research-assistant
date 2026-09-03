// Shareholding Pattern tab ("Major Holders") — real data, from
// web/shareholding_feed.py's /companies/<id>/shareholding-feed.json.
//
// Ported from a Claude Design prototype (claude.ai/design, "Signals.dc.html"),
// then reshaped into a Screener-style wide table (rows = Promoters/FII/DII/
// Public and their individually-named holders, columns = every quarter on
// file, oldest to newest) at the user's request, matching the same
// .vm-table-scroll/.table conventions the Financials/Valuation Model tabs
// already use (sticky Parameter + Trend columns — see the CSS comment on
// .vm-table-scroll in company.html for why sticky-top isn't also used).
// Each category row expands to reveal its named holders as sub-rows across
// the same quarter columns, native <tr hidden> toggling rather than
// per-holder <details> (there can be two dozen holder rows under one
// category; a table row is compact enough not to need capping/"view all").
//
// Sub-tab row (Major Holders / Insider Roster / Insider Transactions /
// Insider Sentiment) carried over unchanged: only Major Holders has real
// content, this app has no insider-trading data source, so Insider Roster/
// Transactions show a plain "not tracked" state and Insider Sentiment stays
// locked, matching the prototype's own gating rather than fabricating a
// demo view for it.
(function () {
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function fmtPct(v) {
    return v === null || v === undefined ? "—" : v.toFixed(2) + "%";
  }

  function fmtDelta(delta) {
    if (delta === null || delta === undefined) return "—";
    if (Math.abs(delta) < 0.005) return '<span class="shp-delta is-flat">flat</span>';
    const up = delta > 0;
    return `<span class="shp-delta ${up ? "is-up" : "is-down"}">${up ? "+" : ""}${delta.toFixed(2)}pp</span>`;
  }

  // A simple min/max-normalized polyline over whatever points a series
  // actually has (nulls -- a holder not yet named, or a quarter whose SHP
  // XBRL never parsed a category breakdown -- just leave a gap).
  function sparklinePath(values) {
    const w = 88, h = 20, pad = 2;
    const present = values.filter((v) => v !== null && v !== undefined);
    if (present.length < 2) return "";
    const min = Math.min(...present), max = Math.max(...present);
    const span = max - min || 1;
    const step = (w - pad * 2) / (values.length - 1);
    let d = "";
    let started = false;
    values.forEach((v, i) => {
      if (v === null || v === undefined) return;
      const x = pad + i * step;
      const y = h - pad - ((v - min) / span) * (h - pad * 2);
      d += (started ? "L" : "M") + x.toFixed(1) + "," + y.toFixed(1) + " ";
      started = true;
    });
    return d.trim();
  }

  // Stroke color comes from CSS (.shp-row-category .vm-spark path), which
  // reads the row's own --bucket-color custom property -- no inline color
  // here so it stays in sync with a single source of truth per bucket.
  function sparkSvg(values) {
    const path = sparklinePath(values);
    return path ? `<svg class="vm-spark" viewBox="0 0 88 20" preserveAspectRatio="none"><path d="${path}"></path></svg>` : "";
  }

  function lastNonNull(arr) {
    for (let i = arr.length - 1; i >= 0; i--) if (arr[i] !== null && arr[i] !== undefined) return arr[i];
    return null;
  }
  function firstNonNull(arr) {
    for (let i = 0; i < arr.length; i++) if (arr[i] !== null && arr[i] !== undefined) return arr[i];
    return null;
  }
  function totalDelta(values) {
    const present = values.filter((v) => v !== null && v !== undefined);
    if (present.length < 2) return null;
    return firstNonNull(values) === null || lastNonNull(values) === null ? null : lastNonNull(values) - firstNonNull(values);
  }

  const BUCKET_ORDER = [
    { key: "promoter", label: "Promoters", nounSingular: "promoter" },
    { key: "fii", label: "FII", nounSingular: "FII" },
    { key: "dii", label: "DII", nounSingular: "DII" },
    { key: "public", label: "Public", nounSingular: "public" },
  ];

  // Every holder ever individually named under this bucket across the
  // company's whole filing history, one row per name, with a value (or
  // null) for every quarter column -- reshapes the feed's per-quarter
  // holder lists (JSON already has everything needed; no extra fetch) into
  // the holder x quarter matrix the table renders.
  function buildHolderRows(quartersAsc, bucketKey) {
    const byName = new Map();
    quartersAsc.forEach((q, i) => {
      const bucket = q.buckets.find((b) => b.key === bucketKey);
      bucket.holders.forEach((h) => {
        if (!byName.has(h.name)) {
          byName.set(h.name, { name: h.name, category: h.category, values: new Array(quartersAsc.length).fill(null) });
        }
        byName.get(h.name).values[i] = h.percent;
      });
    });
    const rows = Array.from(byName.values());
    rows.sort((a, b) => {
      const av = lastNonNull(a.values), bv = lastNonNull(b.values);
      if (av === null && bv === null) return a.name.localeCompare(b.name);
      if (av === null) return 1;
      if (bv === null) return -1;
      return bv - av;
    });
    return rows;
  }

  function rowCellsHtml(values) {
    return values.map((v) => `<td class="vm-num">${fmtPct(v)}</td>`).join("");
  }

  function categoryRowHtml(meta, values, holderCount, expanded) {
    const chevron = holderCount > 0 ? `<span class="shp-chevron${expanded ? " is-open" : ""}">▸</span>` : `<span class="shp-chevron shp-chevron-empty"></span>`;
    const toggle = holderCount > 0
      ? `<button type="button" class="shp-row-toggle" data-action="toggle-bucket" data-bucket="${meta.key}">`
      : `<span class="shp-row-toggle shp-row-toggle-static">`;
    const closeToggle = holderCount > 0 ? "</button>" : "</span>";
    const sub = holderCount > 0 ? ` <span class="shp-row-sub">(${holderCount} named)</span>` : "";
    return `<tr class="shp-row shp-row-category shp-bucket-${meta.key}">
      <td>${toggle}${chevron}<span class="shp-swatch"></span>${escapeHtml(meta.label)}${closeToggle}${sub}</td>
      <td class="vm-trend-cell">${sparkSvg(values)}</td>
      ${rowCellsHtml(values)}
      <td class="vm-num shp-delta-col">${fmtDelta(totalDelta(values))}</td>
    </tr>`;
  }

  function holderRowHtml(bucketKey, holder, visible) {
    return `<tr class="shp-row shp-row-holder" data-parent="${bucketKey}" ${visible ? "" : "hidden"}>
      <td class="shp-row-label-sub" title="${escapeHtml(holder.category)}">${escapeHtml(holder.name)}</td>
      <td class="vm-trend-cell"></td>
      ${rowCellsHtml(holder.values)}
      <td class="vm-num shp-delta-col">${fmtDelta(totalDelta(holder.values))}</td>
    </tr>`;
  }

  const SUBTABS = [
    { key: "major", label: "Major Holders" },
    { key: "roster", label: "Insider Roster" },
    { key: "transactions", label: "Insider Transactions" },
    { key: "sentiment", label: "Insider Sentiment", locked: true },
  ];

  function init(root) {
    const companyId = root.dataset.companyId;
    const dataUrl = root.dataset.url;

    const state = {
      loading: true,
      loadError: null,
      data: null, // { quarters: [...] }, newest first from the feed
      activeSubtab: "major",
      periodType: "quarterly", // "quarterly" | "annual" (Q4 of each fiscal year)
      expandedBuckets: null, // bucketKey -> bool, seeded once data loads
    };

    function load() {
      state.loading = true;
      state.loadError = null;
      render();
      fetch(dataUrl)
        .then((r) => {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then((data) => {
          state.data = data;
          state.loading = false;
          // Every category starts collapsed, always -- no auto-opening the
          // largest buckets. Real-estate-efficient default: reveal a
          // holder list only on an explicit click.
          state.expandedBuckets = {};
          BUCKET_ORDER.forEach((b) => { state.expandedBuckets[b.key] = false; });
          render();
        })
        .catch(() => {
          state.loading = false;
          state.loadError = "Could not load shareholding data.";
          render();
        });
    }

    function subtabsHtml() {
      return SUBTABS.map((t) => {
        const active = t.key === state.activeSubtab ? " is-active" : "";
        const lockIcon = t.locked ? '<span class="shp-lock" title="Not available">\u{1F512}</span>' : "";
        const disabled = t.locked ? "disabled" : "";
        return `<button type="button" class="shp-subtab${active}" ${disabled} data-action="subtab" data-key="${t.key}">${escapeHtml(t.label)}${lockIcon}</button>`;
      }).join("");
    }

    function majorHoldersHtml() {
      const allQuartersAsc = state.data.quarters.slice().reverse(); // oldest -> newest, left to right
      const isAnnual = state.periodType === "annual";
      // Annual = Q4 of each fiscal year, the fiscal-year-end shareholding
      // snapshot -- same "Q4 stands in for annual" convention this app's
      // own Financials tab already uses for balance-sheet-style figures
      // (NIFTY500_USA_XBRL_BATCHES.md: "quarterly + Q4-derived annual").
      // SEBI's SHP filing is itself always quarterly -- there's no separate
      // annual submission to fetch -- so this is a client-side view over
      // the same data, not a different feed.
      const quartersAsc = isAnnual ? allQuartersAsc.filter((q) => q.quarter === "Q4") : allQuartersAsc;

      const latest = (isAnnual ? quartersAsc[quartersAsc.length - 1] : null) || state.data.quarters[0];
      const asOfBits = [`Data through ${escapeHtml(latest.label)}`];
      if (latest.submission_date) asOfBits.push(`filed ${escapeHtml(latest.submission_date)}`);
      if (latest.num_shareholders) asOfBits.push(`${latest.num_shareholders.toLocaleString()} total shareholders`);
      const sourceLink = latest.source_url
        ? ` &middot; <a href="${escapeHtml(latest.source_url)}" target="_blank" rel="noopener noreferrer">Latest source filing (XBRL)&nbsp;↗</a>`
        : "";

      const periodToggle = `
        <div class="vm-period-toggle" data-vm-period-toggle style="margin-bottom: var(--space-3)">
          <button type="button" class="vm-period-btn${isAnnual ? "" : " is-active"}" data-action="period-type" data-type="quarterly">Quarterly</button>
          <button type="button" class="vm-period-btn${isAnnual ? " is-active" : ""}" data-action="period-type" data-type="annual" title="Q4 of each fiscal year">Annual</button>
        </div>`;

      if (!quartersAsc.length) {
        return `
          <p class="muted">${asOfBits.join(" · ")}${sourceLink}</p>
          ${periodToggle}
          <div class="empty-state">No fiscal-year-end (Q4) shareholding filing on file yet for this company.</div>
        `;
      }

      const headerCells = quartersAsc
        .map((q) => `<th class="vm-num">${escapeHtml(isAnnual ? q.fiscal_year : q.label)}</th>`)
        .join("");

      const rows = BUCKET_ORDER.map((meta) => {
        const values = quartersAsc.map((q) => q.buckets.find((b) => b.key === meta.key).percent);
        const holderRows = buildHolderRows(quartersAsc, meta.key);
        const expanded = !!(state.expandedBuckets && state.expandedBuckets[meta.key]);
        return (
          categoryRowHtml(meta, values, holderRows.length, expanded) +
          holderRows.map((h) => holderRowHtml(meta.key, h, expanded)).join("")
        );
      }).join("");

      const footnote = isAnnual
        ? `Annual view shows each fiscal year's Q4 (year-end) shareholding filing — SEBI's Shareholding Pattern filing is itself always quarterly, there is no separate annual submission. Click a category to reveal its individually named holders; "Change" is the percentage-point move from the first to the last year shown.`
        : `Named holders sourced from NSE's Shareholding Pattern (SEBI LODR Regulation 31) XBRL filings. Click a category to reveal its individually named holders; "Change" is the percentage-point move from its first to its last quarter on file.`;

      return `
        <p class="muted">${asOfBits.join(" · ")}${sourceLink}</p>
        ${periodToggle}
        <div class="vm-table-scroll shp-table-scroll">
          <table class="table">
            <thead><tr><th>Category / Holder</th><th class="vm-trend-header">Trend</th>${headerCells}<th class="vm-num shp-delta-col">Change</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        <p class="muted shp-footnote">${footnote}</p>
      `;
    }

    function notTrackedHtml(label) {
      return `<div class="empty-state">${escapeHtml(label)} isn't tracked here — NSE's Shareholding Pattern filing (SEBI LODR Regulation 31) doesn't disclose insider-trading activity, only the shareholding register.</div>`;
    }

    function render() {
      if (state.loading) {
        root.innerHTML = '<p class="shp-loading">Loading shareholding data…</p>';
        return;
      }
      if (state.loadError) {
        root.innerHTML = `<p class="shp-error">${escapeHtml(state.loadError)}</p>`;
        return;
      }
      if (!state.data || !state.data.quarters.length) {
        root.innerHTML = `<div class="empty-state">No shareholding pattern data on file yet for this company &mdash; NSE's Regulation 31
          filings, fetched via <code>python -m scripts.fetch_nse_shareholding ${escapeHtml(companyId)}</code>.</div>`;
        return;
      }

      let body;
      if (state.activeSubtab === "major") body = majorHoldersHtml();
      else if (state.activeSubtab === "roster") body = notTrackedHtml("Insider roster");
      else body = notTrackedHtml("Insider transactions");

      root.innerHTML = `<div class="shp-subtabs">${subtabsHtml()}</div>${body}`;
    }

    root.addEventListener("click", (e) => {
      const el = e.target.closest("[data-action]");
      if (!el) return;
      const action = el.dataset.action;
      if (action === "subtab") {
        state.activeSubtab = el.dataset.key;
        render();
      } else if (action === "period-type") {
        state.periodType = el.dataset.type;
        render();
      } else if (action === "toggle-bucket") {
        const key = el.dataset.bucket;
        state.expandedBuckets[key] = !state.expandedBuckets[key];
        render();
      }
    });

    load();
  }

  const root = document.getElementById("shp-root");
  if (root) init(root);
})();
