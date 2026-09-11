// Corporate Actions tab — real data, from web/corporate_actions_feed.py's
// /companies/<id>/corporate-actions-feed.json.
//
// One fetch on load, then every filter-pill click just re-renders the
// already-fetched list client-side (same "filter what's already there,
// don't re-request per click" shape as charts_overlay.js's own metric
// toggles) — a company's corporate-actions history is small enough (tens
// of rows, not thousands) that there's no pagination/virtualization need.
(function () {
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function fmtDate(isoDate) {
    const [y, m, d] = isoDate.split("-").map(Number);
    return `${d} ${MONTHS[m - 1]} ${y}`;
  }

  // Order here is the pill display order (matches the mockup) and the
  // "All" filter's implicit default. Label text is what shows on the pill
  // and the Type badge — "other" reads as "Capital Structure" in this UI
  // rather than the internal action_type name (see ingestion/
  // corporate_actions.py's classify_action_type() docstring for why
  // "other" stays the correct catch-all name in code/DB even though this
  // tab labels it something more specific-sounding). tagClass values are
  // this app's own existing .tag-accent/.tag-neutral/.tag-outline
  // modifiers (styles.css) — no new color CSS: whichever theme is active
  // (dark/signals/green/schwab/...) already themes these correctly, so
  // this tab automatically matches the current theme rather than needing
  // its own hardcoded palette.
  const TYPES = [
    { key: "all", label: "All" },
    { key: "bonus", label: "Bonus", tagClass: "tag-accent" },
    { key: "split", label: "Split", tagClass: "tag-outline" },
    { key: "fv_split", label: "FV Split", tagClass: "tag-accent" },
    { key: "rights", label: "Rights", tagClass: "tag-outline" },
    { key: "dividend", label: "Dividend", tagClass: "tag-neutral" },
    { key: "scheme_of_arrangement", label: "Scheme of Arrangement", tagClass: "tag-neutral" },
    { key: "other", label: "Capital Structure", tagClass: "tag-neutral" },
  ];
  const TYPE_BY_KEY = {};
  TYPES.forEach((t) => { TYPE_BY_KEY[t.key] = t; });

  function init(root) {
    const dataUrl = root.dataset.url;

    const state = {
      loading: true,
      loadError: null,
      actions: [],
      years: 0,
      activeType: "all",
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
          state.actions = data.actions || [];
          state.years = data.years || 0;
          state.loading = false;
          render();
        })
        .catch(() => {
          state.loading = false;
          state.loadError = "Could not load corporate actions data.";
          render();
        });
    }

    function pillsHtml() {
      return TYPES.map((t) => {
        const active = state.activeType === t.key ? " active" : "";
        return `<a href="#"${active ? ' class="active"' : ""} data-action="filter" data-type="${t.key}">${escapeHtml(t.label)}</a>`;
      }).join("");
    }

    function rowsHtml(rows) {
      if (rows.length === 0) {
        return '<tr><td colspan="3" class="ca-empty">No corporate actions of this type on file.</td></tr>';
      }
      return rows
        .map((a) => {
          const type = TYPE_BY_KEY[a.action_type] || TYPE_BY_KEY.other;
          return (
            "<tr>" +
            `<td>${escapeHtml(fmtDate(a.ex_date))}</td>` +
            `<td><span class="tag ${type.tagClass}">${escapeHtml(type.label)}</span></td>` +
            `<td>${escapeHtml(a.subject.trim())}</td>` +
            "</tr>"
          );
        })
        .join("");
    }

    function render() {
      if (state.loading) {
        root.innerHTML = '<p class="ca-loading">Loading corporate actions…</p>';
        return;
      }
      if (state.loadError) {
        root.innerHTML = `<p class="ca-error">${escapeHtml(state.loadError)}</p>`;
        return;
      }
      if (state.actions.length === 0) {
        root.innerHTML = '<p class="ca-empty">No corporate actions on file for this company.</p>';
        return;
      }
      const filtered = state.activeType === "all"
        ? state.actions
        : state.actions.filter((a) => a.action_type === state.activeType);

      root.innerHTML =
        `<div class="ca-toolbar">` +
        `<span class="card-kicker">Corporate Actions · Last ${state.years} Years</span>` +
        `<div class="statement-toggle">${pillsHtml()}</div>` +
        `</div>` +
        '<table class="table ca-table">' +
        "<thead><tr><th>Date</th><th>Type</th><th>Detail</th></tr></thead>" +
        `<tbody>${rowsHtml(filtered)}</tbody>` +
        "</table>";
    }

    root.addEventListener("click", (e) => {
      const el = e.target.closest("[data-action]");
      if (!el) return;
      e.preventDefault();
      if (el.dataset.action === "filter") {
        state.activeType = el.dataset.type;
        render();
      }
    });

    load();
  }

  const root = document.getElementById("ca-root");
  if (root) init(root);
})();
