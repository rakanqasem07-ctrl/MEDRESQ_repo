"""Stock policies (report section 9).

A  Site by site          every site decides alone (the documented baseline)
B  Simple manual transfers   rule-of-thumb phone calls between sites, no optimisation
C  MEDRESQ               forecast + risk + network optimisation + safe-to-recommend gate

All three use the same standard reorder-point rule for supplier orders:
    ROP = d x L + z x sigma x sqrt(L),  order up to ROP + review_cover_days x d.
They differ in what they know and whether sites can help each other.
"""
from statistics import NormalDist

import numpy as np
from scipy.optimize import linprog

from config import Config, DEFAULT
from sim import Order, Transfer, View


_ND = NormalDist()


def _exp_short(x, mu, sd):
    """Expected units short, E[(D - x)^+], for D ~ Normal(mu, sd) (vectorised in x)."""
    x = np.asarray(x, dtype=float)
    sd = max(sd, 0.5)
    a = (x - mu) / sd
    phi = np.exp(-0.5 * a * a) / np.sqrt(2 * np.pi)
    from scipy.special import ndtr
    return sd * (phi - a * (1 - ndtr(a)))


def _order_list(rop, order_up_to, position, d, pack):
    orders = []
    trigger = (position <= rop) & (d > 0)
    for s, i in zip(*np.nonzero(trigger)):
        qty = int(np.ceil(order_up_to[s, i] - position[s, i]))
        qty = int(np.ceil(qty / pack[i]) * pack[i])      # whole packs only
        if qty > 0:
            orders.append(Order(int(s), int(i), qty))
    return orders


# --------------------------------------------------------------------------- A
class PolicyA:
    name = "A_site_by_site"

    def __init__(self, cfg: Config = DEFAULT):
        self.cfg = cfg

    def decide(self, day: int, view: View):
        c = self.cfg
        hist = view.history(day, c.history_window)          # past demand only
        d = hist.mean(axis=0)                                # (S, I)
        sigma = hist.std(axis=0)
        lead = np.array([view.lead_time(i) for i in range(view.n_items)])[None, :]
        rop = d * lead + c.service_z * sigma * np.sqrt(lead)
        position = view.on_hand() + view.on_order()
        order_up_to = rop + c.review_cover_days * d
        pack = view.catalogue["pack_size"].to_numpy()
        return _order_list(rop, order_up_to, position, d, pack)


# --------------------------------------------------------------------------- B
class PolicyB:
    """Policy A plus rule-of-thumb transfers.

    When a site is at its reorder point it phones the first site (in list order) that holds
    more than `surplus_cover_days` of cover and asks for what it needs. No forecast of the
    donor's own needs beyond that rule, no cost comparison, no scenario awareness.
    """
    name = "B_manual_transfers"
    surplus_cover_days = 45

    def __init__(self, cfg: Config = DEFAULT):
        self.cfg = cfg

    def decide(self, day: int, view: View):
        c = self.cfg
        hist = view.history(day, c.history_window)
        d = hist.mean(axis=0)
        sigma = hist.std(axis=0)
        lead = np.array([view.lead_time(i) for i in range(view.n_items)])[None, :]
        rop = d * lead + c.service_z * sigma * np.sqrt(lead)
        on_hand = view.on_hand()
        position = on_hand + view.on_order()
        order_up_to = rop + c.review_cover_days * d
        pack = view.catalogue["pack_size"].to_numpy()
        transfers = []
        trigger = (position <= rop) & (d > 0)
        for s, i in zip(*np.nonzero(trigger)):
            need = int(np.ceil(order_up_to[s, i] - position[s, i]))
            for donor in range(view.n_sites):
                if donor == s or d[donor, i] <= 0:
                    continue
                spare = on_hand[donor, i] - self.surplus_cover_days * d[donor, i]
                if spare >= 1:
                    q = int(min(need, spare))
                    transfers.append(Transfer(donor, int(s), int(i), q, "manual call"))
                    position[s, i] += q
                    on_hand[donor, i] -= q
                    break
        orders = _order_list(rop, order_up_to, position, d, pack)
        return orders, transfers


# --------------------------------------------------------------------------- C
class PolicyC:
    """MEDRESQ: every component can be switched off for the ablation study."""

    def __init__(self, cfg: Config = DEFAULT, *, transfers=True, adaptive_lead=True,
                 priority_z=True, gate=True, donor_reserve=True, expiry=True,
                 forecast_method="shock", margin=2.0, transfer_kinds=None, explain=False, label="C_medresq"):
        self.cfg = cfg
        self.name = label
        self.use_transfers, self.use_adaptive_lead = transfers, adaptive_lead
        self.use_priority_z, self.use_gate = priority_z, gate
        self.use_donor_reserve, self.use_expiry = donor_reserve, expiry
        self.forecast_method = forecast_method
        self.transfer_kinds = transfer_kinds
        self.margin = margin       # expected benefit must exceed margin x (donor harm + move cost)
        self.explain = explain
        self.abstentions = 0
        self.recs = []                       # explained recommendations (for the app)
        self._z_tier = {1: 1.28, 2: 1.65, 3: 2.05}   # DESIGN CHOICE: higher z for critical items
        self._ndist = NormalDist()

    # ---- forecast and risk ------------------------------------------------
    def forecast(self, day, view):
        """Expected daily demand and its spread, from past demand only."""
        from forecast import CANDIDATES
        hist = view.history(day, 56)
        d = CANDIDATES[self.forecast_method](hist)
        sigma = view.history(day, self.cfg.history_window).std(axis=0)
        return d, sigma

    def _z(self, view):
        crit = view.catalogue["criticality"].to_numpy()
        if not self.use_priority_z:
            return np.full(view.n_items, self.cfg.service_z)
        return np.array([self._z_tier[int(k)] for k in crit])

    # ---- decide -----------------------------------------------------------
    def decide(self, day: int, view: View):
        c = self.cfg
        S, I = view.n_sites, view.n_items
        d, sigma = self.forecast(day, view)
        z = self._z(view)[None, :]
        nominal = np.array([view.lead_time(i) for i in range(I)])
        if self.use_adaptive_lead:
            overdue = np.minimum(view.max_overdue_days(day), 35)
            lead = (nominal + overdue)[None, :]
        else:
            lead = nominal[None, :]
        on_hand = view.on_hand()
        on_order = view.on_order()
        age = view.record_age()
        pack = view.catalogue["pack_size"].to_numpy()
        transfers = []

        if self.use_transfers:
            transfers = self._plan_transfers(day, view, d, sigma, z, on_hand, on_order, age, lead)

        rop = d * lead + z * sigma * np.sqrt(lead)
        position = on_hand + on_order      # transfers already folded in (arrays updated in place)
        order_up_to = rop + c.review_cover_days * d
        orders = _order_list(rop, order_up_to, position, d, pack)
        return orders, transfers

    # ---- network optimisation --------------------------------------------
    def _plan_transfers(self, day, view, d, sigma, z, on_hand, on_order, age, lead):
        c = self.cfg
        S, I = view.n_sites, view.n_items
        dist = view.distance_km
        nominal = np.array([view.lead_time(i) for i in range(I)])
        promised = view.next_promised(day)
        cat = view.catalogue
        unit_cost = cat["unit_cost"].to_numpy()
        mult = np.array([c.shortage_multiplier[int(k) - 1] for k in cat["criticality"]])
        # lumpy demand floor: a donor keeps at least twice its largest recent daily demand
        lump_floor = 2.0 * view.history(day, 56).max(axis=0)          # (S, I)
        transfers = []
        kinds = cat["kind"].to_numpy()
        self._touched = set()                            # donor (site, item) pairs used today
        for i in range(I):
            if not (d[:, i] > 0).any():
                continue
            if self.transfer_kinds is not None and kinds[i] not in self.transfer_kinds:
                continue
            H = np.maximum(promised[:, i] - day, 1).astype(float)         # risk horizon
            need = d[:, i] * H + z[0, i] * sigma[:, i] * np.sqrt(H) - on_hand[:, i] - 0.0
            # units already on the way count against the need (transfers in transit too)
            inbound_known = np.maximum(on_order[:, i], 0)
            need = np.maximum(need - 0 * inbound_known, 0)
            # only sites whose next supplier delivery is NOT before they run dry
            reserve = np.maximum(d[:, i] * c.donor_cover_days
                                 + z[0, i] * sigma[:, i] * np.sqrt(c.donor_cover_days),
                                 lump_floor[:, i]) if self.use_donor_reserve else np.zeros(S)
            surplus = np.maximum(on_hand[:, i] - reserve, 0)
            recip = [r for r in range(S) if need[r] >= 1 and d[r, i] > 0]
            donors = [s for s in range(S) if surplus[s] >= 1 and need[s] < 1]
            if not recip or not donors:
                continue
            # ---- safe-to-recommend gate: stale records are not used -------------
            if self.use_gate:
                ok_r = [r for r in recip if age[r, i] <= 1]
                ok_d = [s for s in donors if age[s, i] <= 1]
                self.abstentions += (len(recip) - len(ok_r)) + (len(donors) - len(ok_d))
                if self.explain:
                    names = view.network["site_id"].tolist()
                    item_id = view.catalogue["item_id"].tolist()[i]
                    for s_ in [x for x in recip if x not in ok_r] + [x for x in donors if x not in ok_d]:
                        self.recs.append({
                            "day": int(day), "item": item_id, "item_index": int(i),
                            "src": None, "dst": names[s_], "qty": 0, "reason_code": "ABSTAIN",
                            "reason": (f"Insufficient reliable data: the {item_id} stock record at "
                                       f"{names[s_]} is {int(age[s_, i])} days old."),
                            "constraints": {"records_fresh": False},
                            "cost_jd": 0.0, "avoided_shortage_value_jd": 0.0,
                            "alternative": "Count the stock and update the record, then re-run.",
                            "action": "Manual review required", "status": "manual_review"})
                recip, donors = ok_r, ok_d
                if not recip or not donors:
                    continue
            # ---- hard constraint: only units with shelf life left on arrival -----
            elig = {}
            for s in donors:
                tmax = 2
                cutoff = day + tmax + c.transfer_min_shelf_days
                e = sum(q for ex, q in view.batches(s, i) if ex >= cutoff) \
                    if age[s, i] <= 1 else on_hand[s, i]
                elig[s] = min(surplus[s], e)
            donors = [s for s in donors if elig[s] >= 1]
            if not donors:
                continue
            penalty = unit_cost[i] * mult[i]                      # cost of 1 unit short
            flows = self._solve(recip, donors, need, elig, dist, penalty, self.cfg.transfer_unit_km_cost)
            for (s, r), q in flows.items():
                q = int(np.floor(q + 1e-6))
                if q < 1:
                    continue
                # ---- cost-benefit: expected shortage avoided at the recipient, minus the
                # expected shortage created at the donor, minus transport cost ------------
                qs = np.arange(1, q + 1)
                mu_r, sd_r = d[r, i] * H[r], sigma[r, i] * np.sqrt(H[r])
                Hd = max(promised[s, i] - day, 1)
                mu_d, sd_d = d[s, i] * Hd, sigma[s, i] * np.sqrt(Hd)
                gain = penalty * (_exp_short(on_hand[r, i], mu_r, sd_r)
                                  - _exp_short(on_hand[r, i] + qs, mu_r, sd_r))
                harm = penalty * (_exp_short(on_hand[s, i] - qs, mu_d, sd_d)
                                  - _exp_short(on_hand[s, i], mu_d, sd_d))
                move = c.transfer_fixed_cost + c.transfer_unit_km_cost * qs * dist[s, r]
                net = gain - harm - move
                k = int(np.argmax(np.where(gain >= self.margin * (harm + move), net, -1e18)))
                if gain[k] < self.margin * (harm[k] + move[k]) or net[k] <= 0:
                    continue
                q = int(qs[k])
                transfers.append(Transfer(s, r, i, q, "shortage risk"))
                if self.explain:
                    rec = self._explain(day, view, s, r, i, q, d, sigma, on_hand,
                                        need, elig, dist, penalty, H)
                    rec["avoided_shortage_value_jd"] = round(float(gain[k]), 2)
                    rec["donor_risk_jd"] = round(float(harm[k]), 2)
                    rec["net_benefit_jd"] = round(float(net[k]), 2)
                    self.recs.append(rec)
                on_hand[s, i] -= q                       # donor stock is committed
                on_order[r, i] += q                      # recipient stock is inbound
                self._touched.add((s, i))
        if self.use_expiry:
            cov = c.donor_cover_days
            reserve_all = np.maximum(d * cov + z * sigma * np.sqrt(cov), lump_floor) \
                if self.use_donor_reserve else np.zeros_like(d)
            transfers += self._expiry_rescue(day, view, d, on_hand, on_order, age, reserve_all)
        return transfers

    @staticmethod
    def _solve(recip, donors, need, elig, dist, penalty, unit_km=DEFAULT.transfer_unit_km_cost):
        """Min-cost flow: minimise transport cost + penalty x unmet need.

        Shortcuts keep it fast: with one recipient or one donor the greedy cheapest-first
        allocation is optimal; otherwise a small linear program (HiGHS) is solved.
        """

        if len(recip) == 1 or len(donors) == 1:
            flows = {}
            if len(recip) == 1:
                r = recip[0]
                left = need[r]
                for s in sorted(donors, key=lambda s: dist[s, r]):
                    q = min(left, elig[s])
                    if q > 0 and unit_km * dist[s, r] < penalty:
                        flows[(s, r)] = q
                        left -= q
            else:
                s = donors[0]
                left = elig[s]
                for r in sorted(recip, key=lambda r: dist[s, r]):
                    q = min(left, need[r])
                    if q > 0 and unit_km * dist[s, r] < penalty:
                        flows[(s, r)] = q
                        left -= q
            return flows
        nD, nR = len(donors), len(recip)
        cost = np.array([[unit_km * dist[s, r] for r in recip] for s in donors]).ravel()
        # variables: x[s,r] (nD*nR) then unmet u[r] (nR)
        obj = np.concatenate([cost, np.full(nR, penalty)])
        A_ub = np.zeros((nD, nD * nR + nR))
        for a in range(nD):
            A_ub[a, a * nR:(a + 1) * nR] = 1
        b_ub = np.array([elig[s] for s in donors], dtype=float)
        A_eq = np.zeros((nR, nD * nR + nR))
        for b in range(nR):
            A_eq[b, b:nD * nR:nR] = 1
            A_eq[b, nD * nR + b] = 1
        b_eq = np.array([need[r] for r in recip], dtype=float)
        res = linprog(obj, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=(0, None),
                      method="highs")
        if not res.success:
            return {}
        x = res.x[:nD * nR].reshape(nD, nR)
        return {(donors[a], recip[b]): x[a, b] for a in range(nD) for b in range(nR) if x[a, b] > 1e-6}

    # ---- expiry rescue -----------------------------------------------------
    def _expiry_rescue(self, day, view, d, on_hand, on_order, age, reserve):
        """Move units that the donor cannot sell before expiry to a site that can."""
        c = self.cfg
        if day % 7:
            return []
        S, I = view.n_sites, view.n_items
        dist = view.distance_km
        unit_cost = view.catalogue["unit_cost"].to_numpy()
        out = []
        for i in range(I):
            for s in range(S):
                if age[s, i] > 1 or on_hand[s, i] < 5 or (s, i) in self._touched:
                    continue
                cum = 0
                for ex, q in view.batches(s, i):          # earliest expiry first
                    sellable = d[s, i] * max(ex - day - 5, 0)
                    at_risk = int(max(0, min(q, cum + q - sellable, on_hand[s, i] - reserve[s, i])))
                    cum += q
                    if at_risk < 5 or at_risk * unit_cost[i] < 4 * c.transfer_fixed_cost:
                        continue
                    best, best_cap = None, 0
                    for r in range(S):
                        if r == s or age[r, i] > 1:
                            continue
                        tr = 1 if dist[s, r] <= c.transfer_fast_km else 2
                        if ex < day + tr + c.transfer_min_shelf_days:
                            continue
                        cap = d[r, i] * max(ex - day - tr - 5, 0) - on_hand[r, i] - on_order[r, i]
                        if cap >= 5 and (best is None or dist[s, r] < dist[s, best]):
                            best, best_cap = r, cap
                    if best is not None:
                        q_move = int(min(at_risk, best_cap))
                        if q_move >= 5:
                            out.append(Transfer(s, best, i, q_move, "expiry risk"))
                            on_hand[s, i] -= q_move
                            on_order[best, i] += q_move
                            break
        return out

    # ---- explanation (template filled from solver outputs, never free text) -----
    def _explain(self, day, view, s, r, i, q, d, sigma, on_hand, need, elig, dist, penalty, H):
        c = self.cfg
        names = view.network["site_id"].tolist()
        item = view.catalogue["item_id"].tolist()[i]
        cover_r = on_hand[r, i] / d[r, i] if d[r, i] > 0 else float("inf")
        cover_s = on_hand[s, i] / d[s, i] if d[s, i] > 0 else float("inf")
        cost = c.transfer_fixed_cost + c.transfer_unit_km_cost * q * dist[s, r]
        return {
            "day": int(day), "item": item, "item_index": int(i),
            "src": names[s], "dst": names[r], "qty": int(q),
            "reason_code": "SHORTAGE_RISK",
            "reason": (f"{names[r]} has {cover_r:.1f} days of {item} and its next supplier delivery is "
                       f"about {H[r]:.0f} day(s) away (gap to a safe level: about {need[r]:.0f} units). "
                       f"{names[s]} holds {cover_s:.1f} days. Moving {q} units is the most "
                       f"cost-effective amount given the transport cost and the donor's own risk."),
            "constraints": {
                "donor_keeps_reserve_days": c.donor_cover_days,
                "min_shelf_days_on_arrival": c.transfer_min_shelf_days,
                "eligible_units_at_donor": int(elig[s]),
                "records_fresh": True,
            },
            "cost_jd": round(float(cost), 2),
            "avoided_shortage_value_jd": round(float(q * penalty), 2),
            "alternative": f"Wait for the supplier order (arrives in about {H[r]:.0f} day(s), "
                           f"risking up to {need[r]:.0f} units short).",
            "status": "pending",
        }


def make_policy(name: str, cfg: Config = DEFAULT, **kw):
    if name == "A":
        return PolicyA(cfg)
    if name == "B":
        return PolicyB(cfg)
    if name == "C":
        return PolicyC(cfg, **kw)
    raise ValueError(name)
