// Notes tab — a master-detail rail (list of dated notes, newest first) +
// reading/editing pane, backed by the real /companies/<id>/notes/{add,edit,
// delete} routes (storage/repositories.py's company_notes table). Ported
// from a Claude Design prototype (Signals Docs 1E.dc.html's Notes tab).
//
// Rich text: compose/edit use a contenteditable box with a small
// execCommand-driven toolbar (bold/italic/link/quote/code/lists), not a
// plain textarea. A note's stored `html` is server-sanitized
// (web/rich_text.py) on every save — the reader view trusts that and
// assigns it via innerHTML directly; nothing here re-sanitizes on read,
// since the actual trust boundary is the save routes, not this file.
//
// Attachments only apply to an already-saved note (there's no note_id to
// attach against while composing) — the toolbar's Attach button is disabled
// during compose and explains why.
//
// Renders entirely from an in-memory `state.notes` array seeded once from
// #notes-initial-data (the server-rendered list) and kept in sync with
// every add/edit/delete/attach response — same state+render() shape as
// docs_timeline.js, just no fetch needed up front since the data's already
// on the page.
(function () {
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function excerpt(text) {
    return text.length > 64 ? text.slice(0, 64).trim() + "…" : text;
  }

  // Plain-text preview of rich HTML, for the rail's one-line excerpt — never
  // reinserted as HTML anywhere, just read back out as .textContent.
  function htmlToText(html) {
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    return tmp.textContent || "";
  }

  function isBlankHtml(html) {
    return !htmlToText(html).trim();
  }

  function formatSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  // note.created_at/updated_at are UTC (storage/database.py's utcnow_iso) —
  // the viewer's own local calendar date, not the server's, is what a
  // "dated note" should read as.
  function localDate(isoUtc) {
    const d = new Date(isoUtc);
    if (Number.isNaN(d.getTime())) return isoUtc.slice(0, 10);
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  function init(root) {
    const companyId = root.dataset.companyId;
    const companyName = root.dataset.companyName;
    const notesUrl = (path) => `/companies/${encodeURIComponent(companyId)}/notes${path}`;

    const initialEl = document.getElementById("notes-initial-data");
    const initialNotes = initialEl ? JSON.parse(initialEl.textContent) : [];

    const state = {
      notes: initialNotes, // [{note_id, html, created_at, updated_at, attachments}], newest first
      selectedNoteId: initialNotes.length > 0 ? initialNotes[0].note_id : null,
      composing: false,
      draftHtml: "",
      editingNoteId: null,
      editHtml: "",
      saving: false,
      uploading: false,
      error: null,
    };

    const rail = document.getElementById("notes-rail");
    const reader = document.getElementById("notes-reader");
    const countEl = document.getElementById("notes-count");

    function stampShort(note) {
      return localDate(note.created_at);
    }
    function stampFull(note) {
      let s = localDate(note.created_at);
      if (note.updated_at) s += " · edited " + localDate(note.updated_at);
      return s;
    }

    function toolbarHtml(mode) {
      const attachDisabled = mode === "compose";
      return `
        <div class="notes-toolbar">
          <button type="button" class="notes-toolbar-btn" data-toolbar="bold" title="Bold"><b>B</b></button>
          <button type="button" class="notes-toolbar-btn" data-toolbar="italic" title="Italic"><i>I</i></button>
          <button type="button" class="notes-toolbar-btn" data-toolbar="link" title="Link">Link</button>
          <button type="button" class="notes-toolbar-btn" data-toolbar="quote" title="Quote">&rdquo;</button>
          <button type="button" class="notes-toolbar-btn" data-toolbar="code" title="Code">&lt;&gt;</button>
          <span class="notes-toolbar-sep"></span>
          <button type="button" class="notes-toolbar-btn" data-toolbar="bullet" title="Bulleted list">&bull;</button>
          <button type="button" class="notes-toolbar-btn" data-toolbar="number" title="Numbered list">1.</button>
          <span class="notes-toolbar-sep"></span>
          <button type="button" class="notes-toolbar-btn${attachDisabled ? " is-disabled" : ""}" data-toolbar="attach" title="Attach file">Attach</button>
        </div>`;
    }

    function attachmentsHtml(note) {
      const attachments = note.attachments || [];
      if (attachments.length === 0) return "";
      return `<div class="notes-attachments">${attachments
        .map(
          (a) => `
          <span class="notes-attachment-chip">
            <a href="${escapeHtml(a.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(a.filename)} · ${formatSize(a.size_bytes)}</a>
            <button type="button" class="notes-attachment-remove" data-action="delete-attachment" data-note-id="${note.note_id}" data-attachment-id="${a.attachment_id}" title="Remove">&times;</button>
          </span>`
        )
        .join("")}</div>`;
    }

    function renderRail() {
      let html = `<button type="button" class="notes-new-btn" data-action="new-note">＋ New note</button>`;
      if (state.composing) {
        html += `<div class="notes-rail-draft">
          <div class="notes-rail-draft-label">Draft · unsaved</div>
          <div class="notes-rail-draft-excerpt">${state.draftHtml ? escapeHtml(excerpt(htmlToText(state.draftHtml))) : "New note"}</div>
        </div>`;
      }
      if (state.notes.length === 0) {
        html += `<div class="notes-rail-empty muted">No notes yet.</div>`;
      } else {
        html += state.notes
          .map((n) => {
            const active = !state.composing && state.selectedNoteId === n.note_id;
            return `<div class="notes-rail-item${active ? " notes-rail-item-active" : ""}" data-action="select-note" data-note-id="${n.note_id}">
              <div class="notes-rail-stamp">${stampShort(n)}</div>
              <div class="notes-rail-excerpt">${escapeHtml(excerpt(htmlToText(n.html)))}</div>
            </div>`;
          })
          .join("");
      }
      rail.innerHTML = html;
    }

    function focusEditable() {
      const el = reader.querySelector(".notes-editable");
      if (!el) return;
      el.focus();
      const range = document.createRange();
      range.selectNodeContents(el);
      range.collapse(false);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }

    function renderReader() {
      if (state.composing) {
        reader.innerHTML = `
          <div class="notes-reader-header">
            <span class="notes-reader-kicker">New note</span>
            <span class="muted notes-reader-substamp">Unsaved</span>
          </div>
          ${toolbarHtml("compose")}
          <div class="notes-editable" contenteditable="true" data-mode="compose"
               data-placeholder="Write a note about ${escapeHtml(companyName)}…">${state.draftHtml}</div>
          ${state.error ? `<p class="notes-reader-error">${escapeHtml(state.error)}</p>` : ""}
          <div class="notes-reader-footer">
            <button type="button" class="btn btn-ghost" data-action="cancel-compose" ${state.saving ? "disabled" : ""}>Cancel</button>
            <button type="button" class="btn btn-primary" data-action="save-note" ${state.saving ? "disabled" : ""}>${state.saving ? "Saving…" : "Save note"}</button>
          </div>`;
        focusEditable();
        return;
      }

      const note = state.notes.find((n) => n.note_id === state.selectedNoteId);
      if (!note) {
        reader.innerHTML = `<p class="muted notes-reader-empty">Select a note on the left, or start a new one.</p>`;
        return;
      }

      if (state.editingNoteId === note.note_id) {
        reader.innerHTML = `
          <div class="notes-reader-header">
            <span class="muted notes-reader-substamp">${stampFull(note)}</span>
          </div>
          ${toolbarHtml("edit")}
          <div class="notes-editable" contenteditable="true" data-mode="edit">${state.editHtml}</div>
          <input type="file" id="notes-attach-input" style="display:none">
          ${attachmentsHtml(note)}
          ${state.error ? `<p class="notes-reader-error">${escapeHtml(state.error)}</p>` : ""}
          <div class="notes-reader-footer">
            <button type="button" class="btn btn-ghost" data-action="cancel-edit" ${state.saving ? "disabled" : ""}>Cancel</button>
            <button type="button" class="btn btn-primary" data-action="save-edit" ${state.saving ? "disabled" : ""}>${state.saving ? "Saving…" : "Save"}</button>
          </div>`;
        focusEditable();
        return;
      }

      reader.innerHTML = `
        <div class="notes-reader-header">
          <span class="muted notes-reader-substamp">${stampFull(note)}</span>
          <div class="notes-reader-actions">
            <button type="button" class="notes-link-action" data-action="start-edit">Edit</button>
            <button type="button" class="notes-link-action notes-link-danger" data-action="delete-note">Delete</button>
          </div>
        </div>
        <div class="notes-reader-text">${note.html}</div>
        ${attachmentsHtml(note)}
        ${state.error ? `<p class="notes-reader-error">${escapeHtml(state.error)}</p>` : ""}`;
    }

    function render() {
      countEl.textContent = state.notes.length === 1 ? "1 note" : state.notes.length + " notes";
      renderRail();
      renderReader();
    }

    async function saveNewNote() {
      const html = state.draftHtml;
      if (isBlankHtml(html)) {
        state.error = "Note can't be empty.";
        render();
        return;
      }
      state.saving = true;
      state.error = null;
      render();
      try {
        const response = await fetch(notesUrl("/add"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ html }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Something went wrong.");
        state.notes.unshift({ note_id: data.note_id, html: data.html, created_at: data.created_at, updated_at: null, attachments: [] });
        state.selectedNoteId = data.note_id;
        state.composing = false;
        state.draftHtml = "";
      } catch (err) {
        state.error = err.message || "Network error — try again.";
      } finally {
        state.saving = false;
        render();
      }
    }

    async function saveEdit() {
      const html = state.editHtml;
      if (isBlankHtml(html)) {
        state.error = "Note can't be empty.";
        render();
        return;
      }
      const noteId = state.editingNoteId;
      state.saving = true;
      state.error = null;
      render();
      try {
        const response = await fetch(notesUrl(`/${noteId}/edit`), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ html }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Something went wrong.");
        const note = state.notes.find((n) => n.note_id === noteId);
        note.html = data.html;
        note.updated_at = data.updated_at;
        state.editingNoteId = null;
      } catch (err) {
        state.error = err.message || "Network error — try again.";
      } finally {
        state.saving = false;
        render();
      }
    }

    async function deleteSelected() {
      const noteId = state.selectedNoteId;
      if (!noteId) return;
      if (!window.confirm("Delete this note? This can't be undone.")) return;
      try {
        const response = await fetch(notesUrl(`/${noteId}/delete`), { method: "POST" });
        if (!response.ok) {
          state.error = "Could not delete that note — try again.";
          render();
          return;
        }
        state.notes = state.notes.filter((n) => n.note_id !== noteId);
        state.selectedNoteId = state.notes.length > 0 ? state.notes[0].note_id : null;
        state.editingNoteId = null;
        state.error = null;
      } catch (err) {
        state.error = "Network error — try again.";
      }
      render();
    }

    async function uploadAttachment(file) {
      const noteId = state.editingNoteId;
      if (!noteId) return;
      state.uploading = true;
      state.error = null;
      render();
      const formData = new FormData();
      formData.append("file", file);
      try {
        const response = await fetch(notesUrl(`/${noteId}/attachments/add`), { method: "POST", body: formData });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Upload failed.");
        const note = state.notes.find((n) => n.note_id === noteId);
        note.attachments = note.attachments || [];
        note.attachments.push(data);
      } catch (err) {
        state.error = err.message || "Network error — try again.";
      } finally {
        state.uploading = false;
        render();
      }
    }

    async function deleteAttachment(noteId, attachmentId) {
      if (!window.confirm("Remove this attachment?")) return;
      try {
        const response = await fetch(notesUrl(`/${noteId}/attachments/${attachmentId}/delete`), { method: "POST" });
        if (!response.ok) {
          state.error = "Could not remove that attachment — try again.";
          render();
          return;
        }
        const note = state.notes.find((n) => n.note_id === noteId);
        if (note) note.attachments = (note.attachments || []).filter((a) => a.attachment_id !== attachmentId);
      } catch (err) {
        state.error = "Network error — try again.";
      }
      render();
    }

    // execCommand needs the contenteditable box focused when a toolbar
    // command runs — a plain click on the button would blur it first, so
    // mousedown (fired before blur) is where focus loss gets prevented.
    root.addEventListener("mousedown", (e) => {
      if (e.target.closest("[data-toolbar]")) e.preventDefault();
    });

    root.addEventListener("click", (e) => {
      const toolbarBtn = e.target.closest("[data-toolbar]");
      if (toolbarBtn) {
        const cmd = toolbarBtn.dataset.toolbar;
        if (cmd === "attach") {
          if (toolbarBtn.classList.contains("is-disabled")) {
            state.error = "Save the note first, then attach files from Edit.";
            render();
            return;
          }
          document.getElementById("notes-attach-input").click();
          return;
        }
        document.execCommand("styleWithCSS", false, false);
        if (cmd === "bold") document.execCommand("bold");
        else if (cmd === "italic") document.execCommand("italic");
        else if (cmd === "quote") document.execCommand("formatBlock", false, "blockquote");
        else if (cmd === "bullet") document.execCommand("insertUnorderedList");
        else if (cmd === "number") document.execCommand("insertOrderedList");
        else if (cmd === "code") toggleInlineCode();
        else if (cmd === "link") {
          const url = window.prompt("Link URL:");
          if (url) document.execCommand("createLink", false, url);
        }
        const editable = reader.querySelector(".notes-editable");
        if (editable) syncDraftFromEditable(editable);
        return;
      }

      const el = e.target.closest("[data-action]");
      if (!el) return;
      const action = el.dataset.action;
      if (action === "new-note") {
        state.composing = true;
        state.draftHtml = "";
        state.editingNoteId = null;
        state.error = null;
        render();
      } else if (action === "select-note") {
        state.selectedNoteId = Number(el.dataset.noteId);
        state.composing = false;
        state.editingNoteId = null;
        state.error = null;
        render();
      } else if (action === "cancel-compose") {
        state.composing = false;
        state.draftHtml = "";
        state.error = null;
        render();
      } else if (action === "save-note") {
        saveNewNote();
      } else if (action === "start-edit") {
        const note = state.notes.find((n) => n.note_id === state.selectedNoteId);
        state.editingNoteId = note.note_id;
        state.editHtml = note.html;
        state.error = null;
        render();
      } else if (action === "cancel-edit") {
        state.editingNoteId = null;
        state.error = null;
        render();
      } else if (action === "save-edit") {
        saveEdit();
      } else if (action === "delete-note") {
        deleteSelected();
      } else if (action === "delete-attachment") {
        deleteAttachment(Number(el.dataset.noteId), Number(el.dataset.attachmentId));
      }
    });

    function syncDraftFromEditable(el) {
      if (el.dataset.mode === "compose") state.draftHtml = el.innerHTML;
      else if (el.dataset.mode === "edit") state.editHtml = el.innerHTML;
    }

    // Not re-rendering here (would drop focus/caret) — the draft-excerpt
    // rail preview only refreshes on the next real render, same tradeoff
    // docs_timeline.js's Add-document ref field makes.
    root.addEventListener("input", (e) => {
      if (e.target.classList && e.target.classList.contains("notes-editable")) {
        syncDraftFromEditable(e.target);
      }
    });

    root.addEventListener("change", (e) => {
      if (e.target.id === "notes-attach-input" && e.target.files.length > 0) {
        uploadAttachment(e.target.files[0]);
        e.target.value = "";
      }
    });

    function toggleInlineCode() {
      const sel = window.getSelection();
      if (!sel.rangeCount || sel.isCollapsed) return;
      const range = sel.getRangeAt(0);
      let node = range.commonAncestorContainer;
      if (node.nodeType === Node.TEXT_NODE) node = node.parentElement;
      const codeAncestor = node.closest && node.closest("code");
      if (codeAncestor) {
        const parent = codeAncestor.parentNode;
        while (codeAncestor.firstChild) parent.insertBefore(codeAncestor.firstChild, codeAncestor);
        parent.removeChild(codeAncestor);
        return;
      }
      const code = document.createElement("code");
      try {
        range.surroundContents(code);
      } catch (err) {
        code.appendChild(range.extractContents());
        range.insertNode(code);
      }
      range.selectNodeContents(code);
      sel.removeAllRanges();
      sel.addRange(range);
    }

    render();
  }

  document.addEventListener("DOMContentLoaded", function () {
    const root = document.getElementById("notes-panel");
    if (root) init(root);
  });
})();
