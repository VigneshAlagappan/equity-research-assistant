// Delete affordance for the company page's Threads tab (web/templates/company.html,
// #sec-threads). The cards themselves are plain server-rendered Jinja — this
// script only wires up the "Delete" button, since a thread card can be deleted
// after either an Ask AI answer (auto-saved, see web/app.py's company_ask) or a
// /research/thread/generate report.
(() => {
  const list = document.getElementById("threads-list");
  if (!list) return;

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  list.addEventListener("click", async (e) => {
    const btn = e.target.closest('[data-action="delete-thread"]');
    if (!btn) return;
    e.preventDefault();

    const threadId = btn.dataset.threadId;
    if (!window.confirm("Delete this thread? This can't be undone.")) return;

    btn.disabled = true;
    try {
      const response = await fetch(`/research/thread/${encodeURIComponent(threadId)}/delete`, {
        method: "POST",
      });
      if (!response.ok) {
        btn.disabled = false;
        window.alert("Could not delete that thread — try again.");
        return;
      }
    } catch (err) {
      btn.disabled = false;
      window.alert("Network error — try again.");
      return;
    }

    const card = list.querySelector(`.investigation-card[data-thread-id="${CSS.escape(threadId)}"]`);
    if (card) card.remove();

    if (!list.querySelector(".investigation-card")) {
      const companyName = list.dataset.companyName || "this company";
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.id = "threads-empty";
      empty.innerHTML =
        `No research threads yet for ${escapeHtml(companyName)}. Ask a question about this ` +
        "company via Ask AI, or generate a full report from the Research tab — either shows up " +
        "here (and in Investigations) automatically, timestamped and deletable.";
      list.replaceWith(empty);
    }
  });
})();
