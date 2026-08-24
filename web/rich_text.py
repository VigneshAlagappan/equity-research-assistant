"""Sanitizes rich-text HTML from the Notes tab's contenteditable editor
(web/static/js/notes_panel.js) before it's ever written to company_notes.

This is the actual trust boundary: the browser's contenteditable innerHTML
is attacker-controllable (anyone who can type into the note box), and
notes_panel.js renders a note's stored text with innerHTML, not textContent
— so an unsanitized write here is a stored-XSS hole. sanitize_note_html()
must run on every write path (company_add_note / company_edit_note in
web/app.py); nothing downstream re-sanitizes on read, so there is exactly
one place this can go wrong.

bleach (not a hand-rolled tag stripper) does the actual parsing/allowlisting
— HTML sanitization is a solved, easy-to-get-subtly-wrong problem, not
something worth re-implementing for this feature.
"""

from __future__ import annotations

import bleach

# Exactly what the toolbar's execCommand calls can produce (see
# notes_panel.js): bold/italic, links, blockquote, inline code, and the two
# list types, plus the block-level tags contenteditable divs itself into.
# Nothing else survives — no img/script/style/svg/iframe, no on* handlers,
# no inline style attributes (execCommand('styleWithCSS', false, false) in
# the editor already discourages these, but the allowlist is what actually
# enforces it).
ALLOWED_TAGS = [
    "b", "strong", "i", "em", "a", "blockquote", "code", "pre",
    "ul", "ol", "li", "br", "div", "p",
]
ALLOWED_ATTRIBUTES = {"a": ["href"]}
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def sanitize_note_html(html: str) -> str:
    """Strip everything outside the allowlist above. Safe to call on empty
    or plain-text input too (a no-op except for entity-escaping)."""
    return bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
