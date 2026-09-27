"""Signal Research Report Design System.

Separates research content (produced by research/investigation.py and
research/hypothesis_evaluator.py — NOT modified by this package) from
presentation:

    Investigation Engine (research/*)
          v
    normalized InvestigationReport (reports/schema)
          v
    Signal Report Design System (reports/components, reports/templates, reports/theme)
          v
    Web / Print PDF / future DOCX (reports/renderers)

Nothing under reports/ generates or alters research conclusions — it only
reshapes and displays data that already exists in storage.
"""
