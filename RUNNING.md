# Running the Crescent Harbor Direct Filer

## Prerequisites

- Python 3.11+
- Docker and Docker Compose (for the mock Authority endpoint)

## Setup

```bash
pip install -r requirements.txt
```

## 1. Start the mock Authority endpoint

**Option A — Docker (recommended):**
```bash
cd mock-customs
docker compose up -d
cd ..
```
The mock Authority listens on `http://localhost:8080`.

**Option B — Python directly (no Docker required):**
```bash
CUSTOMS_PORT=8080 \
CUSTOMS_SCHEMA_PATH=schema/manifest.schema.json \
CUSTOMS_SECRETS_PATH=mock-customs/secrets.json \
python mock-customs/server.py &
```

## 2. Run a single scenario (for debugging)

```bash
python -m filer.pipeline --single 01-aurora-borealis
```

To build and validate without transmitting:

```bash
python -m filer.pipeline --single 01-aurora-borealis --dry-run
```

## 3. Run all 8 scenarios and produce the report

```bash
./run.sh
```

Or equivalently:

```bash
python -m filer.pipeline
```

This processes all files in `scenarios/` alphabetically, then writes `results.json`.

## Output

`results.json` is written to the repo root. It uses Format B from the spec:

```json
{
  "results": [
    { "scenario": "01-aurora-borealis", "outcome": "accepted", "receipt_id": "..." },
    ...
  ]
}
```

Outcome values:
- `accepted` — Authority returned ACCEPTED
- `rejected_by_rules` — blocked by business rules engine before transmission
- `rejected_by_schema` — blocked by JSON Schema validation before transmission
- `rejected_by_authority` — transmitted but Authority returned REJECTED
- `error` — unexpected failure (network, crash, etc.)

## Custom Authority URL

```bash
AUTHORITY_URL=http://my-host:8080 ./run.sh
```

or:

```bash
python -m filer.pipeline --authority http://my-host:8080
```

## Stop the mock server

```bash
cd mock-customs && docker-compose down
```
