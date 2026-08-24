"""web/rich_text.py tests — the actual XSS trust boundary for Notes, so this
allowlist earns its own direct coverage independent of the route tests."""

from __future__ import annotations

from web.rich_text import sanitize_note_html


def test_allows_toolbar_produced_tags():
    html = "<div><b>bold</b> <i>italic</i> <a href='https://example.com'>link</a></div>"
    out = sanitize_note_html(html)
    assert "<b>bold</b>" in out
    assert "<i>italic</i>" in out
    assert 'href="https://example.com"' in out


def test_allows_lists_quote_and_code():
    html = "<ul><li>one</li></ul><ol><li>two</li></ol><blockquote>q</blockquote><code>x</code><pre>y</pre>"
    out = sanitize_note_html(html)
    for tag in ("<ul>", "<li>", "<ol>", "<blockquote>", "<code>", "<pre>"):
        assert tag in out


def test_strips_script_tags():
    # bleach strips the disallowed tag itself but leaves its text content
    # behind as inert plain text (same as any other disallowed tag) — the
    # security property is "no <script> tag survives to execute", not that
    # the word "alert" vanishes from the page.
    out = sanitize_note_html("<div>hi<script>alert(1)</script></div>")
    assert "<script" not in out
    assert "</script>" not in out


def test_strips_event_handler_attributes():
    out = sanitize_note_html('<div onclick="alert(1)">hi</div>')
    assert "onclick" not in out
    assert "alert(1)" not in out


def test_strips_javascript_protocol_links():
    out = sanitize_note_html('<a href="javascript:alert(1)">click</a>')
    assert "javascript:" not in out


def test_strips_img_and_style_tags():
    out = sanitize_note_html('<img src="x" onerror="alert(1)"><style>body{}</style>')
    assert "<img" not in out
    assert "<style>" not in out
    assert "onerror" not in out

    assert "alert(1)" not in out


def test_strips_inline_style_attribute():
    out = sanitize_note_html('<div style="color:red">hi</div>')
    assert "style=" not in out


def test_empty_input_is_a_noop():
    assert sanitize_note_html("") == ""
