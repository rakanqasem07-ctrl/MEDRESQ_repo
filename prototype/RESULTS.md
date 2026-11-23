# MEDRESQ simulation results (all SIMULATED, synthetic data)

Setup: 8 sites, 30 medicines, 2 simulated years, 100 runs per scenario on seeds 1000 to 1099.
Design and tuning used seeds 0 to 9, so these runs are out of sample for the design.
Costs in JD under the assumptions in `config.py` (shortage penalty, transfer cost and expiry
are assumptions, not Jordanian data). Mean total operating cost = shortage penalty + expiry loss
+ ordering + holding + transfers.

## Main comparison (total cost, JD)

| Scenario | A site by site | B manual transfers | C MEDRESQ | C vs A (paired, 95% interval) | Stockout days A to C | Expiry loss A to C |
|---|---|---|---|---|---|---|
| Normal | 516,464 | 636,860 | 413,543 | -19.9% (+-0.7) | 3,055 to 2,594 | 9,297 to 3,825 |
| Delay, all sites | 679,071 | 799,010 | 517,057 | -23.9% (+-0.6) | 4,416 to 3,470 | 9,304 to 3,912 |
| Spike, all sites | 546,056 | 669,213 | 440,258 | -19.4% (+-0.7) | 3,207 to 2,729 | 9,118 to 3,920 |
| Delay, 3 of 8 sites | 576,356 | 696,530 | 448,158 | -22.2% (+-0.8) | 3,568 to 2,859 | 9,293 to 3,848 |
| Spike, 3 of 8 sites | 540,241 | 660,173 | 432,377 | -20.0% (+-0.7) | 3,155 to 2,667 | 9,297 to 3,830 |
| Delay and spike, 3 of 8 | 612,859 | 730,842 | 480,453 | -21.6% (+-0.8) | 3,763 to 3,043 | 9,303 to 3,869 |
| Stale records, 15% | 577,531 | 719,802 | 450,021 | -22.1% (+-0.7) | 3,534 to 2,815 | 10,178 to 5,280 |

C had lower total cost than A in 100 of 100 runs in every scenario.
Policy B (rule-of-thumb phone calls) was 17% to 25% WORSE than A: uncoordinated manual
transfers drained donors (about 40 donor stockouts per run, 34 infeasible transfers per run).

## What drives the gain (ablation, total cost, JD, 20 runs)

| Variant | Normal | Local delay+spike | Stale records | Verdict |
|---|---|---|---|---|
| C full | 412,994 | 475,763 | 447,543 | reference |
| no transfers | 475,489 (+15%) | 545,349 (+15%) | 526,582 (+18%) | network optimisation earns its place |
| no adaptive lead time | 442,608 (+7%) | 516,337 (+9%) | 478,220 (+7%) | earns its place |
| forecast MA28 instead of shock-aware | 437,364 (+6%) | 498,358 (+5%) | 469,026 (+5%) | earns its place |
| no safe-to-recommend gate | 412,994 (0%) | 475,763 (0%) | 478,462 (+7%) | matters only when records are stale: 198 infeasible transfers and 305 donor stockouts per run without it, 0 and 12 with it |
| no expiry rescue | 404,724 (-2%) | 468,220 (-1.6%) | 444,949 (-0.6%) | does NOT pay for itself in cost; keep only if expiry matters more than cost |
| no priority service levels | 400,356 (-3%) | 460,509 (-3%) | 438,314 (-2%) | costs about 3%, buys 25% fewer stockout days on critical medicines (548 to 409) |
| no donor reserve | 435,987 (+6%) | 453,804 (-4.6%) | 447,595 (0%) | removes 22% of stockout days but makes donor-site stockouts about 90 times more frequent (868 versus 10 per run): a guardrail, not a cost saver |

Honest reading: in the normal scenario, the variant without transfers already beats A by about 8%,
so roughly 60% of the saving (about 62,000 of 103,000 JD) comes from network transfers and the rest
from forecasting, adaptive lead times and priorities. The gain is similar in systemic shocks and local
shocks, so most of the value is routine rebalancing, not only crisis response.

## Sensitivity: fixed cost per transfer (C total cost versus A)

| Fixed cost | Normal | Delay and spike, 3 of 8 sites |
|---|---|---|
| 0 JD | 387,708 | 448,613 |
| 5 JD | 401,240 | 460,691 |
| 10 JD (assumed) | 412,994 | 475,763 |
| 20 JD | 431,498 | 491,838 |
| 40 JD | 457,261 (A: 517,403) | 516,691 (A: 602,238) |

C stays cheaper than A even at four times the assumed fixed cost.

## Decision gates (report section 9)

| Gate | Result |
|---|---|
| Hard-constraint integrity | 0 infeasible transfers with fresh records in all runs; 0 with the gate under stale records (198 per run without it) |
| Traceability | Every recommendation carries inputs, constraints, cost, alternative and a template reason; every decision is logged |
| Comparative value | Total cost interval excludes zero in all 7 scenarios |
| Trade-off guardrail | Donor stockouts within 7 days of sending: 0.5% of transfers; smallest-site service level rises from 0.979 to 0.982 (normal) |
| Reproducibility | Same seed gives the same result (tested) |

## Limits you must state

* No real data: expiry, prices, shortage penalty and transfer cost are assumptions.
* Transfers assume a single owner and no legal barrier (report section 7: unconfirmed).
* Stylised disruptions; no emergency purchasing, substitutions or cold-chain differences.
* A and B are simple baselines; a well-run real network may beat A. The 20% is against A, not against every real practice.
* Forecast: gradient boosting had the lowest error (0.126 versus 0.131 for a 28-day mean) but the shock-aware mean was kept because it reacts to surges and is auditable.
* Expiry rescue and priority levels are not cost-saving here; they are kept for other goals.
