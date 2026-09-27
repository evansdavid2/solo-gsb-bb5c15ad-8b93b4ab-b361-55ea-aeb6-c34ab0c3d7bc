"""FastAPI application exposing the carpool fare-splitting API."""

from __future__ import annotations

from fastapi import FastAPI

from .models import (
    AdmitRequest,
    AdmitResponse,
    FareEnvelopeRequest,
    FareEnvelopeResponse,
    FareEnvelopeWitness,
    RevisionImpactRequest,
    RevisionImpactResponse,
    SegmentContribution,
    SegmentSplit,
    SettleRequest,
    SettleResponse,
    SplitRequest,
    SplitResponse,
    Transfer,
)
from .splitter import (
    admit_passengers,
    fare_envelope as compute_fare_envelope,
    revision_impact as compute_revision_impact,
    settle_balances,
    split_fares,
)

app = FastAPI(
    title="Carpool Fare Splitter",
    version="1.1.0",
    summary="Split a carpool trip's per-segment costs fairly between passengers.",
)


@app.get("/", include_in_schema=False)
def root():
    return {"service": "carpool-fare-splitter", "docs": "/docs", "health": "/healthz"}


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.post(
    "/split",
    response_model=SplitResponse,
    responses={422: {"description": "Invalid trip (bad interval, out-of-range "
                                     "station, duplicate id, empty-load charged "
                                     "segment, malformed payload, ...)"}},
)
def split(request: SplitRequest) -> SplitResponse:
    totals, details = split_fares(request.segments, request.passengers)
    return SplitResponse(
        total_cost=sum(request.segments),
        totals={str(pid): amount for pid, amount in totals.items()},
        segments=[
            SegmentSplit(
                index=d["index"],
                cost=d["cost"],
                passengers=d["passengers"],
                shares={str(pid): amount for pid, amount in d["shares"].items()},
            )
            for d in details
        ],
    )


@app.post(
    "/settle",
    response_model=SettleResponse,
    responses={422: {"description": "Invalid trip (bad interval, out-of-range "
                                     "station, duplicate id, empty-load charged "
                                     "segment, malformed payload), or invalid "
                                     "payments (missing/duplicate/unknown id, "
                                     "negative or float amount, totals that do "
                                     "not match the trip cost, ...)"}},
)
def settle(request: SettleRequest) -> SettleResponse:
    owed, _ = split_fares(request.segments, request.passengers)
    paid_by_str = {str(p.id): p.amount for p in request.payments}
    balances = {
        pid: paid_by_str[str(pid)] - amount for pid, amount in owed.items()
    }
    transfers = settle_balances(balances)
    return SettleResponse(
        owed={str(pid): amount for pid, amount in owed.items()},
        paid={str(pid): paid_by_str[str(pid)] for pid in owed},
        balance={str(pid): balances[pid] for pid in owed},
        transfers=[
            Transfer(**{"from": frm, "to": to, "amount": amount})
            for frm, to, amount in transfers
        ],
    )


@app.post(
    "/admit",
    response_model=AdmitResponse,
    responses={422: {"description": "Invalid trip or request (bad interval, "
                                     "out-of-range station, duplicate id, "
                                     "empty-load charged segment, existing load "
                                     "already over capacity, more than 12 "
                                     "candidates, malformed payload, ...)"}},
)
def admit(request: AdmitRequest) -> AdmitResponse:
    admitted_ids, totals, details = admit_passengers(
        request.segments, request.passengers, request.candidates, request.capacity
    )
    return AdmitResponse(
        admitted=admitted_ids,
        totals={str(pid): amount for pid, amount in totals.items()},
        segments=[
            SegmentSplit(
                index=d["index"],
                cost=d["cost"],
                passengers=d["passengers"],
                shares={str(pid): amount for pid, amount in d["shares"].items()},
            )
            for d in details
        ],
    )


@app.post(
    "/fare-envelope",
    response_model=FareEnvelopeResponse,
    responses={422: {"description": "Invalid trip or request (bad interval, "
                                     "out-of-range station, duplicate id, "
                                     "empty-load charged segment, not exactly "
                                     "two undetermined segments, undetermined "
                                     "segment with nobody on board, malformed "
                                     "bounds, unknown target, or no feasible "
                                     "combination reaching the total, ...)"}},
)
def fare_envelope(request: FareEnvelopeRequest) -> FareEnvelopeResponse:
    min_amount, min_costs, max_amount, max_costs = compute_fare_envelope(
        request.segments,
        [(bound.min, bound.max) for bound in request.bounds],
        request.passengers,
        request.total_cost,
        request.target,
    )
    return FareEnvelopeResponse(
        target=request.target,
        total_cost=request.total_cost,
        minimum=FareEnvelopeWitness(amount=min_amount, segments=min_costs),
        maximum=FareEnvelopeWitness(amount=max_amount, segments=max_costs),
    )


@app.post(
    "/revision-impact",
    response_model=RevisionImpactResponse,
    responses={422: {"description": "Invalid trip or revision (bad interval, "
                                     "out-of-range station, duplicate id, "
                                     "empty-load charged segment in either "
                                     "cost list, unequal-length segment lists, "
                                     "more than 8 changed segments, malformed "
                                     "payload, ...)"}},
)
def revision_impact(request: RevisionImpactRequest) -> RevisionImpactResponse:
    before, after, changes = compute_revision_impact(
        request.segments, request.revised_segments, request.passengers
    )
    return RevisionImpactResponse(
        before={str(pid): amount for pid, amount in before.items()},
        after={str(pid): amount for pid, amount in after.items()},
        deltas={str(pid): after[pid] - before[pid] for pid in before},
        changes=[
            SegmentContribution(
                index=c["index"],
                original_cost=c["original_cost"],
                revised_cost=c["revised_cost"],
                contributions={
                    str(pid): str(value) for pid, value in c["contributions"].items()
                },
            )
            for c in changes
        ],
    )
