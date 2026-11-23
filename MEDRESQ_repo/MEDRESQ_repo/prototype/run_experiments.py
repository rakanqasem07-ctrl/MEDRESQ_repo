"""Full experiments (report section 9). Usage: python3 run_experiments.py [main_runs] [ablation_runs]

Seeds 1000+ are used for evaluation. All design and tuning was done on seeds 0 to 9,
so these runs are out of sample for the design choices. Everything is SIMULATED.
Writes outputs/main_runs.csv, ablation_runs.csv, sensitivity_runs.csv and summaries.
"""
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

from config import Config, DEFAULT
from data_gen import make_scenario
from policies import PolicyA, PolicyB, PolicyC
from sim import Simulation

EVAL_SEED0 = 1000
MAIN_SCENARIOS = list(DEFAULT.scenarios)
ABLATIONS = {
    "C_full": {},
    "C_no_transfers": dict(transfers=False),
    "C_no_adaptive_lead": dict(adaptive_lead=False),
    "C_no_priority_z": dict(priority_z=False),
    "C_no_gate": dict(gate=False),
    "C_no_donor_reserve": dict(donor_reserve=False),
    "C_no_expiry_rescue": dict(expiry=False),
    "C_forecast_ma28": dict(forecast_method="ma28"),
}
ABLATION_SCENARIOS = ["normal", "local_both", "stale"]
SENS_SCENARIOS = ["normal", "local_both"]
SENS_FIXED = [0.0, 5.0, 10.0, 20.0, 40.0]       # JD fixed cost per transfer


def _row(m, **kw):
    r = m.as_row()
    r.update(kw)
    return r


def run_main(args):
    scen, seed, pol = args
    sc = make_scenario(scen, seed)
    p = {"A": PolicyA, "B": PolicyB, "C": PolicyC}[pol]()
    name = {"A": "A_site_by_site", "B": "B_manual_transfers", "C": "C_medresq"}[pol]
    return _row(Simulation(sc, p).run(), policy=name, scenario=scen, seed=seed)


def run_ablation(args):
    scen, seed, name = args
    sc = make_scenario(scen, seed)
    return _row(Simulation(sc, PolicyC(label=name, **ABLATIONS[name])).run(),
                policy=name, scenario=scen, seed=seed)


def run_sens(args):
    scen, seed, fixed, pol = args
    cfg = Config(transfer_fixed_cost=fixed)
    sc = make_scenario(scen, seed, cfg)
    p = PolicyA(cfg) if pol == "A" else PolicyC(cfg)
    return _row(Simulation(sc, p, cfg).run(), policy=pol, scenario=scen, seed=seed,
                fixed_cost=fixed)


def summarise(df, by):
    cols = ["stockout_days", "service_level", "min_site_service", "expiry_loss",
            "shortage_penalty", "transfer_cost", "total_cost", "n_transfers",
            "donor_stockouts", "infeasible_transfers", "abstentions", "stockout_days_crit3"]
    g = df.groupby(by)[cols]
    mean, sd, n = g.mean(), g.std(ddof=1), g.size()
    out = mean.copy()
    for k in cols:
        out[f"{k}_ci95"] = 1.96 * sd[k] / np.sqrt(n)
    out["runs"] = n
    return out.reset_index()


def paired(df, base="A_site_by_site", other="C_medresq", metric="total_cost"):
    """Paired difference (other - base) per scenario with a 95% CI over seeds."""
    rows = []
    for scen, g in df.groupby("scenario"):
        a = g[g.policy == base].set_index("seed")[metric]
        b = g[g.policy == other].set_index("seed")[metric]
        d = (b - a).dropna()
        rows.append(dict(scenario=scen, metric=metric, base_mean=a.mean(), other_mean=b.mean(),
                         diff_mean=d.mean(), diff_pct=100 * d.mean() / a.mean(),
                         ci95=1.96 * d.std(ddof=1) / np.sqrt(len(d)),
                         wins=int((d < 0).sum()), runs=len(d)))
    return pd.DataFrame(rows)


if __name__ == "__main__":
    n_main = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    n_abl = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    seeds = range(EVAL_SEED0, EVAL_SEED0 + n_main)
    t0 = time.time()
    with Pool() as pool:
        jobs = [(s, sd, p) for s in MAIN_SCENARIOS for sd in seeds for p in "ABC"]
        main = pd.DataFrame(pool.map(run_main, jobs, chunksize=3))
        main.to_csv("outputs/main_runs.csv", index=False)
        summarise(main, ["policy", "scenario"]).to_csv("outputs/main_summary.csv", index=False)
        for m in ("total_cost", "stockout_days", "expiry_loss"):
            pass
        pd.concat([paired(main, metric=m) for m in
                   ("total_cost", "stockout_days", "expiry_loss", "shortage_penalty")]
                  ).to_csv("outputs/paired_C_vs_A.csv", index=False)
        print(f"main done {time.time() - t0:.0f}s", flush=True)

        aseeds = range(EVAL_SEED0, EVAL_SEED0 + n_abl)
        jobs = [(s, sd, n) for s in ABLATION_SCENARIOS for sd in aseeds for n in ABLATIONS]
        abl = pd.DataFrame(pool.map(run_ablation, jobs, chunksize=3))
        abl.to_csv("outputs/ablation_runs.csv", index=False)
        summarise(abl, ["policy", "scenario"]).to_csv("outputs/ablation_summary.csv", index=False)
        print(f"ablation done {time.time() - t0:.0f}s", flush=True)

        sseeds = range(EVAL_SEED0, EVAL_SEED0 + min(n_abl, 20))
        jobs = [(s, sd, f, "C") for s in SENS_SCENARIOS for sd in sseeds for f in SENS_FIXED]
        jobs += [(s, sd, 10.0, "A") for s in SENS_SCENARIOS for sd in sseeds]
        sens = pd.DataFrame(pool.map(run_sens, jobs, chunksize=3))
        sens.to_csv("outputs/sensitivity_runs.csv", index=False)
        summarise(sens, ["policy", "scenario", "fixed_cost"]).to_csv(
            "outputs/sensitivity_summary.csv", index=False)
        print(f"sensitivity done {time.time() - t0:.0f}s", flush=True)
