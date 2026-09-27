"""Request/response schemas and request validation.

All domain-rule violations (invalid intervals, out-of-range stations,
duplicate ids, empty-load charged segments, ...) are raised as
``ValueError`` inside Pydantic validators, so FastAPI turns them into a
uniform HTTP 422 response.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, model_validator

PassengerId = Union[str, int]

# At most 100 segments / 12 passengers; every money amount (segment costs,
# integer bounds and the trip total) is at most 10**12 cents.
MAX_CENTS = 10**12
MAX_ENVELOPE_SEGMENTS = 100
MAX_PASSENGERS = 12


class Passenger(BaseModel):
    """One passenger's ride, expressed as station indices.

    A trip with ``N`` segments has stations ``0..N``; a passenger who boards
    at ``board`` and alights at ``alight`` occupies segments
    ``board, board+1, ..., alight-1``.
    """

    model_config = ConfigDict(extra="forbid")

    id: PassengerId = Field(description="unique passenger id (string or integer)")
    board: NonNegativeInt = Field(description="station index where the passenger boards")
    alight: NonNegativeInt = Field(description="station index where the passenger alights")


def _validate_roster(n: int, passengers) -> None:
    """Reject duplicate ids and invalid boarding intervals for a trip."""
    seen = set()
    for p in passengers:
        key = str(p.id)
        if key in seen:
            raise ValueError(f"duplicate passenger id: {p.id!r}")
        seen.add(key)
        if p.board >= p.alight:
            raise ValueError(
                f"passenger {p.id!r}: invalid interval board={p.board}, "
                f"alight={p.alight} (board must be < alight)"
            )
        if p.alight > n:
            raise ValueError(
                f"passenger {p.id!r}: alight station {p.alight} is out of range; "
                f"a trip with {n} segment(s) has stations 0..{n}"
            )


def _validate_charged_segments(segment_costs: List[int], passengers) -> None:
    """Reject positive-cost segments that carry no passengers."""
    for i, cost in enumerate(segment_costs):
        if cost > 0 and not any(p.board <= i < p.alight for p in passengers):
            raise ValueError(
                f"segment {i} has cost {cost} but no passengers on board "
                f"(empty-load charged segment)"
            )


class SplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: List[NonNegativeInt] = Field(
        min_length=1,
        description="integer cost of each segment in cents, in travel order",
    )
    passengers: List[Passenger] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_trip(self) -> "SplitRequest":
        n = len(self.segments)
        _validate_roster(n, self.passengers)
        _validate_charged_segments(self.segments, self.passengers)
        return self


class SegmentSplit(BaseModel):
    """How one segment's cost was divided."""

    index: int = Field(description="segment index, 0-based, in travel order")
    cost: NonNegativeInt = Field(description="segment cost in cents")
    passengers: List[PassengerId] = Field(description="ids on board during this segment")
    shares: Dict[str, NonNegativeInt] = Field(
        description="cents charged to each on-board passenger; sums to cost"
    )


class SplitResponse(BaseModel):
    total_cost: NonNegativeInt = Field(description="sum of all segment costs in cents")
    totals: Dict[str, NonNegativeInt] = Field(
        description="cents charged to each passenger over the whole trip; sums to total_cost"
    )
    segments: List[SegmentSplit] = Field(description="per-segment split detail")


class Payment(BaseModel):
    """One amount already paid, in cents — exactly one per passenger."""

    model_config = ConfigDict(extra="forbid")

    id: PassengerId = Field(description="id of the paying passenger (string or integer)")
    amount: NonNegativeInt = Field(description="cents already paid by this passenger; must be >= 0")


class SettleRequest(SplitRequest):
    """A split request plus one non-negative payment record per passenger."""

    payments: List[Payment] = Field(
        min_length=0,
        description="one paid amount (integer cents) per passenger, no more and no less",
    )

    @model_validator(mode="after")
    def _check_payments(self) -> "SettleRequest":
        passenger_ids = {str(p.id): p.id for p in self.passengers}
        paid: Dict[str, int] = {}
        for payment in self.payments:
            key = str(payment.id)
            if key in paid:
                raise ValueError(f"duplicate payment id: {payment.id!r}")
            if key not in passenger_ids:
                raise ValueError(
                    f"payment for unknown passenger id: {payment.id!r} "
                    f"(no such passenger in the trip)"
                )
            paid[key] = payment.amount

        missing = [passenger_ids[k] for k in passenger_ids if k not in paid]
        if missing:
            raise ValueError(
                f"missing payment for passenger id(s): "
                f"{', '.join(repr(i) for i in missing)}"
            )

        total_paid = sum(paid.values())
        total_cost = sum(self.segments)
        if total_paid != total_cost:
            raise ValueError(
                f"payments total {total_paid} cents but the trip costs "
                f"{total_cost} cents (paid amounts must cover the trip exactly)"
            )
        return self


class Transfer(BaseModel):
    """One debt-settling payment in cents."""

    model_config = ConfigDict(populate_by_name=True)

    from_: PassengerId = Field(
        alias="from",
        description="id of the passenger who owes money (negative balance)",
    )
    to: PassengerId = Field(
        description="id of the passenger who receives money (positive balance)",
    )
    amount: PositiveInt = Field(description="cents transferred; always > 0")


class SettleResponse(BaseModel):
    owed: Dict[str, NonNegativeInt] = Field(
        description="cents each passenger should pay (same as /split totals)"
    )
    paid: Dict[str, NonNegativeInt] = Field(
        description="cents each passenger actually paid, per the request payments"
    )
    balance: Dict[str, int] = Field(
        description="paid - owed per passenger; negative means the passenger owes money"
    )
    transfers: List[Transfer] = Field(
        description="minimum set of payments that settles every balance, "
                    "ordered by (from, to, amount) in canonical passenger-id order"
    )


class Candidate(Passenger):
    """A passenger applying to be admitted, with a willingness-to-pay cap."""

    model_config = ConfigDict(extra="forbid")

    max_fare: NonNegativeInt = Field(
        description="most the candidate is willing to pay in integer cents; "
                    "they are admitted only if their recomputed share does not exceed it"
    )


class AdmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: List[NonNegativeInt] = Field(
        min_length=1,
        description="integer cost of each segment in cents, in travel order",
    )
    passengers: List[Passenger] = Field(
        default_factory=list,
        description="passengers already on the trip",
    )
    capacity: PositiveInt = Field(
        description="uniform per-segment seat cap; the number of passengers "
                    "(existing plus newly admitted) on every segment must not exceed it",
    )
    candidates: List[Candidate] = Field(
        max_length=12,
        description="up to 12 candidate passengers (id/board/alight/max_fare) "
                    "considered for admission",
    )

    @model_validator(mode="after")
    def _check_admit(self) -> "AdmitRequest":
        n = len(self.segments)
        _validate_roster(n, self.passengers)

        seen = {str(p.id) for p in self.passengers}
        for c in self.candidates:
            key = str(c.id)
            if key in seen:
                raise ValueError(f"duplicate passenger id: {c.id!r}")
            seen.add(key)
            if c.board >= c.alight:
                raise ValueError(
                    f"candidate {c.id!r}: invalid interval board={c.board}, "
                    f"alight={c.alight} (board must be < alight)"
                )
            if c.alight > n:
                raise ValueError(
                    f"candidate {c.id!r}: alight station {c.alight} is out of range; "
                    f"a trip with {n} segment(s) has stations 0..{n}"
                )

        for i, cost in enumerate(self.segments):
            load = sum(p.board <= i < p.alight for p in self.passengers)
            if load > self.capacity:
                raise ValueError(
                    f"segment {i} already carries {load} existing passenger(s), "
                    f"exceeding capacity {self.capacity}"
                )
            if cost > 0 and load == 0:
                raise ValueError(
                    f"segment {i} has cost {cost} but no passengers on board "
                    f"(empty-load charged segment)"
                )
        return self


class AdmitResponse(BaseModel):
    admitted: List[PassengerId] = Field(
        description="ids of the admitted candidates in canonical id order "
                    "(integer ids numerically, then string ids lexicographically); "
                    "empty when no candidate can be admitted"
    )
    totals: Dict[str, NonNegativeInt] = Field(
        description="cents charged to every passenger (existing plus admitted); "
                    "sums to total_cost",
    )
    segments: List[SegmentSplit] = Field(
        description="per-segment split detail of the resulting trip"
    )


class RevisionImpactRequest(BaseModel):
    """Two equal-length cost lists for one fixed roster (a fare revision)."""

    model_config = ConfigDict(extra="forbid")

    segments: List[NonNegativeInt] = Field(
        min_length=1,
        description="original integer cost of each segment in cents, in travel order",
    )
    revised_segments: List[NonNegativeInt] = Field(
        min_length=1,
        description="revised integer cost of each segment in cents; same length "
                    "and order as `segments`, only some values may differ",
    )
    passengers: List[Passenger] = Field(
        default_factory=list,
        description="the same passenger roster for both versions of the trip",
    )

    @model_validator(mode="after")
    def _check_revision(self) -> "RevisionImpactRequest":
        n = len(self.segments)
        if len(self.revised_segments) != n:
            raise ValueError(
                f"segments and revised_segments must have equal length "
                f"({n} vs {len(self.revised_segments)})"
            )
        _validate_roster(n, self.passengers)
        # Both versions must be splittable: neither may charge a fare on a
        # segment with no passengers on board.
        _validate_charged_segments(self.segments, self.passengers)
        _validate_charged_segments(self.revised_segments, self.passengers)
        changed = sum(a != b for a, b in zip(self.segments, self.revised_segments))
        if changed > 8:
            raise ValueError(
                f"{changed} segment(s) have a revised cost, but at most 8 "
                f"changed segments are supported (the impact analysis "
                f"enumerates all 2^n change subsets)"
            )
        return self


class SegmentContribution(BaseModel):
    """One changed segment's average marginal effect on every passenger."""

    index: int = Field(description="index of the changed segment, 0-based")
    original_cost: NonNegativeInt = Field(description="cost in cents before the revision")
    revised_cost: NonNegativeInt = Field(description="cost in cents after the revision")
    contributions: Dict[str, str] = Field(
        description="exact average marginal effect on each passenger, as a "
                    "reduced fraction string of cents (e.g. \"-1/2\", \"3\"); "
                    "negative values keep their sign. Per passenger the "
                    "contributions over all changed segments sum exactly to "
                    "that passenger's delta"
    )


class RevisionImpactResponse(BaseModel):
    before: Dict[str, NonNegativeInt] = Field(
        description="cents each passenger pays under the original costs (same as /split totals)"
    )
    after: Dict[str, NonNegativeInt] = Field(
        description="cents each passenger pays under the revised costs (same as /split totals)"
    )
    deltas: Dict[str, int] = Field(
        description="after - before per passenger in cents; negative means the passenger pays less"
    )
    changes: List[SegmentContribution] = Field(
        description="one entry per segment whose cost changed, ordered by index; "
                    "empty when nothing changed"
    )


class UnknownSegmentBounds(BaseModel):
    """Integer-cents range for one undetermined (null) segment cost."""

    model_config = ConfigDict(extra="forbid")

    index: NonNegativeInt = Field(
        description="index of a segment whose cost is null in `segments`"
    )
    min_cost: NonNegativeInt = Field(
        le=MAX_CENTS,
        description="inclusive integer lower bound for the segment cost, in cents",
    )
    max_cost: NonNegativeInt = Field(
        le=MAX_CENTS,
        description="inclusive integer upper bound for the segment cost, in cents",
    )


class FareEnvelopeRequest(BaseModel):
    """A trip where exactly two segment costs are free within integer ranges."""

    model_config = ConfigDict(extra="forbid")

    segments: List[Optional[NonNegativeInt]] = Field(
        min_length=2,
        max_length=MAX_ENVELOPE_SEGMENTS,
        description="per-segment costs in cents, in travel order; exactly two "
                    "entries must be null (the undetermined segments)",
    )
    passengers: List[Passenger] = Field(
        default_factory=list,
        description="the same board/alight roster structure as /split",
    )
    bounds: List[UnknownSegmentBounds] = Field(
        description="exactly one inclusive [min_cost, max_cost] integer range "
                    "for each null segment",
    )
    total_cost: NonNegativeInt = Field(
        le=MAX_CENTS,
        description="exact whole-trip total in cents that the resolved segment "
                    "costs must sum to",
    )
    target: PassengerId = Field(
        description="id of the passenger whose owed-fare envelope is reported",
    )

    @model_validator(mode="after")
    def _check_envelope(self) -> "FareEnvelopeRequest":
        n = len(self.segments)
        free = [i for i, cost in enumerate(self.segments) if cost is None]
        if len(free) != 2:
            raise ValueError(
                f"segments must contain exactly two null costs, found {len(free)}"
            )
        for cost in self.segments:
            if cost is not None and cost > MAX_CENTS:
                raise ValueError(
                    f"segment cost {cost} exceeds the {MAX_CENTS}-cents limit"
                )
        if len(self.passengers) > MAX_PASSENGERS:
            raise ValueError(
                f"at most {MAX_PASSENGERS} passengers are supported, "
                f"found {len(self.passengers)}"
            )
        _validate_roster(n, self.passengers)

        free_set = set(free)
        if len(self.bounds) != 2:
            raise ValueError(
                f"bounds must contain exactly two entries (one per null "
                f"segment), found {len(self.bounds)}"
            )
        seen = set()
        for bound in self.bounds:
            if bound.index not in free_set:
                raise ValueError(
                    f"bounds entry references segment {bound.index}, which is "
                    f"not a null-cost segment (null segments: "
                    f"{sorted(free_set)})"
                )
            if bound.index in seen:
                raise ValueError(
                    f"duplicate bounds entry for segment {bound.index}"
                )
            seen.add(bound.index)
            if bound.min_cost > bound.max_cost:
                raise ValueError(
                    f"segment {bound.index} bounds are inverted: "
                    f"min_cost {bound.min_cost} > max_cost {bound.max_cost}"
                )
        if not any(str(p.id) == str(self.target) for p in self.passengers):
            raise ValueError(
                f"unknown target passenger id: {self.target!r} "
                f"(no such passenger in the trip)"
            )
        return self


class FareExtreme(BaseModel):
    """One envelope extreme and the full cost vector witnessing it."""

    amount: NonNegativeInt = Field(
        description="cents owed by the target passenger at this extreme"
    )
    segments: List[NonNegativeInt] = Field(
        description="complete resolved per-segment cost vector (no nulls) "
                    "witnessing this amount; sums to total_cost"
    )


class FareEnvelopeResponse(BaseModel):
    target: PassengerId = Field(description="the target passenger id (as requested)")
    total_cost: NonNegativeInt = Field(description="the exact whole-trip total in cents")
    minimum: FareExtreme = Field(
        description="exact minimum amount the target can owe, with a witness"
    )
    maximum: FareExtreme = Field(
        description="exact maximum amount the target can owe, with a witness"
    )
