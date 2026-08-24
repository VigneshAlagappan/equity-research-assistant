"""Example-investigation fixture data for the Research screens.

Ported verbatim from the claude.ai/design wireframe's own mock data
(threadData()/examples in "Screens Wireframes copy.dc.html") — not real
computed answers. Used until real question-answering + persistence lands
(README: Resumable build checklist, step 3 for the result view, step 12 for
real persisted threads replacing this file). Every field here is illustrative
content, not a FACT/CALCULATION grounded in ingested data.
"""

from __future__ import annotations

EXAMPLES = [
    {
        "thread_id": "monsoon-tractor",
        "kicker": "Sector + macro",
        "title": "Does monsoon rainfall affect tractor sales?",
    },
    {
        "thread_id": "bank-rates",
        "kicker": "Interest rates",
        "title": "Kalyan Bank ROA across rate cycles",
    },
    {
        "thread_id": "paint-crude",
        "kicker": "Input costs",
        "title": "How sensitive are paint margins to crude oil?",
    },
]

THREADS = {
    "monsoon-tractor": {
        "title": "Monsoon vs Tractor Demand & Rural Bank Asset Quality",
        "question": (
            "Does monsoon rainfall affect Anand Tractors’ demand, and does it "
            "flow through to a rural-focused bank’s asset quality?"
        ),
        "answer": (
            "Years of above-normal monsoon rainfall are followed by higher tractor "
            "sales and steadier rural loan repayment the same fiscal year. The "
            "relationship has held across most of the last decade, though it has "
            "weakened somewhat since 2020 as non-farm rural income has grown."
        ),
        "confidence": "Moderate",
        "confidence_note": "· correlation, not causation",
        "shared_steps": ["Monsoon rainfall", "Agricultural production", "Farmer income"],
        "has_branches": True,
        "branch_a": ["Tractor demand", "Anand Tractors revenue"],
        "branch_b": ["Loan repayment capacity", "Uttar Gramin Bank asset quality"],
        "chart_title": "Rainfall Index vs Tractor Sales Index, 2013–2024",
        "chart_units": "Indexed to 100 at 2013",
        "series_a_name": "Rainfall index",
        "series_b_name": "Tractor sales index",
        "chart_data": [
            [96, 88, 104, 79, 112, 120, 90, 105, 130, 98, 115, 108],
            [92, 85, 110, 80, 108, 124, 88, 109, 126, 94, 118, 112],
        ],
        "methodology": (
            "Rainfall is the IMD all-India south-west monsoon deviation, indexed "
            "to 100 in 2013. Tractor sales are Anand Tractors’ domestic unit "
            "volumes for the same fiscal year, indexed the same way. No lag is "
            "applied in this view — use the follow-up below to test a "
            "one-year lag."
        ),
        "evidence": [
            {
                "source": "India Meteorological Department",
                "doc": "District Rainfall Normals dataset",
                "period": "1984–2024",
                "type": "Primary · government data",
            },
            {
                "source": "Anand Tractors & Farm Equipment Ltd.",
                "doc": "Annual Report FY24, MD&A section",
                "period": "FY2024",
                "type": "Primary · company filing",
            },
            {
                "source": "Reserve Bank of India",
                "doc": "Financial Stability Report, sectoral NPA tables",
                "period": "FY2019–FY2023",
                "type": "Primary · regulatory data",
            },
        ],
        "follow_ups": [
            "Test a one-year lag",
            "Compare drought years only",
            "Add rural wage growth",
            "Compare against another tractor company",
        ],
    },
    "bank-rates": {
        "title": "Kalyan Bank ROA vs RBI Repo Rate",
        "question": (
            "How has Kalyan Bank’s return on assets moved through different "
            "RBI interest-rate cycles?"
        ),
        "answer": (
            "Kalyan Bank’s ROA has generally improved as the repo rate has "
            "fallen, consistent with lower funding costs supporting margins. The "
            "relationship is well documented and mechanically explainable through "
            "net interest margin."
        ),
        "confidence": "High",
        "confidence_note": "· well-documented mechanism",
        "shared_steps": ["RBI repo rate", "Cost of funds"],
        "has_branches": False,
        "branch_a": [],
        "branch_b": [],
        "chart_title": "ROA Index vs Repo Rate Index, 2014–2024",
        "chart_units": "Indexed to 100 at 2014",
        "series_a_name": "ROA index",
        "series_b_name": "Repo rate index",
        "chart_data": [
            [100, 102, 105, 108, 104, 110, 115, 112, 118, 120, 122],
            [100, 95, 90, 85, 88, 80, 75, 78, 70, 65, 60],
        ],
        "methodology": (
            "Both series are indexed to 100 in FY2014. ROA is net profit over "
            "average total assets, as reported. Repo rate is the RBI policy rate "
            "at fiscal year-end."
        ),
        "evidence": [
            {
                "source": "Kalyan Bank Ltd.",
                "doc": "Annual Report FY24, financial statements",
                "period": "FY2010–FY2024",
                "type": "Primary · company filing",
            },
            {
                "source": "Reserve Bank of India",
                "doc": "Monetary Policy Report, repo rate history",
                "period": "2010–2024",
                "type": "Primary · regulatory data",
            },
        ],
        "follow_ups": [
            "Compare with another private bank",
            "Test before/after 2016",
            "Add credit growth as a variable",
        ],
    },
    "paint-crude": {
        "title": "Rangoli Paints Margins vs Crude Oil",
        "question": "How sensitive are Rangoli Paints’ margins to crude oil price movements?",
        "answer": (
            "Gross margin moves opposite to crude oil roughly two quarters later, "
            "consistent with resin and solvent costs being crude derivatives. The "
            "relationship is directionally reliable but the lag varies by year, "
            "likely due to inventory hedging."
        ),
        "confidence": "Moderate",
        "confidence_note": "· lag varies year to year",
        "shared_steps": ["Crude oil price", "Input cost (resins & solvents)"],
        "has_branches": False,
        "branch_a": [],
        "branch_b": [],
        "chart_title": "Gross Margin Index vs Crude Oil Index, 2015–2024",
        "chart_units": "Indexed to 100 at 2015",
        "series_a_name": "Margin index",
        "series_b_name": "Crude oil index",
        "chart_data": [
            [100, 92, 85, 90, 78, 95, 88, 72, 80, 86],
            [100, 110, 120, 105, 135, 98, 112, 150, 130, 118],
        ],
        "methodology": (
            "Margin index is Rangoli Paints’ reported gross margin, indexed "
            "to 100 in FY2015. Crude index is the Indian basket crude price for "
            "the same fiscal year."
        ),
        "evidence": [
            {
                "source": "Rangoli Paints Ltd.",
                "doc": "Annual Report FY24, cost breakdown notes",
                "period": "FY2015–FY2024",
                "type": "Primary · company filing",
            },
            {
                "source": "Petroleum Planning & Analysis Cell",
                "doc": "Indian basket crude price series",
                "period": "2015–2024",
                "type": "Primary · government data",
            },
        ],
        "follow_ups": [
            "Compare with another paint company",
            "Isolate raw material mix by segment",
            "Test a two-quarter lag",
        ],
    },
}
