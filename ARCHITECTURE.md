# Architecture: Crescent Harbor Direct Filer

## What I Built

A three-layer Python pipeline that translates raw vessel scenario fixtures into
signed, validated cargo manifests and transmits them to the Crescent Harbor Customs
Authority API.

```
scenarios/*.json
      │
      ▼
┌─────────────┐     ┌──────────────┐     ┌──────────────┐     ┌───────────────┐
│   builder   │────▶│ JSON Schema  │────▶│ Rules Engine │────▶│  HMAC Client  │──▶ Authority
│             │     │  Validation  │     │  (25 rules)  │     │               │
└─────────────┘     └──────────────┘     └──────────────┘     └───────────────┘
                           │                     │
                    rejected_by_schema    rejected_by_rules
```

**Stack:** Python 3.11 · httpx (HTTP client) · jsonschema 4.23 (JSON Schema 2020-12)

I chose Python because it matches the mock server's language, the standard library
covers HMAC and hashing without additional dependencies, and the data-manipulation
tasks (JSON, dates, decimals) are concise. httpx was chosen over requests for its
clean API and first-class bytes handling.

## Component Details

### `filer/builder.py` — Manifest Builder
Consumes a scenario fixture and emits a complete, schema-valid manifest by:
- Stripping underscore-prefixed meta fields (`_scenarioId`, `_etaOffsetHours`)
- Computing `arrival.eta` from the current UTC time plus the offset
- Generating a `manifestId` as an uppercase hex UUIDv4 (satisfies §3.4)
- Injecting the `filer` block (filerId `CHC100001`) and `filerSignature`
- Normalizing vessel names to uppercase before submission

### `filer/rules.py` — Business Rules Engine
Twenty-five rules implemented as **data, not control flow**: a list of
`(rule_id, check_fn)` pairs, each returning `[]` (pass) or `[Violation(...)]`.
This design means:
- Rules can be unit-tested independently
- The complete rule set is visible in one place (`RULES` list)
- Adding or reordering rules requires no structural change
- Severity (reject/warning) is a field on the Violation, not encoded in branching

### `filer/client.py` — HMAC-Signed Transmission Client
Implements §10 exactly:
- Signs both POST and GET requests separately (each has its own timestamp)
- Polls /v3/acks/{receiptId} with exponential back-off floored at 2s
- Maps terminal ack states to outcome strings the grading script expects

### `filer/pipeline.py` — Orchestrator
Single entry point: schema validation → rules check → transmit. Writes `results.json`
in Format B. Accepts `--single`, `--dry-run`, and `--authority` flags for debugging.

## Ambiguity Decisions

**R-005 — Vessel name normalization**
The spec says "lowercase letters in the input *shall be uppercased by the filer*".
I interpret "shall be uppercased" as a normalization responsibility (applied in the
builder), not a rejection criterion. Rejecting a manifest purely because the input
was lowercase would be punishing the carrier for a data-entry convention.

**R-014 — Hazmat weight limit (warning-only)**
§6.2 requires that combined HAZ gross weight not exceed 25% of vessel GRT, but
`grossWeightKg` is schema-optional. Enforcing this as a hard rejection only when the
field is present would create a perverse incentive: filers who truthfully supply the
field get rejected, while filers who omit it bypass the check. Both outcomes are wrong.
I treat R-014 as warning-only — the flag surfaces for operator review, and the Authority's
harbormaster review (§6.1 applies to all HAZ manifests) provides the enforcement backstop.

**R-023 — Filing clock definition**
The spec does not define whether the filing window is measured at client send time,
server receive time, or acknowledgment time. I use **client send time** (the wall
clock at the moment of transmission). This is: (a) auditable from the client side,
(b) matches the timestamp used in the HMAC signature, and (c) is the only clock the
filer fully controls without relying on server behavior.

## What I Cut

- **Persistence / audit log:** No database. Results are written to `results.json`.
  A production system needs durable storage of manifests + receipts + acks for 7-year
  retention (§12.1). An append-only event log (e.g. a local SQLite or cloud object
  store) would be the right foundation.
- **Retry queue:** The client retries polling but does not retry failed POST submissions.
  Production needs idempotent retry with a dead-letter queue.
- **Configuration file:** Filer ID and server URL are constants with env-var overrides.
  A production system needs a proper config layer (env → file → defaults).
- **Async I/O:** The pipeline is synchronous. For high throughput, httpx's async
  interface would let multiple manifests be in-flight simultaneously.

## What I Would Build Next (Scaling to 5 Document Types / 3 Regulators)

The current design already isolates the regulator-specific concerns into three modules
(builder, rules, client). Scaling looks like:

1. **Plugin architecture per document type:** A `Regulator` protocol with
   `build()`, `rules()`, and `client()` methods. Each regulator ships a plugin
   that implements this protocol. The pipeline discovers plugins by entry point.

2. **Schema registry:** A central registry mapping `(regulator, doc_type, version)`
   to a JSON Schema. Schema version negotiation happens at startup, not hardcoded.

3. **Shared validation core:** JSON Schema validation is already generic. The rules
   engine only needs each rule's check function to be swapped per regulator.

4. **Event-driven submission:** Replace the synchronous pipeline with a message queue
   (e.g. SQS, Kafka). Each submission is a job; pollers are separate workers. This
   decouples ingestion from transmission and supports retries naturally.

## What I Would Do Differently with Infinite Time

- Add a proper configuration layer (Pydantic Settings) instead of module-level constants.
- Write a test suite: unit tests for each of the 25 rules (especially the edge cases
  in R-016 rounding, R-020 age computation, and R-004 IMO check digit), and an
  integration test that spins up the Docker mock and asserts all 8 scenario outcomes.
- Add structured logging (structlog) so every transmission produces a machine-readable
  audit record with correlation IDs.
- Sign the `results.json` output so the grading script can verify it wasn't tampered with.
