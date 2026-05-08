# Trip Booking Concierge

An autonomous AI agent that takes a natural-language trip request ("4 days in Lisbon next month under $2,000"), researches flights, hotels, weather, and reviews, and presents three priced itinerary options that the user can approve with a hard two-step gate before any payment is executed.

> **This is a portfolio prototype. It does not take real money or create real airline/hotel reservations.** M5 wires Stripe **test mode only** to demonstrate the payment path. Hotel/flight bookings stay mocked.

---

## Why this exists

The single load-bearing element of the design is the **two-step approval gate**. A wrong booking — wrong date, wrong passenger, non-refundable error — costs the user real money and destroys trust permanently. This repo treats that contract as the substrate everything else is built on:

1. **The LLM is a synthesizer, never a source of fact.** Every concrete fact in user-facing output traces to a recorded `ToolCall`. The orchestrator strips narrative claims that lack provenance.
2. **No payment without a verified `ApprovalToken`.** Ed25519-signed, single-use, bound to a specific itinerary, replay-protected, TTL-bounded. Enforced in middleware, handler, AND a CI fitness test.
3. **Append-only, hash-chained audit log.** Tampering breaks the chain. Any disputed booking is reconstructible from events alone.

---

## Quick start (offline demo, no API keys)

```bash
# 1. Install
make install

# 2. Generate signing keys (writes to .env)
cp .env.example .env
make keys

# 3. Run the M1 happy-path demo (uses MOCK_LLM=1, no Anthropic key needed)
make demo

# 4. Run the attack demo — every bypass attempt is refused with audit entries
make attack-demo

# 5. Run the test suite
make test
```

The demo plans a Lisbon trip on mocked providers, presents three options, accepts a selection, runs revalidation, mints an approval token after a 2FA code, and "books" via the `mock_always` payment provider. The audit log is hash-chained; tamper a row and `audit/verify.py` flags the broken link.

---

## Architecture

```
┌──────────┐   trip request    ┌──────────────────┐
│  CLI/Web │──────────────────►│ Agent Orchestr.  │
└──────────┘                   │  state machine   │
                               │  tool-use loop   │
                               └────────┬─────────┘
                                        │ tool calls
                                        ▼
                  ┌──────────┬──────────┬──────────┬──────────┐
                  │ flights  │ hotels   │ reviews  │ weather  │
                  │ (mock/   │ (mock/   │ (gstack  │ (Open-   │
                  │ Amadeus) │ Amadeus) │ scraper) │ Weather) │
                  └──────────┴──────────┴──────────┴──────────┘
                                        │
                                        ▼
                               ┌──────────────────┐
                               │ 3 ItineraryOpts  │
                               │ (provenance-     │
                               │  grounded)       │
                               └────────┬─────────┘
                                        │ user selects
                                        ▼
                               ┌──────────────────┐
                               │ Approval Gate    │
                               │  ─ revalidate    │
                               │  ─ 2FA           │
                               │  ─ Ed25519 sign  │
                               └────────┬─────────┘
                                        │ ApprovalToken
                                        ▼
                               ┌──────────────────┐
                               │ Two-Phase Commit │
                               │ Tier A/B/C       │
                               │ + Compensator    │
                               └────────┬─────────┘
                                        │
                                        ▼
                               ┌──────────────────┐
                               │ Append-only      │ ◄── audit/verify.py
                               │ hash-chained     │     walks the chain
                               │ audit log        │
                               └──────────────────┘
```

---

## Directory layout

| Path | What's there |
|---|---|
| `api/` | FastAPI app, schemas, config, auth |
| `agent/` | Orchestrator, state machine, prompts, tool wrappers |
| `tools/` | Provider adapters: flights, hotels, reviews, weather (mock + real) |
| `reviews/` | Semantic review reading + prompt-injection guard |
| `booking/` | Two-phase commit, compensator, payment providers |
| `audit/` | Hash-chained event log + verifier |
| `approval/` | Ed25519 token signing, two-step gate, drift revalidation |
| `cli/` | `trip` CLI (typer) |
| `web/` | Next.js 15 frontend (M4) |
| `tests/` | pytest unit + fitness tests + eval harness |
| `scripts/` | `demo.sh`, `attack_demo.sh` |

---

## Milestones

| | Status | Description |
|---|---|---|
| **M1** | done in this repo | Approval gate, 2PC, audit log, agent loop, CLI demo, attack tests. Mocks only. |
| **M2** | scaffolded | Real Amadeus + OpenWeather + gstack review reading. |
| **M3** | scaffolded | Flaky payment provider, full compensation cascade. |
| **M4** | scaffolded | Next.js UI, WebAuthn, SSE activity stream. |
| **M5** | scaffolded | Stripe test mode, anomaly detection, OOB SMS. |

See `C:\Users\sohan\.claude\plans\trip-booking-concierge-cryptic-bonbon.md` for the full plan.

---

## Safety properties enforced by code

- **Hash chain** (`tests/test_audit_chain.py`): tampering breaks `audit/verify.py`.
- **Token replay** (`tests/test_approval_gate.py`): single-use `jti`; reused tokens 403.
- **TTL** (`tests/test_approval_gate.py`): 90-second token lifetime.
- **Itinerary binding** (`tests/test_approval_gate.py`): a token signed for option A cannot pay option B.
- **Provenance** (`tests/test_provenance.py`): every fact in every option points to a `ToolCall`.
- **Fitness gate** (`tests/fitness/no_untokened_booking.py`): AST scan rejects any booking handler that doesn't start with `verify_approval_token(...)`.

---

## License

MIT — but read the disclaimer above. **Do not run this against real money.**
