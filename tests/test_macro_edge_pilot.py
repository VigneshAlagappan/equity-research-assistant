import numpy as np

from research.macro_edge_pilot import (
    EdgeTestResult, aggregate_mean, fiscal_quarter_index, lagged_panel_test, month_index, yoy,
)


def _synthetic(lag: int, sign: int, n: int = 80, noise: float = 0.3, seed: int = 1):
    rng = np.random.default_rng(seed)
    cause = rng.normal(size=n + 20)
    cause = np.convolve(cause, np.ones(3) / 3, mode="same")  # some autocorrelation
    effect = {t: sign * cause[t - lag] + rng.normal(scale=noise) for t in range(20, n + 20)}
    return {t: float(v) for t, v in enumerate(cause)}, {"A": effect}


def test_recovers_known_lag_and_sign():
    cause, effects = _synthetic(lag=3, sign=+1)
    res = lagged_panel_test(cause, effects, list(range(0, 8)), expected_sign=+1, n_perm=500)
    assert res.best_lag == 3 and res.r_best > 0.8 and res.classification == "DIRECTION_SUPPORTED"


def test_opposite_sign_is_contradicted_not_supported():
    cause, effects = _synthetic(lag=2, sign=-1)
    res = lagged_panel_test(cause, effects, list(range(0, 8)), expected_sign=+1, n_perm=500)
    assert res.best_lag == 2 and res.classification == "DIRECTION_CONTRADICTED"


def test_independent_noise_is_not_detected():
    rng = np.random.default_rng(7)
    cause = {t: float(v) for t, v in enumerate(rng.normal(size=100))}
    effects = {"A": {t: float(rng.normal()) for t in range(20, 90)}}
    res = lagged_panel_test(cause, effects, list(range(0, 8)), expected_sign=+1, n_perm=500)
    assert res.classification == "NOT_DETECTED" and res.p_adjusted > 0.05


def test_too_few_periods_is_insufficient():
    cause = {t: float(t % 5) for t in range(40)}
    effects = {"A": {t: float(t % 3) for t in range(30, 38)}}
    res = lagged_panel_test(cause, effects, [0, 1, 2], expected_sign=+1, n_perm=50)
    assert isinstance(res, EdgeTestResult) and res.classification == "INSUFFICIENT_DATA" and res.p_adjusted is None


def test_period_helpers():
    assert month_index("2024-03-15") == 2024 * 12 + 2 and month_index("bad") is None
    assert fiscal_quarter_index("FY2024", "Q1") == 2023 * 4 + 1   # quarter ending June 2023
    assert fiscal_quarter_index("FY2024", "Q4") == 2024 * 4 + 0   # quarter ending March 2024
    assert fiscal_quarter_index("FY2024", "Q9") is None
    m = aggregate_mean([("2024-01-01", 2.0), ("2024-01-15", 4.0), ("2024-02-01", 10.0)], "M")
    assert m == {2024 * 12: 3.0, 2024 * 12 + 1: 10.0}
    assert abs(yoy({0: 100.0, 12: 110.0}, 12, "pct")[12] - 10.0) < 1e-9
    assert yoy({0: 5.0, 4: 6.5}, 4, "diff") == {4: 1.5}
