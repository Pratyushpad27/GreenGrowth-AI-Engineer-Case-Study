"""
Manifest builder: transforms raw scenario fixtures into complete Manifest documents
conforming to manifest.schema.json and the Crescent Harbor Manifest Filing Specification v3.0.
"""

import copy
import uuid
from datetime import datetime, timezone, timedelta


FILER_ID = "CHC100001"
FILER_LEGAL_NAME = "Crescent Harbor Direct Filer Inc."
FILER_CONTACT_EMAIL = "ops@chdf.example.com"
SIGNER_NAME = "Pratyush Padhy"
SIGNER_TITLE = "Authorized Filing Agent"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc_string(dt: datetime) -> str:
    """Format as ISO 8601 UTC without sub-second precision, ending in Z."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build(scenario: dict) -> dict:
    """
    Build a complete Manifest from a scenario fixture.

    The scenario fixture may contain underscore-prefixed meta fields:
      _scenarioId      — scenario name (stripped)
      _etaOffsetHours  — hours from now until ETA (used to compute arrival.eta)

    Returns a dict ready for JSON serialization that satisfies the manifest schema.
    """
    scenario = copy.deepcopy(scenario)
    now = _utc_now()

    # Extract meta fields
    eta_offset_hours = scenario.pop("_etaOffsetHours", 48)
    scenario.pop("_scenarioId", None)

    # Compute ETA
    eta_dt = now + timedelta(hours=eta_offset_hours)
    eta_str = _to_utc_string(eta_dt)

    # Generate a unique manifest ID: UUIDv4, hyphens removed, uppercased (satisfies R-001)
    manifest_id = uuid.uuid4().hex.upper()

    # Normalize vessel name to uppercase per §4.1 and R-005
    # Interpretation: the spec says "shall be uppercased by the filer" — this is a filer
    # responsibility, not a rejection condition.
    if "vessel" in scenario and "name" in scenario["vessel"]:
        scenario["vessel"]["name"] = scenario["vessel"]["name"].upper()

    # Inject computed arrival.eta
    if "arrival" not in scenario:
        scenario["arrival"] = {}
    scenario["arrival"]["eta"] = eta_str

    # Inject filer block
    filer = {
        "filerId": FILER_ID,
        "legalName": FILER_LEGAL_NAME,
        "contactEmail": FILER_CONTACT_EMAIL,
    }

    # Inject filerSignature (audit record — §10.5; no cryptographic role)
    filer_signature = {
        "signerName": SIGNER_NAME,
        "signerTitle": SIGNER_TITLE,
        "signedAtUtc": _to_utc_string(now),
    }

    manifest = {
        "manifestId": manifest_id,
        "filer": filer,
        "vessel": scenario.get("vessel", {}),
        "arrival": scenario["arrival"],
        "containers": scenario.get("containers", []),
        "crew": scenario.get("crew", []),
        "declaredValueTotal": scenario.get("declaredValueTotal", 0),
        "filerSignature": filer_signature,
    }

    return manifest
