"""MEDRESQ simulation parameters.

Every number here is a DESIGN ASSUMPTION (see report section 9), not a sourced fact.
Change a value here and every part of the simulation follows.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    # --- network -----------------------------------------------------------
    n_sites: int = 8                 # report: 6 to 10 simulated sites
    n_items: int = 30                # report: 20 to 40 synthetic medicines
    horizon_days: int = 730          # 2 years: shelf life is 12 to 36 months, so expiry needs a long horizon
    warmup_days: int = 56            # history the policy sees before day 0 (longest window used)

    # --- demand ------------------------------------------------------------
    # demand types and their share of the catalogue
    share_chronic: float = 0.50      # steady demand
    share_seasonal: float = 0.30     # respiratory / antibiotics, winter peak
    share_intermittent: float = 0.20 # low volume, high cost, many zero days
    seasonal_amplitude: float = 0.40
    intermittent_prob: float = 0.15  # chance of any demand on a given day
    intermittent_mean: float = 4.0   # mean units when demand occurs

    # --- stock -------------------------------------------------------------
    shelf_life_min_days: int = 365   # 12 months
    shelf_life_max_days: int = 1080  # 36 months
    arrival_shelf_frac_min: float = 0.50   # deliveries arrive with 50-90% left
    arrival_shelf_frac_max: float = 0.90
    imported_share: float = 0.50     # share of items that are imported
    short_dated_prob: float = 0.08   # share of deliveries that arrive short-dated (assumption)
    short_dated_frac_min: float = 0.05
    short_dated_frac_max: float = 0.25
    pack_size_min: int = 10          # orders are rounded up to whole packs (assumption)
    pack_size_max: int = 60
    lead_time_local: int = 3         # days
    lead_time_imported: int = 14     # days
    lead_time_noise_days: int = 2    # +0..2 days random

    # --- policy A (site by site) -------------------------------------------
    service_z: float = 1.65          # about 95% cycle service level
    review_cover_days: int = 14      # order-up-to = ROP + this many days demand
    history_window: int = 28         # days of history the forecast uses

    # --- costs (JD, assumed) -----------------------------------------------
    price_scale: float = 1.0         # multiplies every unit price (so you can plug in real prices)
    order_fixed_cost: float = 15.0   # per purchase order line
    holding_rate_per_day: float = 0.0002   # fraction of unit cost per unit-day
    # shortage penalty per unit short = unit_cost * multiplier[criticality]
    shortage_multiplier: tuple = (3.0, 6.0, 10.0)  # criticality 1, 2, 3

    # --- transfers (assumptions) ---------------------------------------------
    transfer_fixed_cost: float = 10.0      # JD per transfer (handling, paperwork)
    transfer_unit_km_cost: float = 0.002   # JD per unit per km
    transfer_fast_km: float = 40.0         # within this distance: 1 day, else 2 days
    transfer_min_shelf_days: int = 30      # donor batch must have this much life left on arrival
    donor_cover_days: int = 21             # donor keeps this many days of cover after sending
    map_size_km: float = 80.0

    # --- records (stale-data scenario) -----------------------------------------
    stale_period_days: int = 7             # a stale record refreshes only this often

    # --- scenarios ---------------------------------------------------------
    # name -> dict(delay days on imported items, spike pct, share of stale records)
    scenarios: dict = field(default_factory=lambda: {
        # systemic: every site is hit (nothing to pool); local: only `share` of the sites
        "normal":      dict(delay=0,  spike=0.0, stale=0.0,  share=1.0),
        "delay":       dict(delay=21, spike=0.0, stale=0.0,  share=1.0),   # report: 2 to 4 weeks, imported items
        "spike":       dict(delay=0,  spike=0.6, stale=0.0,  share=1.0),   # report: 30% to 100%
        "local_delay": dict(delay=21, spike=0.0, stale=0.0,  share=0.375), # 3 of 8 sites
        "local_spike": dict(delay=0,  spike=1.0, stale=0.0,  share=0.375),
        "local_both":  dict(delay=21, spike=1.0, stale=0.0,  share=0.375),
        "stale":       dict(delay=0,  spike=0.0, stale=0.15, share=1.0),   # report SH3: stale records
    })
    disruption_start_min: int = 60
    disruption_start_max: int = 600
    disruption_len_days: int = 18


DEFAULT = Config()
