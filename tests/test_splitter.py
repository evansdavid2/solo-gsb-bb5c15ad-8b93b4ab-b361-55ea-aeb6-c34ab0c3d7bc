"""Unit tests for the core splitting algorithm.

Standard-library only: runs with ``python3 -m unittest tests.test_splitter``
as well as under pytest.
"""

import random
import unittest
from fractions import Fraction
from itertools import permutations
from math import factorial
from types import SimpleNamespace as P

from app.splitter import (
    _triple,
    admit_passengers,
    revision_impact,
    settle_balances,
    split_fares,
    tie_key,
)


def ride(pid, board, alight):
    return P(id=pid, board=board, alight=alight)


def cand(pid, board, alight, max_fare):
    return P(id=pid, board=board, alight=alight, max_fare=max_fare)


class SplitFaresTests(unittest.TestCase):
    def test_even_split_single_segment(self):
        totals, details = split_fares([90], [ride("a", 0, 1), ride("b", 0, 1)])
        self.assertEqual(totals, {"a": 45, "b": 45})
        self.assertEqual(details[0]["shares"], {"a": 45, "b": 45})

    def test_remainder_tie_broken_by_id(self):
        # 10 / 3 -> base 3, remainder 1; all deficits equal, id "a" wins.
        totals, _ = split_fares([10], [ride("c", 0, 1), ride("a", 0, 1), ride("b", 0, 1)])
        self.assertEqual(totals, {"a": 4, "b": 3, "c": 3})

    def test_deficit_beats_id_order(self):
        # seg0: a,b,c share 10 -> a=4 (id tiebreak), b=3, c=3.
        # seg1: a,b share 7 -> base 3 rem 1; b's deficit (5/6) > a's (-1/6),
        # so b gets the extra cent despite "a" < "b".
        passengers = [ride("a", 0, 2), ride("b", 0, 2), ride("c", 0, 1)]
        totals, details = split_fares([10, 7], passengers)
        self.assertEqual(totals, {"a": 7, "b": 7, "c": 3})
        self.assertEqual(details[1]["shares"], {"a": 3, "b": 4})

    def test_remainder_rotates_over_segments(self):
        passengers = [ride("a", 0, 3), ride("b", 0, 3), ride("c", 0, 3)]
        totals, details = split_fares([100, 100, 100], passengers)
        self.assertEqual(totals, {"a": 100, "b": 100, "c": 100})
        self.assertEqual([d["shares"] for d in details], [
            {"a": 34, "b": 33, "c": 33},
            {"a": 33, "b": 34, "c": 33},
            {"a": 33, "b": 33, "c": 34},
        ])

    def test_passengers_only_pay_while_on_board(self):
        passengers = [ride("alice", 0, 3), ride("bob", 1, 3), ride("carol", 0, 1)]
        totals, details = split_fares([100, 251, 80], passengers)
        # seg1: alice and bob tie on deficit -> alice (id) gets the odd cent.
        self.assertEqual(totals, {"alice": 216, "bob": 165, "carol": 50})
        self.assertEqual(details[0]["passengers"], ["alice", "carol"])
        self.assertEqual(details[1]["shares"], {"alice": 126, "bob": 125})
        self.assertEqual(sum(totals.values()), 431)

    def test_zero_cost_empty_segment_is_allowed(self):
        totals, details = split_fares([0, 10], [ride("a", 1, 2)])
        self.assertEqual(totals, {"a": 10})
        self.assertEqual(details[0], {"index": 0, "cost": 0, "passengers": [], "shares": {}})

    def test_integer_ids_sort_numerically_on_ties(self):
        # id 9 < id 10 numerically, so 9 wins the tie (string order would
        # pick "10" first).
        totals, _ = split_fares([10], [ride(10, 0, 1), ride(9, 0, 1), ride(11, 0, 1)])
        self.assertEqual(totals, {9: 4, 10: 3, 11: 3})


class PropertyTests(unittest.TestCase):
    def test_conservation_invariance_and_fairness(self):
        rng = random.Random(20260919)
        for _ in range(500):
            n_seg = rng.randint(1, 8)
            segments = [rng.randint(0, 999) for _ in range(n_seg)]
            passengers = [ride("anchor", 0, n_seg)]  # covers every segment
            for i in range(rng.randint(0, 5)):
                board = rng.randint(0, n_seg - 1)
                alight = rng.randint(board + 1, n_seg)
                passengers.append(ride(f"p{i}", board, alight))

            totals, details = split_fares(segments, passengers)

            # conservation: per segment and overall
            for seg, cost in zip(details, segments):
                self.assertEqual(sum(seg["shares"].values()), cost)
                n = len(seg["passengers"])
                if n:
                    base, rem = divmod(cost, n)
                    # every share is the equal split or the equal split + 1,
                    # and exactly `rem` passengers get the extra cent
                    self.assertEqual(sorted(seg["shares"].values()),
                                     [base] * (n - rem) + [base + 1] * rem)
            self.assertEqual(sum(totals.values()), sum(segments))

            # input-order invariance: shuffling passengers changes nothing
            shuffled = passengers[:]
            rng.shuffle(shuffled)
            totals2, details2 = split_fares(segments, shuffled)
            self.assertEqual(totals, totals2)
            self.assertEqual(details, details2)


class AdmitPassengersTests(unittest.TestCase):
    def test_admit_all_when_everyone_fits(self):
        existing = [ride("a", 0, 2)]
        candidates = [cand("b", 0, 2, 100), cand("c", 0, 1, 100)]
        admitted, totals, _ = admit_passengers([100, 100], existing, candidates, 3)
        # seg0: a,b,c -> 34/33/33 (a wins tie); seg1: a,b -> 50/50
        self.assertEqual(admitted, ["b", "c"])
        self.assertEqual(totals, {"a": 84, "b": 83, "c": 33})

    def test_maximize_count_first(self):
        # cap 2 with one whole-trip rider: two short riders can be admitted
        # together on different segments, which beats one whole-trip rider.
        existing = [ride("a", 0, 3)]
        candidates = [
            cand("long", 0, 3, 1000),
            cand("x", 0, 1, 1000),
            cand("y", 2, 3, 1000),
        ]
        admitted, totals, _ = admit_passengers([100, 100, 100], existing, candidates, 2)
        self.assertEqual(admitted, ["x", "y"])
        self.assertEqual(totals, {"a": 200, "x": 50, "y": 50})

    def test_then_minimize_admitted_fare_sum(self):
        # cap 2: "rich" rides the whole trip for 150, "cheap" only one
        # segment for 50; both are solo options of the same max count.
        existing = [ride("a", 0, 3)]
        candidates = [
            cand("rich", 0, 3, 1000),
            cand("cheap", 0, 1, 1000),
        ]
        admitted, totals, _ = admit_passengers([100, 100, 100], existing, candidates, 2)
        self.assertEqual(admitted, ["cheap"])
        self.assertEqual(totals, {"a": 250, "cheap": 50})

    def test_max_fare_binds_after_full_roster_re_split(self):
        # Two candidates split 100 evenly (50 each); "fussy" caps at 49 and
        # must be rejected. Alone the winner pays the whole 100, which is
        # beyond both caps, so nobody is admitted and the empty (original)
        # trip is returned.
        existing = []
        candidates = [cand("ok", 0, 1, 50), cand("fussy", 0, 1, 49)]
        admitted, totals, _ = admit_passengers([100], existing, candidates, 4)
        self.assertEqual(admitted, [])
        self.assertEqual(totals, {})

    def test_max_fare_is_checked_on_recomputed_share(self):
        # With two segments each rider alone pays 120; both ride and each
        # pays 60, so a cap of 60 admits both — the cap is evaluated on the
        # recomputed full-roster share, not the pre-admission fare (0).
        existing = []
        candidates = [cand("a", 0, 2, 60), cand("b", 0, 2, 60)]
        admitted, totals, _ = admit_passengers([60, 60], existing, candidates, 4)
        self.assertEqual(admitted, ["a", "b"])
        self.assertEqual(totals, {"a": 60, "b": 60})
        # Tighten one cap below 60: alone the survivor pays 120 > 59 too,
        # so no newcomer works at all.
        candidates[1] = cand("b", 0, 2, 59)
        admitted, totals, _ = admit_passengers([60, 60], existing, candidates, 4)
        self.assertEqual(admitted, [])

    def test_no_feasible_newcomer_returns_original_split(self):
        existing = [ride("a", 0, 1)]
        admitted, totals, details = admit_passengers(
            [100], existing, [cand("b", 0, 1, 49)], 2
        )
        self.assertEqual(admitted, [])
        base_totals, base_details = split_fares([100], existing)
        self.assertEqual(totals, base_totals)
        self.assertEqual(details, base_details)

    def test_empty_candidates(self):
        existing = [ride("a", 0, 1)]
        admitted, totals, details = admit_passengers([100], existing, [], 2)
        self.assertEqual(admitted, [])
        self.assertEqual(totals, {"a": 100})
        self.assertEqual(details[0]["shares"], {"a": 100})

    def test_tie_break_canonical_id_order_ints_before_strings(self):
        # cap allows exactly 2 of 3; every pair pays the same, so the
        # canonical-smallest pair {1, 2} wins over {2, "x"} and {1, "x"}.
        existing = [ride("z", 0, 1)]
        candidates = [
            cand("x", 0, 1, 100),
            cand(1, 0, 1, 100),
            cand(2, 0, 1, 100),
        ]
        admitted, _, _ = admit_passengers([90], existing, candidates, 3)
        self.assertEqual(admitted, [1, 2])

    def test_tie_break_integers_compare_numerically(self):
        # 9 sorts before 10 numerically (not lexicographically).
        existing = [ride("z", 0, 1)]
        candidates = [cand(10, 0, 1, 100), cand(9, 0, 1, 100)]
        admitted, _, _ = admit_passengers([90], existing, candidates, 3)
        self.assertEqual(admitted, [9, 10])

    def test_input_order_does_not_matter(self):
        import random
        existing = [ride("a", 0, 3), ride("b", 1, 3)]
        candidates = [
            cand("c1", 0, 2, 200), cand(2, 1, 3, 80),
            cand("c2", 0, 1, 80), cand(5, 2, 3, 40),
        ]
        segments = [100, 251, 80]
        baseline = admit_passengers(segments, existing, candidates, 4)
        for seed in range(8):
            rng = random.Random(seed)
            ex = existing[:]; cs = candidates[:]
            rng.shuffle(ex); rng.shuffle(cs)
            self.assertEqual(
                admit_passengers(segments, ex, cs, 4), baseline
            )

    def test_details_match_split_fares_for_chosen_roster(self):
        existing = [ride("a", 0, 3)]
        candidates = [cand("b", 0, 2, 500), cand("c", 1, 3, 500)]
        admitted, totals, details = admit_passengers(
            [100, 251, 80], existing, candidates, 3
        )
        chosen = [p for p in existing + candidates
                  if p.id in set(admitted) or p.id == "a"]
        ref_totals, ref_details = split_fares([100, 251, 80], chosen)
        self.assertEqual(totals, ref_totals)
        self.assertEqual(details, ref_details)


class RevisionImpactTests(unittest.TestCase):
    def _permutation_reference(self, original, revised, passengers):
        """Brute-force reference: average marginals over all change orders."""
        changed = [i for i, (a, b) in enumerate(zip(original, revised)) if a != b]
        ids = sorted((p.id for p in passengers), key=tie_key)

        def totals(applied):
            costs = [
                revised[i] if i in applied else original[i]
                for i in range(len(original))
            ]
            t, _ = split_fares(costs, passengers)
            return t

        contributions = {i: {pid: Fraction(0) for pid in ids} for i in changed}
        for order in permutations(changed):
            applied = frozenset()
            prev = totals(applied)
            for i in order:
                nxt = totals(applied | {i})
                for pid in ids:
                    contributions[i][pid] += Fraction(
                        nxt[pid] - prev[pid]
                    )
                applied = applied | {i}
                prev = nxt
        n_orders = factorial(len(changed))
        for i in changed:
            for pid in ids:
                contributions[i][pid] /= n_orders
        return changed, contributions

    def test_no_changes_returns_empty_contributions_and_zero_deltas(self):
        passengers = [ride("a", 0, 2), ride("b", 1, 2)]
        before, after, changes = revision_impact([30, 10], [30, 10], passengers)
        self.assertEqual(changes, [])
        self.assertEqual(before, after)
        # segment 0: a alone pays 30; segment 1: a,b split 10 as 5/5
        self.assertEqual(dict(before), {"a": 35, "b": 5})

    def test_single_change_attributes_the_whole_delta(self):
        # a rides both segments, b only segment 1
        before, after, changes = revision_impact(
            [8, 8], [11, 8], [ride("a", 0, 2), ride("b", 1, 2)]
        )
        self.assertEqual(dict(before), {"a": 12, "b": 4})
        self.assertEqual(dict(after), {"a": 15, "b": 4})
        self.assertEqual(len(changes), 1)
        change = changes[0]
        self.assertEqual(change["index"], 0)
        self.assertEqual(change["original_cost"], 8)
        self.assertEqual(change["revised_cost"], 11)
        self.assertEqual(change["contributions"], {"a": Fraction(3), "b": Fraction(0)})

    def test_shapley_half_share_example(self):
        # [8,5] -> [9,4]: a rides both segments, b segment 0, c segment 1.
        # Each segment's +/-1 cent lands on different passengers depending on
        # application order, so every average marginal effect is +/-1/2; the
        # two revisions cancel out and every net delta is zero.
        passengers = [ride("a", 0, 2), ride("b", 0, 1), ride("c", 1, 2)]
        before, after, changes = revision_impact([8, 5], [9, 4], passengers)
        self.assertEqual(dict(before), {"a": 7, "b": 4, "c": 2})
        self.assertEqual(dict(after), {"a": 7, "b": 4, "c": 2})
        by_index = {c["index"]: c["contributions"] for c in changes}
        half = Fraction(1, 2)
        self.assertEqual(by_index[0], {"a": half, "b": Fraction(0), "c": half})
        self.assertEqual(by_index[1], {"a": -half, "b": Fraction(0), "c": -half})
        for pid in before:
            self.assertEqual(
                sum(c["contributions"][pid] for c in changes),
                after[pid] - before[pid],
            )

    def test_negative_contribution_keeps_sign_but_additivity_holds(self):
        # [8,8] -> [11,7], a rides both segments, b only segment 1. Segment 1
        # drops by 1 and b rides it alone, so the whole drop is attributed to
        # b (-1); segment 0's +3 goes entirely to a. Contributions can be
        # negative even though the trip still costs more overall.
        before, after, changes = revision_impact(
            [8, 8], [11, 7], [ride("a", 0, 2), ride("b", 1, 2)]
        )
        self.assertEqual(dict(before), {"a": 12, "b": 4})
        self.assertEqual(dict(after), {"a": 15, "b": 3})
        by_index = {c["index"]: c["contributions"] for c in changes}
        self.assertEqual(by_index[0], {"a": Fraction(3), "b": Fraction(0)})
        self.assertEqual(by_index[1], {"a": Fraction(0), "b": Fraction(-1)})
        for pid in before:
            total = sum(c["contributions"][pid] for c in changes)
            self.assertEqual(total, after[pid] - before[pid])

    def test_matches_brute_force_permutation_average(self):
        passengers = [
            ride("a", 0, 3), ride("b", 0, 2), ride("c", 1, 3),
        ]
        original, revised = [10, 7, 13], [11, 9, 10]
        changed, expected = self._permutation_reference(
            original, revised, passengers
        )
        before, after, changes = revision_impact(original, revised, passengers)
        self.assertEqual([c["index"] for c in changes], changed)
        for change in changes:
            for pid, value in change["contributions"].items():
                self.assertEqual(value, expected[change["index"]][pid])

    def test_contributions_exactly_reconstruct_every_delta(self):
        rng = random.Random(20260926)
        for _ in range(300):
            n_seg = rng.randint(1, 6)
            passengers = [ride("anchor", 0, n_seg)]
            for i in range(rng.randint(0, 4)):
                b0 = rng.randint(0, n_seg - 1)
                passengers.append(
                    ride(f"p{i}", b0, rng.randint(b0 + 1, n_seg))
                )
            original = [rng.randint(0, 60) for _ in range(n_seg)]
            revised = list(original)
            for i in rng.sample(range(n_seg), rng.randint(0, n_seg)):
                revised[i] = rng.randint(0, 60)

            before, after, changes = revision_impact(
                original, revised, passengers
            )
            # before/after agree with a plain split of each cost list
            self.assertEqual(dict(before), dict(split_fares(original, passengers)[0]))
            self.assertEqual(dict(after), dict(split_fares(revised, passengers)[0]))
            # sum of per-segment contributions is exactly the integer delta
            for pid in before:
                total = sum(
                    (c["contributions"][pid] for c in changes), Fraction(0)
                )
                self.assertEqual(total, after[pid] - before[pid], pid)
            # money conservation: deltas over passengers equal the fare delta
            self.assertEqual(
                sum(after.values()) - sum(before.values()),
                sum(revised) - sum(original),
            )

    def test_input_order_does_not_matter(self):
        passengers = [ride("a", 0, 3), ride("b", 1, 3), ride("c", 0, 1)]
        original, revised = [100, 251, 80], [120, 240, 90]
        baseline = revision_impact(original, revised, passengers)
        for seed in range(6):
            shuffled = passengers[:]
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(
                revision_impact(original, revised, shuffled), baseline
            )

    def test_unequal_lengths_rejected(self):
        with self.assertRaises(ValueError):
            revision_impact([1, 2], [1], [ride("a", 0, 2)])


class SettleBalancesTests(unittest.TestCase):
    def _check(self, balances, transfers):
        net = {pid: 0 for pid in balances}
        for frm, to, amount in transfers:
            self.assertGreater(amount, 0)
            net[frm] -= amount
            net[to] += amount
        self.assertEqual(net, {pid: b for pid, b in balances.items()})

    def test_empty_and_all_zero(self):
        self.assertEqual(settle_balances({}), [])
        self.assertEqual(settle_balances({"a": 0, "b": 0}), [])

    def test_single_pair(self):
        balances = {"a": -2, "b": 2}
        transfers = settle_balances(balances)
        self.assertEqual(transfers, [("a", "b", 2)])
        self._check(balances, transfers)

    def test_zero_balances_never_appear(self):
        balances = {"a": -3, "b": 0, "c": 3, "d": 0}
        transfers = settle_balances(balances)
        self.assertEqual(transfers, [("a", "c", 3)])
        self._check(balances, transfers)

    def test_zero_sum_groups_collapse_transfers(self):
        # (-5,-1,+5,+1) splits into two zero-sum groups: d1<->c2 (5)
        # and d2<->c1 (1), needing only 2 transfers instead of the greedy 3.
        balances = {"d1": -5, "d2": -1, "c1": 1, "c2": 5}
        transfers = settle_balances(balances)
        self.assertEqual(transfers, [("d1", "c2", 5), ("d2", "c1", 1)])
        self.assertEqual(len(transfers), 2)
        self._check(balances, transfers)

    def test_tie_broken_by_canonical_id_order(self):
        # One atomic zero-sum group; smallest debtor d1 must pay smallest
        # creditor c1, and the smallest feasible bridge flow is 1.
        balances = {"d1": -6, "d2": -3, "c1": 4, "c2": 5}
        transfers = settle_balances(balances)
        self.assertEqual(
            transfers,
            [("d1", "c1", 1), ("d1", "c2", 5), ("d2", "c1", 3)],
        )
        self._check(balances, transfers)

    def test_integer_ids_compare_numerically(self):
        # 9 sorts before 10 numerically (string order would differ).
        balances = {9: -1, 10: 1}
        self.assertEqual(settle_balances(balances), [(9, 10, 1)])
        balances = {"a": -3, 2: 1, "b": 2}  # ints before strings
        transfers = settle_balances(balances)
        self.assertEqual(transfers, [("a", 2, 1), ("a", "b", 2)])
        self._check(balances, transfers)

    def test_input_order_does_not_matter(self):
        balances = {"a": -6, "b": -3, "c": 4, "d": 5}
        expected = settle_balances(balances)
        for order in [reversed(list(balances)), list("cbad"), list("dbca")]:
            self.assertEqual(
                settle_balances({pid: balances[pid] for pid in order}),
                expected,
            )

    def test_unbalanced_input_is_rejected(self):
        with self.assertRaises(ValueError):
            settle_balances({"a": -2, "b": 1})

    def test_greedy_fallback_beyond_12_nonzero(self):
        # 13 non-zero balances: solver switches to the canonical greedy path;
        # the result must still settle every balance and stay <= n-1 edges.
        balances = {f"d{i}": -2 for i in range(7)}
        balances.update({f"c{i}": 2 for i in range(6)})
        balances["c0"] = 4
        self.assertEqual(len([b for b in balances.values() if b]), 13)
        transfers = settle_balances(balances)
        self.assertLessEqual(len(transfers), 12)
        self._check(balances, transfers)
        self.assertEqual(transfers, sorted(transfers, key=_triple))

    def test_optimum_and_lex_uniqueness_against_brute_force(self):
        rng = random.Random(20260919)
        for _ in range(100):
            n = rng.randint(2, 6)
            nd = rng.randint(1, n - 1)
            debtors = [rng.randint(1, 20) for _ in range(nd)]
            creditors = [rng.randint(1, 20) for _ in range(n - nd)]
            debtors[0] += sum(creditors) - sum(debtors)
            values = [-v for v in debtors] + creditors
            rng.shuffle(values)
            balances = {f"p{i}": values[i] for i in range(n)}
            got = tuple(settle_balances(balances))
            expected = _brute_force_best(balances)
            self.assertEqual(got, expected, balances)


def _brute_force_best(balances):
    """Minimum-count, then lex-smallest, settlement by exhaustive DFS."""
    from app.splitter import _triple
    ids = list(balances)
    best = None

    def rec(net, edges):
        nonlocal best
        if best is not None and len(edges) > len(best):
            return
        if all(v == 0 for v in net.values()):
            seq = tuple(sorted(edges, key=_triple))
            if best is None or (len(seq), seq) < (len(best), best):
                best = seq
            return
        for frm in ids:
            if net[frm] >= 0:
                continue
            for to in ids:
                if net[to] <= 0:
                    continue
                amount = min(-net[frm], net[to])
                nxt = dict(net)
                nxt[frm] += amount
                nxt[to] -= amount
                rec(nxt, edges + [(frm, to, amount)])

    rec(dict(balances), [])
    return best


class SettleOptimalityPropertyTests(unittest.TestCase):
    @staticmethod
    def _min_transfer_count(values):
        """Independent reference: n - (maximum zero-sum subset partition)."""
        n = len(values)
        full = (1 << n) - 1
        sums = [0] * (1 << n)
        for mask in range(1, 1 << n):
            bit = mask & -mask
            sums[mask] = sums[mask ^ bit] + values[bit.bit_length() - 1]
        zero_sums = frozenset(mask for mask in range(1, 1 << n) if sums[mask] == 0)

        groups = [-1] * (1 << n)  # maximum number of zero-sum groups in mask

        def g(mask):
            if mask == 0:
                return 0
            if groups[mask] < 0:
                best = 0
                sub = mask
                while sub:
                    if sub in zero_sums:
                        best = max(best, 1 + g(mask ^ sub))
                    sub = (sub - 1) & mask
                groups[mask] = best
            return groups[mask]

        return n - g(full)

    def test_transfer_count_is_globally_minimal(self):
        rng = random.Random(99173)
        for _ in range(60):
            n = rng.randint(2, 12)
            nd = rng.randint(1, n - 1)
            debtors = [rng.randint(1, 30) for _ in range(nd)]
            creditors = [rng.randint(1, 30) for _ in range(n - nd)]
            debtors[0] += sum(creditors) - sum(debtors)
            values = [-v for v in debtors] + creditors
            rng.shuffle(values)
            balances = {f"p{i}": values[i] for i in range(n)}
            transfers = settle_balances(balances)
            self.assertEqual(len(transfers), self._min_transfer_count(values), balances)
            # settlement is sound and the sequence is canonically ordered
            net = {pid: 0 for pid in balances}
            for frm, to, amount in transfers:
                net[frm] -= amount
                net[to] += amount
            self.assertEqual(net, balances)
            self.assertEqual(transfers, sorted(transfers, key=_triple))


if __name__ == "__main__":
    unittest.main()
