# Macro edge pilot results

Generated 2026-10-02 by `python -m scripts.macro_edge_pilot` (read-only; no database writes, no LLM). Candidate edges are hand-written from domain knowledge. **Nothing here is a causal claim**: each row is direction + lag + significance of an association over a short history (ADR-009: connectivity and correlation are evidence for investigation, not proof).

Method: both series as YoY changes; effect series z-scored per company and pooled; cause leads effect by the lag; best lag by |r|; p-value from a circular-shift permutation test that repeats the lag search (so it already accounts for lag-picking and autocorrelation). Classified at p < 0.05; INSUFFICIENT_DATA below 12 distinct periods.

| Edge | Expected | Periods | Pairs | Best lag | r | p (adj.) | Result |
|---|---|---|---|---|---|---|---|
| repo_to_base_rate | + | 96 | 96 | 3 mo | +0.94 | 0.000 | DIRECTION_SUPPORTED |
| repo_to_gsec10 | + | 96 | 96 | 0 mo | +0.61 | 0.142 | NOT_DETECTED |
| repo_to_tbill91 | + | 96 | 96 | 0 mo | +0.97 | 0.000 | DIRECTION_SUPPORTED |
| repo_to_bank_credit | − | 96 | 96 | 10 mo | +0.71 | 0.301 | NOT_DETECTED |
| repo_to_m3 | − | 96 | 96 | 0 mo | -0.34 | 0.243 | NOT_DETECTED |
| us10y_to_gsec10 | + | 96 | 96 | 0 mo | +0.81 | 0.000 | DIRECTION_SUPPORTED |
| dxy_to_usdinr | + | 125 | 125 | 0 mo | +0.46 | 0.349 | NOT_DETECTED |
| oil_to_usdinr | + | 125 | 125 | 0 mo | -0.22 | 0.804 | NOT_DETECTED |
| vix_to_usdinr | + | 125 | 125 | 1 mo | +0.42 | 0.182 | NOT_DETECTED |
| repo_to_bank_interest_expense | + | 9 | 41 | n/a | n/a | n/a | INSUFFICIENT_DATA |
| repo_to_bank_interest_income | + | 9 | 41 | n/a | n/a | n/a | INSUFFICIENT_DATA |
| repo_to_auto_revenue | − | 13 | 62 | 6 qtr | -0.52 | 0.767 | NOT_DETECTED |
| bankcredit_to_auto_revenue | + | 13 | 62 | 3 qtr | -0.65 | 0.750 | NOT_DETECTED |
| usdinr_to_it_revenue | + | 12 | 57 | 0 qtr | +0.70 | 0.000 | DIRECTION_SUPPORTED |
| oil_to_indigo_margin | − | 12 | 12 | 6 qtr | +0.78 | 0.260 | NOT_DETECTED |
| oil_to_asianpaints_margin | − | 12 | 12 | 1 qtr | -0.71 | 0.535 | NOT_DETECTED |

## Mechanisms

- **repo_to_base_rate**: Policy rate passes through to banks' base lending rate (policy_repo_rate → base_rate)
- **repo_to_gsec10**: Policy rate anchors the 10Y G-sec yield (policy_repo_rate → 10_year_g_sec_yield_fbil)
- **repo_to_tbill91**: Policy rate drives short-term T-bill yields (policy_repo_rate → 91_day_treasury_bill_primary_yield)
- **repo_to_bank_credit**: Higher borrowing cost slows credit growth (policy_repo_rate → bank_credit)
- **repo_to_m3**: Tighter policy slows broad-money growth (policy_repo_rate → m3)
- **us10y_to_gsec10**: US yields spill over into Indian G-sec yields (dgs10 → 10_year_g_sec_yield_fbil)
- **dxy_to_usdinr**: A stronger dollar weakens the rupee (dtwexbgs → usd_inr_reference_rate)
- **oil_to_usdinr**: India imports oil: dearer crude raises dollar demand (dcoilwtico → usd_inr_reference_rate)
- **vix_to_usdinr**: Risk-off episodes weaken the rupee (vixcls → usd_inr_reference_rate)
- **repo_to_bank_interest_expense**: Higher policy rate raises banks' funding cost (policy_repo_rate → HDFCBANK, ICICIBANK, SBIN, KOTAKBANK, AXISBANK interest_expended)
- **repo_to_bank_interest_income**: Higher policy rate raises banks' lending yields (policy_repo_rate → HDFCBANK, ICICIBANK, SBIN, KOTAKBANK, AXISBANK interest_earned)
- **repo_to_auto_revenue**: Dearer vehicle finance dampens auto demand (policy_repo_rate → MARUTI, M&M, HEROMOTOCO, EICHERMOT, BAJAJAUTO total_revenue)
- **bankcredit_to_auto_revenue**: Credit availability supports vehicle purchases (bank_credit → MARUTI, M&M, HEROMOTOCO, EICHERMOT, BAJAJAUTO total_revenue)
- **usdinr_to_it_revenue**: A weaker rupee lifts IT services' rupee revenue (usd_inr_reference_rate → TCS, INFY, WIPRO, HCLTECH, TECHM total_revenue)
- **oil_to_indigo_margin**: Fuel is an airline's largest variable cost (dcoilwtico → INDIGO margin)
- **oil_to_asianpaints_margin**: Crude derivatives are key paint inputs (dcoilwtico → ASIANPAINT margin)
