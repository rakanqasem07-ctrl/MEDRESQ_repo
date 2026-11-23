"""Demand forecasting candidates and a rolling-origin benchmark (report section 8).

Every candidate maps a history array (T, sites, items) to a level (sites, items) =
expected daily demand over the next planning window. The benchmark compares them on
future 7-day mean demand, by demand type, with origins that never see the future.

Rule from the report: a machine-learning model must beat the simple baselines
out of sample, otherwise the baselines are used.
"""
import numpy as np

from config import Config, DEFAULT


# ----------------------------------------------------------------- candidates
def ma(hist, n):
    return hist[-n:].mean(axis=0)


def ewma(hist, span):
    h = hist[-max(span * 4, 8):]
    w = (1 - 2 / (span + 1)) ** np.arange(len(h) - 1, -1, -1)
    return np.tensordot(w / w.sum(), h, axes=(0, 0))


def seasonal_naive(hist, period=7):
    return hist[-period:].mean(axis=0)            # same as MA7 for non-weekly data


def shock_aware(hist, base_n=28, recent_n=7, k=2.0):
    """28-day mean, switching toward the last 7 days when they are clearly higher.

    The jump is accepted only if the recent mean exceeds the base by k standard errors,
    so ordinary Poisson noise does not move the forecast but a real surge does.
    """
    base = hist[-base_n:].mean(axis=0)
    sd = hist[-base_n:].std(axis=0)
    recent = hist[-recent_n:].mean(axis=0)
    se = np.maximum(sd, 1e-6) / np.sqrt(recent_n)
    surge = recent > base + k * se
    return np.where(surge, recent, base)


CANDIDATES = {
    "MA28": lambda h: ma(h, 28),
    "MA56": lambda h: ma(h, 56),
    "MA14": lambda h: ma(h, 14),
    "EWMA14": lambda h: ewma(h, 14),
    "EWMA28": lambda h: ewma(h, 28),
    "SeasonalNaive7": seasonal_naive,
    "ShockAware": shock_aware,
}
CANDIDATES["shock"] = shock_aware
CANDIDATES["ma28"] = CANDIDATES["MA28"]
CANDIDATES["ewma14"] = CANDIDATES["EWMA14"]


# ------------------------------------------------------------------ benchmark
def _features(hist, cfg: Config, t_abs, size, kind_code):
    """Features for the gradient-boosting candidate (all use past data only)."""
    f = [hist[-7:].mean(0), hist[-14:].mean(0), hist[-28:].mean(0), hist[-56:].mean(0),
         hist[-28:].std(0), np.broadcast_to(size, hist.shape[1:]),
         np.broadcast_to(kind_code, hist.shape[1:]),
         np.full(hist.shape[1:], np.cos(2 * np.pi * (t_abs - 20) / 365))]
    return np.stack([x.ravel() for x in f], axis=1)


def benchmark(scenarios=("normal", "spike"), seeds=range(3), train_seeds=range(100, 104),
              horizon=7, step=14, cfg: Config = DEFAULT):
    """Return a DataFrame: error (WAPE, lower is better) by method and demand type."""
    import pandas as pd
    from sklearn.ensemble import HistGradientBoostingRegressor
    from data_gen import make_catalogue, make_network, make_scenario

    cat = make_catalogue(cfg, 0)
    net = make_network(cfg)
    size = net["size"].to_numpy()[:, None]
    kind_code = cat["kind"].map({"chronic": 0, "seasonal": 1, "intermittent": 2}).to_numpy()[None, :]
    kinds = cat["kind"].to_numpy()
    w = cfg.warmup_days
    origins = range(w + 56, w + cfg.horizon_days - horizon, step)

    # ---- train the GBM on separate seeds (normal + spike), never on test seeds
    X, y = [], []
    for sc_name in ("normal", "spike"):
        for sd in train_seeds:
            sc = make_scenario(sc_name, sd, cfg)
            for o in origins:
                X.append(_features(sc.demand[:o], cfg, o - w, size, kind_code))
                y.append(sc.demand[o:o + horizon].mean(0).ravel())
    gbm = HistGradientBoostingRegressor(max_iter=150, learning_rate=0.08, max_depth=4,
                                        random_state=0).fit(np.vstack(X), np.concatenate(y))

    rows = []
    for sc_name in scenarios:
        for sd in seeds:
            sc = make_scenario(sc_name, sd, cfg)
            for o in origins:
                hist = sc.demand[:o]
                actual = sc.demand[o:o + horizon].mean(0)
                preds = {k: f(hist) for k, f in CANDIDATES.items() if k[0].isupper()}
                preds["GradientBoosting"] = gbm.predict(
                    _features(hist, cfg, o - w, size, kind_code)).reshape(actual.shape)
                for k, p in preds.items():
                    for kind in ("chronic", "seasonal", "intermittent"):
                        m = kinds == kind
                        rows.append((sc_name, k, kind, np.abs(p[:, m] - actual[:, m]).sum(),
                                     actual[:, m].sum()))
    df = pd.DataFrame(rows, columns=["scenario", "method", "kind", "abs_err", "actual"])
    g = df.groupby(["scenario", "method", "kind"])[["abs_err", "actual"]].sum()
    g["WAPE"] = g.abs_err / g.actual
    return g["WAPE"].unstack("kind").assign(all=df.groupby(["scenario", "method"]).abs_err.sum()
                                            / df.groupby(["scenario", "method"]).actual.sum())


if __name__ == "__main__":
    import pandas as pd
    pd.set_option("display.width", 200)
    res = benchmark()
    print(res.round(3).to_string())
    res.to_csv("outputs/forecast_benchmark.csv")
