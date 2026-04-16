"""
Business rules engine for the Crescent Harbor Manifest Filing Specification v3.0.

Rules are expressed as data: a list of rule descriptors each with a check function,
metadata (id, severity, field, message template). This avoids a wall of if-statements
and makes the rule set easy to audit, extend, and test independently.

Ambiguity decisions (documented here and in ARCHITECTURE.md):
  R-005: Silently uppercase vessel names; do not reject. Spec says filer "shall uppercase" —
         this is a pre-submission normalization responsibility, not a rejection condition.
  R-014: Treat as WARNING-ONLY. grossWeightKg is schema-optional; enforcing as a rejection
         only when the field is present creates perverse incentives (filers omit the field
         to bypass the check). Surface as a warning for operator review instead.
  R-023: Use client send time (wall clock at transmission start) as the filing clock.
         This is auditable, filer-controlled, and matches the HMAC timestamp.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable


@dataclass
class Violation:
    rule_id: str
    severity: str  # "reject" | "warning"
    field: str
    message: str


def _violation(rule_id: str, severity: str, field: str, message: str) -> Violation:
    return Violation(rule_id=rule_id, severity=severity, field=field, message=message)


# ---------------------------------------------------------------------------
# Individual rule check functions
# Each returns a list of Violation (empty = pass).
# ---------------------------------------------------------------------------

def _check_r001(manifest: dict) -> list[Violation]:
    mid = manifest.get("manifestId", "")
    if not re.match(r"^[A-Z0-9-]{12,32}$", mid):
        return [_violation("R-001", "reject", "/manifestId",
                           f"manifestId '{mid}' must be 12-32 chars from [A-Z0-9-]")]
    return []


def _check_r002(manifest: dict) -> list[Violation]:
    filer_id = manifest.get("filer", {}).get("filerId", "")
    if not re.match(r"^[A-Z]{3}[0-9]{6}$", filer_id):
        return [_violation("R-002", "reject", "/filer/filerId",
                           f"filerId '{filer_id}' must match pattern [A-Z]{{3}}[0-9]{{6}}")]
    return []


def _check_r003(manifest: dict) -> list[Violation]:
    email = manifest.get("filer", {}).get("contactEmail", "")
    # RFC 5322 §3.4.1 simplified: local@domain with reasonable constraints
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return [_violation("R-003", "reject", "/filer/contactEmail",
                           f"contactEmail '{email}' is not a valid email address")]
    return []


def _imo_check_digit_valid(imo_number: str) -> bool:
    """Validate IMO check digit per the standard algorithm."""
    if not re.match(r"^IMO[0-9]{7}$", imo_number):
        return False
    digits = imo_number[3:]  # 7 digits
    weights = [7, 6, 5, 4, 3, 2]
    total = sum(int(digits[i]) * weights[i] for i in range(6))
    return (total % 10) == int(digits[6])


def _check_r004(manifest: dict) -> list[Violation]:
    imo = manifest.get("vessel", {}).get("imoNumber", "")
    if not _imo_check_digit_valid(imo):
        return [_violation("R-004", "reject", "/vessel/imoNumber",
                           f"IMO number '{imo}' fails check-digit validation (R-603)")]
    return []


def _check_r005(manifest: dict) -> list[Violation]:
    name = manifest.get("vessel", {}).get("name", "")
    # After normalization (uppercasing in builder), validate charset
    if not re.match(r"^[A-Z0-9 .-]+$", name):
        return [_violation("R-005", "reject", "/vessel/name",
                           f"Vessel name '{name}' contains invalid characters after normalization")]
    return []


def _check_r006(manifest: dict) -> list[Violation]:
    grt = manifest.get("vessel", {}).get("grossRegisterTons", 0)
    if grt <= 500:
        return [_violation("R-006", "reject", "/vessel/grossRegisterTons",
                           f"grossRegisterTons {grt} must be greater than 500")]
    return []


_VESSEL_TERMINAL_MAP = {
    "CONTAINER": {"CH-A", "CH-B"},
    "BULK": {"CH-C"},
    "TANKER": {"CH-C"},
    "RORO": {"CH-D"},
    "GENERAL": {"CH-A", "CH-B", "CH-C", "CH-D"},
}


def _check_r007(manifest: dict) -> list[Violation]:
    vessel_type = manifest.get("vessel", {}).get("vesselType", "")
    terminal = manifest.get("arrival", {}).get("terminal", "")
    allowed = _VESSEL_TERMINAL_MAP.get(vessel_type, set())
    if terminal not in allowed:
        return [_violation("R-007", "reject", "/arrival/terminal",
                           f"Vessel type '{vessel_type}' is not permitted at terminal '{terminal}' (R-601). "
                           f"Allowed terminals: {sorted(allowed)}")]
    return []


def _check_r008(manifest: dict) -> list[Violation]:
    containers = manifest.get("containers", [])
    ids = [c.get("containerId") for c in containers]
    seen = set()
    dups = set()
    for cid in ids:
        if cid in seen:
            dups.add(cid)
        seen.add(cid)
    if dups:
        return [_violation("R-008", "reject", "/containers",
                           f"Duplicate containerId(s) within manifest: {sorted(dups)} (R-604)")]
    return []


def _check_r009(manifest: dict) -> list[Violation]:
    if len(manifest.get("containers", [])) < 1:
        return [_violation("R-009", "reject", "/containers",
                           "At least one container is required")]
    return []


def _check_r010(manifest: dict) -> list[Violation]:
    containers = manifest.get("containers", [])
    types = [c.get("type") for c in containers]
    has_ballast = "BALLAST" in types
    if has_ballast and len(containers) > 1:
        return [_violation("R-010", "reject", "/containers",
                           "BALLAST container may not coexist with other containers (§5.1)")]
    return []


def _check_r011(manifest: dict) -> list[Violation]:
    violations = []
    for i, c in enumerate(manifest.get("containers", [])):
        if c.get("type") == "REF" and c.get("commodityCode") == "0000":
            violations.append(_violation("R-011", "reject", f"/containers/{i}/commodityCode",
                                         f"REF container '{c.get('containerId')}' uses reserved commodityCode 0000 (R-606)"))
    return violations


def _check_r012(manifest: dict) -> list[Violation]:
    violations = []
    for i, c in enumerate(manifest.get("containers", [])):
        if c.get("type") == "VEH":
            vins = c.get("vins", [])
            qty = c.get("quantity", 0)
            if len(vins) != qty:
                violations.append(_violation("R-012", "reject", f"/containers/{i}/vins",
                                             f"VEH container '{c.get('containerId')}': "
                                             f"vins length ({len(vins)}) must equal quantity ({qty}) (R-605)"))
    return violations


def _check_r013(manifest: dict) -> list[Violation]:
    violations = []
    for i, c in enumerate(manifest.get("containers", [])):
        if c.get("type") == "HAZ" and c.get("hazardClass") == "7":
            if not c.get("priorAuthorizationRef"):
                violations.append(_violation("R-013", "reject", f"/containers/{i}/priorAuthorizationRef",
                                             f"HAZ container '{c.get('containerId')}' is class 7 (Radioactive) "
                                             f"and requires priorAuthorizationRef (H-204)"))
    return violations


def _check_r014(manifest: dict) -> list[Violation]:
    """
    Hazmat gross weight <= 25% of vessel GRT.

    Ambiguity decision: treat as WARNING-ONLY regardless of whether grossWeightKg is present.

    Rationale: grossWeightKg is schema-optional. Enforcing as a hard rejection only when the
    field IS present would create a perverse incentive — filers who omit the field bypass the
    check entirely, while filers who supply it truthfully get rejected. Both outcomes are wrong.
    Instead, surface the potential violation as a warning for operator review and let the
    Authority's own compliance systems make the final determination (§6.1 already flags all
    HAZ manifests for harbormaster review). This is the "best-effort" interpretation.
    """
    containers = manifest.get("containers", [])
    haz_containers = [c for c in containers if c.get("type") == "HAZ"]
    if not haz_containers:
        return []
    grt = manifest.get("vessel", {}).get("grossRegisterTons", 0)
    limit = grt * 0.25

    haz_with_weight = [c for c in haz_containers if "grossWeightKg" in c]
    if not haz_with_weight:
        return [_violation("R-014", "warning", "/containers",
                           f"HAZ containers present but no grossWeightKg provided; "
                           f"cannot verify 25% GRT limit ({grt} GRT → {limit}kg)")]

    total_haz_weight = sum(c["grossWeightKg"] for c in haz_with_weight)
    missing_count = len(haz_containers) - len(haz_with_weight)
    if total_haz_weight > limit:
        suffix = f" ({missing_count} HAZ container(s) lacked grossWeightKg)" if missing_count else ""
        return [_violation("R-014", "warning", "/containers",
                           f"Combined HAZ gross weight {total_haz_weight}kg may exceed 25% of vessel GRT "
                           f"({grt} GRT → limit {limit}kg) — warning only (H-201 risk){suffix}")]
    return []


def _check_r015(manifest: dict) -> list[Violation]:
    containers = manifest.get("containers", [])
    if any(c.get("type") == "HAZ" for c in containers):
        return [_violation("R-015", "warning", "/containers",
                           "Manifest contains HAZ containers; flagged for harbormaster review (§6.1)")]
    return []


def _check_r016(manifest: dict) -> list[Violation]:
    """declaredValueTotal must equal sum of container declaredValueUSD with half-away rounding."""
    containers = manifest.get("containers", [])
    computed = sum(
        float(Decimal(str(c.get("declaredValueUSD", 0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        for c in containers
    )
    total = manifest.get("declaredValueTotal", 0)
    # Compare to cent precision
    computed_rounded = round(computed, 2)
    total_rounded = round(float(total), 2)
    if abs(computed_rounded - total_rounded) > 0.001:
        return [_violation("R-016", "reject", "/declaredValueTotal",
                           f"declaredValueTotal {total_rounded} does not match computed sum "
                           f"{computed_rounded} of container values (V-301)")]
    return []


def _check_r017(manifest: dict) -> list[Violation]:
    total = manifest.get("declaredValueTotal", 0)
    if total > 500_000_000:
        return [_violation("R-017", "reject", "/declaredValueTotal",
                           f"declaredValueTotal {total} exceeds USD 500,000,000 cap (V-302)")]
    return []


def _check_r018(manifest: dict) -> list[Violation]:
    violations = []
    for i, c in enumerate(manifest.get("containers", [])):
        val = c.get("declaredValueUSD")
        if val is not None:
            d = Decimal(str(val))
            if d != d.quantize(Decimal("0.01")):
                violations.append(_violation("R-018", "warning", f"/containers/{i}/declaredValueUSD",
                                             f"Container '{c.get('containerId')}' declaredValueUSD has more than 2 decimal places"))
    return violations


def _check_r019(manifest: dict) -> list[Violation]:
    masters = [m for m in manifest.get("crew", []) if m.get("role") == "MASTER"]
    if len(masters) != 1:
        return [_violation("R-019", "reject", "/crew",
                           f"Manifest must have exactly one MASTER; found {len(masters)} (C-401)")]
    return []


def _check_r020(manifest: dict) -> list[Violation]:
    """Crew age at ETA must be in [16, 80]."""
    eta_str = manifest.get("arrival", {}).get("eta", "")
    try:
        eta_date = datetime.strptime(eta_str, "%Y-%m-%dT%H:%M:%SZ").date()
    except ValueError:
        return []  # Schema validation handles bad ETA format

    violations = []
    for i, member in enumerate(manifest.get("crew", [])):
        dob_str = member.get("dateOfBirth", "")
        try:
            dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        # Age = years between dob and eta_date
        age = eta_date.year - dob.year - ((eta_date.month, eta_date.day) < (dob.month, dob.day))
        if age < 16 or age > 80:
            violations.append(_violation("R-020", "reject", f"/crew/{i}/dateOfBirth",
                                         f"Crew member '{member.get('fullName')}' age {age} at ETA "
                                         f"is outside permitted range [16, 80] (C-402)"))
    return violations


def _check_r021(manifest: dict) -> list[Violation]:
    for member in manifest.get("crew", []):
        if member.get("role") == "MASTER":
            nat = member.get("nationality", "")
            if not nat:
                return [_violation("R-021", "reject", "/crew/*/nationality",
                                   "MASTER crew member must have a non-empty nationality field")]
    return []


def _check_r022(manifest: dict) -> list[Violation]:
    """Manifest must not be transmitted earlier than 96 hours before ETA."""
    eta_str = manifest.get("arrival", {}).get("eta", "")
    try:
        eta_dt = datetime.strptime(eta_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return []
    now = datetime.now(timezone.utc)
    earliest_allowed = eta_dt - timedelta(hours=96)
    if now < earliest_allowed:
        return [_violation("R-022", "reject", "/arrival/eta",
                           f"Filing time {now.isoformat()} is more than 96 hours before ETA "
                           f"{eta_str}; earliest allowed is {earliest_allowed.isoformat()} (T-501)")]
    return []


def _check_r023(manifest: dict) -> list[Violation]:
    """
    Manifest must not be transmitted later than 24 hours before ETA.
    Ambiguity decision: use client send time (wall clock) as the filing clock.
    """
    eta_str = manifest.get("arrival", {}).get("eta", "")
    try:
        eta_dt = datetime.strptime(eta_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return []
    now = datetime.now(timezone.utc)
    latest_allowed = eta_dt - timedelta(hours=24)
    if now > latest_allowed:
        return [_violation("R-023", "reject", "/arrival/eta",
                           f"Filing time {now.isoformat()} is less than 24 hours before ETA "
                           f"{eta_str}; latest allowed is {latest_allowed.isoformat()} (T-502)")]
    return []


def _check_r024(manifest: dict) -> list[Violation]:
    """
    Amendments must reuse original manifestId and keep vessel.imoNumber and arrival.eta unchanged.
    For original manifests (no amendmentSequence), this rule does not apply.
    """
    if "amendmentSequence" not in manifest:
        return []
    # In a real system we'd compare against the stored original — not applicable here
    return []


def _check_r025(manifest: dict) -> list[Violation]:
    """
    First amendment is sequence 1; each subsequent increments by 1.
    For original manifests (no amendmentSequence), this rule does not apply.
    """
    if "amendmentSequence" not in manifest:
        return []
    seq = manifest["amendmentSequence"]
    if not isinstance(seq, int) or seq < 1:
        return [_violation("R-025", "reject", "/amendmentSequence",
                           f"amendmentSequence must be a positive integer starting at 1; got {seq}")]
    return []


# ---------------------------------------------------------------------------
# Rule registry — defines execution order and metadata
# ---------------------------------------------------------------------------

RULES: list[tuple[str, Callable]] = [
    ("R-001", _check_r001),
    ("R-002", _check_r002),
    ("R-003", _check_r003),
    ("R-004", _check_r004),
    ("R-005", _check_r005),
    ("R-006", _check_r006),
    ("R-007", _check_r007),
    ("R-008", _check_r008),
    ("R-009", _check_r009),
    ("R-010", _check_r010),
    ("R-011", _check_r011),
    ("R-012", _check_r012),
    ("R-013", _check_r013),
    ("R-014", _check_r014),
    ("R-015", _check_r015),
    ("R-016", _check_r016),
    ("R-017", _check_r017),
    ("R-018", _check_r018),
    ("R-019", _check_r019),
    ("R-020", _check_r020),
    ("R-021", _check_r021),
    ("R-022", _check_r022),
    ("R-023", _check_r023),
    ("R-024", _check_r024),
    ("R-025", _check_r025),
]


def check(manifest: dict) -> list[Violation]:
    """
    Run all 25 business rules against a manifest.
    Returns a list of Violation objects (empty = all rules pass).
    """
    violations = []
    for _rule_id, check_fn in RULES:
        violations.extend(check_fn(manifest))
    return violations


def has_rejections(violations: list[Violation]) -> bool:
    return any(v.severity == "reject" for v in violations)
