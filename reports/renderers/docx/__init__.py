"""Future work: DOCX export for Signal Research Reports.

Not implemented. No DOCX-writing library (e.g. python-docx) exists in
requirements.txt or requirements-docker.txt today, and none is added here.

If implemented later, it should consume a `reports.schema.InvestigationReport`
directly (same as reports/renderers/html and the future reports/renderers/pdf)
so all three output formats stay downstream of one normalized schema instead
of each re-deriving content from the research pipeline independently.
"""
