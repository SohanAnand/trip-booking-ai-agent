"""FastAPI surface for the agent.

Endpoints:
  POST  /v1/trips                          → run agent, return 3 options
  POST  /v1/trips/{rid}/select             → step 1 of approval gate
  POST  /v1/trips/{rid}/approve            → step 2 of approval gate + booking
  GET   /v1/trips/{rid}/events             → audit events for a request
  GET   /v1/audit/verify                   → walk the hash chain
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agent.orchestrator import run_agent
from api.config import settings
from api.schemas import ItineraryOption
from approval.gate import ApprovalGate, AuthorizationFailed
from audit.log import get_default_log
from audit.verify import walk_chain
from booking.providers.mock_always import get_provider as get_mock_payment
from booking.two_phase import BookingLeg, execute_booking

app = FastAPI(title="Trip Booking Concierge", version="0.1.0")

# Open CORS for the Next.js dev server. In production, restrict to the actual origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_gate: ApprovalGate | None = None
_request_options: dict[str, list[ItineraryOption]] = {}

# Per-request streaming queues. The orchestrator writes events; the SSE endpoint reads.
_stream_queues: dict[str, asyncio.Queue] = {}


def _get_queue(request_id: str) -> asyncio.Queue:
    q = _stream_queues.get(request_id)
    if q is None:
        q = asyncio.Queue(maxsize=200)
        _stream_queues[request_id] = q
    return q


def _get_gate() -> ApprovalGate:
    global _gate
    if _gate is None:
        _gate = ApprovalGate(get_default_log())
    return _gate


def _options_for(request_id: str) -> list[ItineraryOption] | None:
    """Process-local cache lookup with audit-log fallback. After a server
    restart `_request_options` is empty, but the option snapshots persist in
    the audit log — re-hydrate them on first access so /select and /approve
    don't 404 just because uvicorn restarted between create_trip and approve."""
    cached = _request_options.get(request_id)
    if cached:
        return cached
    log = get_default_log()
    snapshots = log.get_options_for_request(request_id)
    if not snapshots:
        return None
    rehydrated: list[ItineraryOption] = []
    for s in snapshots:
        try:
            rehydrated.append(ItineraryOption(**s["snapshot"]))
        except Exception:
            # Skip a corrupt snapshot rather than failing the whole rehydrate.
            continue
    if not rehydrated:
        return None
    _request_options[request_id] = rehydrated
    return rehydrated


# ----- Request bodies --------------------------------------------------------

class TripCreate(BaseModel):
    raw_text: str
    user_id: str | None = None


class TripSelect(BaseModel):
    option_id: str


class TripApprove(BaseModel):
    option_id: str
    code: str   # OTP from the select step


# ----- Endpoints -------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "mock_llm": settings.mock_llm}


@app.post("/v1/trips")
async def create_trip(body: TripCreate) -> dict:
    user_id = body.user_id or settings.demo_user_id
    log = get_default_log()
    request_id_holder: dict[str, str] = {}

    def progress(stage: str, payload: dict) -> None:
        rid = request_id_holder.get("rid")
        if rid is None:
            return
        q = _stream_queues.get(rid)
        if q is None:
            return
        try:
            q.put_nowait(json.dumps({"stage": stage, "payload": payload}))
        except asyncio.QueueFull:
            pass    # drop events under back-pressure rather than block the agent

    # We don't know request_id until run_agent starts. Stream events use a callback;
    # the agent calls progress(stage, payload) for each milestone. Buffer until the
    # client subscribes (the queue is created on first /stream request).
    try:
        session, options = await run_agent(
            raw_text=body.raw_text, user_id=user_id, log=log, progress=progress,
        )
    except ValueError as e:
        # Intent parser couldn't identify a destination (or similar). Surface as
        # 400 so the UI can render the message instead of a 500 stack trace.
        raise HTTPException(status_code=400, detail=str(e)) from e
    request_id_holder["rid"] = session.request_id
    # If a /stream subscriber created a queue while we were running, send a final
    # complete event.
    if session.request_id in _stream_queues:
        await _stream_queues[session.request_id].put(json.dumps({
            "stage": "complete", "payload": {"option_count": len(options)},
        }))
    if not options:
        raise HTTPException(status_code=422, detail="no options found")
    _request_options[session.request_id] = options
    return {
        "request_id": session.request_id,
        "options": [o.model_dump(mode="json") for o in options],
    }


@app.get("/v1/trips/{request_id}/stream")
async def stream_trip_events(request_id: str):
    """Server-Sent Events stream of agent activity for a request."""
    q = _get_queue(request_id)

    async def event_gen() -> AsyncIterator[dict]:
        # Start with whatever the audit log already has so late subscribers see history.
        log = get_default_log()
        for ev in log.events_for_request(request_id):
            yield {
                "event": "history",
                "data": json.dumps({"seq": ev.seq, "type": ev.type,
                                    "actor": ev.actor, "payload": ev.payload}),
            }
        try:
            while True:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
                yield {"event": "progress", "data": msg}
        except asyncio.TimeoutError:
            yield {"event": "ping", "data": "{}"}

    return EventSourceResponse(event_gen())


@app.get("/v1/trips/{request_id}")
def get_trip(request_id: str) -> dict:
    options = _options_for(request_id)
    if not options:
        raise HTTPException(status_code=404, detail="unknown request_id")
    return {
        "request_id": request_id,
        "options": [o.model_dump(mode="json") for o in options],
    }


@app.post("/v1/trips/{request_id}/select")
async def select_option(request_id: str, body: TripSelect) -> dict:
    options = _options_for(request_id)
    if not options:
        raise HTTPException(status_code=404, detail="unknown request_id")
    selected = next((o for o in options if o.id == body.option_id), None)
    if not selected:
        raise HTTPException(status_code=404, detail="unknown option_id")
    summary, otp, drift = _get_gate().select(
        request_id=request_id,
        user_id=settings.demo_user_id,
        option=selected,
    )
    # In production the OTP is sent via SMS, never returned in the response.
    # For the M1 demo, the response includes it under `dev_otp` so the CLI/UI
    # can complete the flow without external SMS infrastructure.
    return {
        "summary": summary.model_dump(mode="json"),
        "drift": {"has_drift": drift.has_drift, "diffs": drift.diffs,
                  "requires_replan": drift.requires_replan},
        "dev_otp": otp,
    }


@app.post("/v1/trips/{request_id}/approve")
async def approve_and_book(request_id: str, body: TripApprove) -> JSONResponse:
    options = _options_for(request_id)
    if not options:
        raise HTTPException(status_code=404, detail="unknown request_id")
    selected = next((o for o in options if o.id == body.option_id), None)
    if not selected:
        raise HTTPException(status_code=404, detail="unknown option_id")
    log = get_default_log()
    try:
        token = _get_gate().authorize(option_id=body.option_id, otp=body.code)
    except AuthorizationFailed as e:
        raise HTTPException(status_code=401, detail=str(e))

    snap = log.get_option_snapshot(selected.id)
    currency = selected.currency.value
    booking_legs: list[BookingLeg] = []
    for leg_opt in selected.legs:
        booking_legs.append(BookingLeg(
            leg_id=f"flight-{leg_opt.leg_index}",
            label=f"flight to {leg_opt.destination}",
            amount_cents=leg_opt.flight.value.total_price_cents,
            currency=currency,
            provider=get_mock_payment(),
        ))
        booking_legs.append(BookingLeg(
            leg_id=f"hotel-{leg_opt.leg_index}",
            label=f"{leg_opt.hotel.value.name} ({leg_opt.destination})",
            amount_cents=leg_opt.hotel.value.total_price_cents,
            currency=currency,
            provider=get_mock_payment(),
        ))
    if selected.return_flight is not None:
        booking_legs.append(BookingLeg(
            leg_id="flight-return",
            label="return flight",
            amount_cents=selected.return_flight.value.total_price_cents,
            currency=currency,
            provider=get_mock_payment(),
        ))

    result = await execute_booking(
        token=token, legs=booking_legs, log=log,
        request_id=request_id, option_id=selected.id,
        option_hash=snap["snapshot_hash"],
    )

    # Post-commit: roll the booking into the user's persistent profile so
    # the next session can apply preferences (refundable share, avg star
    # rating, recent neighborhoods).
    if getattr(result, "state", None) == "COMMITTED":
        try:
            from memory.profile import update_profile_after_booking
            stars = [L.hotel.value.star_rating for L in selected.legs]
            avg_star = sum(stars) / len(stars) if stars else 0.0
            any_refundable = any(
                any(seg.refundable for seg in L.flight.value.outbound)
                for L in selected.legs
            )
            neighborhoods = [L.hotel.value.neighborhood for L in selected.legs]
            destinations = [L.destination for L in selected.legs]
            update_profile_after_booking(
                user_id=settings.demo_user_id,
                refundable=any_refundable,
                avg_star=avg_star,
                neighborhoods=neighborhoods,
                destinations=destinations,
            )
        except Exception as e:
            log.append("memory", "profile.update_failed", {
                "error": f"{type(e).__name__}: {str(e)[:200]}",
            }, request_id=request_id)

    # Map booking state to a meaningful HTTP status. Returning 200 for
    # FAILED/COMPENSATED was misleading — naive clients couldn't tell apart
    # success from a token replay or an unwound booking.
    response_body = result.model_dump(mode="json")
    state = getattr(result, "state", "FAILED")
    if state == "COMMITTED":
        status = 200
    elif state == "COMPENSATED":
        status = 422       # Unprocessable: the booking unwound cleanly.
    else:
        # FAILED — distinguish token-replay (idempotency conflict) from other.
        err = (getattr(result, "error", "") or "").lower()
        status = 409 if "replay" in err or "idempot" in err else 500
    return JSONResponse(content=response_body, status_code=status)


@app.get("/v1/trips/{request_id}/events")
def request_events(request_id: str) -> dict:
    log = get_default_log()
    return {
        "events": [
            {
                "seq": e.seq, "type": e.type, "actor": e.actor,
                "payload": e.payload, "event_hash": e.event_hash,
                "prev_hash": e.prev_hash, "created_at": e.created_at,
            }
            for e in log.events_for_request(request_id)
        ]
    }


@app.get("/v1/audit/verify")
def verify_chain() -> dict:
    log = get_default_log()
    res = walk_chain(log)
    return {
        "ok": res.ok, "events_checked": res.events_checked,
        "broken_at_seq": res.broken_at_seq, "reason": res.reason,
    }
