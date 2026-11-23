"""Day-by-day inventory simulator with batch-level stock (earliest expiry first).

The simulator does not know which policy it runs. A policy is any object with
    decide(day, view) -> (orders, transfers)         # or just orders
`view` exposes only what a real site could know (recorded stock, pipeline, past
demand, overdue orders), never future demand and never the disruption schedule.
Ground truth (true stock, what really ships) stays inside the simulator.
"""
from dataclasses import dataclass, field

import numpy as np

from config import Config, DEFAULT
from data_gen import Scenario, distance_matrix, make_catalogue, make_network


@dataclass
class Order:
    site: int
    item: int
    qty: int


@dataclass
class Transfer:
    src: int
    dst: int
    item: int
    qty: int
    reason: str = ""


@dataclass
class Metrics:
    demand_units: float = 0.0
    unmet_units: float = 0.0
    stockout_days: int = 0            # site-item days with any unmet demand
    expiry_units: float = 0.0
    expiry_loss: float = 0.0          # JD, units x unit cost
    shortage_penalty: float = 0.0     # JD, assumed criticality-weighted
    order_cost: float = 0.0
    holding_cost: float = 0.0
    transfer_cost: float = 0.0
    n_orders: int = 0
    n_transfers: int = 0
    transfer_units: float = 0.0
    infeasible_transfers: int = 0     # shipped less than recommended (record was wrong)
    donor_stockouts: int = 0          # stockout at a donor within 7 days of sending
    abstentions: int = 0              # safe-to-recommend gate said "manual review"
    received_units: float = 0.0
    sent_units: float = 0.0
    served_units: float = 0.0
    initial_units: float = 0.0
    stockout_days_by_site: np.ndarray = None
    penalty_by_item: np.ndarray = None
    unmet_by_item: np.ndarray = None
    demand_by_site: np.ndarray = None
    unmet_by_site: np.ndarray = None
    stockout_days_by_crit: dict = field(default_factory=lambda: {1: 0, 2: 0, 3: 0})

    @property
    def service_level(self) -> float:
        return 1.0 - self.unmet_units / self.demand_units if self.demand_units else 1.0

    @property
    def total_cost(self) -> float:
        return (self.expiry_loss + self.shortage_penalty + self.order_cost
                + self.holding_cost + self.transfer_cost)

    def site_service(self) -> np.ndarray:
        return 1.0 - self.unmet_by_site / np.maximum(self.demand_by_site, 1)

    def as_row(self) -> dict:
        ss = self.site_service()
        return {
            "stockout_days": self.stockout_days,
            "service_level": self.service_level,
            "min_site_service": float(ss.min()),
            "expiry_loss": self.expiry_loss,
            "shortage_penalty": self.shortage_penalty,
            "order_cost": self.order_cost,
            "holding_cost": self.holding_cost,
            "transfer_cost": self.transfer_cost,
            "total_cost": self.total_cost,
            "n_orders": self.n_orders,
            "n_transfers": self.n_transfers,
            "transfer_units": self.transfer_units,
            "infeasible_transfers": self.infeasible_transfers,
            "donor_stockouts": self.donor_stockouts,
            "abstentions": self.abstentions,
            "stockout_days_crit3": self.stockout_days_by_crit[3],
        }


class View:
    """Read-only window a policy gets onto the simulation."""

    def __init__(self, sim: "Simulation"):
        self._s = sim

    @property
    def n_sites(self): return self._s.cfg.n_sites
    @property
    def n_items(self): return self._s.cfg.n_items
    @property
    def catalogue(self): return self._s.cat
    @property
    def network(self): return self._s.net
    @property
    def distance_km(self): return self._s.dist

    def on_hand(self) -> np.ndarray:
        """What the records SAY (stale for stale records)."""
        return self._s.recorded.copy()

    def record_age(self) -> np.ndarray:
        """Days since each record was last refreshed (0 = fresh today)."""
        return self._s.record_age.copy()

    def on_order(self) -> np.ndarray:
        return self._s.on_order.copy()

    def history(self, day: int, window: int) -> np.ndarray:
        """Past demand only: days [day-window, day), shape (window, sites, items)."""
        t = self._s.cfg.warmup_days + day
        if t - window < 0:
            raise ValueError(f"history window {window} exceeds available history {t}")
        return self._s.sc.demand[t - window: t]

    def lead_time(self, item: int) -> int:
        """Nominal (documented) lead time."""
        return self._s.expected_lead(item)

    def max_overdue_days(self, day: int) -> np.ndarray:
        """Per item: how many days late the most overdue open order is (order tracking)."""
        return self._s.overdue(day)

    def next_promised(self, day: int) -> np.ndarray:
        """(sites, items): day the earliest open supplier order is promised; day+nominal lead
        if none is open; day+7 if the earliest is already overdue (arrival unknown)."""
        return self._s.next_promised(day)

    def batches(self, s: int, i: int):
        """Batch list [expiry_day, qty] as recorded (only meaningful for fresh records)."""
        return [list(b) for b in self._s.batches[s][i]]


class Simulation:
    def __init__(self, scenario: Scenario, policy, cfg: Config = DEFAULT):
        self.cfg = cfg
        self.sc = scenario
        self.policy = policy
        self.cat = make_catalogue(cfg, seed=0)
        self.net = make_network(cfg)
        self.dist = distance_matrix(self.net)
        S, I = cfg.n_sites, cfg.n_items
        self.batches = [[[] for _ in range(I)] for _ in range(S)]   # [expiry_day, qty]
        # pipeline entry: [arrive_day, qty, expiry_day or -1, shelf_frac, promised_day]
        self.pipeline = [[[] for _ in range(I)] for _ in range(S)]
        self.on_order = np.zeros((S, I), dtype=np.int64)
        self.stock = np.zeros((S, I), dtype=np.int64)               # ground truth
        self.recorded = np.zeros((S, I), dtype=np.int64)            # what records say
        self.record_age = np.zeros((S, I), dtype=np.int64)
        self.last_sent = np.full((S, I), -999, dtype=np.int64)
        self.unit_cost = self.cat["unit_cost"].to_numpy()
        self.crit = self.cat["criticality"].to_numpy()
        self.imported = self.cat["imported"].to_numpy()
        self.shelf = self.cat["shelf_life_days"].to_numpy()
        self.metrics = Metrics(stockout_days_by_site=np.zeros(S, dtype=int),
                               demand_by_site=np.zeros(S), unmet_by_site=np.zeros(S),
                               penalty_by_item=np.zeros(I), unmet_by_item=np.zeros(I))
        self.day = 0
        self.log = []                     # (day, kind, details) for the audit/animation
        self.daily = []                   # per day: stockout events, expired units, transfers
        self._init_stock()

    # ---- helpers ---------------------------------------------------------
    def expected_lead(self, item: int) -> int:
        c = self.cfg
        return c.lead_time_imported if self.imported[item] else c.lead_time_local

    def on_hand_array(self) -> np.ndarray:
        return self.stock.copy()

    def overdue(self, day: int) -> np.ndarray:
        out = np.zeros(self.cfg.n_items, dtype=np.int64)
        for s in range(self.cfg.n_sites):
            for i in range(self.cfg.n_items):
                for arr, qty, exp, frac, promised in self.pipeline[s][i]:
                    if exp < 0 and promised < day:           # supplier orders only
                        out[i] = max(out[i], day - promised)
        return out

    def next_promised(self, day: int) -> np.ndarray:
        S, I = self.cfg.n_sites, self.cfg.n_items
        out = np.zeros((S, I), dtype=np.int64)
        for s in range(S):
            for i in range(I):
                prom = [p for _, _, exp, _, p in self.pipeline[s][i] if exp < 0]
                if not prom:
                    out[s, i] = day + self.expected_lead(i)
                else:
                    p = min(prom)
                    out[s, i] = p if p >= day else day + 7
        return out

    def _init_stock(self):
        c = self.cfg
        mean = self.sc.demand[: c.warmup_days].mean(axis=0)
        for s in range(c.n_sites):
            for i in range(c.n_items):
                lead = self.expected_lead(i)
                qty = int(np.ceil(mean[s, i] * (lead + c.review_cover_days)))
                if qty > 0:
                    self.batches[s][i].append([int(self.shelf[i] * 0.6), qty])
                    self.stock[s, i] = qty
                    self.metrics.initial_units += qty
        self.recorded[:] = self.stock

    def _lead_for(self, day: int, s: int, i: int) -> int:
        sc = self.sc
        lead = self.expected_lead(i) + int(sc.lead_noise[day, s, i])
        a, b = sc.delay_window
        if sc.delay_days and self.imported[i] and sc.site_mask[s] and a <= day < b:
            lead += sc.delay_days                 # imported items only
        return lead

    def _refresh_records(self, day: int):
        """Fresh records copy the truth every day; stale ones only on their weekly day."""
        c, sc = self.cfg, self.sc
        due = ~sc.stale_mask | (((day + sc.stale_offset) % c.stale_period_days) == 0)
        self.recorded = np.where(due, self.stock, self.recorded)
        self.record_age = np.where(due, 0, self.record_age + 1)

    # ---- one day ---------------------------------------------------------
    def step(self):
        c, m, day = self.cfg, self.metrics, self.day
        so0, ex0, tr0 = m.stockout_days, m.expiry_units, m.n_transfers
        S, I = c.n_sites, c.n_items
        # 1. receive arrivals (supplier orders and transfers)
        for s in range(S):
            for i in range(I):
                pipe = self.pipeline[s][i]
                if not pipe:
                    continue
                keep = []
                for e in pipe:
                    arr, qty, exp, frac, promised = e
                    if arr <= day:
                        if exp < 0:
                            exp = day + int(self.shelf[i] * frac)
                        self.batches[s][i].append([exp, qty])
                        self.stock[s, i] += qty
                        self.on_order[s, i] -= qty
                        m.received_units += qty
                    else:
                        keep.append(e)
                self.pipeline[s][i] = keep
        # 2. expire, 3. serve demand (earliest expiry first), 4. holding
        for s in range(S):
            for i in range(I):
                bs = self.batches[s][i]
                if bs:
                    live = []
                    for exp, q in bs:
                        if exp <= day:
                            m.expiry_units += q
                            m.expiry_loss += q * self.unit_cost[i]
                            self.stock[s, i] -= q
                        else:
                            live.append([exp, q])
                    bs = self.batches[s][i] = sorted(live)
                d = int(self.sc.demand[c.warmup_days + day, s, i])
                if d:
                    m.demand_units += d
                    m.demand_by_site[s] += d
                    need = d
                    for b in bs:
                        take = min(b[1], need)
                        b[1] -= take
                        need -= take
                        m.served_units += take
                        self.stock[s, i] -= take
                        if need == 0:
                            break
                    self.batches[s][i] = [b for b in bs if b[1] > 0]
                    if need > 0:
                        m.unmet_units += need
                        m.unmet_by_site[s] += need
                        m.stockout_days += 1
                        m.stockout_days_by_site[s] += 1
                        m.stockout_days_by_crit[int(self.crit[i])] += 1
                        pen = need * self.unit_cost[i] * c.shortage_multiplier[int(self.crit[i]) - 1]
                        m.shortage_penalty += pen
                        m.penalty_by_item[i] += pen
                        m.unmet_by_item[i] += need
                        if day - self.last_sent[s, i] <= 7:
                            m.donor_stockouts += 1
                m.holding_cost += self.stock[s, i] * self.unit_cost[i] * c.holding_rate_per_day
        self._refresh_records(day)
        # 5. policy decides
        out = self.policy.decide(day, View(self))
        orders, transfers = out if isinstance(out, tuple) else (out, [])
        for o in orders:
            self._place_order(day, o)
        for t in transfers:
            self._execute_transfer(day, t)
        m.abstentions = getattr(self.policy, "abstentions", 0)
        self.daily.append((m.stockout_days - so0, m.expiry_units - ex0, m.n_transfers - tr0))
        self.day += 1

    def _place_order(self, day, o: Order):
        if o.qty <= 0:
            return
        c, m = self.cfg, self.metrics
        lead = self._lead_for(day, o.site, o.item)
        promised = day + self.expected_lead(o.item)
        frac = float(self.sc.arrival_frac[day, o.site, o.item])
        self.pipeline[o.site][o.item].append([day + lead, int(o.qty), -1, frac, promised])
        self.on_order[o.site, o.item] += int(o.qty)
        m.n_orders += 1
        m.order_cost += c.order_fixed_cost
        self.log.append((day, "order", (o.site, o.item, int(o.qty))))

    def transit_days(self, a: int, b: int) -> int:
        return 1 if self.dist[a, b] <= self.cfg.transfer_fast_km else 2

    def _execute_transfer(self, day, t: Transfer):
        """Ship from TRUE stock. If the record was wrong, less (or nothing) ships."""
        c, m = self.cfg, self.metrics
        if t.qty <= 0 or t.src == t.dst:
            return
        transit = self.transit_days(t.src, t.dst)
        min_exp = day + transit + c.transfer_min_shelf_days
        shipped, batches_out = 0, []
        for b in self.batches[t.src][t.item]:            # sorted, earliest expiry first
            if shipped >= t.qty:
                break
            if b[0] >= min_exp and b[1] > 0:
                take = min(b[1], t.qty - shipped)
                b[1] -= take
                shipped += take
                batches_out.append((b[0], take))
        self.batches[t.src][t.item] = [b for b in self.batches[t.src][t.item] if b[1] > 0]
        self.stock[t.src, t.item] -= shipped
        m.sent_units += shipped
        if shipped < t.qty:
            m.infeasible_transfers += 1
        if shipped == 0:
            self.log.append((day, "transfer_failed", (t.src, t.dst, t.item, t.qty)))
            return
        for exp, q in batches_out:
            self.pipeline[t.dst][t.item].append([day + transit, q, exp, 0.0, day + transit])
            self.on_order[t.dst, t.item] += q
        self.last_sent[t.src, t.item] = day
        m.n_transfers += 1
        m.transfer_units += shipped
        m.transfer_cost += c.transfer_fixed_cost + c.transfer_unit_km_cost * shipped * self.dist[t.src, t.dst]
        # supplier-order bookkeeping: transfers are not supplier orders, so keep promised=-1 semantics
        self.log.append((day, "transfer", (t.src, t.dst, t.item, int(shipped), t.reason)))

    def run(self) -> Metrics:
        while self.day < self.cfg.horizon_days:
            self.step()
        return self.metrics
