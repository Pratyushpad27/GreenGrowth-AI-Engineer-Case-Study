# Scenario Fixtures

Each `*.json` file in this directory is one Cargo Arrival Manifest as input data for the Crescent Harbor Direct Filer Program. You map these into your own internal data model however you like — the field names mirror the specification's terms to make the mapping obvious.

Six of these scenarios should produce an `ACCEPTED` acknowledgment from the mock Authority endpoint. Two should be rejected. Which two, and at which layer, is something your pipeline must figure out — they are not labeled.

## Two things to know about the input format

1. **Relative ETA.** Each scenario carries an `_etaOffsetHours` field instead of a literal `eta`. Your pipeline must compute the manifest's `arrival.eta` at submission time as `now() + _etaOffsetHours`, formatted per §4.2 of the specification. This makes the test deterministic regardless of when you run it. The `_etaOffsetHours` field is *input metadata*; it should not appear in the manifest you transmit to the Authority.

2. **Filer identity is yours.** The scenarios contain only the substantive cargo, vessel, and crew data. You supply the `manifestId`, the `filer` block, and the `filerSignature` block from your own configuration. (For the case study, your `filerId` is `CHC100001`, your shared HMAC secret is in `mock-customs/secret.txt`, and you can pick any `manifestId` you want as long as it satisfies §3.4.)

## What your pipeline must produce

A single results report (JSON or markdown — your call) listing every scenario and its outcome category:

- `accepted` — the mock Authority returned `ACCEPTED`
- `rejected_by_rules` — your rules engine blocked it before transmission
- `rejected_by_schema` — your JSON Schema validator blocked it before transmission
- `rejected_by_authority` — the mock Authority returned `REJECTED`
- `error` — anything unexpected

The expected outcomes for the failing two scenarios are *deterministic* — the same scenario always fails at the same layer for the same reason. Hardcoding which scenarios fail by filename is grounds for disqualification (we read the code carefully).
