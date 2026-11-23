# MEDRESQ prototype

Software-only prototype of an AI agent that predicts medicine shortages across a network of
sites and recommends transfers and orders for a pharmacist to approve.
**All data are synthetic and every number is SIMULATED.** Assumptions live in `config.py`.
Controlled drugs are excluded by design. No patient data is used.

## Quick start (one click)

* Windows: double-click `START_Windows.bat`. Mac or Linux: double-click `START_Mac_Linux.command`.
* Needs Python 3.10+ installed (python.org, tick "Add to PATH"). First run downloads the libraries (internet, 1 to 3 minutes); later runs start in seconds. The browser opens by itself.
* Or by hand: `python3 launch.py`.

## Run it by hand

```bash
pip install numpy pandas scipy scikit-learn flask matplotlib     # nothing else needed
python3 run_experiments.py 100 20     # full Monte Carlo (about 45 min); skip to use shipped outputs/
python3 forecast.py                   # forecast benchmark (5 s)
python3 app.py                        # demo app at http://127.0.0.1:5000
python3 -m unittest discover -s tests -v    # 46 tests
```

## What is where

| File | What it does |
|---|---|
| `config.py` | Every parameter and cost assumption in one place |
| `data_gen.py` | Network (8 sites), catalogue (30 medicines), demand and disruption scenarios. Same seed gives the same data; scenarios share random draws |
| `sim.py` | Day-by-day simulator: batch-level stock, earliest expiry first, true stock versus recorded stock, transfers, costs, metrics |
| `forecast.py` | Forecast candidates and a rolling-origin benchmark (baselines versus gradient boosting) |
| `policies.py` | A: site by site. B: manual transfers. C: MEDRESQ (forecast, risk, network optimisation, safe-to-recommend gate, explanations) with switches for the ablation study |
| `run_experiments.py` | Main comparison, ablation study, sensitivity analysis, paired statistics |
| `app.py`, `static/index.html` | Demo app: Network view, Recommendations (approve, reject, override), What-if, Savings, Audit log |
| `tests/` | 46 tests: unit conservation, no future leakage, reproducibility, hard constraints, gate, app flow |

## The three policies

* **A, site by site** (the documented baseline): reorder point `ROP = d x L + z x sigma x sqrt(L)`, order up to ROP plus 14 days of demand. Each site decides alone.
* **B, manual transfers**: A plus rule-of-thumb phone calls: ask the first site that holds more than 45 days of cover. No optimisation, no cost comparison.
* **C, MEDRESQ**: the same ordering rule, plus
  1. forecast with shock detection (28-day mean that switches to the last 7 days only when they are clearly higher),
  2. adaptive lead time from overdue orders,
  3. higher service targets for critical medicines,
  4. network optimisation: a min-cost flow (HiGHS linear programme) over donors and recipients, with a cost-benefit test based on expected shortage avoided versus donor risk and transport cost,
  5. hard constraints: donors keep reserve stock, shelf life on arrival of at least 30 days, owner-approved transfers only,
  6. expiry rescue: move units that will not sell before they expire,
  7. safe-to-recommend gate: stale stock records block a recommendation ("manual review required"),
  8. template explanations filled from solver outputs (never free text).

## What the simulation does NOT do

* It is not validated against any real hospital. Expiry, price, transfer cost and shortage penalty are assumptions.
* No emergency purchasing, no substitutions, no cold-chain differences, no legal approval step beyond the human approval in the app.
* Disruptions are stylised (a delay window on imported items, a demand spike on a subset of medicines).
* Design and tuning used seeds 0 to 9. Reported results use seeds 1000 and above.
