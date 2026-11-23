import os
os.environ["MEDRESQ_EASY_PASSWORDS"] = "1"
import time
import unittest

import app as A


def login(user, pw="demo"):
    c = A.app.test_client()
    r = c.post("/api/login", json={"user": user, "password": pw})
    return c, r


class TestSecurity(unittest.TestCase):
    def setUp(self):
        A.app.config["AUTH"] = True
        A.FAILS.clear()
        A.LIVE.rules = {"controlled": [], "cold": ["Insulin glargine"]}
        self.co, r = login("coordinator")
        self.assertEqual(r.status_code, 200)
        self.co.post("/api/reset", json={"scenario": "local_both", "seed": 7})

    def tearDown(self):
        A.app.config["AUTH"] = False

    def test_everything_needs_login(self):
        c = A.app.test_client()
        for path in ("/api/state", "/api/audit", "/api/audit.csv", "/api/settings", "/api/shipments", "/report", "/report.pdf",
                     "/api/notifications", "/api/rules", "/api/site/S01"):
            self.assertEqual(c.get(path).status_code, 401, path)
        self.assertEqual(c.post("/api/upload", json={"csv": "x"}).status_code, 401)

    def test_post_must_be_json_and_same_origin(self):
        c = self.co
        self.assertEqual(c.post("/api/advance", data="days=1", content_type="application/x-www-form-urlencoded").status_code, 415)
        r = c.post("/api/advance", json={"days": 1}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(c.post("/api/advance", json={"days": 1}, headers={"Origin": "http://localhost"}).status_code, 200)

    def test_headers(self):
        r = self.co.get("/api/state")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        self.assertIn("no-store", r.headers["Cache-Control"])
        self.assertIn("HttpOnly", login("coordinator")[1].headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", login("coordinator")[1].headers["Set-Cookie"])

    def test_lockout_after_wrong_passwords(self):
        for _ in range(5):
            self.assertEqual(login("auditor", "wrong")[1].status_code, 401)
        self.assertEqual(login("auditor", "demo")[1].status_code, 429)

    def test_session_expires(self):
        tok = next(t for t, s in A.TOKENS.items() if s["user"] == "coordinator")
        A.TOKENS[tok]["last"] -= A.IDLE_SECONDS + 5
        # the client holding that token must now be rejected
        c = A.app.test_client()
        c.set_cookie("medresq_token", tok)
        self.assertEqual(c.get("/api/state").status_code, 401)

    def test_roles(self):
        mgr, _ = login("manager_s03")
        aud, _ = login("auditor")
        for c in (mgr, aud):
            self.assertEqual(c.post("/api/decision", json={"id": 1, "action": "approve"}).status_code, 403)
            self.assertEqual(c.post("/api/settings", json={"reset": True}).status_code, 403)
            self.assertEqual(c.post("/api/reset", json={"scenario": "normal", "seed": 1}).status_code, 403)
            self.assertEqual(c.post("/api/pitch", json={"step": "reset"}).status_code, 403)
        self.assertEqual(aud.post("/api/site_request", json={"site": "S03", "mode": "request", "item": 5, "qty": 1}).status_code, 403)
        self.assertEqual(mgr.post("/api/site_request", json={"site": "S01", "mode": "request", "item": 5, "qty": 1}).status_code, 403)
        # a manager only reads their own notifications even if asking for another site
        self.assertEqual(mgr.get("/api/notifications?site=S01").json["site"], "S03")

    def test_identity_cannot_be_spoofed(self):
        recs = self.co.get("/api/recommendations").json["items"]
        r = next(x for x in recs if x["reason_code"] == "SHORTAGE_RISK")
        self.co.post("/api/decision", json={"id": r["id"], "action": "reject", "role": "Hospital Director", "note": "x\x00y"})
        last = self.co.get("/api/audit").json[-1]
        self.assertEqual(last["role"], "Network coordinator")
        self.assertNotIn("\x00", last["note"])

    def test_csv_export_blocks_formulas(self):
        recs = self.co.get("/api/recommendations").json["items"]
        r = next(x for x in recs if x["reason_code"] == "SHORTAGE_RISK")
        self.co.post("/api/decision", json={"id": r["id"], "action": "reject", "note": "=HYPERLINK(\"http://evil\")"})
        text = self.co.get("/api/audit.csv").data.decode()
        self.assertIn("'=HYPERLINK", text)
        self.assertNotIn(",=HYPERLINK", text)

    def test_bad_input_is_a_400_not_a_crash(self):
        c = self.co
        for path, body in (("/api/advance", {"days": "abc"}), ("/api/advance", {"days": 9999}), ("/api/reset", {"scenario": "nope"}),
                           ("/api/decision", {"id": "x", "action": "approve"}), ("/api/decision", {"id": 1, "action": "delete"}),
                           ("/api/whatif", {"delay": -5}), ("/api/whatif", {"spike": "NaN"}),
                           ("/api/settings/compare", {"scenario": "normal", "values": {"price_scale": 1e9}}),
                           ("/api/upload", {"csv": "a,b\n1,2"}), ("/api/upload", {"csv": 5}), ("/api/rules", {"cold": ["Nothing"]})):
            r = c.post(path, json=body)
            self.assertEqual(r.status_code, 400, (path, body, r.get_data(as_text=True)[:100]))
        mgr, _ = login("manager_s03")
        for body in ({"site": "S03", "mode": "request", "item": 999, "qty": 1}, {"site": "S03", "mode": "x", "item": 1, "qty": 1},
                     {"site": "S03", "mode": "request", "item": 1, "qty": -3}, {"site": "../x", "mode": "request", "item": 1, "qty": 1}):
            self.assertIn(mgr.post("/api/site_request", json=body).status_code, (400, 403))

    def test_upload_limits_and_sanitising(self):
        big = "site,item,units,daily_demand\n" + "A,x,1,1\n" * 4000
        r = self.co.post("/api/upload", json={"csv": big}).json
        self.assertEqual(r["rows"], 3000)
        r = self.co.post("/api/upload", json={"csv": "site,item,units,daily_demand\n<img src=x onerror=alert(1)>,Amlodipine,1,5\nB,Amlodipine,500,5\n"}).json
        self.assertEqual(r["at_risk"][0]["site"], "<img src=x onerror=alert(1)>")   # kept as text; the page escapes it
        self.assertEqual(r["plan"][0]["to"], "<img src=x onerror=alert(1)>")

    def test_controlled_medicines_are_blocked(self):
        self.co.post("/api/rules", json={"controlled": ["Insulin glargine", "Ondansetron"], "cold": []})
        mgr, _ = login("manager_s03")
        r = mgr.post("/api/site_request", json={"site": "S03", "mode": "request", "item": 29, "qty": 1})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Controlled", r.json["error"])
        recs = self.co.get("/api/recommendations").json["items"]
        self.assertFalse(any(x.get("item_name") in ("Insulin glargine", "Ondansetron") for x in recs))

    def test_cold_chain_must_be_confirmed(self):
        mgr, _ = login("manager_s03")
        res = mgr.post("/api/site_request", json={"site": "S03", "mode": "request", "item": 5, "qty": 2}).json
        self.assertTrue(res["ok"])
        src = next(s for s in A.LIVE.queue if s["id"] == res["id"])["src"]
        smgr, _ = login("manager_" + src.lower())
        no = smgr.post("/api/site_action", json={"id": res["id"], "site": src, "action": "accept"})
        self.assertEqual(no.status_code, 400)
        self.assertIn("Cold-chain", no.json["error"])
        ok = smgr.post("/api/site_action", json={"id": res["id"], "site": src, "action": "accept", "cold_ok": True})
        self.assertEqual(ok.status_code, 200)

    def test_settings_change_the_numbers(self):
        r = self.co.post("/api/settings/compare", json={"scenario": "normal", "seed": 3, "values": {"transfer_fixed_cost": 40}}).json
        self.assertNotEqual(r["mine"]["C"]["total"], r["example"]["C"]["total"])
        self.assertEqual(r["mine"]["A"]["total"], r["example"]["A"]["total"])   # site-by-site makes no transfers
        self.assertEqual(self.co.post("/api/settings", json={"values": {"price_scale": 2}}).status_code, 200)
        self.assertEqual(self.co.get("/api/settings").json["values"]["price_scale"], 2)
        self.co.post("/api/settings", json={"reset": True})
        self.assertEqual(self.co.get("/api/settings").json["values"]["price_scale"], 1.0)

    def test_pdf_and_pitch(self):
        self.assertEqual(self.co.post("/api/pitch", json={"step": "reset"}).status_code, 200)
        self.assertEqual(self.co.post("/api/pitch", json={"step": "confirm_dst"}).json["state"], "waiting")
        self.assertEqual(self.co.post("/api/pitch", json={"step": "confirm_src"}).json["state"], "shipped")
        pdf = self.co.get("/report.pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF"))
        self.assertEqual(self.co.post("/api/pitch", json={"step": "nope"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
