"""Output-format renderers for the Signal Report Design System.

Each subpackage takes a `reports.schema.InvestigationReport` and produces one
output format:

    html/  — Jinja2 templates + components rendered by Flask (implemented;
             see reports/templates/deep_dive and reports/components).
    pdf/   — stub; browser "Print / Export PDF" (reports/theme/print.css)
             covers this today, so no server-side PDF library is wired up.
    docx/  — stub; no DOCX export exists yet.
"""
