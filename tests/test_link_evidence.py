from research.link_evidence import (
    Loader, check_links, match_concept, realized_direction, stated_direction,
)


def loader(data: dict[str, dict[int, float]], is_us: bool = False) -> Loader:
    return Loader(lambda m: dict(data.get(m, {})), is_us)


def test_concept_matching_and_specificity():
    assert match_concept("Operating margin falls").key == "operating_margin"
    assert match_concept("Gross margin expands").key == "gross_margin"
    assert match_concept("Input cost per vehicle falls").key == "material_cost"
    assert match_concept("Employee and opex costs increase").key == "employee_cost"
    assert match_concept("Demand/volumes rise").key == "revenue"
    assert match_concept("Interest rates rise") is None  # macro: out of scope for this check
    assert match_concept("") is None


def test_stated_direction():
    assert stated_direction("Input cost per vehicle falls") == -1
    assert stated_direction("Operating margin expands") == 1
    assert stated_direction("Margins compressed") == -1
    assert stated_direction("Demand changes") is None
    assert stated_direction("Costs rise then fall") is None  # contradictory wording -> unknown


def test_realized_direction_thresholds_and_minimum_points():
    assert realized_direction({2023: 10.0, 2024: 11.0}, "ratio") is None
    assert realized_direction({2022: 50.0, 2023: 50.1, 2024: 50.2}, "ratio")[0] == 0  # < 0.3pp: flat
    assert realized_direction({2022: 50.0, 2023: 49.0, 2024: 48.0}, "ratio")[0] == -1
    assert realized_direction({2021: 100.0, 2022: 105.0, 2023: 110.0, 2024: 120.0, 2025: 130.0}, "level")[1:3] == (2022, 2025)


def _maruti_like():
    revenue = {2022: 100.0, 2023: 110.0, 2024: 120.0}
    return {
        "total_revenue": revenue,
        "cost_of_materials_consumed": {2022: 70.0, 2023: 75.0, 2024: 78.0},  # 70% -> 68.2% -> 65%: share falls
        "purchases_of_stock_in_trade": {2022: 0.0, 2023: 0.0, 2024: 0.0},
        "other_expenses": {2022: 10.0, 2023: 10.0, 2024: 10.0},
        "operating_expenses": {2022: 85.0, 2023: 92.0, 2024: 98.0},
        "interest_expended": {2022: 1.0, 2023: 1.0, 2024: 1.0},
        "depreciation": {2022: 3.0, 2023: 3.0, 2024: 3.0},
    }


def test_supporting_when_both_ends_move_as_stated():
    # material share 70 -> 65 (down), EBITDA margin: (rev - (opex - fin - dep))/rev = 18% -> 19.2% (up)
    steps = ["Input cost per vehicle falls", "Operating margin expands"]
    out = check_links(steps, loader(_maruti_like()), "MARUTI")
    assert len(out) == 1
    stance, item = out[0]
    assert stance == "supporting" and item.chain_step == 0 and item.source_tier == "CALCULATED"
    assert item.kind == "CALCULATION" and "(proxy)" in item.label and "computed" in item.label
    assert "70.0% -> 65.0%" in item.value and "both as the chain states" in item.value


def test_contradicting_when_an_end_moves_the_other_way():
    steps = ["Input cost per vehicle rises", "Operating margin falls"]  # cost actually fell, margin rose
    stance, item = check_links(steps, loader(_maruti_like()), "MARUTI")[0]
    assert stance == "contradicting" and "differs from the chain" in item.value


def test_skips_untestable_links_without_error():
    d = _maruti_like()
    assert check_links(["Interest rates rise", "Demand softens"], loader(d), "X") == []  # macro cause
    assert check_links(["Input cost falls", "Margin expands"], loader({}), "X") == []  # no data
    flat = {**d, "other_expenses": {2022: 10.0, 2023: 11.0, 2024: 12.0}, "total_revenue": {2022: 100.0, 2023: 110.0, 2024: 120.0}}
    assert check_links(["Opex rises", "Employee costs rise"], loader(flat), "X") == []  # same-concept/employee missing -> none
    assert check_links(["only one step"], loader(d), "X") == []
    assert check_links(["Input cost falls", "Input cost falls again"], loader(d), "X") == []  # same concept both ends


def test_us_company_uses_cost_of_revenue():
    data = {
        "total_revenue": {2022: 100.0, 2023: 100.0, 2024: 100.0},
        "cost_of_revenue": {2022: 60.0, 2023: 58.0, 2024: 55.0},
        "net_profit": {2022: 10.0, 2023: 12.0, 2024: 15.0},
    }
    out = check_links(["Cost of revenue falls", "Net profit rises"], loader(data, is_us=True), "AAPL")
    assert out and out[0][0] == "supporting"
