# Macro edge pilot results

Generated 2026-10-02 by `python -m scripts.macro_edge_pilot` (read-only; no database writes, no LLM). Candidate edges are hand-written from domain knowledge. **Nothing here is a causal claim**: each row is direction + lag + significance of an association over a short history (ADR-009: connectivity and correlation are evidence for investigation, not proof).

Method: both series as YoY changes; effect series z-scored per company and pooled; cause leads effect by the lag; best lag by |r|; p-value from a circular-shift permutation test that repeats the lag search (so it already accounts for lag-picking and autocorrelation). Classified at p < 0.05; INSUFFICIENT_DATA below 12 distinct periods.


## India (RBI series + Indian filings, FY2023 onward: 9-17 quarters)

| Edge | Expected | Periods | Best lag | r | p (adj.) | Result | Ex-2020/21: r | Ex-2020/21 result |
|---|---|---|---|---|---|---|---|---|
| repo_to_base_rate | + | 96 | 3 mo | +0.94 | 0.000 | DIRECTION_SUPPORTED | +0.95 | DIRECTION_SUPPORTED |
| repo_to_gsec10 | + | 96 | 0 mo | +0.61 | 0.142 | NOT_DETECTED | +0.52 | NOT_DETECTED |
| repo_to_tbill91 | + | 96 | 0 mo | +0.97 | 0.000 | DIRECTION_SUPPORTED | +0.97 | DIRECTION_SUPPORTED |
| repo_to_bank_credit | − | 96 | 10 mo | +0.71 | 0.301 | NOT_DETECTED | +0.52 | NOT_DETECTED |
| repo_to_m3 | − | 96 | 0 mo | -0.34 | 0.243 | NOT_DETECTED | -0.23 | NOT_DETECTED |
| us10y_to_gsec10 | + | 96 | 0 mo | +0.81 | 0.000 | DIRECTION_SUPPORTED | +0.79 | DIRECTION_SUPPORTED |
| dxy_to_usdinr | + | 125 | 0 mo | +0.46 | 0.349 | NOT_DETECTED | +0.37 | NOT_DETECTED |
| oil_to_usdinr | + | 125 | 0 mo | -0.22 | 0.804 | NOT_DETECTED | +0.21 | NOT_DETECTED |
| vix_to_usdinr | + | 125 | 1 mo | +0.42 | 0.182 | NOT_DETECTED | +0.51 | NOT_DETECTED |
| repo_to_bank_interest_expense | + | 9 | n/a | n/a | n/a | INSUFFICIENT_DATA | n/a | INSUFFICIENT_DATA |
| repo_to_bank_interest_income | + | 9 | n/a | n/a | n/a | INSUFFICIENT_DATA | n/a | INSUFFICIENT_DATA |
| repo_to_auto_revenue | − | 13 | 6 qtr | -0.52 | 0.767 | NOT_DETECTED | -0.52 | NOT_DETECTED |
| bankcredit_to_auto_revenue | + | 13 | 3 qtr | -0.65 | 0.750 | NOT_DETECTED | -0.65 | NOT_DETECTED |
| usdinr_to_it_revenue | + | 12 | 0 qtr | +0.70 | 0.000 | DIRECTION_SUPPORTED | +0.70 | DIRECTION_SUPPORTED |
| oil_to_indigo_margin | − | 12 | 6 qtr | +0.78 | 0.260 | NOT_DETECTED | +0.78 | NOT_DETECTED |
| oil_to_asianpaints_margin | − | 12 | 1 qtr | -0.71 | 0.535 | NOT_DETECTED | -0.71 | NOT_DETECTED |

## United States (FRED series + SEC filings, FY2008 onward: ~66 quarters)

| Edge | Expected | Periods | Best lag | r | p (adj.) | Result | Ex-2020/21: r | Ex-2020/21 result |
|---|---|---|---|---|---|---|---|---|
| us_fedfunds_to_dgs2 | + | 592 | 0 mo | +0.85 | 0.000 | DIRECTION_SUPPORTED | +0.85 | DIRECTION_SUPPORTED |
| us_fedfunds_to_mortgage | + | 654 | 1 mo | +0.65 | 0.000 | DIRECTION_SUPPORTED | +0.65 | DIRECTION_SUPPORTED |
| us_dgs10_to_mortgage | + | 654 | 1 mo | +0.91 | 0.000 | DIRECTION_SUPPORTED | +0.91 | DIRECTION_SUPPORTED |
| us_fedfunds_to_loans | − | 633 | 4 mo | +0.47 | 0.027 | DIRECTION_CONTRADICTED | +0.47 | DIRECTION_CONTRADICTED |
| us_fedfunds_to_housing_starts | − | 800 | 7 mo | -0.46 | 0.000 | DIRECTION_SUPPORTED | -0.46 | DIRECTION_SUPPORTED |
| us_oil_to_cpi | + | 942 | 1 mo | +0.55 | 0.000 | DIRECTION_SUPPORTED | +0.52 | DIRECTION_SUPPORTED |
| us_fedfunds_to_bank_interest_expense | + | 68 | 0 qtr | +0.86 | 0.000 | DIRECTION_SUPPORTED | +0.88 | DIRECTION_SUPPORTED |
| us_fedfunds_to_bank_interest_income | + | 68 | 0 qtr | +0.88 | 0.000 | DIRECTION_SUPPORTED | +0.87 | DIRECTION_SUPPORTED |
| us_curve_to_bank_profit | + | 68 | 0 qtr | +0.16 | 0.219 | NOT_DETECTED | +0.12 | NOT_DETECTED |
| us_mortgage_to_homebuilder_revenue | − | 65 | 4 qtr | -0.27 | 0.294 | NOT_DETECTED | -0.26 | NOT_DETECTED |
| us_mortgage_to_home_retail_revenue | − | 68 | 4 qtr | -0.38 | 0.037 | DIRECTION_SUPPORTED | -0.32 | NOT_DETECTED |
| us_oil_to_airline_margin | − | 70 | 0 qtr | +0.55 | 0.000 | DIRECTION_CONTRADICTED | +0.06 | NOT_DETECTED |
| us_oil_to_producer_revenue | + | 70 | 0 qtr | +0.78 | 0.000 | DIRECTION_SUPPORTED | +0.69 | DIRECTION_SUPPORTED |
| us_dollar_to_multinational_revenue | − | 69 | 0 qtr | -0.42 | 0.000 | DIRECTION_SUPPORTED | -0.41 | DIRECTION_SUPPORTED |
| us_fedfunds_to_auto_revenue | − | 68 | 0 qtr | +0.38 | 0.000 | DIRECTION_CONTRADICTED | +0.41 | DIRECTION_CONTRADICTED |
| us_sentiment_to_retail_revenue | + | 69 | 1 qtr | -0.27 | 0.088 | NOT_DETECTED | -0.19 | NOT_DETECTED |
| us_indpro_to_industrial_revenue | + | 68 | 0 qtr | +0.35 | 0.000 | DIRECTION_SUPPORTED | +0.33 | DIRECTION_SUPPORTED |
| us_cpi_to_retail_revenue | + | 69 | 1 qtr | +0.35 | 0.124 | NOT_DETECTED | +0.50 | DIRECTION_SUPPORTED |

## Reading the 'Ex-2020/21' columns

The same test with effect periods in 2020-21 dropped. A common shock (COVID: demand, oil and rates all collapsing and rebounding together) can create a strong association between series with no mechanism linking them, so an edge that is SUPPORTED or CONTRADICTED only with 2020-21 included deserves suspicion. Wrong-signed results that disappear ex-2020/21 are most likely this artefact, not evidence against the mechanism.

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
- **us_fedfunds_to_dgs2**: Policy rate anchors 2Y Treasury yields (fedfunds → dgs2)
- **us_fedfunds_to_mortgage**: Policy rate feeds mortgage rates (fedfunds → mortgage30us)
- **us_dgs10_to_mortgage**: Mortgage rates price off the 10Y Treasury (dgs10 → mortgage30us)
- **us_fedfunds_to_loans**: Dearer money slows bank lending (fedfunds → totll)
- **us_fedfunds_to_housing_starts**: Higher rates depress housing starts (fedfunds → houst)
- **us_oil_to_cpi**: Energy prices feed consumer inflation (dcoilwtico → cpiaucsl)
- **us_fedfunds_to_bank_interest_expense**: Higher policy rate raises banks' funding cost (fedfunds → JPM, BAC, WFC, C, USB interest_expended)
- **us_fedfunds_to_bank_interest_income**: Higher policy rate raises banks' asset yields (fedfunds → JPM, BAC, WFC, C, USB interest_earned)
- **us_curve_to_bank_profit**: A steeper curve widens bank net interest margins (t10y2y → JPM, BAC, WFC, C, USB net_profit)
- **us_mortgage_to_homebuilder_revenue**: Higher mortgage rates cut homebuyer demand (mortgage30us → DHI, LEN, PHM, NVR total_revenue)
- **us_mortgage_to_home_retail_revenue**: Housing turnover drives home-improvement spend (mortgage30us → HD, LOW total_revenue)
- **us_oil_to_airline_margin**: Fuel is an airline's largest variable cost (dcoilwtico → DAL, UAL, LUV margin)
- **us_oil_to_producer_revenue**: Producers' revenue follows crude prices (dcoilwtico → COP, OXY, CVX total_revenue)
- **us_dollar_to_multinational_revenue**: A stronger dollar shrinks translated foreign revenue (dtwexbgs → KO, PG, MMM, CAT total_revenue)
- **us_fedfunds_to_auto_revenue**: Dearer auto loans dampen vehicle demand (fedfunds → F, GM total_revenue)
- **us_sentiment_to_retail_revenue**: Consumer sentiment leads discretionary spend (umcsent → WMT, COST, HD, LOW total_revenue)
- **us_indpro_to_industrial_revenue**: Industrial output drives equipment and materials demand (indpro → CAT, MMM total_revenue)
- **us_cpi_to_retail_revenue**: Inflation lifts nominal retail revenue (cpiaucsl → WMT, COST total_revenue)
