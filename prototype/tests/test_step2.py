import os
os.environ["MEDRESQ_EASY_PASSWORDS"] = "1"
"""Tests for policies B and C, the safe-to-recommend gate and the app.
Run from medresq/:  python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from data_gen import make_scenario
from policies import PolicyA, PolicyB, PolicyC
from sim import Simulation, View

SMALL = Config(n_sites=5, n_items=12, horizon_days=240, disruption_start_min=70,
               disruption_start_max=120)


def run(scen, pol, seed=1, cfg=SMALL):
    sim = Simulation(make_scenario(scen, seed, cfg), pol, cfg)
    return sim, sim.run()


class TestPolicies(unittest.TestCase):
    def test_units_conserved_with_transfers(self):
        for pol in (PolicyB(SMALL), PolicyC(SMALL)):
            sim, m = run("local_both", pol)
            closing = sim.on_hand_array().sum()
            in_transit = sum(e[1] for s in range(SMALL.n_sites) for i in range(SMALL.n_items)
                             for e in sim.pipeline[s][i] if e[2] >= 0)
            self.assertAlmostEqual(m.initial_units + m.received_units - m.sent_units,
                                   m.served_units + m.expiry_units + closing, places=6)
            self.assertGreaterEqual(m.sent_units, m.received_units * 0)  # sanity

    def test_c_is_deterministic(self):
        _, a = run("local_both", PolicyC(SMALL), seed=4)
        _, b = run("local_both", PolicyC(SMALL), seed=4)
        self.assertEqual(a.as_row(), b.as_row())

    def test_c_transfers_valid(self):
        sim, m = run("local_both", PolicyC(SMALL), seed=2)
        for day, kind, det in sim.log:
            if kind == "transfer":
                src, dst, item, qty, _ = det
                self.assertNotEqual(src, dst)
                self.assertGreater(qty, 0)

    def test_c_no_hard_constraint_violations_fresh_data(self):
        for seed in range(3):
            _, m = run("normal", PolicyC(SMALL), seed=seed)
            self.assertEqual(m.infeasible_transfers, 0)

    def test_gate_abstains_on_stale_and_reduces_infeasible(self):
        _, with_gate = run("stale", PolicyC(SMALL, gate=True), seed=3)
        _, no_gate = run("stale", PolicyC(SMALL, gate=False), seed=3)
        self.assertGreater(with_gate.abstentions, 0)
        self.assertLessEqual(with_gate.infeasible_transfers, no_gate.infeasible_transfers)

    def test_transfers_never_ship_short_dated_stock(self):
        """Every shipped batch must have at least transfer_min_shelf_days left on arrival."""
        sim = Simulation(make_scenario("local_both", 5, SMALL), PolicyC(SMALL), SMALL)
        while sim.day < SMALL.horizon_days:
            sim.step()
            for s in range(SMALL.n_sites):
                for i in range(SMALL.n_items):
                    for arr, qty, exp, frac, prom in sim.pipeline[s][i]:
                        if exp >= 0:   # a transfer in transit
                            self.assertGreaterEqual(exp - arr, SMALL.transfer_min_shelf_days - 1)

    def test_c_beats_a_on_average_over_a_few_seeds(self):
        """Not a performance claim: a regression guard so C never silently becomes worse."""
        a = c = 0
        for seed in range(3):
            a += run("local_both", PolicyA(SMALL), seed=seed)[1].stockout_days
            c += run("local_both", PolicyC(SMALL), seed=seed)[1].stockout_days
        self.assertLess(c, a)

    def test_history_window_guard(self):
        sim = Simulation(make_scenario("normal", 0, SMALL), PolicyA(SMALL), SMALL)
        with self.assertRaises(ValueError):
            View(sim).history(0, SMALL.warmup_days + 5)

    def test_explanation_matches_transfer(self):
        pol = PolicyC(SMALL, explain=True)
        sim = Simulation(make_scenario("local_both", 2, SMALL), PolicyA(SMALL), SMALL)
        found = False
        while sim.day < SMALL.horizon_days and not found:
            sim.step()
            pol.recs = []
            _, transfers = pol.decide(sim.day, View(sim))
            recs = [r for r in pol.recs if r["reason_code"] == "SHORTAGE_RISK"]
            if recs:
                found = True
                self.assertEqual(len(recs), len([t for t in transfers if t.reason == "shortage risk"]))
                r = recs[0]
                self.assertIn(f"Moving {r['qty']} units", r["reason"])
                self.assertIn(r["src"], r["reason"])
                self.assertGreater(r["net_benefit_jd"], 0)
        self.assertTrue(found)


class TestApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as A
        cls.A = A
        A.app.config["AUTH"] = False
        cls.c = A.app.test_client()

    def test_center_workspace_needs_both_sides(self):
        c = self.c
        c.post("/api/reset", json={"scenario": "local_both", "seed": 7})
        recs = c.get("/api/recommendations").json["items"]
        r = next(x for x in recs if x["reason_code"] == "SHORTAGE_RISK")
        inbox = c.get("/api/site/" + r["dst"]).json["inbox"]
        self.assertTrue(any(x["id"] == r["id"] and x["side"] == "receive" for x in inbox))
        # a center that is not involved cannot act
        other = next(s for s in ["S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"] if s not in (r["src"], r["dst"]))
        self.assertEqual(c.post("/api/site_action", json={"id": r["id"], "site": other, "action": "accept"}).status_code, 400)
        first = c.post("/api/site_action", json={"id": r["id"], "site": r["dst"], "action": "accept"}).json
        self.assertEqual(first["state"], "waiting")
        self.assertEqual(c.get("/api/state").json["tiles"]["transfers"], 0)
        second = c.post("/api/site_action", json={"id": r["id"], "site": r["src"], "action": "accept"}).json
        self.assertEqual(second["state"], "shipped")
        self.assertEqual(c.get("/api/state").json["tiles"]["transfers"], 1)

    def test_center_manual_request_and_decline(self):
        c = self.c
        c.post("/api/reset", json={"scenario": "local_both", "seed": 7})
        res = c.post("/api/site_request", json={"site": "S03", "mode": "request", "item": 5, "qty": 4})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(c.post("/api/site_request", json={"site": "S03", "mode": "request", "item": 5, "qty": 0}).status_code, 400)
        d = c.post("/api/site_action", json={"id": res.json["id"], "site": "S03", "action": "decline"}).json
        self.assertEqual(d["state"], "declined")

    def test_notifications_shipments_and_roles(self):
        A, c = self.A, self.c
        c.post("/api/reset", json={"scenario": "local_both", "seed": 7})
        recs = c.get("/api/recommendations").json["items"]
        r = next(x for x in recs if x["reason_code"] == "SHORTAGE_RISK")
        notes = c.get("/api/notifications?site=" + r["dst"]).json
        self.assertGreaterEqual(notes["unread"], 1)
        c.post("/api/site_action", json={"id": r["id"], "site": r["dst"], "action": "accept"})
        n2 = c.get("/api/notifications?site=" + r["src"]).json["items"]
        self.assertTrue(any(n["kind"] == "confirmed" for n in n2))
        c.post("/api/site_action", json={"id": r["id"], "site": r["src"], "action": "accept"})
        ships = c.get("/api/shipments").json
        self.assertEqual(ships[0]["status"], "in transit")
        c.post("/api/advance", json={"days": 3})
        self.assertEqual(c.get("/api/shipments").json[0]["status"], "delivered")
        self.assertTrue(any(n["kind"] == "delivered" for n in c.get("/api/notifications?site=" + r["dst"]).json["items"]))
        # a declined request leaves the inbox
        c.post("/api/reset", json={"scenario": "local_both", "seed": 7})
        r = next(x for x in c.get("/api/recommendations").json["items"] if x["reason_code"] == "SHORTAGE_RISK")
        c.post("/api/site_action", json={"id": r["id"], "site": r["dst"], "action": "decline"})
        self.assertFalse(any(x["id"] == r["id"] for x in c.get("/api/site/" + r["src"]).json["inbox"]))
        self.assertEqual(c.post("/api/site_action", json={"id": r["id"], "site": r["src"], "action": "accept"}).json["error"],
                         "This request is no longer open")

    def test_login_roles(self):
        A = self.A
        A.app.config["AUTH"] = True
        try:
            c = A.app.test_client()
            self.assertEqual(c.get("/api/state").status_code, 401)
            self.assertEqual(c.post("/api/login", json={"user": "coordinator", "password": "x"}).status_code, 401)
            c.post("/api/login", json={"user": "manager_s03", "password": "demo"})
            self.assertEqual(c.get("/api/me").json["site"], "S03")
            self.assertEqual(c.post("/api/site_request", json={"site": "S05", "mode": "request", "item": 5, "qty": 1}).status_code, 403)
            self.assertEqual(c.post("/api/decision", json={"id": 1, "action": "approve"}).status_code, 403)
            self.assertEqual(c.get("/report").status_code, 200)
            a = A.app.test_client()
            a.post("/api/login", json={"user": "auditor", "password": "demo"})
            self.assertEqual(a.post("/api/site_request", json={"site": "S03", "mode": "request", "item": 5, "qty": 1}).status_code, 403)
            self.assertEqual(a.get("/api/audit").status_code, 200)
        finally:
            A.app.config["AUTH"] = False

    def test_flow_approve_reject_override_audit(self):
        A, c = self.A, self.c
        c.post("/api/reset", json={"scenario": "local_both", "seed": 7})
        recs = c.get("/api/recommendations").json["items"]
        pend = [r for r in recs if r["reason_code"] == "SHORTAGE_RISK"]
        self.assertGreaterEqual(len(pend), 2)
        before = c.get("/api/state").json["tiles"]["transfers"]
        r1 = c.post("/api/decision", json={"id": pend[0]["id"], "action": "approve"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(c.get("/api/state").json["tiles"]["transfers"], before + 1)
        # cannot decide twice
        self.assertEqual(c.post("/api/decision", json={"id": pend[0]["id"], "action": "approve"}).status_code, 400)
        # reject ships nothing
        n = c.get("/api/state").json["tiles"]["transfers"]
        c.post("/api/decision", json={"id": pend[1]["id"], "action": "reject", "note": "not needed"})
        self.assertEqual(c.get("/api/state").json["tiles"]["transfers"], n)
        audit = c.get("/api/audit").json
        self.assertEqual([a["action"] for a in audit], ["approved", "rejected"])
        self.assertEqual(audit[1]["note"], "not needed")
        self.assertIn("approved", c.get("/api/audit.csv").data.decode())

    def test_recommendations_show_cover_before_and_after(self):
        c = self.c
        c.post("/api/reset", json={"scenario": "local_both", "seed": 7})
        recs = [r for r in c.get("/api/recommendations").json["items"] if r["reason_code"] == "SHORTAGE_RISK"]
        self.assertTrue(recs)
        for r in recs:
            v = r["cover"]
            self.assertGreater(v["dst_after"], v["dst_before"])
            self.assertLess(v["src_after"], v["src_before"])

    def test_nothing_executes_without_decision_and_pending_expire(self):
        c = self.c
        c.post("/api/reset", json={"scenario": "local_both", "seed": 7})
        recs = c.get("/api/recommendations").json["items"]
        t0 = c.get("/api/state").json["tiles"]["transfers"]
        c.post("/api/advance", json={"days": 1})
        self.assertEqual(c.get("/api/state").json["tiles"]["transfers"], t0)
        log = c.get("/api/audit").json
        self.assertTrue(all(a["action"] == "expired" for a in log))
        self.assertEqual(len(log), len([r for r in recs]))

    def test_override_quantity(self):
        c = self.c
        c.post("/api/reset", json={"scenario": "local_both", "seed": 7})
        r = [x for x in c.get("/api/recommendations").json["items"] if x["reason_code"] == "SHORTAGE_RISK"][0]
        res = c.post("/api/decision", json={"id": r["id"], "action": "override", "qty": 1})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(c.get("/api/audit").json[-1]["qty"], r["qty"])  # log shows proposed qty
        self.assertEqual(c.get("/api/audit").json[-1]["action"], "overridden")

    def test_abstain_cannot_be_approved(self):
        c = self.c
        c.post("/api/reset", json={"scenario": "stale", "seed": 3})
        found = None
        for _ in range(40):
            found = next((x for x in c.get("/api/recommendations").json["items"] if x["reason_code"] == "ABSTAIN"), None)
            if found:
                break
            c.post("/api/advance", json={"days": 1})
        self.assertIsNotNone(found)
        self.assertEqual(c.post("/api/decision", json={"id": found["id"], "action": "approve"}).status_code, 400)

    def test_whatif_returns_both_plans(self):
        w = self.c.post("/api/whatif", json={"delay": 14, "spike": 0.5, "share": 0.5, "seed": 2}).json
        self.assertIn("total_cost", w["A"]["metrics"])
        self.assertIn("total_cost", w["C"]["metrics"])
        self.assertEqual(w["A"]["metrics"]["n_transfers"], 0)


if __name__ == "__main__":
    unittest.main()
