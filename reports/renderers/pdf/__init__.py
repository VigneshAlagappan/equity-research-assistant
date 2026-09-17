"""Future work: server-side PDF rendering for Signal Research Reports.

Not implemented. No PDF generation library (e.g. WeasyPrint, wkhtmltopdf,
Playwright-based rendering) exists anywhere in requirements.txt or
requirements-docker.txt today, and per the design-system task this package
deliberately doesn't add one. PDF export currently uses the browser's own
"Print / Export PDF" button (window.print()) against
reports/theme/print.css, which needs no server dependency.

If a real PDF renderer is added later, it should take a
`reports.schema.InvestigationReport` (or render the same
reports/templates/deep_dive/report.html HTML and rasterize it) so it stays
downstream of the same normalized schema the web renderer uses — never a
second, diverging presentation of the research data.
"""
