"""Synthetic data generator: network, medicine catalogue, demand and disruption scenarios.

Everything is reproducible from (seed, scenario). The same draws are used for every
policy, so policies are compared on identical demand and identical disruptions.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import Config, DEFAULT

# site sizes loosely follow a mix of large, medium and small facilities (assumption)
_SITE_SIZES = [2.0, 1.6, 1.2, 1.0, 0.8, 0.6, 0.5, 0.4, 0.4, 0.3]


def make_network(cfg: Config = DEFAULT, seed: int = 0) -> pd.DataFrame:
    """One row per site. `size` scales demand."""
    sizes = _SITE_SIZES[: cfg.n_sites]
    xy = np.random.default_rng(4242 + seed).uniform(0, cfg.map_size_km, size=(cfg.n_sites, 2))
    return pd.DataFrame({
        "site_id": [f"S{i + 1:02d}" for i in range(cfg.n_sites)],
        "size": sizes,
        "owner_group": "G1",           # single owner: starting segment (report s.4)
        "cold_chain": True,
        "x_km": xy[:, 0].round(1),
        "y_km": xy[:, 1].round(1),
    })


def distance_matrix(net: pd.DataFrame) -> np.ndarray:
    p = net[["x_km", "y_km"]].to_numpy()
    return np.sqrt(((p[:, None, :] - p[None, :, :]) ** 2).sum(-1))


def make_catalogue(cfg: Config = DEFAULT, seed: int = 0) -> pd.DataFrame:
    """One row per medicine."""
    rng = np.random.default_rng(10_000 + seed)
    n = cfg.n_items
    n_chronic = round(n * cfg.share_chronic)
    n_seasonal = round(n * cfg.share_seasonal)
    kinds = (["chronic"] * n_chronic + ["seasonal"] * n_seasonal)
    kinds += ["intermittent"] * (n - len(kinds))

    imported = np.zeros(n, dtype=bool)
    imported[rng.permutation(n)[: round(n * cfg.imported_share)]] = True

    base_rate = np.where(
        np.array(kinds) == "intermittent", 1.0,
        rng.lognormal(mean=np.log(6.0), sigma=0.6, size=n))     # units/day at size 1
    # intermittent items are costly, others cheap to moderate (assumption)
    unit_cost = np.where(
        np.array(kinds) == "intermittent",
        rng.uniform(20, 120, n), rng.lognormal(np.log(2.0), 0.8, n))
    unit_cost = (unit_cost * cfg.price_scale).round(2)
    return pd.DataFrame({
        "item_id": [f"M{i + 1:03d}" for i in range(n)],
        "kind": kinds,
        "imported": imported,
        "base_rate": base_rate,
        "unit_cost": unit_cost,
        "criticality": rng.choice([1, 2, 3], size=n, p=[0.4, 0.4, 0.2]),
        "shelf_life_days": rng.integers(cfg.shelf_life_min_days, cfg.shelf_life_max_days + 1, n),
        "pack_size": rng.integers(cfg.pack_size_min, cfg.pack_size_max + 1, n),
        "controlled": False,           # controlled drugs are excluded by design
    })


@dataclass
class Scenario:
    name: str
    seed: int
    demand: np.ndarray        # (warmup+horizon, sites, items), integer units
    lead_noise: np.ndarray    # (horizon, sites, items), extra days of lead time
    arrival_frac: np.ndarray  # (horizon, sites, items), shelf life fraction on arrival
    delay_days: int
    delay_window: tuple       # (start, end) day indexes, within horizon
    spike_window: tuple
    spike_pct: float
    spike_items: np.ndarray   # bool mask over items
    site_mask: np.ndarray     # (sites,) bool: sites hit by the disruption
    stale_mask: np.ndarray    # (sites, items) bool: record refreshes only weekly
    stale_offset: np.ndarray  # (sites, items) day offset of the weekly refresh


def make_scenario(name: str, seed: int, cfg: Config = DEFAULT) -> Scenario:
    """Draw demand and disruptions. Same (name-independent) base draws for a given seed.

    The base demand uses only `seed`, so scenarios 'normal', 'delay', 'spike' and
    'combined' share the same underlying randomness and differ only in the disruption.
    """
    spec = cfg.scenarios[name]
    delay_days, spike_pct, stale_frac = spec["delay"], spec["spike"], spec["stale"]
    share = spec.get("share", 1.0)
    net = make_network(cfg)
    cat = make_catalogue(cfg, seed=0)          # catalogue fixed across runs
    rng = np.random.default_rng(seed)
    total = cfg.warmup_days + cfg.horizon_days
    t = np.arange(total)

    size = net["size"].to_numpy()[None, :, None]            # (1,S,1)
    base = cat["base_rate"].to_numpy()[None, None, :]       # (1,1,I)
    kind = cat["kind"].to_numpy()
    season = np.where(kind == "seasonal",
                      1 + cfg.seasonal_amplitude * np.cos(2 * np.pi * (t[:, None] - 20) / 365),
                      1.0)                                   # (T,I)
    mean = base * size * season[:, None, :]                  # (T,S,I)

    # disruption windows are drawn from the seed, identical across scenarios
    start = int(rng.integers(cfg.disruption_start_min, cfg.disruption_start_max))
    window = (start, start + cfg.disruption_len_days)
    spike_items = rng.random(cat.shape[0]) < 0.5
    u = rng.random(mean.shape)
    lead_noise = rng.integers(0, cfg.lead_time_noise_days + 1,
                              size=(cfg.horizon_days, cfg.n_sites, cfg.n_items))
    arrival_frac = rng.uniform(cfg.arrival_shelf_frac_min, cfg.arrival_shelf_frac_max,
                               size=(cfg.horizon_days, cfg.n_sites, cfg.n_items))
    short = rng.random(arrival_frac.shape) < cfg.short_dated_prob
    arrival_frac = np.where(short, rng.uniform(cfg.short_dated_frac_min, cfg.short_dated_frac_max,
                                               size=arrival_frac.shape), arrival_frac)

    # which sites are hit: separate stream, so the base draws do not change
    k = max(1, round(share * cfg.n_sites))
    order = np.random.default_rng(seed + 5151).permutation(cfg.n_sites)
    site_mask = np.zeros(cfg.n_sites, dtype=bool)
    site_mask[order[:k]] = True
    base_mean = mean
    spiked_mean = mean.copy()
    if spike_pct > 0:
        s, e = cfg.warmup_days + window[0], cfg.warmup_days + window[1]
        sub = spiked_mean[s:e]
        sub[:, site_mask[:, None] & spike_items[None, :]] *= (1 + spike_pct)

    # Common random numbers: the baseline draw is identical in every scenario;
    # a spike only ADDS extra demand from a separate stream.
    is_int = (kind == "intermittent")[None, None, :]
    base_rng = np.random.default_rng(seed + 777)
    extra_rng = np.random.default_rng(seed + 999)
    regular = base_rng.poisson(base_mean) + extra_rng.poisson(spiked_mean - base_mean)

    # intermittent items: demand occurs with probability p (uniform u fixed per seed),
    # so a spike (larger p) can only add occurrence days, never remove them.
    qty = np.maximum(1, base_rng.poisson(cfg.intermittent_mean, size=mean.shape))
    p_spk = np.clip(cfg.intermittent_prob * size * (spiked_mean / np.maximum(base_mean, 1e-9)), 0, 0.95)
    occurs = u < p_spk
    intermittent = occurs * qty
    demand = np.where(is_int, intermittent, regular).astype(np.int64)

    st_rng = np.random.default_rng(seed + 31337)      # separate stream: other draws unchanged
    stale_mask = st_rng.random((cfg.n_sites, cfg.n_items)) < stale_frac
    stale_offset = st_rng.integers(0, cfg.stale_period_days, (cfg.n_sites, cfg.n_items))
    return Scenario(name, seed, demand, lead_noise, arrival_frac, delay_days,
                    window, window, spike_pct, spike_items, site_mask, stale_mask, stale_offset)
