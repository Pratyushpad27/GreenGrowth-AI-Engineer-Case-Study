"""
End-to-end pipeline: iterates all scenario fixtures, builds manifests,
validates them, and transmits to the mock Authority.

Usage:
    python -m filer.pipeline [--scenarios DIR] [--schema FILE] [--secrets FILE]
                              [--authority URL] [--output FILE] [--single SCENARIO]
"""

import argparse
import json
import os
import sys
from pathlib import Path

import jsonschema
from jsonschema import Draft202012Validator, FormatChecker

from filer import builder, rules
from filer.client import AuthorityClient, load_secret


DEFAULT_SCENARIOS_DIR = Path(__file__).parent.parent / "scenarios"
DEFAULT_SCHEMA_FILE = Path(__file__).parent.parent / "schema" / "manifest.schema.json"
DEFAULT_SECRETS_FILE = Path(__file__).parent.parent / "mock-customs" / "secrets.json"
DEFAULT_AUTHORITY_URL = os.environ.get("AUTHORITY_URL", "http://localhost:8080")
DEFAULT_OUTPUT_FILE = Path(__file__).parent.parent / "results.json"
FILER_ID = "CHC100001"


def load_schema(schema_path: Path) -> dict:
    with open(schema_path) as f:
        return json.load(f)


def validate_schema(manifest: dict, schema: dict) -> list[str]:
    """
    Validate manifest against JSON Schema.
    Returns a list of error messages (empty = valid).
    """
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(manifest), key=lambda e: str(e.path))
    return [f"{'.'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in errors]


def run_scenario(
    scenario_path: Path,
    schema: dict,
    client: AuthorityClient,
    transmit: bool = True,
) -> dict:
    """
    Process a single scenario file end-to-end.

    Returns a result dict with:
      scenario, outcome, violations, schema_errors, authority_errors, receipt_id
    """
    scenario_id = scenario_path.stem  # filename without .json
    print(f"\n{'='*60}")
    print(f"Scenario: {scenario_id}")

    # Load scenario fixture
    with open(scenario_path) as f:
        scenario_data = json.load(f)

    # Step 1: Build manifest
    manifest = builder.build(scenario_data)
    print(f"  Built manifest: {manifest['manifestId']}")

    # Step 2: Schema validation
    schema_errors = validate_schema(manifest, schema)
    if schema_errors:
        print(f"  SCHEMA INVALID ({len(schema_errors)} error(s)):")
        for err in schema_errors:
            print(f"    - {err}")
        return {
            "scenario": scenario_id,
            "outcome": "rejected_by_schema",
            "schema_errors": schema_errors,
        }
    print("  Schema: VALID")

    # Step 3: Business rules
    violations = rules.check(manifest)
    warnings = [v for v in violations if v.severity == "warning"]
    rejections = [v for v in violations if v.severity == "reject"]

    if warnings:
        print(f"  Rules warnings ({len(warnings)}):")
        for w in warnings:
            print(f"    [WARN] {w.rule_id} {w.field}: {w.message}")

    if rejections:
        print(f"  Rules REJECTIONS ({len(rejections)}):")
        for r in rejections:
            print(f"    [REJECT] {r.rule_id} {r.field}: {r.message}")
        return {
            "scenario": scenario_id,
            "outcome": "rejected_by_rules",
            "violations": [
                {"ruleId": v.rule_id, "severity": v.severity, "field": v.field, "message": v.message}
                for v in violations
            ],
        }
    print(f"  Rules: PASS (0 rejections, {len(warnings)} warning(s))")

    # Step 4: Transmit
    if not transmit:
        print("  Transmission: SKIPPED (dry-run mode)")
        return {"scenario": scenario_id, "outcome": "skipped"}

    print("  Transmitting to Authority...")
    result = client.transmit(manifest)
    outcome = result["outcome"]
    receipt_id = result.get("receipt_id", "")
    authority_errors = result.get("errors", [])
    detail = result.get("detail", "")

    if outcome == "accepted":
        print(f"  Authority: ACCEPTED (receipt: {receipt_id})")
    elif outcome == "rejected_by_authority":
        print(f"  Authority: REJECTED (receipt: {receipt_id})")
        for err in authority_errors:
            print(f"    [{err.get('code')}] {err.get('message')}")
    else:
        print(f"  Authority: ERROR — {detail}")

    return {
        "scenario": scenario_id,
        "outcome": outcome,
        "receipt_id": receipt_id,
        **({"authority_errors": authority_errors} if authority_errors else {}),
        **({"detail": detail} if detail else {}),
        **({"violations": [
            {"ruleId": v.rule_id, "severity": v.severity, "field": v.field, "message": v.message}
            for v in violations
        ]} if violations else {}),
    }


def main():
    parser = argparse.ArgumentParser(description="Crescent Harbor Direct Filer pipeline")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS_DIR)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_FILE)
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS_FILE)
    parser.add_argument("--authority", default=DEFAULT_AUTHORITY_URL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--single", default=None,
                        help="Run only this scenario (filename stem, e.g. '01-aurora-borealis')")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build and validate only; do not transmit")
    args = parser.parse_args()

    # Load schema
    if not args.schema.exists():
        print(f"ERROR: Schema file not found: {args.schema}", file=sys.stderr)
        sys.exit(1)
    schema = load_schema(args.schema)

    # Load secret and build client
    if args.dry_run:
        client = None
    else:
        if not args.secrets.exists():
            print(f"ERROR: Secrets file not found: {args.secrets}", file=sys.stderr)
            sys.exit(1)
        secret = load_secret(str(args.secrets), FILER_ID)
        client = AuthorityClient(base_url=args.authority, filer_id=FILER_ID, secret=secret)

    # Discover scenario files
    scenario_files = sorted(args.scenarios.glob("*.json"))
    if not scenario_files:
        print(f"ERROR: No scenario files found in {args.scenarios}", file=sys.stderr)
        sys.exit(1)

    if args.single:
        scenario_files = [f for f in scenario_files if f.stem == args.single]
        if not scenario_files:
            print(f"ERROR: Scenario '{args.single}' not found in {args.scenarios}", file=sys.stderr)
            sys.exit(1)

    print(f"Crescent Harbor Direct Filer — processing {len(scenario_files)} scenario(s)")
    print(f"Authority: {args.authority}")

    results_list = []
    for scenario_path in scenario_files:
        result = run_scenario(
            scenario_path=scenario_path,
            schema=schema,
            client=client,
            transmit=not args.dry_run,
        )
        results_list.append(result)

    # Write results.json (Format B)
    output = {"results": results_list}
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Results written to: {args.output}")
    print("\nSummary:")
    for r in results_list:
        print(f"  {r['scenario']:35s} → {r['outcome']}")

    # Exit non-zero if any scenario errored unexpectedly
    if any(r["outcome"] == "error" for r in results_list):
        sys.exit(1)


if __name__ == "__main__":
    main()
