# Threat Model: Crescent Harbor Direct Filer

## Sensitive Data Inventory

| Asset | Location at Rest | In Transit |
|---|---|---|
| HMAC shared secret | `mock-customs/secrets.json` (local); prod: env var / secrets manager | Never transmitted — used only to compute a signature |
| Manifest contents | Memory only during processing; `results.json` (no manifest body) | HTTPS to Authority (TLS in prod) |
| Crew PII (names, passport numbers, dates of birth) | Scenario fixtures on disk; never written to results.json | Included in manifest body over HTTPS |
| Receipt IDs | `results.json` | Plain HTTP in dev (mock); HTTPS in prod |

## HMAC Secret Handling

The shared secret is loaded from `mock-customs/secrets.json` at startup via
`client.load_secret()`. In production:

- The secret **must not** be committed to source control. The `secrets.json` file
  is acceptable for the mock setup only and is explicitly documented as
  `do-not-use-in-production` in the filename.
- Production deployment should read the secret from an environment variable
  (`CRESCENT_HMAC_SECRET`) or a secrets manager (AWS Secrets Manager, HashiCorp Vault).
- The secret is never logged, never included in `results.json`, and never echoed
  in error messages.
- Each request uses the current Unix timestamp in the signed message, making
  replayed requests invalid after the Authority's ±300s clock tolerance window.

## What a Security Reviewer Sees in the Code

- **`filer/client.py:_sign()`** — The HMAC key is `self._secret.encode("utf-8")`.
  The secret is stored in a private instance attribute (`_secret`) and never
  serialized. The signing function produces a fresh timestamp on every call, so
  signatures are not reusable.

- **No secret in logs** — The pipeline prints manifest IDs, receipt IDs, and outcome
  strings. No secret material, passport numbers, or crew PII appears in stdout.

- **No SQL / shell injection surface** — The pipeline reads JSON files and makes
  HTTP calls. There is no database layer and no shell subprocess invocation, so
  the classic injection vectors do not apply.

- **Scenario input is untrusted** — Scenario fixtures are treated as data and passed
  through the schema validator and rules engine before use. Malformed JSON raises
  a `json.JSONDecodeError` at the load step, not silently accepted.

## Audit Trail

Each pipeline run produces:
- **Console output** with scenario name, manifest ID, outcome, and any rule violations
- **`results.json`** with outcome per scenario and receipt IDs

A production audit trail should additionally:
- Persist the full manifest body + Authority receipt + final ack in durable storage
  (§12.1 requires 7-year retention)
- Log structured events (structlog / Cloud Logging) with correlation IDs linking
  manifest ID → receipt ID → ack ID
- Implement log integrity (append-only storage, WORM bucket) so the audit log
  cannot be silently altered after the fact

## Threat Vectors and Mitigations

| Threat | Mitigation |
|---|---|
| Secret leakage via source control | `secrets.json` is mock-only; prod uses env var / secrets manager |
| Replay attack on Authority API | Per-request HMAC timestamp; Authority rejects if clock skew > 300s |
| Man-in-the-middle on submission | TLS (HTTPS) in production; mock uses plain HTTP on localhost only |
| Manifest tampering in transit | HMAC signature covers the SHA-256 of the body bytes; any alteration invalidates the signature |
| Malicious scenario fixture | Fixtures are validated against JSON Schema before processing; no eval or exec of fixture content |
| Credential stuffing / brute-force of HMAC secret | Secret is 40+ character random string; HMAC-SHA256 is not brute-forceable at this length |
