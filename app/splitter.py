"""Core fare-splitting algorithm.

Pure standard library (exact ``fractions.Fraction`` arithmetic), so it can be
unit-tested without any web dependencies and stays free of float rounding
errors. Money amounts are integer cents; theoretical shares are exact
fractions.
"""

from __future__ import annotations

from fractions import Fraction
from functools import lru_cache
from itertools import combinations
from math import factorial, gcd as _gcd
from typing import Dict, List, Tuple


def tie_key(passenger_id) -> tuple:
    """Total, input-order-independent order used ONLY to break deficit ties.

    Integer ids compare numerically and sort before string ids, which compare
    lexicographically. Because every ranking ends with this key, permuting the
    passengers in the request never changes the outcome.
    """
    if isinstance(passenger_id, bool):  # bool is an int subclass; keep it sane
        return (1, str(passenger_id))
    if isinstance(passenger_id, int):
        return (0, passenger_id)
    return (1, str(passenger_id))


def split_fares(segment_costs: List[int], passengers) -> Tuple[Dict, List[dict]]:
    """Split per-segment costs between passengers, segment by segment.

    ``passengers`` items must expose ``id``, ``board`` and ``alight``
    attributes; a passenger occupies segments ``board <= i < alight``.

    Per segment the cost is shared equally between everyone on board. When the
    cost does not divide evenly, the leftover cents (``cost % n``) go one each
    to the passengers with the largest *deficit* — their cumulative exact
    theoretical share minus what they have actually been allocated so far —
    with ties broken by :func:`tie_key`.

    Returns ``(totals, details)``: ``totals`` maps passenger id -> allocated
    cents (inserted in canonical id order); ``details`` is a per-segment list
    of dicts with keys ``index``, ``cost``, ``passengers`` and ``shares``.
    """
    allocated = {p.id: 0 for p in passengers}
    theoretical = {p.id: Fraction(0) for p in passengers}
    details: List[dict] = []

    for index, cost in enumerate(segment_costs):
        onboard = sorted(
            (p.id for p in passengers if p.board <= index < p.alight),
            key=tie_key,
        )
        if not onboard:
            # Only reachable for zero-cost segments; charged empty segments
            # are rejected during request validation (HTTP 422).
            details.append({"index": index, "cost": cost, "passengers": [], "shares": {}})
            continue

        base, remainder = divmod(cost, len(onboard))
        share = Fraction(cost, len(onboard))
        for pid in onboard:
            theoretical[pid] += share

        # Deficit if everyone on board received only the base amount: how far
        # the passenger's actual allocation would lag their cumulative
        # theoretical share. (Adding the current segment's share and base to
        # every onboard passenger shifts all deficits by the same constant,
        # so the ranking is identical to using pre-segment state.)
        deficits = {pid: theoretical[pid] - (allocated[pid] + base) for pid in onboard}
        ranked = sorted(onboard, key=lambda pid: (-deficits[pid], tie_key(pid)))
        extra = set(ranked[:remainder])

        shares = {pid: base + (1 if pid in extra else 0) for pid in onboard}
        for pid, amount in shares.items():
            allocated[pid] += amount

        details.append(
            {"index": index, "cost": cost, "passengers": onboard, "shares": shares}
        )

    totals = {pid: allocated[pid] for pid in sorted(allocated, key=tie_key)}
    return totals, details


# --- admission: pick the best feasible subset of candidate passengers


def admit_passengers(segment_costs: List[int], passengers, candidates,
                     capacity: int) -> Tuple[List, Dict, List[dict]]:
    """Choose which candidate passengers to admit onto an existing trip.

    ``passengers`` are already on the trip; ``candidates`` (at most 12)
    additionally expose a ``max_fare`` attribute — the most they are willing
    to pay, in cents. A subset of candidates is *feasible* when, with those
    candidates riding alongside the existing passengers:

    * every segment carries at most ``capacity`` passengers, and
    * each admitted candidate's share — recomputed by :func:`split_fares`
      over the *full* resulting roster — does not exceed their ``max_fare``.

    Feasible subsets are ranked by, in order: most admitted candidates,
    then least total cents owed by the admitted candidates, then the
    lexicographically smallest candidate-id list in :func:`tie_key` order.
    The empty subset is always feasible, so when no candidate can be
    admitted the result is simply the original trip's split.

    Returns ``(admitted_ids, totals, details)`` where ``admitted_ids`` is
    the winning candidate-id list in canonical :func:`tie_key` order and
    ``totals``/``details`` are the :func:`split_fares` result for the full
    roster (existing passengers plus the admitted candidates).
    """
    n_segments = len(segment_costs)

    # Existing roster, pre-sorted in canonical order per segment.
    existing = sorted(passengers, key=lambda p: tie_key(p.id))
    existing_by_seg = [
        [p for p in existing if p.board <= i < p.alight]
        for i in range(n_segments)
    ]

    ordered = sorted(candidates, key=lambda c: tie_key(c.id))
    n = len(ordered)
    candidate_key = [tie_key(c.id) for c in ordered]
    id_key = {c.id: candidate_key[j] for j, c in enumerate(ordered)}
    id_key.update({p.id: tie_key(p.id) for p in existing})
    max_fare = [c.max_fare for c in ordered]
    # Per candidate, a bitmask of the segments they occupy.
    cand_mask = [
        sum(1 << i for i in range(c.board, c.alight)) for c in ordered
    ]
    # Per segment, candidates (canonical index) present on it.
    cands_by_seg = [
        [j for j in range(n) if cand_mask[j] & (1 << i)]
        for i in range(n_segments)
    ]
    # Capacity slack per segment after the existing passengers.
    slack = [capacity - len(existing_by_seg[i]) for i in range(n_segments)]

    # Deficits are exact fractions with denominator <= number of onboard
    # passengers; scaling by lcm(1..max_onboard) makes every deficit an
    # integer, so the whole search is Fraction-free. Onboard count never
    # exceeds existing passengers plus the (at most 12) candidates.
    max_onboard = min(capacity, len(passengers) + n)
    scale = 1
    for k in range(2, max_onboard + 1):
        scale = scale * k // _gcd(scale, k)

    def evaluate(mask: int, want_details: bool):
        """Run the same allocation as split_fares for the roster in ``mask``.

        Returns ``(totals, details)`` exactly as :func:`split_fares` would
        for the full roster, or ``None`` as soon as some selected candidate
        can no longer finish within their ``max_fare``. Deficits are kept
        scaled by ``scale`` rather than as :class:`Fraction` values.
        """
        deficits = {p.id: 0 for p in existing}
        deficits.update(
            (ordered[j].id, 0) for j in range(n) if mask & (1 << j)
        )
        allocated = {pid: 0 for pid in deficits}
        chosen_set = mask
        details: List[dict] = []

        for i, cost in enumerate(segment_costs):
            present = [j for j in cands_by_seg[i] if chosen_set & (1 << j)]
            if not present:
                onboard_ids = [p.id for p in existing_by_seg[i]]
            else:
                # Merge two canonical-id-sorted lists.
                chosen_ids = [ordered[j].id for j in present]
                merged, a, b = [], 0, 0
                old = [p.id for p in existing_by_seg[i]]
                while a < len(old) and b < len(chosen_ids):
                    if id_key[old[a]] <= candidate_key[present[b]]:
                        merged.append(old[a]); a += 1
                    else:
                        merged.append(chosen_ids[b]); b += 1
                merged.extend(old[a:]); merged.extend(chosen_ids[b:])
                onboard_ids = merged

            if not onboard_ids:
                if want_details:
                    details.append({"index": i, "cost": cost,
                                    "passengers": [], "shares": {}})
                continue

            m = len(onboard_ids)
            base, rem = divmod(cost, m)
            add = scale * rem // m
            # Largest deficit (including this segment's add) wins the
            # leftover cents; ties are broken by canonical id order.
            ranked = sorted(
                onboard_ids,
                key=lambda pid: (-(deficits[pid] + add), id_key[pid]),
            )
            extra = set(ranked[:rem])
            shares = {}
            for pid in onboard_ids:
                amount = base + (1 if pid in extra else 0)
                shares[pid] = amount
                allocated[pid] += amount
                # Scaled deficit accumulates rem/m; recipients of a leftover
                # cent are charged 1 (== scale/scale). The shared base term
                # never affects rankings and is omitted.
                deficits[pid] += add
                if pid in extra:
                    deficits[pid] -= scale
            if not want_details:
                # Selected candidates already above their cap can never
                # recover (future allocations are non-negative).
                for j in present:
                    if allocated[ordered[j].id] > max_fare[j]:
                        return None
            if want_details:
                details.append({"index": i, "cost": cost,
                                "passengers": onboard_ids, "shares": shares})

        if not want_details:
            for j in range(n):
                if chosen_set & (1 << j) and allocated[ordered[j].id] > max_fare[j]:
                    return None
        totals = {pid: allocated[pid] for pid in sorted(allocated, key=id_key.__getitem__)}
        return totals, details if want_details else None

    best = None  # ((fare_sum, canonical id tuple), mask, totals)

    # Largest subsets first; capacity feasibility is downward closed, so
    # stop splitting as soon as one size yields a fare-feasible roster.
    for size in range(n, -1, -1):
        for combo in combinations(range(n), size):
            mask = 0
            for j in combo:
                mask |= 1 << j
            # Union of the chosen candidates' occupied segments.
            over = False
            for i in range(n_segments):
                extra = 0
                for j in combo:
                    if cand_mask[j] & (1 << i):
                        extra += 1
                if extra > slack[i]:
                    over = True
                    break
            if over:
                continue

            result = evaluate(mask, want_details=False)
            if result is None:
                continue
            totals, _ = result
            fare_sum = sum(totals[ordered[j].id] for j in combo)
            rank = (fare_sum, tuple(candidate_key[j] for j in combo))
            if best is None or rank < best[0]:
                best = (rank, mask, totals)
        if best is not None:
            break

    _, mask, totals = best
    if mask == 0:
        # Preserve split_fares output for the no-newcomer fallback.
        totals, details = split_fares(segment_costs, list(passengers))
    else:
        _, details = evaluate(mask, want_details=True)
    return [ordered[j].id for j in range(n) if mask & (1 << j)], totals, details


# --- revision impact: Shapley-style marginal effect of each changed segment


def revision_impact(original_costs: List[int], revised_costs: List[int],
                    passengers) -> Tuple[Dict, Dict, List[dict]]:
    """Attribute each passenger's fare change to the changed-cost segments.

    ``original_costs`` and ``revised_costs`` are two equal-length segment cost
    lists over the *same* fixed roster ``passengers`` (the same segments /
    passengers on board — only the money charged on some segments changes).

    Think of applying the changed segments one at a time: starting from the
    original costs, every application order is a path from the original split
    to the revised one, and a changed segment's *marginal effect* is how much
    every passenger's allocation moves when that segment is switched to its
    revised cost, given the set of other changed segments already applied.
    Because deficit allocation is sequential (a segment's leftovers depend on
    the costs processed earlier), each intermediate subset of changes is
    re-split from scratch with :func:`split_fares`, using the original cost
    outside the subset and the revised cost inside it.

    Averaging each changed segment's marginal effect over all application
    orders is its Shapley contribution. For changed segment ``j`` and
    passenger ``p``::

        contribution(j, p) =
            sum over subsets S of the *other* changed segments of
                w(k) * (allocation_p(costs with S U {j}) - allocation_p(costs with S)),
            k = |S|,  w(k) = k! (m - 1 - k)! / m!,  m = #changed segments.

    The contributions are exact :class:`Fraction` values; they may be
    negative (switching a segment can reduce a passenger's allocation, e.g.
    when an odd leftover cent moves elsewhere), and for every passenger they
    sum exactly to ``revised_total - original_total``.

    Returns ``(before, after, changes)``: ``before``/``after`` map passenger
    id (canonical order) to original/revised integer-cents totals; ``changes``
    lists one dict per changed segment, in index order, with keys ``index``,
    ``original_cost``, ``revised_cost`` and ``contributions`` (id -> Fraction
    for every passenger). With no changed segments ``changes`` is empty.
    """
    if len(original_costs) != len(revised_costs):
        raise ValueError("original and revised segment lists must have equal length")

    changed = [i for i, (a, b) in enumerate(zip(original_costs, revised_costs)) if a != b]
    m = len(changed)
    ids = sorted((p.id for p in passengers), key=tie_key)

    before, _ = split_fares(original_costs, passengers)
    after, _ = split_fares(revised_costs, passengers)

    changes: List[dict] = []
    if m == 0:
        return before, after, changes

    # Totals for every subset of changed segments (bit j of the mask selects
    # changed[j]); selected segments use their revised cost, the rest the
    # original.
    totals_for_mask: Dict[int, Dict] = {}
    for mask in range(1 << m):
        costs = list(original_costs)
        for j, index in enumerate(changed):
            if mask & (1 << j):
                costs[index] = revised_costs[index]
        totals_for_mask[mask], _ = split_fares(costs, passengers)

    for j, index in enumerate(changed):
        bit = 1 << j
        others = ((1 << m) - 1) ^ bit
        contributions: Dict = {pid: Fraction(0) for pid in ids}
        sub = others
        while True:
            k = bin(sub).count("1")
            weight = Fraction(
                factorial(k) * factorial(m - 1 - k), factorial(m)
            )
            with_j = totals_for_mask[sub | bit]
            without_j = totals_for_mask[sub]
            for pid in ids:
                contributions[pid] += weight * (with_j[pid] - without_j[pid])
            if sub == 0:
                break
            sub = (sub - 1) & others

        changes.append({
            "index": index,
            "original_cost": original_costs[index],
            "revised_cost": revised_costs[index],
            "contributions": contributions,
        })

    return before, after, changes


# --- fare envelope: one passenger's payable range over two open segment fares


def fare_envelope(segments, bounds, passengers, total_cost, target_id):
    """Exact min/max of one passenger's split total over two open fares.

    ``segments`` is the trip's cost list in travel order with exactly two
    ``None`` entries (the undetermined segments); ``bounds`` holds one
    inclusive ``(min, max)`` integer-cents pair per undetermined segment,
    in segment order. A *completion* fills the two blanks with integer
    cents inside their bounds so that all costs sum to ``total_cost``;
    every completion is split with the same cumulative-deficit rule as
    :func:`split_fares`. Returns ``(min_amount, min_costs, max_amount,
    max_costs)``: the target passenger's exact payable extremes and, for
    each, a complete witnessing cost vector — the lexicographically
    smallest vector when several completions attain the same extreme.

    Completions are never enumerated cent by cent. With ``u < v`` the open
    positions and ``S = total_cost - sum(fixed)``, the totals constraint
    forces the second open fare to ``S - x`` when the first is ``x``, so a
    single integer variable ``x`` ranges over ``[lo, hi]`` (the bounds plus
    the totals constraint). The deficit state driving the leftover-cent
    allocation evolves only through the per-segment *remainders*
    ``cost % n``: each segment's equal base cancels between the
    theoretical share and the allocated amount. The whole remainder
    sequence — hence every deficit comparison and every leftover-cent
    assignment, cross-segment deficit carry included — is periodic in
    ``x`` with period ``P = lcm(n_u, n_v)``, where ``n_u``/``n_v`` are the
    on-board counts of the two open segments (at most 12 passengers, so
    ``P <= 132``). The target's payable amount is therefore::

        f(x) = k * x + g(x mod P) + const,
        k = [target on u] / n_u - [target on v] / n_v,

    because only the two open segments' equal bases are non-periodic. For
    ``k > 0`` the minimum lies in the first period window and the maximum
    in the last one (conversely for ``k < 0``; for ``k == 0`` the amount
    is purely periodic). Scanning those two windows — at most ``2P``
    candidates, however wide ``[lo, hi]`` is — yields the exact extremes;
    scanning each window in ascending ``x`` keeps the smallest ``x`` on
    ties, and since ``u`` is the first position where two completions
    differ, the smallest ``x`` is the lexicographically smallest witness.
    """
    blanks = [i for i, cost in enumerate(segments) if cost is None]
    if len(blanks) != 2:
        raise ValueError(
            f"exactly two undetermined (None) segments are required, "
            f"found {len(blanks)}"
        )
    u, v = blanks
    (lo_u, hi_u), (lo_v, hi_v) = bounds

    residual = total_cost - sum(c for c in segments if c is not None)
    lo = max(lo_u, residual - hi_v)
    hi = min(hi_u, residual - lo_v)
    if lo > hi:
        raise ValueError(
            f"no feasible cost combination: the two undetermined segments "
            f"must sum to {residual} cents, which the bounds "
            f"[{lo_u}, {hi_u}] and [{lo_v}, {hi_v}] cannot reach"
        )

    onboard = [
        [p.id for p in passengers if p.board <= i < p.alight]
        for i in range(len(segments))
    ]
    n_u, n_v = len(onboard[u]), len(onboard[v])
    if n_u == 0 or n_v == 0:
        raise ValueError(
            "undetermined segments must carry at least one passenger"
        )
    target = next(
        (p.id for p in passengers if str(p.id) == str(target_id)), None
    )
    if target is None:
        raise ValueError(f"unknown target passenger id: {target_id!r}")

    # f(x + P) = f(x) + k*P (see docstring); `trend` is k scaled by
    # n_u * n_v, so its sign decides which period window holds each extreme.
    period = n_u * n_v // _gcd(n_u, n_v)
    trend = (target in onboard[u]) * n_v - (target in onboard[v]) * n_u

    values = {}

    def payable(x: int) -> int:
        """Target's split total when the open fares are x and residual-x."""
        if x not in values:
            costs = list(segments)
            costs[u] = x
            costs[v] = residual - x
            totals, _ = split_fares(costs, passengers)
            values[x] = totals[target]
        return values[x]

    head = range(lo, min(hi, lo + period - 1) + 1)
    tail = range(max(lo, hi - period + 1), hi + 1)

    def arg_extreme(candidates, minimize: bool):
        """First (smallest-x) minimizer/maximizer of ``payable``, in order."""
        best_x = best_val = None
        for x in candidates:
            value = payable(x)
            if best_val is None or (value < best_val if minimize
                                    else value > best_val):
                best_x, best_val = x, value
        return best_x, best_val

    if trend > 0:
        min_x, min_amount = arg_extreme(head, minimize=True)
        max_x, max_amount = arg_extreme(tail, minimize=False)
    elif trend < 0:
        min_x, min_amount = arg_extreme(tail, minimize=True)
        max_x, max_amount = arg_extreme(head, minimize=False)
    else:
        min_x, min_amount = arg_extreme(head, minimize=True)
        max_x, max_amount = arg_extreme(head, minimize=False)

    def witness(x: int) -> List[int]:
        costs = list(segments)
        costs[u] = x
        costs[v] = residual - x
        return costs

    return min_amount, witness(min_x), max_amount, witness(max_x)


# --- settlement: turn balances (paid - owed) into a minimal set of transfers


def _triple(transfer):
    """Comparable identity of one transfer: ``(from_key, to_key, amount)``."""
    frm, to, amount = transfer[:3]
    return (tie_key(frm), tie_key(to), amount)


def settle_balances(balances: Dict) -> List[Tuple]:
    """Settle integer ``balances`` (id -> paid - owed cents) with transfers.

    Every balance must be an integer; positive participants overpaid,
    negative participants underpaid, and the values sum to zero. Returns a
    list of ``(from, to, amount)`` tuples where ``from`` owes ``amount`` cents
    to ``to``; zero-balance participants never appear.

    For at most 12 non-zero balances the result has the globally minimal
    number of transfers (found with an exact subset partition + bridge-cut
    dynamic program). When several minima exist, the sequence of
    ``(from, to, amount)`` triples — compared in the canonical passenger-id
    order defined by :func:`tie_key` — is lexicographically minimal, which
    makes the result independent of the dict's input order. Beyond 12
    non-zero balances a deterministic greedy settlement (no worse than
    ``n - 1`` transfers) is used instead.
    """
    nonzero = [
        (pid, balances[pid])
        for pid in sorted(balances, key=tie_key)
        if balances[pid] != 0
    ]
    if sum(amount for _, amount in nonzero) != 0:
        raise ValueError("balances must sum to zero")
    if len(nonzero) > 12:
        return _greedy_transfers(nonzero)
    return _optimal_transfers(nonzero)


def _greedy_transfers(nonzero: List[Tuple]) -> List[Tuple]:
    """Canonical greedy fallback: smallest debtor pays smallest creditor.

    Always terminates within ``n - 1`` transfers; used only when too many
    non-zero balances exist for the exact solver to be worthwhile.
    """
    state = {pid: amount for pid, amount in nonzero}
    transfers: List[Tuple] = []
    while state:
        debtor = min((pid for pid in state if state[pid] < 0), key=tie_key)
        creditor = min((pid for pid in state if state[pid] > 0), key=tie_key)
        amount = min(-state[debtor], state[creditor])
        transfers.append((debtor, creditor, amount))
        state[debtor] += amount
        state[creditor] -= amount
        if state[debtor] == 0:
            del state[debtor]
        if creditor in state and state[creditor] == 0:
            del state[creditor]
    return sorted(transfers, key=_triple)


def _optimal_transfers(nonzero: List[Tuple]) -> List[Tuple]:
    """Exact minimum-transfer settlement (``len(nonzero) <= 12``).

    Idea: view transfers as edges of a flow graph. A set of participants can
    be settled internally iff its balances sum to zero; hence the minimum
    number of transfers is ``n - k`` where ``k`` is the maximum number of
    non-empty zero-sum subsets partitioning the participants ("atomic"
    groups admit no smaller zero-sum subset and need ``size - 1`` transfers).

    The best partition is found by subset DP; inside each atomic group the
    lexicographically smallest transfer forest is built by recursively
    forcing a bridge from the smallest debtor to the smallest creditor and
    enumerating the cut (flow) across that bridge.
    """
    n = len(nonzero)
    if n == 0:
        return []
    amounts = [amount for _, amount in nonzero]
    full = (1 << n) - 1

    sums = [0] * (1 << n)
    for mask in range(1, 1 << n):
        bit = mask & -mask
        sums[mask] = sums[mask ^ bit] + amounts[bit.bit_length() - 1]

    @lru_cache(maxsize=None)
    def is_atomic(mask: int) -> bool:
        """True for a zero-sum mask with no proper non-empty zero-sum subset."""
        if sums[mask] != 0:
            return False
        rest = mask ^ (mask & -mask)
        if rest == 0:
            return True
        sub = (rest - 1) & rest
        while sub:
            # A zero-sum subset containing the low bit would leave a zero-sum
            # complement in `rest`, so subsets of `rest` suffice.
            if sums[sub] == 0 or sums[rest ^ sub] == 0:
                return False
            sub = (sub - 1) & rest
        return True

    @lru_cache(maxsize=None)
    def solve_state(canonical):
        """Lexicographically smallest minimal transfer tuple for one state.

        ``canonical`` is a tuple of ``(id, balance)`` pairs in canonical id
        order, summing to zero. The lex-min forest must contain an edge from
        the smallest debtor ``d`` to the smallest creditor ``c``; enumerate
        its flow ``t`` via the subset staying on d's side of the cut.
        """
        if len(canonical) <= 1:
            return ()
        debtors = sorted((pid for pid, b in canonical if b < 0), key=tie_key)
        creditors = sorted((pid for pid, b in canonical if b > 0), key=tie_key)
        if not debtors:
            return ()  # all balances zero: nothing to settle
        d_id, c_id = debtors[0], creditors[0]
        balance = dict(canonical)
        d_bal, c_bal = balance[d_id], balance[c_id]
        others = [pid for pid, _ in canonical if pid != d_id and pid != c_id]

        best = None
        for amask in range(1 << len(others)):
            # Side A keeps d; side B keeps c. A's net balance is -t, the
            # amount d pays across the bridge to c.
            side_a = {d_id: d_bal}
            for j, pid in enumerate(others):
                if amask & (1 << j):
                    side_a[pid] = balance[pid]
            t = -sum(side_a.values())
            if t <= 0 or t > -d_bal or t > c_bal:
                continue
            side_a[d_id] += t
            side_b = {c_id: c_bal - t}
            for j, pid in enumerate(others):
                if not (amask & (1 << j)):
                    side_b[pid] = balance[pid]
            state_a = tuple(sorted(
                ((pid, bal) for pid, bal in side_a.items() if bal != 0),
                key=lambda item: tie_key(item[0]),
            ))
            state_b = tuple(sorted(
                ((pid, bal) for pid, bal in side_b.items() if bal != 0),
                key=lambda item: tie_key(item[0]),
            ))
            candidate = tuple(sorted(
                ((d_id, c_id, t),) + solve_state(state_a) + solve_state(state_b),
                key=_triple,
            ))
            if best is None or _better(candidate, best):
                best = candidate
        return best

    def _better(candidate, best) -> bool:
        if len(candidate) != len(best):
            return len(candidate) < len(best)
        return [_triple(t) for t in candidate] < [_triple(t) for t in best]

    @lru_cache(maxsize=None)
    def partition(mask: int):
        """Best transfer tuple for the zero-sum subset ``mask``.

        The component holding its least set bit must be an atomic zero-sum
        subset; enumerate those and recurse on the remainder.
        """
        if mask == 0:
            return ()
        low = mask & -mask
        remaining = mask ^ low
        best = None
        sub = remaining
        while True:
            atom = low | sub
            if is_atomic(atom):
                group = tuple(sorted(
                    ((nonzero[i][0], amounts[i])
                     for i in range(n) if atom & (1 << i)),
                    key=lambda item: tie_key(item[0]),
                ))
                candidate = tuple(sorted(
                    solve_state(group) + partition(remaining ^ sub),
                    key=_triple,
                ))
                if best is None or _better(candidate, best):
                    best = candidate
            if sub == 0:
                break
            sub = (sub - 1) & remaining
        return best

    return [
        (frm, to, amount)
        for frm, to, amount in sorted(partition(full), key=_triple)
    ]
