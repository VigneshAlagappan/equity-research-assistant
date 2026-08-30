// Docs tab — fiscal-year-grouped document archive, backed by real data:
// web/docs_feed.py's /companies/<id>/docs-feed.json (years from
// canonical_financials — quarterly and/or annual-only — documents from the
// `documents` table) and /companies/<id>/docs/add (upload or link a
// document into a gap).
//
// Ported from a Claude Design prototype (claude.ai/design, "Signals Docs
// Pills.dc.html") — collapsible year groups, an inline Annual Report pill
// per year, every document slot a pill in one of three states (published /
// added by you / a gap you can fill). The prototype fabricates prose body
// text for its doc-preview modal; this one never does — the modal only ever
// shows real recorded metadata (who added it, a real link to open it),
// same rule research/assistant.py's evidence-only answers follow.
(function () {
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // retrieved_at is UTC (storage/database.py's utcnow_iso) — show the
  // viewer's own local calendar date, not the server's.
  function localDate(isoUtc) {
    const d = new Date(isoUtc);
    if (Number.isNaN(d.getTime())) return isoUtc.slice(0, 10);
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  function init(root, modalHost, addModalHost) {
    const companyId = root.dataset.companyId;
    const dataUrl = root.dataset.url;
    const addUrl = `/companies/${encodeURIComponent(companyId)}/docs/add`;

    const state = {
      loading: true,
      loadError: null,
      data: null, // { types, years: [{ fy, label, period_id, quarters, annual, ... }] } from the feed
      openYears: {},
      doc: null, // { typeLabel, periodLabel, added_by_user, file_url, source_url, retrieved_at }
      add: null, // { periodId, typeKey, scope: 'quarter' | 'annual' }
      addSource: "upload",
      addRef: "",
      addFile: null, // File, when addSource === "upload"
      submitting: false,
      addError: null,
    };

    function loadFeed() {
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
          // Default: every fiscal year collapsed — "Expand all" opens them,
          // a single year header click opens just that one.
          state.openYears = {};
          render();
        })
        .catch(() => {
          state.loading = false;
          state.loadError = "Could not load the document archive.";
          render();
        });
    }

    function findQuarter(qid) {
      for (const y of state.data.years) {
        const q = y.quarters.find((r) => r.id === qid);
        if (q) return q;
      }
      return null;
    }

    function openDoc(typeLabel, periodLabel, docInfo) {
      state.doc = Object.assign({ typeLabel, periodLabel }, docInfo);
    }

    function openAdd(periodId, typeKey) {
      // scope isn't stored — it's just "is the selected type Annual Report
      // or not", derived wherever needed from typeKey so it can never drift
      // out of sync with the Document Type dropdown (see typeOptions()
      // below, which always lists every type, annual included).
      state.add = { periodId, typeKey };
      state.addSource = "upload";
      state.addRef = "";
      state.addFile = null;
      state.addError = null;
    }

    function submitAdd() {
      const { periodId, typeKey } = state.add;
      state.addError = null;

      if (state.addSource === "upload") {
        if (!state.addFile) {
          state.addError = "Choose a file to upload.";
          render();
          return;
        }
        const body = new FormData();
        body.append("period", periodId);
        body.append("type", typeKey);
        body.append("source", "upload");
        body.append("file", state.addFile);
        submitAddRequest(body);
      } else {
        const ref = state.addRef.trim();
        if (!ref) {
          state.addError = "Enter a URL.";
          render();
          return;
        }
        submitAddRequest(
          JSON.stringify({ period: periodId, type: typeKey, source: "link", ref }),
          { "Content-Type": "application/json" }
        );
      }
    }

    function submitAddRequest(body, headers) {
      state.submitting = true;
      render();
      fetch(addUrl, { method: "POST", body, headers })
        .then(async (r) => {
          const data = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(data.error || "Something went wrong.");
          state.add = null;
          state.submitting = false;
          loadFeed(); // simplest source of truth: re-fetch rather than hand-merge
        })
        .catch((err) => {
          state.submitting = false;
          state.addError = err.message;
          render();
        });
    }

    function docPillHtml(cls, label, title, action, extra) {
      return `<button type="button" class="${cls}" title="${escapeHtml(title)}" ${extra}>${escapeHtml(label)}</button>`;
    }

    function renderTypePill(qtr, type) {
      const docInfo = qtr.docs[type.key];
      if (docInfo) {
        const mine = !!docInfo.added_by_user;
        return docPillHtml(
          `docs-doc-pill ${mine ? "is-mine" : "is-published"}`,
          type.label,
          mine ? "Added by " + docInfo.added_by_user : "Open " + type.label,
          "open-doc",
          `data-action="open-doc" data-scope="quarter" data-qid="${qtr.id}" data-type="${type.key}"`
        );
      }
      return docPillHtml(
        "docs-doc-pill is-missing",
        type.label,
        "Not published · click to add your copy",
        "open-add",
        `data-action="open-add" data-scope="quarter" data-qid="${qtr.id}" data-type="${type.key}"`
      );
    }

    function renderQuarterRow(qtr, types) {
      const pills = types.map((t) => renderTypePill(qtr, t)).join("");
      return `<div class="docs-quarter-row">
        <div class="docs-quarter-label">
          <div class="docs-quarter-name">${escapeHtml(qtr.label)}</div>
          <div class="docs-quarter-sub">${escapeHtml(qtr.sub)}</div>
        </div>
        <div class="docs-quarter-pills">${pills}</div>
      </div>`;
    }

    function renderAnnualPill(year) {
      if (year.annual) {
        const mine = !!year.annual.added_by_user;
        return docPillHtml(
          `docs-doc-pill docs-annual-pill ${mine ? "is-mine" : "is-published"}`,
          "Annual Report",
          mine ? "Added by " + year.annual.added_by_user : "Open annual report",
          "open-doc",
          `data-action="open-doc" data-scope="annual" data-fy="${year.fy}"`
        );
      }
      return docPillHtml(
        "docs-doc-pill docs-annual-pill is-missing",
        "Annual Report",
        "Not published · click to add your copy",
        "open-add",
        `data-action="open-add" data-scope="annual" data-qid="year:${year.fy}" data-type="annual"`
      );
    }

    function renderYearGroup(year, types) {
      const open = !!state.openYears[year.fy];
      const summary = year.quarter_count === 0
        ? "No quarterly data on file"
        : `${year.quarter_count} ${year.quarter_count === 1 ? "quarter" : "quarters"} · ${year.published_count} documents${year.gap_count ? " · " + year.gap_count + " missing" : " · complete"}`;

      const body = !open ? "" : year.quarter_count === 0
        ? `<div class="docs-no-quarters">No quarterly-granularity financials on file for this fiscal year yet.</div>`
        : `<div class="docs-quarters">${year.quarters.map((q) => renderQuarterRow(q, types)).join("")}</div>`;

      return `<div class="docs-year">
        <div class="docs-year-header" data-action="toggle-year" data-fy="${year.fy}">
          <span class="docs-chevron${open ? " docs-chevron-open" : ""}">▾</span>
          <span class="docs-year-label">${escapeHtml(year.label)}</span>
          ${renderAnnualPill(year)}
          <span class="docs-year-summary">${escapeHtml(summary)}</span>
        </div>
        ${body}
      </div>`;
    }

    function periodOptions() {
      // Independent of state.data.years (the archive view's own display
      // window) — web/docs_feed.py generates these across a much wider
      // range (2005 onward) so a document can be attached to a fiscal year
      // the archive isn't currently showing. Which list applies follows the
      // currently-selected Document Type, not a scope fixed when the modal
      // was opened — switching type between Annual Report and a quarterly
      // type re-scopes the period list to match (see the "type" change
      // handler below).
      return state.add && state.add.typeKey === "annual"
        ? state.data.annual_period_options
        : state.data.quarter_period_options;
    }

    function typeOptions() {
      // Always every type, Annual Report included — the user picks
      // whichever document they're actually adding rather than being
      // locked to whatever pill they happened to click to open the modal.
      return state.data.types.map((t) => ({ value: t.key, label: t.label })).concat([{ value: "annual", label: "Annual Report" }]);
    }

    function renderAddModal() {
      if (!state.add) {
        addModalHost.innerHTML = "";
        return;
      }
      const pOpts = periodOptions();
      const tOpts = typeOptions();
      const periodLabel = (pOpts.find((o) => o.value === state.add.periodId) || {}).label || state.add.periodId;
      const typeLabel = (tOpts.find((o) => o.value === state.add.typeKey) || {}).label || state.add.typeKey;
      const isUpload = state.addSource === "upload";

      addModalHost.innerHTML = `
        <div class="docs-modal-backdrop">
          <div class="docs-modal docs-addmodal">
            <div class="docs-modal-header">
              <div>
                <div class="docs-modal-kicker">Add a document</div>
                <div class="docs-modal-title">${escapeHtml(typeLabel)} · ${escapeHtml(periodLabel)}</div>
                <p class="docs-addmodal-desc">Fill a gap in the archive with your own copy. It'll be marked as added by you, not officially sourced.</p>
              </div>
            </div>
            <div class="docs-modal-body docs-addmodal-body">
              <div class="docs-field-row">
                <label class="docs-field">
                  <span class="docs-field-label">Period</span>
                  <select class="input docs-select" data-field="period">
                    ${pOpts.map((o) => `<option value="${o.value}"${o.value === state.add.periodId ? " selected" : ""}>${escapeHtml(o.label)}</option>`).join("")}
                  </select>
                </label>
                <label class="docs-field">
                  <span class="docs-field-label">Document type</span>
                  <select class="input docs-select" data-field="type">
                    ${tOpts.map((o) => `<option value="${o.value}"${o.value === state.add.typeKey ? " selected" : ""}>${escapeHtml(o.label)}</option>`).join("")}
                  </select>
                </label>
              </div>

              <div class="docs-field">
                <span class="docs-field-label">Source</span>
                <div class="docs-source-row">
                  <button type="button" class="docs-pill${isUpload ? " docs-pill-active" : ""}" data-action="add-source" data-source="upload">Upload a file</button>
                  <button type="button" class="docs-pill${!isUpload ? " docs-pill-active" : ""}" data-action="add-source" data-source="link">Link a URL</button>
                </div>
              </div>

              ${isUpload
                ? `<label class="docs-field">
                     <span class="docs-field-label">File</span>
                     <input type="file" class="input docs-select" data-field="file">
                     ${state.addFile ? `<span class="docs-file-chosen">${escapeHtml(state.addFile.name)}</span>` : ""}
                   </label>`
                : `<label class="docs-field">
                     <span class="docs-field-label">Document URL</span>
                     <input class="input docs-select" data-field="ref" value="${escapeHtml(state.addRef)}" placeholder="https://…">
                   </label>`
              }
              ${state.addError ? `<p class="docs-addmodal-error">${escapeHtml(state.addError)}</p>` : ""}
            </div>
            <div class="docs-addmodal-footer">
              <button type="button" class="docs-pill" data-action="close-add" ${state.submitting ? "disabled" : ""}>Cancel</button>
              <button type="button" class="docs-pill" style="border-color:var(--color-accent);color:var(--color-accent-700)" data-action="submit-add" ${state.submitting ? "disabled" : ""}>${state.submitting ? "Adding…" : "Add to docs"}</button>
            </div>
          </div>
        </div>`;
    }

    function renderDocModal() {
      if (!state.doc) {
        modalHost.innerHTML = "";
        return;
      }
      const d = state.doc;
      const attribution = d.added_by_user ? "Added by " + escapeHtml(d.added_by_user) : "Officially sourced";
      const openLink = d.file_url
        ? `<a href="${escapeHtml(d.file_url)}" target="_blank" rel="noopener noreferrer">Open file</a>`
        : d.source_url
        ? `<a href="${escapeHtml(d.source_url)}" target="_blank" rel="noopener noreferrer">Open link</a>`
        : "";
      modalHost.innerHTML = `
        <div class="docs-modal-backdrop">
          <div class="docs-modal">
            <div class="docs-modal-header">
              <div>
                <div class="docs-modal-kicker">${escapeHtml(d.typeLabel)} · ${escapeHtml(d.periodLabel)}</div>
                <div class="docs-modal-title">${attribution}</div>
                ${d.retrieved_at ? `<div class="docs-modal-meta">Added ${escapeHtml(localDate(d.retrieved_at))}</div>` : ""}
              </div>
              <button type="button" class="docs-modal-close" data-action="close-doc">Close</button>
            </div>
            <div class="docs-modal-body">
              ${openLink ? `<p>${openLink}</p>` : `<p class="muted">No file or link on record for this document.</p>`}
            </div>
          </div>
        </div>`;
    }

    function firstAvailableGap() {
      for (const y of state.data.years) {
        if (!y.annual) return { qid: y.period_id, type: "annual" };
        for (const q of y.quarters) {
          for (const t of state.data.types) {
            if (!q.docs[t.key]) return { qid: q.id, type: t.key };
          }
        }
      }
      // No years currently shown (nothing has content yet) — land on the
      // most recent year from the independent, always-populated period
      // range rather than reading state.data.years[0], which would be
      // undefined here.
      return { qid: state.data.annual_period_options[0].value, type: "annual" };
    }

    function render() {
      if (state.loading) {
        root.innerHTML = `<p class="muted">Loading documents&hellip;</p>`;
        return;
      }
      if (state.loadError) {
        root.innerHTML = `<div class="empty-state">${escapeHtml(state.loadError)}</div>`;
        return;
      }
      if (state.data.years.length === 0) {
        root.innerHTML = `
          <h3 class="docs-card-title">Documents</h3>
          <div class="empty-state">No documents on file for this company yet.</div>
          <button type="button" class="docs-add-missing-link" style="margin-top:var(--space-3)" data-action="open-add-blank">+ Add Missing</button>`;
        renderDocModal();
        renderAddModal();
        return;
      }

      const types = state.data.types;
      const allOpen = state.data.years.every((y) => state.openYears[y.fy]);
      const addedCount = state.data.years.reduce(
        (n, y) =>
          n +
          (y.annual && y.annual.added_by_user ? 1 : 0) +
          y.quarters.reduce((m, q) => m + types.filter((t) => q.docs[t.key] && q.docs[t.key].added_by_user).length, 0),
        0
      );

      const ledeText = state.data.synthetic
        ? "No financials ingested for this company yet, so only years you've actually added a document to are shown below — use + Add Missing to start on a year that isn't listed."
        : "Each fiscal year carries its annual report; open a year for the quarterly result and concall material. Faded pills were never published — click one to add your own copy.";

      root.innerHTML = `
        <h2 class="docs-card-title" style="margin-bottom:6px">Documents</h2>
        <p class="muted docs-lede">${escapeHtml(ledeText)}</p>

        <div class="docs-card">
          <div class="docs-card-header">
            <h3 class="docs-card-title">Filings by period</h3>
            <div class="docs-card-actions">
              ${addedCount > 0 ? `<span class="docs-added-note">${addedCount} ${addedCount === 1 ? "document" : "documents"} added by you</span>` : ""}
              <button type="button" class="docs-pill" data-action="toggle-all-years">${allOpen ? "Collapse all" : "Expand all"}</button>
              <button type="button" class="docs-add-missing-link" data-action="open-add-blank">+ Add Missing</button>
            </div>
          </div>
          <div class="docs-years-scroll">
            ${state.data.years.map((y) => renderYearGroup(y, types)).join("")}
          </div>
        </div>

        <p class="muted docs-footnote">Regulatory announcements are not shown here.</p>
      `;

      renderDocModal();
      renderAddModal();
    }

    root.addEventListener("click", (e) => {
      const el = e.target.closest("[data-action]");
      if (!el) return;
      const action = el.dataset.action;
      if (action === "toggle-year") {
        state.openYears[el.dataset.fy] = !state.openYears[el.dataset.fy];
      } else if (action === "toggle-all-years") {
        const allOpen = state.data.years.every((y) => state.openYears[y.fy]);
        state.openYears = {};
        if (!allOpen) state.data.years.forEach((y) => { state.openYears[y.fy] = true; });
      } else if (action === "open-doc") {
        e.preventDefault();
        if (el.dataset.scope === "annual") {
          const year = state.data.years.find((y) => y.fy === el.dataset.fy);
          openDoc("Annual Report", year.label, year.annual);
        } else {
          const qtr = findQuarter(el.dataset.qid);
          const type = state.data.types.find((t) => t.key === el.dataset.type);
          openDoc(type.label, qtr.label, qtr.docs[type.key]);
        }
      } else if (action === "open-add") {
        e.preventDefault();
        openAdd(el.dataset.qid, el.dataset.type);
      } else if (action === "open-add-blank") {
        const gap = firstAvailableGap();
        openAdd(gap.qid, gap.type);
      }
      render();
    });

    modalHost.addEventListener("click", (e) => {
      if (e.target.classList.contains("docs-modal-backdrop")) {
        state.doc = null;
        render();
        return;
      }
      const el = e.target.closest("[data-action]");
      if (el && el.dataset.action === "close-doc") {
        state.doc = null;
        render();
      }
    });

    addModalHost.addEventListener("click", (e) => {
      if (e.target.classList.contains("docs-modal-backdrop")) {
        state.add = null;
        render();
        return;
      }
      const el = e.target.closest("[data-action]");
      if (!el) return;
      if (el.dataset.action === "close-add") {
        state.add = null;
        render();
      } else if (el.dataset.action === "submit-add") {
        submitAdd();
      } else if (el.dataset.action === "add-source") {
        state.addSource = el.dataset.source;
        state.addError = null;
        render();
      }
    });

    addModalHost.addEventListener("change", (e) => {
      const field = e.target.dataset.field;
      if (field === "period") {
        state.add.periodId = e.target.value;
        render();
      } else if (field === "type") {
        const wasAnnual = state.add.typeKey === "annual";
        state.add.typeKey = e.target.value;
        const isAnnual = state.add.typeKey === "annual";
        // Crossing the annual/quarterly boundary switches which period list
        // applies (see periodOptions()) — the previously-selected periodId
        // belongs to the other list's id format, so it wouldn't be a valid
        // option any more. Land on that list's newest period rather than
        // leaving state.add.periodId pointing at a value the dropdown can't
        // actually show as selected.
        if (wasAnnual !== isAnnual) {
          const opts = periodOptions();
          state.add.periodId = opts.length ? opts[0].value : "";
        }
        render();
      } else if (field === "file") {
        state.addFile = e.target.files[0] || null;
        render();
      }
    });

    addModalHost.addEventListener("input", (e) => {
      if (e.target.dataset.field === "ref") {
        state.addRef = e.target.value;
        // Don't re-render on every keystroke — it would rebuild the input
        // and drop focus/caret position.
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (state.add) {
        state.add = null;
        render();
      } else if (state.doc) {
        state.doc = null;
        render();
      }
    });

    loadFeed();
  }

  document.addEventListener("DOMContentLoaded", function () {
    const root = document.getElementById("docs-timeline-root");
    const modalHost = document.getElementById("docs-modal-host");
    const addModalHost = document.getElementById("docs-add-modal-host");
    if (root && modalHost && addModalHost) init(root, modalHost, addModalHost);
  });
})();
