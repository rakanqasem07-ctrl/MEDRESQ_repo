"""MEDRESQ demo app (Flask). Everything shown is SIMULATED on synthetic data.

Run:  python3 app.py        then open http://127.0.0.1:5000

Human in the loop: MEDRESQ proposes transfers, nothing executes until a pharmacist
approves, rejects or overrides it. Every decision goes to the audit log.
Supplier orders follow the routine reorder-point rule, as they would today.
"""
import csv
import io
import json
import os
import threading
import time

import numpy as np
import pandas as pd
from flask import Flask, Response, jsonify, request, send_from_directory

import dataclasses
from config import Config, DEFAULT
from data_gen import make_scenario
from forecast import CANDIDATES
from policies import PolicyA, PolicyC
from sim import Simulation, Transfer, View

# Example medicine names for the 30 synthetic catalogue items (the data itself is synthetic).
NAMES = ["Metformin 500 mg", "Vitamin D3", "Amlodipine", "Omeprazole", "Atorvastatin", "Insulin glargine",
         "Folic acid", "Warfarin", "Calcium carbonate", "Levothyroxine", "Paracetamol 500 mg", "Loratadine",
         "Losartan", "Ibuprofen", "Digoxin", "Cough syrup", "Vitamin C", "Salbutamol inhaler",
         "Nasal decongestant", "Oseltamivir", "Epinephrine auto-injector", "Throat lozenges",
         "Antihistamine syrup", "Amoxicillin", "Oral rehydration salts", "Antiseptic solution",
         "Ceftriaxone injection", "Hydrocortisone injection", "Lidocaine gel", "Ondansetron"]

# Real Amman hospitals are used only as map locations. Pins are approximate and every stock figure is simulated.
# (id, name, short label, type, lat, lng). Coordinates: Wikipedia for Jordan University Hospital, Jordan Hospital,
# Arab Medical Center and Al Khalidi Medical Center; the Specialty Hospital and the three generic pharmacies are
# approximate district locations.
SITE_META = [
    ("S01", "Jordan University Hospital", "JUH", "hospital", 32.0075, 35.8747),
    ("S02", "Al Khalidi Medical Center", "Khalidi", "hospital", 31.9503, 35.9036),
    ("S03", "Specialty Hospital", "Specialty", "hospital", 31.9690, 35.9100),
    ("S04", "Arab Medical Center", "Arab MC", "hospital", 31.9594, 35.8786),
    ("S05", "Jordan Hospital", "Jordan Hosp.", "hospital", 31.9607, 35.9000),
    ("S06", "Pharmacy, Abdoun", "Abdoun", "pharmacy", 31.9530, 35.8830),
    ("S07", "Pharmacy, Sweileh", "Sweileh", "pharmacy", 32.0240, 35.8420),
    ("S08", "Pharmacy, Marka", "Marka", "pharmacy", 31.9830, 35.9920),
]
SITE_NAME = {m[0]: m[1] for m in SITE_META}


def pretty(text, item_id=None, item_name=None):
    """Swap internal ids (S03, M026) for the names people read."""
    import re
    text = re.sub(r"\bS0[1-8]\b", lambda m: SITE_NAME.get(m.group(0), m.group(0)), text)
    text = text.replace("inf days", "99+ days")
    return text.replace(item_id, item_name) if item_id and item_name else text


HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "outputs")
app = Flask(__name__, static_folder=os.path.join(HERE, "static"))
LOCK = threading.Lock()


class Live:
    """The live network the pharmacist is looking at."""

    def __init__(self, scenario="local_both", seed=7):
        self.cfg = DEFAULT
        self.rules = {"controlled": [], "cold": ["Insulin glargine"]}
        self.reset(scenario, seed)

    def reset(self, scenario, seed):
        self.scenario, self.seed = scenario, int(seed)
        self.sc = make_scenario(scenario, self.seed, self.cfg)
        self.sim = Simulation(self.sc, PolicyA(self.cfg), self.cfg)    # routine supplier ordering
        self.engine = PolicyC(self.cfg, explain=True)                  # MEDRESQ recommendations
        self.queue, self.audit, self._rec_day, self._next_id = [], [], -1, 1
        self.notes, self.ships, self._note_id = [], [], 1
        # start a few days into the disruption so there is something to look at
        start = self.sc.delay_window[0] + 4 if scenario != "normal" else 60
        while self.sim.day < start:
            self.sim.step()
        self.refresh()

    def refresh(self):
        """Ask MEDRESQ what it would recommend today (once per simulated day)."""
        d = self.sim.day
        if self._rec_day == d:
            return
        self._expire_old()
        self.engine.recs = []
        self.engine.decide(d, View(self.sim))
        d28 = CANDIDATES["shock"](View(self.sim).history(d, 56))   # same demand forecast MEDRESQ uses
        names = list(self.sim.net["site_id"])

        def cover(site, item, delta):
            return round(float(min((self.sim.stock[site, item] + delta) / max(d28[site, item], 1e-9), 99)), 1)
        self.engine.recs = [r for r in self.engine.recs if NAMES[r["item_index"]] not in self.rules["controlled"]]
        for r in self.engine.recs:
            if r["reason_code"] != "ABSTAIN":
                si, di, ii = names.index(r["src"]), names.index(r["dst"]), r["item_index"]
                r["cover"] = {"dst_before": cover(di, ii, 0), "dst_after": cover(di, ii, r["qty"]),
                              "src_before": cover(si, ii, 0), "src_after": cover(si, ii, -r["qty"])}
            r["item_name"] = NAMES[r["item_index"]]
            r["src_name"] = SITE_NAME.get(r["src"]) if r.get("src") else None
            r["dst_name"] = SITE_NAME.get(r["dst"])
            r["reason"] = pretty(r["reason"], r["item"], r["item_name"])
            r["alternative"] = pretty(r["alternative"], r["item"], r["item_name"])
            r["id"] = self._next_id
            self._next_id += 1
            r["day"] = d
            self.queue.append(r)
            if r["reason_code"] != "ABSTAIN":
                self._note(r["dst"], "suggest", f"MEDRESQ suggests you receive {r['qty']} units of {r['item_name']} from {r['src_name']}", r["id"])
                self._note(r["src"], "suggest", f"MEDRESQ suggests you send {r['qty']} units of {r['item_name']} to {r['dst_name']}", r["id"])
        self._rec_day = d

    def _expire_old(self):
        for r in self.queue:
            if r["status"] == "pending":
                r["status"] = "expired"
                self._log("system", "expired", r, "Not decided before the next day")

    def _note(self, site, kind, text, rid=None):
        self.notes.append({"id": self._note_id, "site": site, "kind": kind, "text": text, "rec_id": rid,
                           "day": self.sim.day, "time": time.strftime("%H:%M:%S"), "read": False})
        self._note_id += 1

    def _log(self, role, action, r, note=""):
        self.audit.append({
            "time": time.strftime("%H:%M:%S"), "sim_day": self.sim.day, "role": role,
            "action": action, "rec_id": r["id"], "item": r.get("item_name", r["item"]), "from": r.get("src_name"),
            "to": r.get("dst_name"), "qty": r.get("qty"), "net_benefit_jd": r.get("net_benefit_jd"), "reason": r["reason"], "note": note})

    def decide(self, rid, action, qty=None, note="", role="Pharmacist"):
        r = next((x for x in self.queue if x["id"] == rid), None)
        if r is None or r["status"] != "pending":
            return {"error": "Recommendation is not pending"}
        if r["reason_code"] == "ABSTAIN":
            return {"error": "Manual review required: this is not an executable recommendation"}
        names = list(self.sim.net["site_id"])
        if action == "reject":
            r["status"] = "rejected"
            self._log(role, "rejected", r, note)
            return {"ok": True}
        q = int(qty) if (action == "override" and qty is not None) else r["qty"]
        if q <= 0:
            return {"error": "Quantity must be positive"}
        t = Transfer(names.index(r["src"]), names.index(r["dst"]), r["item_index"], q, "approved")
        before, units0 = self.sim.metrics.n_transfers, self.sim.metrics.sent_units
        self.sim._execute_transfer(self.sim.day, t)
        shipped = self.sim.metrics.n_transfers > before
        if shipped:
            sent = int(round(self.sim.metrics.sent_units - units0))
            si_, di_ = names.index(r["src"]), names.index(r["dst"])
            days = self.sim.transit_days(si_, di_)
            self.ships.append({"id": len(self.ships) + 1, "rec_id": r["id"], "src": r["src"], "dst": r["dst"],
                               "src_name": r["src_name"], "dst_name": r["dst_name"], "item_name": r["item_name"],
                               "qty": sent, "sent_day": self.sim.day, "arrive_day": self.sim.day + days,
                               "notified": False})
            txt = f"{sent} units of {r['item_name']} are on the way from {r['src_name']} to {r['dst_name']}"
            self._note(r["src"], "shipped", txt, r["id"])
            self._note(r["dst"], "shipped", txt, r["id"])
        r["status"] = "overridden" if action == "override" else "approved"
        r["executed_qty"] = q
        self._log(role, r["status"], r, note + ("" if shipped else " (nothing shipped: stock not available)"))
        return {"ok": True, "shipped": shipped}

    # ---- center workspaces: each center answers for itself ----------------------
    def _ok(self, r):
        return r.setdefault("ok", {"src": False, "dst": False})

    def _days(self):
        return CANDIDATES["shock"](View(self.sim).history(self.sim.day, 56))

    def site_view(self, sid):
        names = list(self.sim.net["site_id"])
        if sid not in names:
            return {"error": "Unknown center"}
        si = names.index(sid)
        d28 = self._days()
        age = self.sim.record_age
        stock = []
        for i in range(self.sim.stock.shape[1]):
            u = float(self.sim.stock[si, i])
            stock.append({"i": i, "name": NAMES[i], "units": int(u),
                          "days": round(float(min(u / max(d28[si, i], 1e-9), 99)), 1),
                          "crit": int(self.sim.crit[i]), "age": int(age[si, i]),
                          "cold": NAMES[i] in self.rules["cold"], "controlled": NAMES[i] in self.rules["controlled"]})
        inbox = []
        for r in self.queue:
            if r["day"] != self.sim.day or r["reason_code"] == "ABSTAIN":
                continue
            if sid not in (r["src"], r["dst"]) or r["status"] not in ("pending", "approved", "overridden"):
                continue
            ok = self._ok(r)
            side = "send" if r["src"] == sid else "receive"
            mine, other = ("src", "dst") if side == "send" else ("dst", "src")
            inbox.append({**r, "cold": r["item_name"] in self.rules["cold"], "side": side, "me_ok": ok[mine], "other_ok": ok[other],
                          "other_name": r["dst_name"] if side == "send" else r["src_name"]})
        nm = SITE_NAME[sid]
        act = [a for a in self.audit if nm in (a.get("from"), a.get("to")) or a.get("role", "").startswith(nm)]
        meta = next(m for m in SITE_META if m[0] == sid)
        return {"id": sid, "name": nm, "short": meta[2], "type": meta[3], "day": self.sim.day,
                "stock": stock, "inbox": inbox, "activity": act[-12:][::-1]}

    def site_action(self, rid, sid, action, qty=None, cold_ok=False):
        r = next((x for x in self.queue if x["id"] == rid), None)
        if r is None or r["status"] != "pending":
            return {"error": "This request is no longer open"}
        if sid not in (r["src"], r["dst"]):
            return {"error": "This request does not involve your center"}
        nm, ok = SITE_NAME[sid], self._ok(r)
        side = "src" if r["src"] == sid else "dst"
        other = r["dst"] if side == "src" else r["src"]
        if action == "decline":
            r["status"] = "rejected"
            self._log(nm, "declined", r, "Declined by " + nm)
            self._note(other, "declined", f"{nm} declined the transfer of {r['item_name']}", r["id"])
            return {"ok": True, "state": "declined"}
        if side == "src" and r["item_name"] in self.rules["cold"] and not cold_ok:
            return {"error": "Cold-chain transport must be confirmed before sending"}
        if side == "src" and qty is not None and int(qty) != r["qty"]:
            q = int(qty)
            if q <= 0 or q > r["qty"]:
                return {"error": "You can send up to the requested quantity"}
            r["qty"] = q
            ok["dst"] = False
            self._log(nm, "changed quantity", r, f"Donor offers {q} units; the receiving center must confirm again")
        ok[side] = True
        self._log(nm, "confirmed", r, "Confirmed by " + nm)
        if not (ok["src"] and ok["dst"]):
            self._note(other, "confirmed", f"{nm} confirmed the transfer of {r['item_name']}. Your answer is needed", r["id"])
        if ok["src"] and ok["dst"]:
            res = self.decide(rid, "approve", None, "Both centers agreed", "Both centers")
            return {**res, "state": "shipped" if res.get("shipped") else "failed"}
        return {"ok": True, "state": "waiting"}

    def site_request(self, sid, mode, item, qty):
        names = list(self.sim.net["site_id"])
        if sid not in names or not (0 <= item < len(NAMES)) or qty <= 0:
            return {"error": "Choose a medicine and a positive quantity"}
        if NAMES[item] in self.rules["controlled"]:
            return {"error": "Controlled medicines cannot be transferred in this app"}
        me = names.index(sid)
        d28, stk, c = self._days(), self.sim.stock, self.sim.cfg
        cov = lambda s, delta=0: float(min((stk[s, item] + delta) / max(d28[s, item], 1e-9), 99))
        others = [s for s in range(len(names)) if s != me]
        if mode == "request":
            ok = [s for s in others if stk[s, item] >= qty and cov(s, -qty) >= 14]
            if not ok:
                return {"error": "No other center can spare that quantity safely"}
            src, dst = max(ok, key=lambda s: cov(s)), me
        else:
            if stk[me, item] < qty or cov(me, -qty) < 14:
                return {"error": "You would be left with less than 14 days of stock"}
            src, dst = me, min(others, key=lambda s: cov(s))
        n = self.sim.net
        km = float(np.hypot(n.x_km[src] - n.x_km[dst], n.y_km[src] - n.y_km[dst]))
        cost = round(c.transfer_fixed_cost + c.transfer_unit_km_cost * qty * km, 2)
        avoided = round(float(qty * self.sim.unit_cost[item] * c.shortage_multiplier[int(self.sim.crit[item]) - 1]), 2)
        a, b = SITE_NAME[names[src]], SITE_NAME[names[dst]]
        rec = {"day": self.sim.day, "item": self.sim.cat["item_id"].iloc[item], "item_index": item,
               "src": names[src], "dst": names[dst], "qty": int(qty),
               "reason_code": "REQUEST" if mode == "request" else "OFFER",
               "reason": (f"{b} asked for {qty} units of {NAMES[item]}. {a} has the most spare stock ({cov(src):.1f} days)."
                          if mode == "request" else
                          f"{a} offers {qty} units of {NAMES[item]}. {b} has the least stock ({cov(dst):.1f} days)."),
               "constraints": {"donor_keeps_reserve_days": round(cov(src, -qty)), "min_shelf_days_on_arrival": c.transfer_min_shelf_days,
                               "eligible_units_at_donor": int(stk[src, item]), "records_fresh": True},
               "cost_jd": cost, "avoided_shortage_value_jd": avoided, "alternative": "", "status": "pending",
               "donor_risk_jd": 0.0, "net_benefit_jd": round(avoided - cost, 2),
               "cover": {"dst_before": round(cov(dst), 1), "dst_after": round(cov(dst, qty), 1),
                         "src_before": round(cov(src), 1), "src_after": round(cov(src, -qty), 1)},
               "item_name": NAMES[item], "src_name": a, "dst_name": b, "id": self._next_id,
               "ok": {"src": mode == "offer", "dst": mode == "request"}}
        self._next_id += 1
        self.queue.append(rec)
        self._log(SITE_NAME[sid], "requested" if mode == "request" else "offered", rec, f"Manual {mode} by {SITE_NAME[sid]}")
        if mode == "request":
            self._note(names[src], "asked", f"{b} asked you for {qty} units of {NAMES[item]}", rec["id"])
        else:
            self._note(names[dst], "offered", f"{a} offered you {qty} units of {NAMES[item]}", rec["id"])
        return {"ok": True, "id": rec["id"], "from": a, "to": b}

    def advance(self, days):
        self._expire_old()
        for _ in range(days):
            if self.sim.day >= self.sim.cfg.horizon_days - 1:
                break
            self.sim.step()
        for sh in self.ships:
            if not sh["notified"] and self.sim.day >= sh["arrive_day"]:
                sh["notified"] = True
                self._note(sh["dst"], "delivered", f"{sh['qty']} units of {sh['item_name']} arrived from {sh['src_name']}", sh["rec_id"])
        self._rec_day = -1
        self.refresh()

    # ---- views -------------------------------------------------------------
    def tiles(self):
        m = self.sim.metrics
        return {"day": self.sim.day, "stockout_days": int(m.stockout_days),
                "service_level": round(m.service_level * 100, 2),
                "expiry_loss": round(m.expiry_loss, 0), "transfers": int(m.n_transfers),
                "total_cost": round(m.total_cost, 0)}

    def network(self):
        sim, c = self.sim, self.sim.cfg
        d = View(sim).history(sim.day, 28).mean(axis=0)
        cover = np.where(d > 0, sim.stock / np.maximum(d, 1e-9), 99)
        age = sim.record_age
        sites = []
        for s, row in sim.net.iterrows():
            meta = SITE_META[s]
            sites.append({"id": row.site_id, "name": meta[1], "short": meta[2], "type": meta[3],
                          "lat": meta[4], "lng": meta[5], "size": float(row["size"]), "x": float(row.x_km),
                          "y": float(row.y_km), "hit": bool(self.sc.site_mask[s])
                          if self.scenario != "normal" else False,
                          "low": int((cover[s] < 3).sum()),
                          "value": round(float((sim.stock[s] * sim.unit_cost).sum()), 0)})
        items = sim.cat[["item_id", "kind", "criticality", "imported"]].to_dict("records")
        for k, it in enumerate(items):
            it["name"] = NAMES[k]
        return {"sites": sites, "items": items,
                "cover": np.round(np.minimum(cover, 99), 1).tolist(),
                "age": age.tolist(), "stock": sim.stock.tolist(),
                "scenario": self.scenario, "seed": self.seed,
                "window": list(self.sc.delay_window),
                "delay_days": self.sc.delay_days, "spike_pct": self.sc.spike_pct}


LIVE = Live()


def num(v, lo, hi, what="value"):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number")
    if not (lo <= x <= hi) or x != x:
        raise ValueError(f"{what} must be between {lo} and {hi}")
    return x


def whatif(params):
    """Run policies A and C on a user-defined disruption (one seed, 1 year)."""
    delay = whole(params.get("delay", 21), 0, 28, "Delay")
    spike = num(params.get("spike", 0.6), 0, 1, "Spike")
    share = num(params.get("share", 0.375), 0.1, 1, "Sites affected")
    stale = num(params.get("stale", 0.0), 0, 0.4, "Stale records")
    length = whole(params.get("length", 18), 7, 28, "Duration")
    seed = whole(params.get("seed", 3), 1, 20, "Seed")
    spec = dict(delay=delay, spike=spike, stale=stale, share=share)
    cfg = dataclasses.replace(LIVE.cfg, horizon_days=365, disruption_start_min=60, disruption_start_max=200,
                              disruption_len_days=length, scenarios={**DEFAULT.scenarios, "custom": spec})
    out = {}
    for name, pol in (("A", PolicyA(cfg)), ("C", PolicyC(cfg, label="C"))):
        sc = make_scenario("custom", seed, cfg)
        sim = Simulation(sc, pol, cfg)
        m = sim.run()
        daily = np.array(sim.daily)
        out[name] = {"metrics": {k: (round(float(v), 3) if isinstance(v, float) else v)
                                 for k, v in m.as_row().items()},
                     "stockouts_weekly": [int(x) for x in daily[:, 0][: (len(daily) // 7) * 7]
                                          .reshape(-1, 7).sum(axis=1)],
                     "window": list(sc.delay_window)}
    out["params"] = {**spec, "length": length, "seed": seed}
    return out


def read_csv(name):
    p = os.path.join(OUT, name)
    return pd.read_csv(p) if os.path.exists(p) else None


# --------------------------------------------------------------------------- routes
# --------------------------------------------------------------------------- security
# Accounts: every account has its own password, stored only as a salted hash (accounts.json).
# First run: random passwords are generated and written to PASSWORDS.txt (delete it after you note them).
# MEDRESQ_EASY_PASSWORDS=1 uses the shared password "demo" (for demos and tests only).
import re
import secrets
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

EASY = os.environ.get("MEDRESQ_EASY_PASSWORDS") == "1"
IDLE_SECONDS, MAX_SECONDS = 60 * 60, 12 * 60 * 60          # session: 1 h idle, 12 h absolute
MAX_FAILS, FAIL_WINDOW = 5, 300                              # 5 wrong passwords per 5 minutes
ACCOUNTS = {"coordinator": {"role": "coordinator", "site": None, "name": "Network coordinator"},
            "auditor": {"role": "reviewer", "site": None, "name": "Reviewer (read only)"}}
for _m in SITE_META:
    ACCOUNTS["manager_" + _m[0].lower()] = {"role": "manager", "site": _m[0], "name": "Manager, " + _m[1]}
HASHES = {}
TOKENS = {}
FAILS = {}
app.config["AUTH"] = True
app.config["MAX_CONTENT_LENGTH"] = 1_500_000


def _load_hashes():
    path = os.path.join(HERE, "accounts.json")
    if EASY:
        return {u: generate_password_hash("demo") for u in ACCOUNTS}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if set(data) >= set(ACCOUNTS):
            return data
    plain = {u: secrets.token_urlsafe(6) for u in ACCOUNTS}
    hashes = {u: generate_password_hash(p) for u, p in plain.items()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hashes, f, indent=1)
    with open(os.path.join(HERE, "PASSWORDS.txt"), "w", encoding="utf-8") as f:
        f.write("MEDRESQ demo passwords (note them, then delete this file).\n"
                "To get new ones, delete accounts.json and restart.\n\n")
        f.writelines(f"{u:16} {p}\n" for u, p in plain.items())
    print("New passwords were written to PASSWORDS.txt next to the app.", flush=True)
    return hashes


HASHES = _load_hashes()


def clean_text(v, n=200):
    """Text from a person: plain string, no control characters, limited length."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(v or ""))[:n].strip()


def csv_safe(v):
    """Stop spreadsheet formulas in exported text (CSV injection)."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def whole(v, lo, hi, what="value"):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a whole number")
    if not lo <= n <= hi:
        raise ValueError(f"{what} must be between {lo} and {hi}")
    return n


def current_user():
    tok = request.cookies.get("medresq_token") or request.headers.get("X-Token", "")
    s = TOKENS.get(tok)
    if not s:
        return None
    now = time.time()
    if now - s["last"] > IDLE_SECONDS or now - s["created"] > MAX_SECONDS:
        TOKENS.pop(tok, None)
        return None
    s["last"] = now
    return {"user": s["user"], **ACCOUNTS[s["user"]]}


@app.before_request
def guard():
    p = request.path
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and p.startswith("/api/"):
        if not request.is_json:
            return jsonify({"error": "JSON requests only"}), 415
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:
            return jsonify({"error": "Cross-site request blocked"}), 403
    if not app.config["AUTH"] or not p.startswith(("/api/", "/report")):
        return None
    if p in ("/api/login", "/api/accounts", "/api/me"):
        return None
    if current_user() is None:
        return jsonify({"error": "Please sign in"}), 401
    return None


@app.after_request
def secure_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=(), payment=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
    if request.path.startswith(("/api/", "/report")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(Exception)
def handle_error(e):
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description if e.code < 500 else "Server error"}), e.code
    if isinstance(e, ValueError):
        return jsonify({"error": str(e)}), 400
    if isinstance(e, (KeyError, TypeError)):
        return jsonify({"error": "Missing or wrong input"}), 400
    app.logger.exception("Unhandled error")
    return jsonify({"error": "Server error"}), 500


def deny(role_ok, site=None):
    """Return an error response unless the signed-in user may act (auth off = allow, for tests)."""
    if not app.config["AUTH"]:
        return None
    u = current_user()
    if u is None or u["role"] not in role_ok or (site is not None and u["role"] == "manager" and u["site"] != site):
        return jsonify({"error": "Your role is not allowed to do this"}), 403
    return None


@app.get("/api/accounts")
def api_accounts():
    return jsonify({"easy": EASY, "accounts": [{"user": k, **v} for k, v in ACCOUNTS.items()]})


@app.post("/api/login")
def api_login():
    j = request.get_json(force=True)
    user, ip, now = str(j.get("user", "")), request.remote_addr, time.time()
    key = (ip, user)
    FAILS[key] = [t for t in FAILS.get(key, []) if now - t < FAIL_WINDOW]
    if len(FAILS[key]) >= MAX_FAILS:
        return jsonify({"error": "Too many wrong attempts. Wait a few minutes and try again."}), 429
    ok = user in HASHES and check_password_hash(HASHES[user], str(j.get("password", "")))
    if not ok:
        FAILS[key].append(now)
        return jsonify({"error": "Wrong user or password"}), 401
    FAILS.pop(key, None)
    for t in [t for t, s in TOKENS.items() if now - s["last"] > IDLE_SECONDS]:
        TOKENS.pop(t, None)
    tok = secrets.token_hex(24)
    TOKENS[tok] = {"user": user, "created": now, "last": now}
    resp = jsonify({"ok": True, "user": user, **ACCOUNTS[user]})
    resp.set_cookie("medresq_token", tok, httponly=True, samesite="Strict", secure=request.is_secure,
                    max_age=MAX_SECONDS)
    return resp


@app.post("/api/logout")
def api_logout():
    TOKENS.pop(request.cookies.get("medresq_token", ""), None)
    resp = jsonify({"ok": True})
    resp.delete_cookie("medresq_token")
    return resp


@app.get("/api/me")
def api_me():
    u = current_user()
    return (jsonify(u), 200) if u else (jsonify({"error": "Please sign in"}), 401)


@app.get("/sw.js")
def service_worker():
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
def api_state():
    with LOCK:
        return jsonify({"tiles": LIVE.tiles(), "network": LIVE.network()})


@app.post("/api/reset")
def api_reset():
    j = request.get_json(force=True)
    bad = deny(("coordinator",))
    if bad:
        return bad
    with LOCK:
        sc = j.get("scenario", "local_both")
        if sc not in LIVE.cfg.scenarios:
            raise ValueError("Unknown scenario")
        LIVE.reset(sc, whole(j.get("seed", 7), 0, 1_000_000, "Seed"))
        return jsonify({"ok": True})


@app.post("/api/advance")
def api_advance():
    j = request.get_json(force=True)
    bad = deny(("coordinator",))
    if bad:
        return bad
    with LOCK:
        LIVE.advance(whole(j.get("days", 1), 1, 30, "Days"))
        return jsonify({"ok": True, "day": LIVE.sim.day})


@app.get("/api/recommendations")
def api_recs():
    with LOCK:
        LIVE.refresh()
        return jsonify({"day": LIVE.sim.day, "items": [r for r in LIVE.queue
                                                         if r["day"] == LIVE.sim.day]})


@app.post("/api/decision")
def api_decision():
    j = request.get_json(force=True)
    bad = deny(("coordinator",))
    if bad:
        return bad
    with LOCK:
        if j.get("action") not in ("approve", "reject", "override"):
            raise ValueError("Unknown action")
        who = (current_user() or {}).get("name", "Pharmacist")
        res = LIVE.decide(whole(j["id"], 0, 10**9, "id"), j["action"],
                          None if j.get("qty") is None else whole(j["qty"], 1, 100000, "Quantity"),
                          clean_text(j.get("note", "")), who)
        return jsonify(res), (400 if "error" in res else 200)


@app.get("/api/site/<sid>")
def api_site(sid):
    with LOCK:
        LIVE.refresh()
        v = LIVE.site_view(sid)
        return jsonify(v), (404 if "error" in v else 200)


@app.post("/api/site_action")
def api_site_action():
    j = request.get_json(force=True)
    bad = deny(("manager",), j.get("site"))
    if bad:
        return bad
    with LOCK:
        if j.get("action") not in ("accept", "decline"):
            raise ValueError("Unknown action")
        res = LIVE.site_action(whole(j["id"], 0, 10**9, "id"), j["site"], j["action"],
                               None if j.get("qty") is None else whole(j["qty"], 1, 100000, "Quantity"),
                               bool(j.get("cold_ok")))
        return jsonify(res), (400 if "error" in res else 200)


@app.post("/api/site_request")
def api_site_request():
    j = request.get_json(force=True)
    bad = deny(("manager",), j.get("site"))
    if bad:
        return bad
    with LOCK:
        if j.get("mode", "request") not in ("request", "offer"):
            raise ValueError("Unknown mode")
        item, qty = whole(j["item"], 0, len(NAMES) - 1, "Medicine"), whole(j["qty"], 1, 100000, "Quantity")
        res = LIVE.site_request(j["site"], j.get("mode", "request"), item, qty)
        if "error" in res and res["error"].startswith("No other center"):
            res["alternatives"] = alternatives(item, qty)
        return jsonify(res), (400 if "error" in res else 200)


@app.get("/api/notifications")
def api_notes():
    u = current_user() if app.config["AUTH"] else None
    site = (u["site"] if u and u["role"] == "manager" else None) or request.args.get("site") or (u and u["site"])
    with LOCK:
        LIVE.refresh()
        items = [n for n in LIVE.notes if n["site"] == site][-30:][::-1]
        return jsonify({"site": site, "unread": sum(1 for n in items if not n["read"]), "items": items})


@app.post("/api/notifications/read")
def api_notes_read():
    j = request.get_json(force=True)
    with LOCK:
        u = current_user() if app.config["AUTH"] else None
        for n in LIVE.notes:
            if n["site"] == j.get("site") and (u is None or u["role"] != "manager" or u["site"] == n["site"]):
                n["read"] = True
        return jsonify({"ok": True})


@app.get("/api/shipments")
def api_ships():
    with LOCK:
        d = LIVE.sim.day
        return jsonify([{**x, "status": "delivered" if d >= x["arrive_day"] else "in transit", "today": d}
                        for x in LIVE.ships][::-1])


# Ideas only: same therapeutic class, never a substitution rule. A pharmacist decides.
CLASS_IDEAS = [("antihistamine", ["Loratadine", "Antihistamine syrup"]),
               ("pain and fever", ["Paracetamol 500 mg", "Ibuprofen"]),
               ("blood pressure", ["Amlodipine", "Losartan"])]


def alternatives(item, qty):
    names = list(LIVE.sim.net["site_id"])
    out = []
    for _, group in CLASS_IDEAS:
        if NAMES[item] in group:
            for nm in group:
                k = NAMES.index(nm)
                if k == item or int(LIVE.sim.crit[k]) == 3:
                    continue
                d28 = LIVE._days()
                for si, sid in enumerate(names):
                    cover = float(min(LIVE.sim.stock[si, k] / max(d28[si, k], 1e-9), 99))
                    if LIVE.sim.stock[si, k] >= qty and cover >= 21:
                        out.append({"item": nm, "site": SITE_NAME[sid], "days": round(cover, 1)})
                        break
    return out


@app.get("/api/alternatives")
def api_alt():
    with LOCK:
        return jsonify(alternatives(whole(request.args["item"], 0, len(NAMES) - 1, "Medicine"), whole(request.args.get("qty", 1), 1, 100000, "Quantity")))


def report_data():
    sim, tiles = LIVE.sim, LIVE.tiles()
    done = [r for r in LIVE.queue if r["status"] in ("approved", "overridden")]
    saved = sum((r.get("net_benefit_jd") or 0) * min(1.0, (r.get("executed_qty") or r["qty"]) / r["qty"]) for r in done if r.get("qty"))
    by_site = {}
    for r in LIVE.queue:
        if r["reason_code"] == "ABSTAIN" or r["status"] not in ("approved", "overridden"):
            continue
        q = r.get("executed_qty") or r["qty"]
        by_site.setdefault(r["src"], {"sent": 0, "received": 0})["sent"] += q
        by_site.setdefault(r["dst"], {"sent": 0, "received": 0})["received"] += q
    return sim, tiles, done, saved, by_site


@app.get("/report.pdf")
def report_pdf():
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.graphics.shapes import Circle, Drawing, Rect
    from xml.sax.saxutils import escape
    with LOCK:
        LIVE.refresh()
        sim, tiles, done, saved, by_site = report_data()
        audit = list(LIVE.audit[-18:])
        ships = len(LIVE.ships)
        units = sum(x["qty"] for x in LIVE.ships)
    teal = colors.HexColor("#0e6b6f")
    st = getSampleStyleSheet()
    h = ParagraphStyle("h", parent=st["Heading2"], textColor=teal, spaceBefore=14)
    small = ParagraphStyle("s", parent=st["BodyText"], fontSize=8.5, textColor=colors.HexColor("#587077"))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title="MEDRESQ report", author="MEDRESQ")
    logo = Drawing(34, 34)
    logo.add(Circle(17, 17, 15, fillColor=teal, strokeColor=teal))
    logo.add(Rect(14.5, 8, 5, 18, fillColor=colors.white, strokeColor=colors.white))
    logo.add(Rect(8, 14.5, 18, 5, fillColor=colors.white, strokeColor=colors.white))
    head = Table([[logo, Paragraph("<b>MEDRESQ report</b>", ParagraphStyle("t", parent=st["Title"], alignment=0, fontSize=22, textColor=teal)),
                   Paragraph(f"Simulated day {sim.day}<br/>Created {time.strftime('%Y-%m-%d %H:%M')}", small)]], colWidths=[14 * mm, 110 * mm, 50 * mm])
    kp = [["Transfers shipped", ships, "Units shipped", units],
          ["Estimated net benefit (JD)", f"{saved:,.0f}", "Service level", f"{tiles['service_level']}%"],
          ["Stockout days", f"{tiles['stockout_days']:,}", "Expired value (JD)", f"{tiles['expiry_loss']:,}"],
          ["Total cost (JD)", f"{tiles['total_cost']:,}", "", ""]]
    t1 = Table(kp, colWidths=[52 * mm, 35 * mm, 52 * mm, 35 * mm])
    t1.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .4, colors.HexColor("#d3dedc")), ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#587077")), ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#587077")),
                            ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"), ("FONTNAME", (3, 0), (3, -1), "Helvetica-Bold")]))
    rows = [["Center", "Units received", "Units sent"]] + [[SITE_NAME[k], v["received"], v["sent"]] for k, v in sorted(by_site.items())]
    t2 = Table(rows if len(rows) > 1 else rows + [["No transfers yet", "", ""]], colWidths=[84 * mm, 45 * mm, 45 * mm])
    t3rows = [["Day", "Who", "Action", "Item", "Qty"]] + [[a["sim_day"], escape(a["role"])[:26], a["action"], a["item"], a["qty"] or ""] for a in audit]
    t3 = Table(t3rows if len(t3rows) > 1 else t3rows + [["", "No decisions yet", "", "", ""]], colWidths=[14 * mm, 56 * mm, 32 * mm, 52 * mm, 20 * mm])
    for t in (t2, t3):
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), teal), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                               ("GRID", (0, 0), (-1, -1), .3, colors.HexColor("#d3dedc")),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f7f6")])]))
    doc.build([head, Spacer(1, 6), Paragraph("SIMULATED DATA", ParagraphStyle("b", parent=st["BodyText"], textColor=colors.HexColor("#b9770e"), fontName="Helvetica-Bold")),
               Spacer(1, 6), t1, Paragraph("By center", h), t2, Paragraph("Latest decisions", h), t3, Spacer(1, 10),
               Paragraph("All stock, shortages, transfers and savings in this report are simulated on synthetic data under assumed prices. "
                         "Hospital names mark real places only. It is not a measured result from real hospitals.", small)])
    return Response(buf.getvalue(), mimetype="application/pdf", headers={"Content-Disposition": "attachment; filename=medresq_report.pdf"})


@app.get("/report")
def report():
    with LOCK:
        LIVE.refresh()
        sim, tiles = LIVE.sim, LIVE.tiles()
        done = [r for r in LIVE.queue if r["status"] in ("approved", "overridden")]
        saved = sum((r.get("net_benefit_jd") or 0) * min(1.0, (r.get("executed_qty") or r["qty"]) / r["qty"]) for r in done if r.get("qty"))
        units = sum(x["qty"] for x in LIVE.ships)
        decided = [r for r in LIVE.queue if r["reason_code"] != "ABSTAIN"]
        by_site = {}
        for r in decided:
            for side in ("src", "dst"):
                d = by_site.setdefault(r[side], {"sent": 0, "received": 0, "asked": 0})
                if r["status"] in ("approved", "overridden"):
                    d["sent" if side == "src" else "received"] += r.get("executed_qty") or r["qty"]
        for r in decided:
            if r["reason_code"] in ("REQUEST", "OFFER"):
                by_site.setdefault(r["dst"] if r["reason_code"] == "REQUEST" else r["src"], {"sent": 0, "received": 0, "asked": 0})["asked"] += 1
        rows = "".join(f"<tr><td>{SITE_NAME[k]}</td><td class='n'>{v['received']}</td><td class='n'>{v['sent']}</td><td class='n'>{v['asked']}</td></tr>"
                       for k, v in sorted(by_site.items()))
        aud = "".join(f"<tr><td>{a['sim_day']}</td><td>{a['role']}</td><td>{a['action']}</td><td>{a['item']}</td><td class='n'>{a['qty'] or ''}</td></tr>"
                      for a in LIVE.audit[-25:])
        html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>MEDRESQ report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font:14px/1.5 "Segoe UI",system-ui,sans-serif;color:#12262b;max-width:860px;margin:0 auto;padding:28px}}
h1{{margin:0;font-size:28px}}h2{{margin:26px 0 8px;font-size:17px;border-bottom:2px solid #0e6b6f;padding-bottom:4px}}
.k{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:14px}}.k div{{border:1px solid #d3dedc;border-radius:10px;padding:10px 12px}}
.k small{{display:block;color:#587077}}.k b{{font-size:22px;color:#0e6b6f}}table{{width:100%;border-collapse:collapse}}th,td{{padding:6px 8px;border-bottom:1px solid #e1e8e6;text-align:left}}
.n{{text-align:right}}.note{{color:#587077;font-size:12.5px}}.tag{{display:inline-block;background:#e8a21a;color:#1b1300;font-weight:700;font-size:12px;padding:2px 10px;border-radius:99px}}
button{{background:#0e6b6f;color:#fff;border:0;border-radius:99px;padding:9px 18px;font:inherit;font-weight:600;cursor:pointer}}
html[dir=rtl] th,html[dir=rtl] td{{text-align:right}}html[dir=rtl] .n{{text-align:left}}
@media print{{.noprint{{display:none}}body{{padding:0}}}}@media(max-width:640px){{.k{{grid-template-columns:1fr 1fr}}}}</style></head><body>
<div class="noprint" style="text-align:right"><button onclick="window.print()">Print or save as PDF</button></div>
<h1>MEDRESQ report</h1><p class="note">Simulated day {sim.day}. Created {time.strftime("%Y-%m-%d %H:%M")}.</p><span class="tag">SIMULATED DATA</span>
<div class="k"><div><small>Requests and suggestions</small><b>{len(decided)}</b></div><div><small>Transfers shipped</small><b>{len(LIVE.ships)}</b></div>
<div><small>Units shipped</small><b>{units}</b></div><div><small>Estimated net benefit</small><b>{saved:,.0f} JD</b></div>
<div><small>Service level</small><b>{tiles['service_level']}%</b></div><div><small>Stockout days</small><b>{tiles['stockout_days']:,}</b></div>
<div><small>Expired value (JD)</small><b>{tiles['expiry_loss']:,}</b></div><div><small>Total cost (JD)</small><b>{tiles['total_cost']:,}</b></div></div>
<h2>By center</h2><table><tr><th>Center</th><th class="n">Units received</th><th class="n">Units sent</th><th class="n">Manual requests</th></tr>{rows or '<tr><td colspan=4>No transfers yet.</td></tr>'}</table>
<h2>Latest decisions</h2><table><tr><th>Sim day</th><th>Role</th><th>Action</th><th>Item</th><th class="n">Qty</th></tr>{aud or '<tr><td colspan=5>No decisions yet.</td></tr>'}</table>
<p class="note">All stock, shortages, transfers and savings in this report are simulated on synthetic data under assumed prices. Hospital names mark real places only. It is not a measured result from real hospitals.</p>
<script src="/static/i18n.js"></script></body></html>"""
        return Response(html, mimetype="text/html")


# --------------------------------------------------------------------------- rules, settings
@app.get("/api/rules")
def api_rules():
    return jsonify({"items": NAMES, **LIVE.rules})


@app.post("/api/rules")
def api_rules_set():
    bad = deny(("coordinator",))
    if bad:
        return bad
    j = request.get_json(force=True)
    out = {}
    for k in ("controlled", "cold"):
        v = j.get(k, [])
        if not isinstance(v, list) or any(x not in NAMES for x in v):
            raise ValueError("Unknown medicine in the list")
        out[k] = sorted(set(v))
    with LOCK:
        LIVE.rules = out
        LIVE.queue = [r for r in LIVE.queue if not (r["day"] == LIVE.sim.day and r["status"] == "pending")]
        LIVE._rec_day = -1
        LIVE.refresh()
    return jsonify({"ok": True, **out})


SETTING_FIELDS = [  # key, label, lower, upper, step
    ("price_scale", "Medicine prices (x the example prices)", 0.2, 5, 0.1),
    ("transfer_fixed_cost", "Cost of one transfer (JD)", 0, 200, 1),
    ("transfer_unit_km_cost", "Transport cost per unit per km (JD)", 0, 0.1, 0.001),
    ("shortage_c1", "Penalty per unit short, low-risk medicine (x price)", 0, 50, 0.5),
    ("shortage_c2", "Penalty per unit short, medium-risk medicine (x price)", 0, 50, 0.5),
    ("shortage_c3", "Penalty per unit short, critical medicine (x price)", 0, 50, 0.5),
    ("holding_rate_per_day", "Daily cost of holding stock (share of price)", 0, 0.01, 0.0001),
    ("order_fixed_cost", "Cost of one supplier order (JD)", 0, 500, 1),
    ("transfer_min_shelf_days", "Minimum shelf life of transferred stock (days)", 0, 180, 5),
    ("donor_cover_days", "Days of stock the sender must keep", 7, 60, 1),
]


def cfg_values(cfg):
    v = {f: getattr(cfg, f) for f, *_ in SETTING_FIELDS if hasattr(cfg, f)}
    v.update(shortage_c1=cfg.shortage_multiplier[0], shortage_c2=cfg.shortage_multiplier[1], shortage_c3=cfg.shortage_multiplier[2])
    return v


def cfg_from(values):
    kw = {}
    for key, label, lo, hi, _ in SETTING_FIELDS:
        if key in values:
            kw[key] = num(values[key], lo, hi, label)
    mult = tuple(kw.pop(k, cfg_values(LIVE.cfg)[k]) for k in ("shortage_c1", "shortage_c2", "shortage_c3"))
    for k in ("transfer_min_shelf_days", "donor_cover_days"):
        if k in kw:
            kw[k] = int(kw[k])
    return dataclasses.replace(LIVE.cfg, shortage_multiplier=mult, **kw)


@app.get("/api/settings")
def api_settings():
    return jsonify({"fields": [{"key": k, "label": l, "min": lo, "max": hi, "step": st} for k, l, lo, hi, st in SETTING_FIELDS],
                    "values": cfg_values(LIVE.cfg), "defaults": cfg_values(DEFAULT)})


def run_pair(cfg, scenario, seed):
    cfg = dataclasses.replace(cfg, horizon_days=365, disruption_start_min=60, disruption_start_max=200)
    out = {}
    for name, pol in (("A", PolicyA(cfg)), ("C", PolicyC(cfg, label="C"))):
        sim = Simulation(make_scenario(scenario, seed, cfg), pol, cfg)
        m = sim.run()
        out[name] = {"total": round(float(m.total_cost), 0), "stockouts": int(m.stockout_days),
                     "expiry": round(float(m.expiry_loss), 0), "transfers": int(m.n_transfers)}
    out["saving_jd"] = out["A"]["total"] - out["C"]["total"]
    out["saving_pct"] = round(100 * out["saving_jd"] / out["A"]["total"], 1) if out["A"]["total"] else 0.0
    return out


@app.post("/api/settings/compare")
def api_settings_compare():
    """Run site-by-site and MEDRESQ with the proposed numbers and with the example numbers."""
    bad = deny(("coordinator",))
    if bad:
        return bad
    j = request.get_json(force=True)
    sc = j.get("scenario", "local_both")
    if sc not in DEFAULT.scenarios:
        raise ValueError("Unknown scenario")
    seed = whole(j.get("seed", 3), 1, 20, "Seed")
    mine = cfg_from(j.get("values", {}))
    return jsonify({"mine": run_pair(mine, sc, seed), "example": run_pair(DEFAULT, sc, seed), "scenario": sc, "seed": seed})


@app.post("/api/settings")
def api_settings_set():
    bad = deny(("coordinator",))
    if bad:
        return bad
    j = request.get_json(force=True)
    with LOCK:
        LIVE.cfg = DEFAULT if j.get("reset") else cfg_from(j.get("values", {}))
        LIVE.reset(LIVE.scenario, LIVE.seed)
    return jsonify({"ok": True, "values": cfg_values(LIVE.cfg)})


# --------------------------------------------------------------------------- upload your own stock (preview)
UPLOAD_TEMPLATE = "site,item,units,daily_demand\nPharmacy A,Amlodipine,40,10\nPharmacy A,Insulin glargine,6,3\nHospital B,Amlodipine,900,12\nHospital B,Insulin glargine,120,2\n"


@app.get("/api/upload/template.csv")
def api_upload_template():
    return Response(UPLOAD_TEMPLATE, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=stock_template.csv"})


def analyse_upload(text):
    """Simple rule-based preview on a person's own stock table (NOT the full MEDRESQ forecast)."""
    rd = csv.DictReader(io.StringIO(text))
    cols = {(c or "").strip().lower(): c for c in (rd.fieldnames or [])}
    need = ("site", "item", "units", "daily_demand")
    if any(n not in cols for n in need):
        raise ValueError("The file needs these columns: site, item, units, daily_demand")
    rows, errors = [], []
    for k, r in enumerate(rd, start=2):
        if k > 3001:
            errors.append("Only the first 3000 rows were read")
            break
        try:
            site, item = clean_text(r[cols["site"]], 60), clean_text(r[cols["item"]], 60)
            units, dem = float(r[cols["units"]]), float(r[cols["daily_demand"]])
            if not site or not item or units < 0 or dem < 0 or units != units or dem != dem or units > 1e7 or dem > 1e6:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            if len(errors) < 8:
                errors.append(f"Row {k} was skipped (check the numbers)")
            continue
        rows.append({"site": site, "item": item, "units": units, "dem": dem,
                     "days": min(units / dem, 99) if dem > 0 else 99})
    blocked = set(LIVE.rules["controlled"])
    plan = []
    for item in sorted({r["item"] for r in rows}):
        if item in blocked:
            continue
        grp = [dict(r) for r in rows if r["item"] == item]
        for rec in sorted([g for g in grp if g["days"] < 3], key=lambda g: g["days"]):
            want = max(0.0, 7 * rec["dem"] - rec["units"])
            for don in sorted(grp, key=lambda g: -g["days"]):
                spare = don["units"] - 14 * don["dem"]
                if don is rec or spare < 1 or want < 1:
                    continue
                q = int(min(want, spare))
                if q < 1:
                    continue
                plan.append({"item": item, "from": don["site"], "to": rec["site"], "qty": q,
                             "to_before": round(rec["days"], 1), "to_after": round(min((rec["units"] + q) / rec["dem"], 99), 1) if rec["dem"] else 99,
                             "from_after": round(min((don["units"] - q) / don["dem"], 99), 1) if don["dem"] else 99})
                don["units"] -= q
                rec["units"] += q
                want -= q
                if want < 1:
                    break
    risky = sorted([r for r in rows if r["days"] < 3], key=lambda r: r["days"])
    return {"rows": len(rows), "errors": errors, "at_risk": [{"site": r["site"], "item": r["item"], "days": round(r["days"], 1)} for r in risky[:40]],
            "plan": plan[:60], "excluded": sorted(blocked)}


@app.post("/api/upload")
def api_upload():
    bad = deny(("coordinator", "manager"))
    if bad:
        return bad
    j = request.get_json(force=True)
    text = j.get("csv", "")
    if not isinstance(text, str) or len(text) > 1_000_000:
        raise ValueError("The file is too large or not text")
    return jsonify(analyse_upload(text))


# --------------------------------------------------------------------------- pitch mode (scripted, coordinator only)
@app.post("/api/pitch")
def api_pitch():
    bad = deny(("coordinator",))
    if bad:
        return bad
    step = request.get_json(force=True).get("step")
    with LOCK:
        if step == "reset":
            LIVE.reset("local_both", 7)
            LIVE.pitch_rec = None
            LIVE.refresh()
            todo = [r for r in LIVE.queue if r["status"] == "pending" and r["reason_code"] == "SHORTAGE_RISK"
                    and r["item_name"] not in LIVE.rules["cold"]]
            r = max(todo, key=lambda x: x.get("net_benefit_jd") or 0)
            LIVE.pitch_rec = r["id"]
            return jsonify({"ok": True, "rec": {k: r[k] for k in ("id", "src", "dst", "src_name", "dst_name", "item_name", "qty")}})
        rid = getattr(LIVE, "pitch_rec", None)
        r = next((x for x in LIVE.queue if x["id"] == rid), None)
        if step in ("confirm_dst", "confirm_src") and r:
            res = LIVE.site_action(rid, r["dst"] if step == "confirm_dst" else r["src"], "accept", None, True)
            return jsonify(res)
        if step == "advance":
            LIVE.advance(2)
            return jsonify({"ok": True})
    raise ValueError("Unknown step")


@app.get("/api/live_savings")
def api_live_savings():
    with LOCK:
        n, net, avoided, cost = 0, 0.0, 0.0, 0.0
        for r in LIVE.queue:
            if r["status"] in ("approved", "overridden") and r.get("qty"):
                f = min(1.0, (r.get("executed_qty") or r["qty"]) / r["qty"])
                n += 1
                net += (r.get("net_benefit_jd") or 0) * f
                avoided += (r.get("avoided_shortage_value_jd") or 0) * f
                cost += (r.get("cost_jd") or 0)
        return jsonify({"approved": n, "net_jd": round(net, 2), "avoided_jd": round(avoided, 2), "cost_jd": round(cost, 2)})


@app.get("/api/audit")
def api_audit():
    with LOCK:
        return jsonify(LIVE.audit)


@app.get("/api/audit.csv")
def api_audit_csv():
    with LOCK:
        buf = io.StringIO()
        cols = ["time", "sim_day", "role", "action", "rec_id", "item", "from", "to", "qty",
                "reason", "note", "net_benefit_jd"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows([{k: csv_safe(v) for k, v in row.items()} for row in LIVE.audit])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=audit_log.csv"})


@app.post("/api/whatif")
def api_whatif():
    return jsonify(whatif(request.get_json(force=True)))


@app.get("/api/results")
def api_results():
    out = {}
    for key, f in (("summary", "main_summary.csv"), ("paired", "paired_C_vs_A.csv"),
                   ("ablation", "ablation_summary.csv"), ("sensitivity", "sensitivity_summary.csv"),
                   ("forecast", "forecast_benchmark.csv")):
        df = read_csv(f)
        out[key] = None if df is None else json.loads(df.round(4).to_json(orient="records"))
    out["assumptions"] = {k: (v if not isinstance(v, dict) else v) for k, v in
                          DEFAULT.__dict__.items() if k != "scenarios"}
    out["scenarios"] = DEFAULT.scenarios
    return jsonify(out)


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)), debug=False)
