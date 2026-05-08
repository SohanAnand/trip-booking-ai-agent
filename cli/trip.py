"""`trip` CLI - the M1 demo surface.

Usage:
  trip plan "4 days in Lisbon next month under $2000"
  trip select <option_id>
  trip authorize <option_id> <code>
  trip verify-chain
  trip events [--request-id REQ_ID] [--booking-id BKG_ID]
  trip cancel <booking_id>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows so Rich's box-drawing chars render.
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from api.config import settings
from api.schemas import ItineraryOption
from agent.orchestrator import run_agent
from approval.gate import ApprovalGate, AuthorizationFailed
from approval.tokens import ApprovalToken, ApprovalTokenPayload
from audit.log import AuditLog, get_default_log
from audit.verify import walk_chain
from booking.providers.mock_always import get_provider as get_mock_payment
from booking.two_phase import BookingLeg, execute_booking

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

STATE_FILE = Path(".trip-cli-state.json")


def _save_state(d: dict) -> None:
    STATE_FILE.write_text(json.dumps(d, indent=2, default=str))


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _print_options(options: list[ItineraryOption]) -> None:
    for opt in options:
        flight = opt.flight.value
        hotel = opt.hotel.value
        outbound = flight.outbound[0]
        inbound = flight.inbound[0]
        title = (
            f"[bold]Option {opt.rank}[/bold] · "
            f"[cyan]{opt.tradeoff_label.upper().replace('_', ' ')}[/cyan]   "
            f"[green]${opt.total_price_cents.value/100:,.2f} {opt.currency.value}[/green]"
        )
        body = []
        body.append(f"Why: {opt.why_this_one}")
        body.append("")
        body.append(
            f"Flights · {outbound.carrier}{outbound.flight_number} {outbound.origin}→{outbound.destination} {outbound.depart[:10]} | "
            f"return {inbound.carrier}{inbound.flight_number} {inbound.depart[:10]} | "
            f"{'refundable' if outbound.refundable else 'non-refundable'} · "
            f"baggage {'incl' if flight.baggage_included else 'excl'}"
        )
        body.append(
            f"Hotel · {hotel.name} ({hotel.neighborhood}) {hotel.star_rating}★ · "
            f"{hotel.nights}n @ ${hotel.nightly_rate_cents/100:.0f}/night · "
            f"{'free cancel until ' + hotel.refundable_until if hotel.refundable_until else 'non-refundable'}"
        )
        if opt.weather:
            w = opt.weather.value
            body.append(
                f"Weather · {w.summary} · {w.avg_high_c:.0f}°C/{w.avg_low_c:.0f}°C · "
                f"{w.rain_probability*100:.0f}% rain"
            )
        body.append("")
        body.append("The catch:")
        for c in opt.the_catch:
            body.append(f"  · {c}")
        body.append("")
        body.append(f"[dim]option_id = {opt.id}[/dim]")
        console.print(Panel("\n".join(body), title=title, border_style="blue"))


@app.command("plan")
def plan_cmd(
    request: str = typer.Argument(..., help="Natural-language trip request"),
    user: str = typer.Option(None, help="User id; defaults to settings.demo_user_id"),
):
    """Plan a trip — runs the agent loop and presents 3 options."""
    user_id = user or settings.demo_user_id

    def progress(stage: str, payload: dict):
        console.print(f"[dim]· {stage}: {payload}[/dim]")

    log = get_default_log()
    session, options = asyncio.run(run_agent(
        raw_text=request, user_id=user_id, log=log, progress=progress,
    ))
    if not options:
        console.print("[red]No options found. Try relaxing budget or dates.[/red]")
        raise typer.Exit(2)

    _print_options(options)
    _save_state({
        "request_id": session.request_id,
        "user_id": user_id,
        "options": [o.model_dump(mode="json") for o in options],
    })
    console.print(
        f"\n[green]✓ Presented 3 options.[/green] "
        f"Next: [bold]trip select <option_id>[/bold]"
    )


@app.command("select")
def select_cmd(
    option_id: str = typer.Argument(..., help="ID of the option to select"),
):
    """Step 1 of approval: select an option, get OTP + final summary."""
    state = _load_state()
    if not state:
        console.print("[red]No active trip. Run `trip plan ...` first.[/red]")
        raise typer.Exit(2)

    options = [ItineraryOption(**o) for o in state["options"]]
    selected = next((o for o in options if o.id == option_id), None)
    if not selected:
        console.print(f"[red]Unknown option_id {option_id}.[/red]")
        raise typer.Exit(2)

    log = get_default_log()
    gate = ApprovalGate(log)
    summary, otp, drift = gate.select(
        request_id=state["request_id"], user_id=state["user_id"], option=selected,
    )

    console.print(Panel(
        f"[bold]Final summary[/bold]\n\n"
        f"{summary.consent_text}\n\n"
        f"Total: [green]{summary.total_price_display}[/green]\n"
        f"Cancellation: {summary.cancellation_policy}\n"
        f"Payment method: {summary.payment_method_id}\n\n"
        + ("[yellow]⚠ Drift detected: " + "; ".join(summary.drift_diffs) + "[/yellow]\n"
           if summary.drift_detected else "[dim]No drift.[/dim]\n"),
        title="Approval (step 1 of 2)", border_style="yellow",
    ))

    state.update({
        "selected_option_id": option_id,
        "selected_option": selected.model_dump(mode="json"),
        "summary": summary.model_dump(mode="json"),
    })
    _save_state(state)

    # In production, OTP is sent to the user's device — we display it for the demo
    console.print(
        f"\n[bold magenta]🔐 Your one-time authorization code: {otp}[/bold magenta]"
    )
    console.print(
        f"\nNext: [bold]trip authorize {option_id} <code>[/bold]"
    )


@app.command("authorize")
def authorize_cmd(
    option_id: str = typer.Argument(...),
    code: str = typer.Argument(..., help="6-digit OTP from `trip select`"),
):
    """Step 2 of approval: authorize and execute booking."""
    state = _load_state()
    if not state or state.get("selected_option_id") != option_id:
        console.print("[red]No pending selection for that option_id. Run `trip select` first.[/red]")
        raise typer.Exit(2)

    log = get_default_log()
    gate = ApprovalGate(log)
    try:
        token = gate.authorize(option_id=option_id, otp=code)
    except AuthorizationFailed as e:
        console.print(f"[red]Authorization failed: {e}[/red]")
        raise typer.Exit(2)

    selected = ItineraryOption(**state["selected_option"])
    legs = _build_legs_from_option(selected)

    result = asyncio.run(execute_booking(
        token=token,
        legs=legs,
        log=log,
        request_id=state["request_id"],
        option_id=selected.id,
        option_hash=log.get_option_snapshot(selected.id)["snapshot_hash"],
    ))

    if result.state == "COMMITTED":
        console.print(Panel(
            f"[bold green]✓ Booking confirmed[/bold green]\n\n"
            f"booking_id: {result.booking_id}\n"
            f"Total charged: ${result.total_charged_cents/100:,.2f}\n"
            f"Confirmations:\n  " + "\n  ".join(
                f"{leg}: {conf}" for leg, conf in result.confirmations.items()
            ),
            border_style="green",
        ))
    else:
        console.print(Panel(
            f"[bold red]✗ Booking {result.state}[/bold red]\n\n"
            f"booking_id: {result.booking_id}\n"
            f"Error: {result.error}\n"
            f"Partial confirmations: {result.confirmations}",
            border_style="red",
        ))

    state["last_booking"] = result.model_dump(mode="json")
    _save_state(state)


@app.command("verify-chain")
def verify_chain_cmd():
    """Walk the audit log hash chain and report integrity."""
    log = get_default_log()
    res = walk_chain(log)
    if res.ok:
        console.print(f"[green]✓ Chain verified.[/green] {res.events_checked} events.")
    else:
        console.print(
            f"[red]✗ Chain BROKEN at seq {res.broken_at_seq}: {res.reason}[/red] "
            f"({res.events_checked} events checked before break)"
        )
        raise typer.Exit(1)


@app.command("events")
def events_cmd(
    request_id: str = typer.Option(None),
    booking_id: str = typer.Option(None),
):
    """Print events for a request or booking, in chain order."""
    log = get_default_log()
    if request_id:
        evs = log.events_for_request(request_id)
    elif booking_id:
        evs = log.events_for_booking(booking_id)
    else:
        evs = log.all_events()

    table = Table(show_header=True, header_style="bold")
    for col in ("seq", "actor", "type", "request_id", "booking_id", "event_hash"):
        table.add_column(col)
    for ev in evs:
        table.add_row(
            str(ev.seq), ev.actor, ev.type,
            (ev.request_id or "")[:14], (ev.booking_id or "")[:14],
            ev.event_hash[:14] + "...",
        )
    console.print(table)


@app.command("cancel")
def cancel_cmd(booking_id: str = typer.Argument(...)):
    """Cancel a booking within the 24h pass-through window.

    Looks up the booking, checks the 24h window, and walks each leg of the
    booking calling the provider's refund API (mock provider always succeeds).
    """
    import asyncio
    from datetime import UTC, datetime, timedelta

    from booking.providers.mock_always import get_provider as get_mock_payment
    log = get_default_log()
    booking = log.get_booking(booking_id)
    if not booking:
        console.print(f"[red]Unknown booking_id {booking_id}.[/red]")
        raise typer.Exit(2)

    created = datetime.fromisoformat(booking["created_at"])
    if datetime.now(UTC) - created > timedelta(hours=24):
        console.print("[red]Cancellation window (24h) has elapsed.[/red]")
        log.append("user", "cancellation.window_expired", {
            "booking_id": booking_id,
        }, booking_id=booking_id)
        raise typer.Exit(2)

    if booking["state"] != "COMMITTED":
        console.print(f"[yellow]Cannot cancel: state is {booking['state']}.[/yellow]")
        raise typer.Exit(2)

    # Walk every leg.captured event for this booking, refund each.
    captured = [e for e in log.events_for_booking(booking_id)
                if e.type == "leg.captured"]
    provider = get_mock_payment()
    refunds = {}
    for ev in captured:
        conf = ev.payload["confirmation"]
        amount = ev.payload["charged_cents"]

        async def _refund():
            return await provider.refund(confirmation=conf, amount_cents=amount)
        result = asyncio.run(_refund())
        refunds[ev.payload["leg"]] = result.refunded_cents
        log.append("user", "cancellation.leg_refunded", {
            "booking_id": booking_id, "leg": ev.payload["leg"],
            "confirmation": conf, "refunded_cents": result.refunded_cents,
        }, booking_id=booking_id)

    log.append("user", "cancellation.completed", {
        "booking_id": booking_id, "refunds": refunds,
    }, booking_id=booking_id)
    console.print(Panel(
        f"[green]Booking cancelled.[/green]\n"
        f"booking_id: {booking_id}\n"
        f"Refunds: {refunds}",
        border_style="green",
    ))


# ----- internals ------------------------------------------------------------

def _build_legs_from_option(opt: ItineraryOption) -> list[BookingLeg]:
    """For M1 we treat the entire option as 2 legs (flight + hotel) on one
    payment provider. M3 wires the flaky provider per-leg."""
    provider = get_mock_payment()
    return [
        BookingLeg(
            leg_id="flight", label=f"{opt.flight.value.outbound[0].carrier} flight bundle",
            amount_cents=opt.flight.value.total_price_cents, currency=opt.currency.value,
            provider=provider,
            metadata={"flight_id": opt.flight.value.id},
        ),
        BookingLeg(
            leg_id="hotel", label=opt.hotel.value.name,
            amount_cents=opt.hotel.value.total_price_cents, currency=opt.currency.value,
            provider=provider,
            metadata={"hotel_id": opt.hotel.value.id},
        ),
    ]


if __name__ == "__main__":
    app()
