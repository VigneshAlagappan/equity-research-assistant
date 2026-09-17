"""HTML rendering for Signal Research Reports.

The actual rendering happens through Flask's own `render_template` against
reports/templates/deep_dive/report.html (registered onto the app's Jinja
loader by web/app.py's `create_app()`), which imports macros from
reports/components/*.html. There is deliberately no separate
"render(report) -> str" function here — the web app already owns request
context (url_for, g.theme, flashed messages, the surrounding base.html
chrome) that a standalone renderer would have to fake, and every other route
in this app renders the same way. This module exists so `reports.renderers`
has a real, importable html subpackage matching pdf/ and docx/, and as the
place a non-Flask HTML renderer (e.g. for a batch export tool) would go if
one is ever needed.
"""
