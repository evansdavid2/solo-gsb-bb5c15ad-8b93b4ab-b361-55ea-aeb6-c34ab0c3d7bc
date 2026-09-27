"""HTTP-level tests for the FastAPI app (require fastapi + httpx installed)."""

import unittest

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PAYLOAD = {
    "segments": [100, 251, 80],
    "passengers": [
        {"id": "alice", "board": 0, "alight": 3},
        {"id": "bob", "board": 1, "alight": 3},
        {"id": "carol", "board": 0, "alight": 1},
    ],
}

SETTLE_PAYLOAD = dict(PAYLOAD, payments=[
    {"id": "alice", "amount": 331},
    {"id": "bob", "amount": 100},
    {"id": "carol", "amount": 0},
])


class SplitApiTests(unittest.TestCase):
    def test_split_ok(self):
        resp = client.post("/split", json=PAYLOAD)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["total_cost"], 431)
        self.assertEqual(body["totals"], {"alice": 216, "bob": 165, "carol": 50})
        self.assertEqual(sum(body["totals"].values()), body["total_cost"])
        self.assertEqual(len(body["segments"]), 3)
        for seg, cost in zip(body["segments"], PAYLOAD["segments"]):
            self.assertEqual(seg["cost"], cost)
            self.assertEqual(sum(seg["shares"].values()), cost)
        self.assertEqual(body["segments"][0]["passengers"], ["alice", "carol"])
        self.assertEqual(body["segments"][1]["shares"], {"alice": 126, "bob": 125})

    def test_passenger_order_does_not_matter(self):
        reversed_payload = dict(PAYLOAD, passengers=list(reversed(PAYLOAD["passengers"])))
        r1 = client.post("/split", json=PAYLOAD)
        r2 = client.post("/split", json=reversed_payload)
        self.assertEqual(r1.status_code, r2.status_code)
        self.assertEqual(r1.json(), r2.json())

    def test_empty_load_charged_segment_is_422(self):
        payload = {"segments": [100, 0, 50],
                   "passengers": [{"id": "a", "board": 2, "alight": 3}]}
        resp = client.post("/split", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("no passengers on board", resp.text)

    def test_zero_cost_empty_segment_is_ok(self):
        payload = {"segments": [0, 50],
                   "passengers": [{"id": "a", "board": 1, "alight": 2}]}
        resp = client.post("/split", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["totals"], {"a": 50})

    def test_board_not_before_alight_is_422(self):
        payload = {"segments": [10, 10],
                   "passengers": [{"id": "a", "board": 1, "alight": 1}]}
        resp = client.post("/split", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("invalid interval", resp.text)

    def test_station_out_of_range_is_422(self):
        payload = {"segments": [10, 10],
                   "passengers": [{"id": "a", "board": 0, "alight": 3}]}
        resp = client.post("/split", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("out of range", resp.text)

    def test_negative_board_is_422(self):
        payload = {"segments": [10],
                   "passengers": [{"id": "a", "board": -1, "alight": 1}]}
        self.assertEqual(client.post("/split", json=payload).status_code, 422)

    def test_duplicate_passenger_id_is_422(self):
        payload = {"segments": [10],
                   "passengers": [{"id": "a", "board": 0, "alight": 1},
                                  {"id": "a", "board": 0, "alight": 1}]}
        resp = client.post("/split", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("duplicate passenger id", resp.text)

    def test_negative_cost_is_422(self):
        payload = {"segments": [10, -5],
                   "passengers": [{"id": "a", "board": 0, "alight": 2}]}
        self.assertEqual(client.post("/split", json=payload).status_code, 422)

    def test_empty_segments_is_422(self):
        self.assertEqual(client.post("/split", json={"segments": []}).status_code, 422)

    def test_unknown_field_is_422(self):
        payload = dict(PAYLOAD, currency="usd")
        self.assertEqual(client.post("/split", json=payload).status_code, 422)


class AdmitApiTests(unittest.TestCase):
    ADMIT_PAYLOAD = {
        "segments": [100, 251, 80],
        "passengers": [
            {"id": "alice", "board": 0, "alight": 3},
            {"id": "carol", "board": 0, "alight": 1},
        ],
        "capacity": 3,
        "candidates": [
            {"id": "bob", "board": 1, "alight": 3, "max_fare": 165},
            {"id": 7, "board": 2, "alight": 3, "max_fare": 40},
        ],
    }

    def test_admit_ok(self):
        resp = client.post("/admit", json=self.ADMIT_PAYLOAD)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        # both fit (seg0: alice,carol; seg1: alice,bob; seg2: alice,bob,7)
        self.assertEqual(body["admitted"], [7, "bob"])
        self.assertEqual(sum(body["totals"].values()), 431)
        seg2 = body["segments"][2]
        self.assertEqual(seg2["passengers"], [7, "alice", "bob"])
        self.assertEqual(sum(seg2["shares"].values()), 80)
        # recomputed shares match a /split over the full resulting roster
        full = {
            "segments": self.ADMIT_PAYLOAD["segments"],
            "passengers": self.ADMIT_PAYLOAD["passengers"] + [
                {"id": "bob", "board": 1, "alight": 3},
                {"id": 7, "board": 2, "alight": 3},
            ],
        }
        self.assertEqual(body["totals"], client.post("/split", json=full).json()["totals"])

    def test_admit_maximizes_count_then_minimizes_fare(self):
        # cap 2 with one whole-trip rider: two segment-local candidates beat
        # the whole-trip one on count.
        payload = {
            "segments": [100, 100, 100],
            "passengers": [{"id": "a", "board": 0, "alight": 3}],
            "capacity": 2,
            "candidates": [
                {"id": "long", "board": 0, "alight": 3, "max_fare": 1000},
                {"id": "x", "board": 0, "alight": 1, "max_fare": 1000},
                {"id": "y", "board": 2, "alight": 3, "max_fare": 1000},
            ],
        }
        body = client.post("/admit", json=payload).json()
        self.assertEqual(body["admitted"], ["x", "y"])
        self.assertEqual(body["totals"], {"a": 200, "x": 50, "y": 50})

    def test_admit_fare_cap_rejects_and_falls_back_to_original_split(self):
        payload = {
            "segments": [100, 100],
            "passengers": [{"id": "a", "board": 0, "alight": 2}],
            "capacity": 4,
            "candidates": [
                {"id": "b", "board": 0, "alight": 2, "max_fare": 99},
                {"id": "c", "board": 0, "alight": 2, "max_fare": 99},
            ],
        }
        # together each newcomer pays ~67 (<= 99): both admitted.
        body = client.post("/admit", json=payload).json()
        self.assertEqual(body["admitted"], ["b", "c"])

        # caps of 60: with both they pay 67 (>60); alone a survivor pays
        # 100 (>60) too, so nobody is admitted and the original split stands.
        payload["candidates"][0]["max_fare"] = 60
        payload["candidates"][1]["max_fare"] = 60
        body = client.post("/admit", json=payload).json()
        self.assertEqual(body["admitted"], [])
        split = client.post("/split", json={
            "segments": [100, 100],
            "passengers": [{"id": "a", "board": 0, "alight": 2}],
        }).json()
        self.assertEqual(body["totals"], split["totals"])
        self.assertEqual(body["segments"], split["segments"])

    def test_admit_empty_candidates_returns_original_split(self):
        payload = {
            "segments": [100],
            "passengers": [{"id": "a", "board": 0, "alight": 1}],
            "capacity": 2,
            "candidates": [],
        }
        body = client.post("/admit", json=payload).json()
        self.assertEqual(body, {
            "admitted": [],
            "totals": {"a": 100},
            "segments": [{"index": 0, "cost": 100, "passengers": ["a"],
                          "shares": {"a": 100}}],
        })

    def test_admit_tie_break_canonical_order(self):
        # room for 2 of 3; equal fares -> canonical-smallest id pair wins
        payload = {
            "segments": [90],
            "passengers": [{"id": "z", "board": 0, "alight": 1}],
            "capacity": 3,
            "candidates": [
                {"id": "x", "board": 0, "alight": 1, "max_fare": 100},
                {"id": 10, "board": 0, "alight": 1, "max_fare": 100},
                {"id": 2, "board": 0, "alight": 1, "max_fare": 100},
            ],
        }
        self.assertEqual(client.post("/admit", json=payload).json()["admitted"], [2, 10])

    def test_admit_input_order_does_not_matter(self):
        import random
        baseline = client.post("/admit", json=self.ADMIT_PAYLOAD).json()
        for seed in range(5):
            p = {
                "segments": self.ADMIT_PAYLOAD["segments"],
                "passengers": self.ADMIT_PAYLOAD["passengers"][:],
                "capacity": 3,
                "candidates": self.ADMIT_PAYLOAD["candidates"][:],
            }
            random.Random(seed).shuffle(p["passengers"])
            random.Random(seed + 50).shuffle(p["candidates"])
            resp = client.post("/admit", json=p)
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json(), baseline)

    def test_admit_existing_load_over_capacity_is_422(self):
        payload = {
            "segments": [10],
            "passengers": [{"id": "a", "board": 0, "alight": 1},
                           {"id": "b", "board": 0, "alight": 1}],
            "capacity": 1,
            "candidates": [],
        }
        resp = client.post("/admit", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("exceeding capacity", resp.text)

    def test_admit_duplicate_existing_id_is_422(self):
        payload = {
            "segments": [10],
            "passengers": [{"id": "a", "board": 0, "alight": 1},
                           {"id": "a", "board": 0, "alight": 1}],
            "capacity": 3,
            "candidates": [],
        }
        resp = client.post("/admit", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("duplicate passenger id", resp.text)

    def test_admit_candidate_id_collision_is_422(self):
        payload = {
            "segments": [10],
            "passengers": [{"id": "a", "board": 0, "alight": 1}],
            "capacity": 3,
            "candidates": [{"id": "a", "board": 0, "alight": 1, "max_fare": 5},
                           {"id": "b", "board": 0, "alight": 1, "max_fare": 5}],
        }
        resp = client.post("/admit", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("duplicate passenger id", resp.text)

    def test_admit_duplicate_candidate_ids_is_422(self):
        payload = {
            "segments": [10],
            "passengers": [],
            "capacity": 3,
            "candidates": [{"id": "a", "board": 0, "alight": 1, "max_fare": 5},
                           {"id": "a", "board": 0, "alight": 1, "max_fare": 5}],
        }
        resp = client.post("/admit", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("duplicate passenger id", resp.text)

    def test_admit_candidate_bad_interval_is_422(self):
        for cand_ in [
            {"id": "a", "board": 1, "alight": 1, "max_fare": 5},
            {"id": "a", "board": 0, "alight": 3, "max_fare": 5},
        ]:
            payload = {"segments": [10, 10], "passengers": [],
                       "capacity": 3, "candidates": [cand_]}
            self.assertEqual(client.post("/admit", json=payload).status_code, 422)

    def test_admit_empty_load_charged_segment_is_422(self):
        # nobody (existing or candidate) covers segment 0, which is charged
        payload = {
            "segments": [10, 10],
            "passengers": [{"id": "a", "board": 1, "alight": 2}],
            "capacity": 3,
            "candidates": [{"id": "b", "board": 1, "alight": 2, "max_fare": 5}],
        }
        resp = client.post("/admit", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("no passengers on board", resp.text)

    def test_admit_more_than_12_candidates_is_422(self):
        payload = {
            "segments": [10],
            "passengers": [{"id": "z", "board": 0, "alight": 1}],
            "capacity": 99,
            "candidates": [
                {"id": f"c{i}", "board": 0, "alight": 1, "max_fare": 1}
                for i in range(13)
            ],
        }
        self.assertEqual(client.post("/admit", json=payload).status_code, 422)

    def test_admit_twelve_candidates_is_ok(self):
        payload = {
            "segments": [120],
            "passengers": [{"id": "z", "board": 0, "alight": 1}],
            "capacity": 13,
            "candidates": [
                {"id": i, "board": 0, "alight": 1, "max_fare": 10}
                for i in range(12)
            ],
        }
        resp = client.post("/admit", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(resp.json()["admitted"]), 12)

    def test_admit_validation_422_cases(self):
        base = {"segments": [10],
                "passengers": [{"id": "a", "board": 0, "alight": 1}]}
        cases = [
            dict(base, capacity=2),  # candidates missing
            dict(base, candidates=[]),  # capacity missing
            dict(base, capacity=0, candidates=[]),  # non-positive capacity
            dict(base, capacity=2, candidates=[]),  # unknown field
            dict(base, capacity=2,
                 candidates=[{"id": "b", "board": 0, "alight": 1,
                              "max_fare": -1}]),  # negative max_fare
            dict(base, capacity=2,
                 candidates=[{"id": "b", "board": 0, "alight": 1,
                              "max_fare": 1.5}]),  # non-integer max_fare
            {"segments": [], "passengers": [], "capacity": 1, "candidates": []},
            {"segments": [-1], "passengers": [], "capacity": 1, "candidates": []},
        ]
        cases[3]["nope"] = True
        for payload in cases:
            self.assertEqual(
                client.post("/admit", json=payload).status_code, 422, payload
            )


class SettleApiTests(unittest.TestCase):
    def test_settle_ok(self):
        resp = client.post("/settle", json=SETTLE_PAYLOAD)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["owed"], {"alice": 216, "bob": 165, "carol": 50})
        self.assertEqual(body["paid"], {"alice": 331, "bob": 100, "carol": 0})
        self.assertEqual(body["balance"], {"alice": 115, "bob": -65, "carol": -50})
        self.assertEqual(body["transfers"], [
            {"from": "bob", "to": "alice", "amount": 65},
            {"from": "carol", "to": "alice", "amount": 50},
        ])
        # balances are exactly paid - owed and sum to zero
        for pid in body["owed"]:
            self.assertEqual(body["balance"][pid], body["paid"][pid] - body["owed"][pid])
        self.assertEqual(sum(body["balance"].values()), 0)
        # every transfer moves money from a debtor to a creditor
        for tr in body["transfers"]:
            self.assertLess(body["balance"][tr["from"]], 0)
            self.assertGreater(body["balance"][tr["to"]], 0)
            self.assertGreater(tr["amount"], 0)

    def test_zero_balances_mean_no_transfers(self):
        payload = {
            "segments": [100],
            "passengers": [{"id": "a", "board": 0, "alight": 1},
                           {"id": "b", "board": 0, "alight": 1}],
            "payments": [{"id": "a", "amount": 50}, {"id": "b", "amount": 50}],
        }
        body = client.post("/settle", json=payload).json()
        self.assertEqual(body["balance"], {"a": 0, "b": 0})
        self.assertEqual(body["transfers"], [])

    def test_empty_trip(self):
        payload = {"segments": [0], "passengers": [], "payments": []}
        resp = client.post("/settle", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(),
                         {"owed": {}, "paid": {}, "balance": {}, "transfers": []})

    def test_input_order_does_not_matter(self):
        import random
        baseline = client.post("/settle", json=SETTLE_PAYLOAD).json()
        for seed in range(5):
            shuffled = {
                "segments": SETTLE_PAYLOAD["segments"],
                "passengers": SETTLE_PAYLOAD["passengers"][:],
                "payments": SETTLE_PAYLOAD["payments"][:],
            }
            random.Random(seed).shuffle(shuffled["passengers"])
            random.Random(seed + 100).shuffle(shuffled["payments"])
            resp = client.post("/settle", json=shuffled)
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json(), baseline)

    def test_integer_passenger_ids(self):
        payload = {
            "segments": [10, 10],
            "passengers": [{"id": 1, "board": 0, "alight": 2},
                           {"id": 2, "board": 0, "alight": 2}],
            "payments": [{"id": 2, "amount": 20}, {"id": 1, "amount": 0}],
        }
        body = client.post("/settle", json=payload).json()
        self.assertEqual(body["owed"], {"1": 10, "2": 10})
        self.assertEqual(body["paid"], {"1": 0, "2": 20})
        self.assertEqual(body["balance"], {"1": -10, "2": 10})
        self.assertEqual(body["transfers"], [{"from": 1, "to": 2, "amount": 10}])

    def test_payments_total_mismatch_is_422(self):
        payload = {
            "segments": [100],
            "passengers": [{"id": "a", "board": 0, "alight": 1}],
            "payments": [{"id": "a", "amount": 99}],
        }
        resp = client.post("/settle", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("payments total", resp.text)

    def test_missing_payment_is_422(self):
        payload = {
            "segments": [100],
            "passengers": [{"id": "a", "board": 0, "alight": 1},
                           {"id": "b", "board": 0, "alight": 1}],
            "payments": [{"id": "a", "amount": 100}],
        }
        resp = client.post("/settle", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("missing payment", resp.text)

    def test_duplicate_payment_id_is_422(self):
        payload = {
            "segments": [100],
            "passengers": [{"id": "a", "board": 0, "alight": 1}],
            "payments": [{"id": "a", "amount": 50}, {"id": "a", "amount": 50}],
        }
        resp = client.post("/settle", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("duplicate payment id", resp.text)

    def test_unknown_payment_id_is_422(self):
        payload = {
            "segments": [100],
            "passengers": [{"id": "a", "board": 0, "alight": 1}],
            "payments": [{"id": "a", "amount": 99}, {"id": "ghost", "amount": 1}],
        }
        resp = client.post("/settle", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("unknown passenger", resp.text)

    def test_negative_payment_is_422(self):
        payload = {
            "segments": [100],
            "passengers": [{"id": "a", "board": 0, "alight": 1}],
            "payments": [{"id": "a", "amount": -1}],
        }
        self.assertEqual(client.post("/settle", json=payload).status_code, 422)

    def test_non_integer_payment_is_422(self):
        payload = {
            "segments": [100],
            "passengers": [{"id": "a", "board": 0, "alight": 1}],
            "payments": [{"id": "a", "amount": 50.5}],
        }
        self.assertEqual(client.post("/settle", json=payload).status_code, 422)

    def test_missing_payments_field_is_422(self):
        payload = {"segments": [100],
                   "passengers": [{"id": "a", "board": 0, "alight": 1}]}
        self.assertEqual(client.post("/settle", json=payload).status_code, 422)

    def test_split_trip_rules_still_apply_on_settle(self):
        # the trip itself is invalid (empty charged segment) even though
        # payments total matches
        payload = {
            "segments": [100],
            "passengers": [{"id": "a", "board": 0, "alight": 0}],
            "payments": [{"id": "a", "amount": 100}],
        }
        self.assertEqual(client.post("/settle", json=payload).status_code, 422)

    def test_healthz(self):
        resp = client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})


class RevisionImpactApiTests(unittest.TestCase):
    REVISION_PAYLOAD = {
        "segments": [100, 251, 80],
        "revised_segments": [100, 252, 80],
        "passengers": [
            {"id": "alice", "board": 0, "alight": 3},
            {"id": "bob", "board": 1, "alight": 3},
            {"id": "carol", "board": 0, "alight": 1},
        ],
    }

    def test_revision_ok_single_change(self):
        resp = client.post("/revision-impact", json=self.REVISION_PAYLOAD)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["before"], {"alice": 216, "bob": 165, "carol": 50})
        self.assertEqual(body["after"], {"alice": 216, "bob": 166, "carol": 50})
        self.assertEqual(body["deltas"], {"alice": 0, "bob": 1, "carol": 0})
        self.assertEqual(len(body["changes"]), 1)
        change = body["changes"][0]
        self.assertEqual(change["index"], 1)
        self.assertEqual(change["original_cost"], 251)
        self.assertEqual(change["revised_cost"], 252)
        # segment 1 carries alice+bob; the extra cent follows deficit order
        self.assertEqual(change["contributions"],
                         {"alice": "0", "bob": "1", "carol": "0"})

    def test_before_and_after_match_plain_splits(self):
        body = client.post("/revision-impact", json=self.REVISION_PAYLOAD).json()
        for key, costs in (("before", self.REVISION_PAYLOAD["segments"]),
                           ("after", self.REVISION_PAYLOAD["revised_segments"])):
            split = client.post("/split", json={
                "segments": costs,
                "passengers": self.REVISION_PAYLOAD["passengers"],
            }).json()
            self.assertEqual(body[key], split["totals"])

    def test_no_changes_returns_empty_contributions_and_zero_deltas(self):
        payload = dict(self.REVISION_PAYLOAD,
                       revised_segments=list(self.REVISION_PAYLOAD["segments"]))
        body = client.post("/revision-impact", json=payload).json()
        self.assertEqual(body["before"], body["after"])
        self.assertEqual(body["deltas"],
                         {pid: 0 for pid in body["before"]})
        self.assertEqual(body["changes"], [])

    def test_fraction_strings_and_exact_additivity_per_passenger(self):
        from fractions import Fraction
        payload = {
            "segments": [10, 7, 13],
            "revised_segments": [11, 9, 10],
            "passengers": [
                {"id": "a", "board": 0, "alight": 3},
                {"id": "b", "board": 0, "alight": 2},
                {"id": "c", "board": 1, "alight": 3},
            ],
        }
        body = client.post("/revision-impact", json=payload).json()
        self.assertEqual(len(body["changes"]), 3)
        # contributions serialize as reduced fraction strings, sign kept
        by_index = {c["index"]: c for c in body["changes"]}
        self.assertEqual(by_index[0]["contributions"],
                         {"a": "1/3", "b": "1/2", "c": "1/6"})
        self.assertEqual(by_index[2]["contributions"],
                         {"a": "-7/6", "b": "0", "c": "-11/6"})
        # per-passenger contributions sum EXACTLY to the integer delta
        for pid, delta in body["deltas"].items():
            total = sum(
                (Fraction(c["contributions"][pid]) for c in body["changes"]),
                Fraction(0),
            )
            self.assertEqual(total, delta, pid)
        # contributions cover every passenger and every changed segment
        for change in body["changes"]:
            self.assertEqual(set(change["contributions"]), set(body["deltas"]))

    def test_negative_contribution_on_fare_decrease(self):
        payload = {
            "segments": [8, 8],
            "revised_segments": [11, 7],
            "passengers": [
                {"id": "a", "board": 0, "alight": 2},
                {"id": "b", "board": 1, "alight": 2},
            ],
        }
        body = client.post("/revision-impact", json=payload).json()
        self.assertEqual(body["deltas"], {"a": 3, "b": -1})
        by_index = {c["index"]: c["contributions"] for c in body["changes"]}
        self.assertEqual(by_index[0], {"a": "3", "b": "0"})
        self.assertEqual(by_index[1], {"a": "0", "b": "-1"})

    def test_eight_changed_segments_is_ok_nine_is_422(self):
        passengers = [{"id": "a", "board": 0, "alight": 9}]
        eight = client.post("/revision-impact", json={
            "segments": [0] + [1] * 8,
            "revised_segments": [0] + [2] * 8,
            "passengers": passengers,
        })
        self.assertEqual(eight.status_code, 200, eight.text)
        self.assertEqual(len(eight.json()["changes"]), 8)

        nine = client.post("/revision-impact", json={
            "segments": [0] * 9,
            "revised_segments": [1] * 9,
            "passengers": passengers,
        })
        self.assertEqual(nine.status_code, 422)
        self.assertIn("at most 8 changed segments", nine.text)

    def test_unequal_lengths_is_422(self):
        payload = {
            "segments": [10, 10],
            "revised_segments": [10],
            "passengers": [{"id": "a", "board": 0, "alight": 2}],
        }
        resp = client.post("/revision-impact", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("equal length", resp.text)

    def test_split_validation_reused_for_both_lists(self):
        # invalid interval on the shared roster
        resp = client.post("/revision-impact", json={
            "segments": [10], "revised_segments": [10],
            "passengers": [{"id": "a", "board": 1, "alight": 1}],
        })
        self.assertEqual(resp.status_code, 422)
        self.assertIn("invalid interval", resp.text)

        # original list has an empty-load charged segment
        resp = client.post("/revision-impact", json={
            "segments": [10, 0], "revised_segments": [0, 0],
            "passengers": [{"id": "a", "board": 1, "alight": 2}],
        })
        self.assertEqual(resp.status_code, 422)
        self.assertIn("no passengers on board", resp.text)

        # revised list introduces an empty-load charged segment
        resp = client.post("/revision-impact", json={
            "segments": [10, 0], "revised_segments": [0, 10],
            "passengers": [{"id": "a", "board": 0, "alight": 1}],
        })
        self.assertEqual(resp.status_code, 422)
        self.assertIn("no passengers on board", resp.text)

    def test_duplicate_ids_negative_costs_empty_list_are_422(self):
        base = {
            "segments": [10, 10],
            "revised_segments": [10, 10],
            "passengers": [{"id": "a", "board": 0, "alight": 2}],
        }
        dup = dict(base, passengers=[
            {"id": "a", "board": 0, "alight": 1},
            {"id": "a", "board": 1, "alight": 2},
        ])
        self.assertEqual(client.post("/revision-impact", json=dup).status_code, 422)
        neg = dict(base, revised_segments=[10, -1])
        self.assertEqual(client.post("/revision-impact", json=neg).status_code, 422)
        empty = {"segments": [], "revised_segments": [], "passengers": []}
        self.assertEqual(client.post("/revision-impact", json=empty).status_code, 422)
        unknown = dict(base, currency="usd")
        self.assertEqual(client.post("/revision-impact", json=unknown).status_code, 422)

    def test_input_order_does_not_matter(self):
        import random
        baseline = client.post(
            "/revision-impact", json=self.REVISION_PAYLOAD
        ).json()
        for seed in range(5):
            shuffled = {
                "segments": self.REVISION_PAYLOAD["segments"],
                "revised_segments": self.REVISION_PAYLOAD["revised_segments"],
                "passengers": self.REVISION_PAYLOAD["passengers"][:],
            }
            random.Random(seed).shuffle(shuffled["passengers"])
            resp = client.post("/revision-impact", json=shuffled)
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json(), baseline)


class FareEnvelopeApiTests(unittest.TestCase):
    ENVELOPE_PAYLOAD = {
        "segments": [None, 5, None],
        "passengers": [
            {"id": "alice", "board": 0, "alight": 3},
            {"id": "bob",   "board": 1, "alight": 2},
            {"id": "carol", "board": 0, "alight": 1},
        ],
        "bounds": [
            {"index": 0, "min_cost": 0, "max_cost": 10},
            {"index": 2, "min_cost": 0, "max_cost": 10},
        ],
        "total_cost": 15,
        "target": "alice",
    }

    def test_envelope_ok(self):
        resp = client.post("/fare-envelope", json=self.ENVELOPE_PAYLOAD)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["target"], "alice")
        self.assertEqual(body["total_cost"], 15)
        # brute table for alice over x+y=10, x in 0..10:
        # 13,12,12,11,11,10,10,9,9,8,8
        self.assertEqual(body["minimum"]["amount"], 8)
        self.assertEqual(body["minimum"]["segments"], [9, 5, 1])
        self.assertEqual(body["maximum"]["amount"], 13)
        self.assertEqual(body["maximum"]["segments"], [0, 5, 10])
        for extreme in (body["minimum"], body["maximum"]):
            self.assertEqual(sum(extreme["segments"]), 15)
            self.assertNotIn(None, extreme["segments"])
            # each witness reproduces its amount under /split
            check = client.post("/split", json={
                "segments": extreme["segments"],
                "passengers": self.ENVELOPE_PAYLOAD["passengers"],
            })
            self.assertEqual(check.status_code, 200)
            self.assertEqual(check.json()["totals"]["alice"], extreme["amount"])

    def test_target_bob_cross_segment_deficit(self):
        payload = dict(self.ENVELOPE_PAYLOAD, target="bob")
        body = client.post("/fare-envelope", json=payload).json()
        self.assertEqual(body["minimum"]["amount"], 2)
        self.assertEqual(body["minimum"]["segments"], [0, 5, 10])
        self.assertEqual(body["maximum"]["amount"], 3)
        self.assertEqual(body["maximum"]["segments"], [1, 5, 9])

    def test_huge_bounds_do_not_scan_cent_by_cent(self):
        big = 10**12
        payload = {
            "segments": [None, None, 0],
            "passengers": self.ENVELOPE_PAYLOAD["passengers"],
            "bounds": [
                {"index": 0, "min_cost": 0, "max_cost": big},
                {"index": 1, "min_cost": 0, "max_cost": big},
            ],
            "total_cost": big,
            "target": "bob",
        }
        resp = client.post("/fare-envelope", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["minimum"]["amount"], 0)
        self.assertEqual(body["maximum"]["amount"], big // 2)
        self.assertEqual(sum(body["minimum"]["segments"]), big)

    def test_no_feasible_combination_is_422(self):
        payload = dict(self.ENVELOPE_PAYLOAD,
                       bounds=[{"index": 0, "min_cost": 0, "max_cost": 3},
                               {"index": 2, "min_cost": 0, "max_cost": 3}])
        resp = client.post("/fare-envelope", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("no feasible fare combination", resp.text)

    def test_fixed_segments_exceed_total_is_422(self):
        payload = dict(self.ENVELOPE_PAYLOAD,
                       segments=[None, 50, None], total_cost=15)
        resp = client.post("/fare-envelope", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("no feasible fare combination", resp.text)

    def test_unknown_target_is_422(self):
        payload = dict(self.ENVELOPE_PAYLOAD, target="nobody")
        resp = client.post("/fare-envelope", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("unknown target passenger id", resp.text)

    def test_free_segment_without_passengers_is_422(self):
        # carol rides seg0; seg1 and seg2 free/empty
        payload = {
            "segments": [0, None, None],
            "passengers": [{"id": "carol", "board": 0, "alight": 1}],
            "bounds": [{"index": 1, "min_cost": 0, "max_cost": 5},
                       {"index": 2, "min_cost": 0, "max_cost": 5}],
            "total_cost": 5,
            "target": "carol",
        }
        resp = client.post("/fare-envelope", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("undetermined cost but no passengers", resp.text)

    def test_empty_load_charged_fixed_segment_is_422(self):
        # a rides both free segments (0 and 2); the fixed segment 1 is
        # charged but empty, which violates the existing trip rules.
        payload = dict(self.ENVELOPE_PAYLOAD,
                       segments=[None, 7, None],
                       passengers=[{"id": "a", "board": 0, "alight": 1},
                                   {"id": "z", "board": 2, "alight": 3}],
                       target="a")
        resp = client.post("/fare-envelope", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("no passengers on board", resp.text)

    def test_bad_interval_and_duplicate_ids_are_422(self):
        bad = dict(self.ENVELOPE_PAYLOAD,
                   passengers=[{"id": "a", "board": 2, "alight": 2}])
        self.assertEqual(client.post("/fare-envelope", json=bad).status_code, 422)
        dup = dict(self.ENVELOPE_PAYLOAD, passengers=[
            {"id": "a", "board": 0, "alight": 3},
            {"id": "a", "board": 0, "alight": 3},
        ])
        self.assertEqual(client.post("/fare-envelope", json=dup).status_code, 422)

    def test_wrong_null_count_is_422(self):
        one_null = dict(self.ENVELOPE_PAYLOAD, segments=[None, 5, 5])
        resp = client.post("/fare-envelope", json=one_null)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("exactly two null", resp.text)

        three_null = dict(self.ENVELOPE_PAYLOAD,
                          segments=[None, None, None])
        resp = client.post("/fare-envelope", json=three_null)
        self.assertEqual(resp.status_code, 422)

    def test_wrong_bounds_entries_are_422(self):
        # one bound instead of two
        one = dict(self.ENVELOPE_PAYLOAD,
                   bounds=[{"index": 0, "min_cost": 0, "max_cost": 10}])
        self.assertEqual(client.post("/fare-envelope", json=one).status_code, 422)
        # bound references a fixed segment
        wrong = dict(self.ENVELOPE_PAYLOAD,
                     bounds=[{"index": 0, "min_cost": 0, "max_cost": 10},
                             {"index": 1, "min_cost": 0, "max_cost": 10}])
        self.assertEqual(client.post("/fare-envelope", json=wrong).status_code, 422)
        # duplicated bound index
        dup = dict(self.ENVELOPE_PAYLOAD,
                   bounds=[{"index": 0, "min_cost": 0, "max_cost": 10},
                           {"index": 0, "min_cost": 0, "max_cost": 10}])
        self.assertEqual(client.post("/fare-envelope", json=dup).status_code, 422)
        # inverted range
        inverted = dict(self.ENVELOPE_PAYLOAD,
                        bounds=[{"index": 0, "min_cost": 9, "max_cost": 2},
                                {"index": 2, "min_cost": 0, "max_cost": 10}])
        self.assertEqual(client.post("/fare-envelope", json=inverted).status_code, 422)

    def test_limits_and_unknown_fields_are_422(self):
        big = 10**12 + 1
        too_big_cost = dict(self.ENVELOPE_PAYLOAD,
                            segments=[None, big, None])
        self.assertEqual(
            client.post("/fare-envelope", json=too_big_cost).status_code, 422
        )
        too_big_bound = dict(
            self.ENVELOPE_PAYLOAD,
            bounds=[{"index": 0, "min_cost": 0, "max_cost": big},
                    {"index": 2, "min_cost": 0, "max_cost": 10}],
        )
        self.assertEqual(
            client.post("/fare-envelope", json=too_big_bound).status_code, 422
        )
        too_big_total = dict(self.ENVELOPE_PAYLOAD, total_cost=big)
        self.assertEqual(
            client.post("/fare-envelope", json=too_big_total).status_code, 422
        )
        unknown = dict(self.ENVELOPE_PAYLOAD, currency="usd")
        self.assertEqual(
            client.post("/fare-envelope", json=unknown).status_code, 422
        )

    def test_more_than_12_passengers_is_422(self):
        pax = [{"id": f"p{i}", "board": 0, "alight": 3} for i in range(13)]
        payload = {
            "segments": [None, 5, None],
            "passengers": pax,
            "bounds": [{"index": 0, "min_cost": 0, "max_cost": 10},
                       {"index": 2, "min_cost": 0, "max_cost": 10}],
            "total_cost": 15,
            "target": "p0",
        }
        resp = client.post("/fare-envelope", json=payload)
        self.assertEqual(resp.status_code, 422)

    def test_more_than_100_segments_is_422(self):
        n = 101
        segments = [0] * n
        segments[0] = segments[50] = None
        payload = {
            "segments": segments,
            "passengers": [{"id": "a", "board": 0, "alight": n}],
            "bounds": [{"index": 0, "min_cost": 0, "max_cost": 10},
                       {"index": 50, "min_cost": 0, "max_cost": 10}],
            "total_cost": 10,
            "target": "a",
        }
        resp = client.post("/fare-envelope", json=payload)
        self.assertEqual(resp.status_code, 422)

    def test_passenger_order_does_not_matter(self):
        import random
        baseline = client.post(
            "/fare-envelope", json=self.ENVELOPE_PAYLOAD
        ).json()
        for seed in range(5):
            shuffled = dict(self.ENVELOPE_PAYLOAD,
                            passengers=self.ENVELOPE_PAYLOAD["passengers"][:])
            random.Random(seed).shuffle(shuffled["passengers"])
            resp = client.post("/fare-envelope", json=shuffled)
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json(), baseline)


if __name__ == "__main__":
    unittest.main()
