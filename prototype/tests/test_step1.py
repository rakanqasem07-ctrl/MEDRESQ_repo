"""Sanity tests for Step 1. Run:  python3 -m unittest discover -s tests -v  (from medresq/)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from data_gen import make_scenario
from policies import PolicyA
from sim import Simulation, Order

SMALL = Config(n_sites=3, n_items=10, horizon_days=200, disruption_start_min=40,
               disruption_start_max=100)


class TestData(unittest.TestCase):
    def test_reproducible(self):
        a = make_scenario("local_both", 3, SMALL)
        b = make_scenario("local_both", 3, SMALL)
        np.testing.assert_array_equal(a.demand, b.demand)

    def test_different_seeds_differ(self):
        a = make_scenario("normal", 1, SMALL)
        b = make_scenario("normal", 2, SMALL)
        self.assertFalse(np.array_equal(a.demand, b.demand))

    def test_common_random_numbers(self):
        """A spike may only ADD demand: spike scenario >= normal everywhere."""
        n = make_scenario("normal", 5, SMALL)
        s = make_scenario("spike", 5, SMALL)
        self.assertTrue((s.demand >= n.demand).all())
        self.assertTrue((s.demand > n.demand).any())

    def test_delay_scenario_same_demand(self):
        n = make_scenario("normal", 5, SMALL)
        d = make_scenario("delay", 5, SMALL)
        np.testing.assert_array_equal(n.demand, d.demand)

    def test_demand_non_negative_integers(self):
        s = make_scenario("local_both", 0, SMALL)
        self.assertTrue((s.demand >= 0).all())
        self.assertTrue(np.issubdtype(s.demand.dtype, np.integer))


class TestSimulation(unittest.TestCase):
    def test_units_conserved(self):
        """initial + received - sent = served + expired + closing stock (no units appear or vanish)."""
        for name in ("normal", "local_both"):
            sc = make_scenario(name, 1, SMALL)
            sim = Simulation(sc, PolicyA(SMALL), SMALL)
            m = sim.run()
            closing = sim.on_hand_array().sum()
            self.assertAlmostEqual(m.initial_units + m.received_units - m.sent_units,
                                   m.served_units + m.expiry_units + closing, places=6)

    def test_demand_balance(self):
        sc = make_scenario("normal", 2, SMALL)
        sim = Simulation(sc, PolicyA(SMALL), SMALL)
        m = sim.run()
        self.assertAlmostEqual(m.demand_units, m.served_units + m.unmet_units, places=6)

    def test_no_negative_stock(self):
        sc = make_scenario("spike", 2, SMALL)
        sim = Simulation(sc, PolicyA(SMALL), SMALL)
        sim.run()
        self.assertTrue((sim.on_hand_array() >= 0).all())

    def test_no_orders_means_stockouts(self):
        class Never:
            def decide(self, day, view): return []
        sc = make_scenario("normal", 2, SMALL)
        m = Simulation(sc, Never(), SMALL).run()
        self.assertGreater(m.stockout_days, 0)
        self.assertEqual(m.n_orders, 0)

    def test_policy_a_beats_no_orders(self):
        class Never:
            def decide(self, day, view): return []
        sc = make_scenario("normal", 2, SMALL)
        a = Simulation(sc, PolicyA(SMALL), SMALL).run()
        n = Simulation(sc, Never(), SMALL).run()
        self.assertGreater(a.service_level, n.service_level)

    def test_history_excludes_today(self):
        """The policy must never see today's or future demand."""
        sc = make_scenario("normal", 2, SMALL)
        seen = {}

        class Spy:
            def decide(self, day, view):
                h = view.history(day, 5)
                t = SMALL.warmup_days + day
                np.testing.assert_array_equal(h, sc.demand[t - 5:t])
                seen[day] = True
                return []

        Simulation(sc, Spy(), SMALL).run()
        self.assertEqual(len(seen), SMALL.horizon_days)

    def test_earliest_expiry_first(self):
        sc = make_scenario("normal", 2, SMALL)
        sim = Simulation(sc, PolicyA(SMALL), SMALL)
        sim.run()
        for s in range(SMALL.n_sites):
            for i in range(SMALL.n_items):
                exps = [e for e, _ in sim.batches[s][i]]
                self.assertEqual(exps, sorted(exps))

    def test_delay_hurts_imported_service(self):
        res = {}
        for name in ("normal", "delay"):
            vals = []
            for seed in range(4):
                sc = make_scenario(name, seed, SMALL)
                vals.append(Simulation(sc, PolicyA(SMALL), SMALL).run().stockout_days)
            res[name] = np.mean(vals)
        self.assertGreater(res["delay"], res["normal"])


if __name__ == "__main__":
    unittest.main()
