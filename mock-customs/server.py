"""
Mock Crescent Harbor Customs Authority endpoint for the case study.

Implements the protocol described in §10 and §11 of the
Crescent Harbor Manifest Filing Specification v3.0:

  POST /v3/manifests       - submit a manifest, returns 202 RECEIVED
  GET  /v3/acks/{receiptId} - poll for the final disposition

Both endpoints require HMAC-SHA256 authentication via the X-Crescent-*
headers described in §10.2 and §10.3.

This is hiring-case-study code, not production code. It is intentionally
single-file and lightly abstracted so a non-engineer grader can read it.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import secrets
import socketserver
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from jsonschema import Draft202012Validator

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("CUSTOMS_PORT", "8080"))
SCHEMA_PATH = os.environ.get(
    "CUSTOMS_SCHEMA_PATH", "/srv/schema/manifest.schema.json"
)
SECRETS_PATH = os.environ.get("CUSTOMS_SECRETS_PATH", "/srv/secrets.json")
TIMESTAMP_TOLERANCE_SECONDS = 300

# ---------------------------------------------------------------------------
# Load schema and filer secrets at startup
# ---------------------------------------------------------------------------

with open(SCHEMA_PATH) as f:
    SCHEMA = json.load(f)
SCHEMA_VALIDATOR = Draft202012Validator(SCHEMA)
print(f"[startup] Loaded JSON Schema from {SCHEMA_PATH}", flush=True)

# secrets.json shape: {"CHC100001": "shared-secret-string", ...}
with open(SECRETS_PATH) as f:
    FILER_SECRETS: Dict[str, str] = json.load(f)
print(f"[startup] Loaded {len(FILER_SECRETS)} filer secret(s)", flush=True)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# receiptId -> ack dict
ACKS: Dict[str, dict] = {}
ACKS_LOCK = threading.Lock()

# (filerId, manifestId) -> first receipt timestamp, for §3.4 90-day duplicate
# detection. The case study runs for minutes, so we keep this in memory.
SEEN_MANIFESTS: Dict[Tuple[str, str], float] = {}

# ---------------------------------------------------------------------------
# HMAC verification per spec §10.3
# ---------------------------------------------------------------------------


def verify_hmac(
    method: str,
    path: str,
    headers: Dict[str, str],
    body: bytes,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Returns (ok, filer_id, error_message). If ok is True, filer_id is set.
    If ok is False, error_message describes the failure.
    """
    filer_id = headers.get("X-Crescent-FilerId", "").strip()
    timestamp = headers.get("X-Crescent-Timestamp", "").strip()
    signature = headers.get("X-Crescent-Signature", "").strip().lower()

    if not filer_id:
        return False, None, "missing X-Crescent-FilerId header"
    if filer_id not in FILER_SECRETS:
        return False, filer_id, f"unknown filerId: {filer_id}"

    if not timestamp.isdigit():
        return False, filer_id, "missing or non-integer X-Crescent-Timestamp"
    drift = abs(time.time() - int(timestamp))
    if drift > TIMESTAMP_TOLERANCE_SECONDS:
        return False, filer_id, f"timestamp drift {drift:.0f}s exceeds tolerance"

    if len(signature) != 64 or not all(c in "0123456789abcdef" for c in signature):
        return False, filer_id, "X-Crescent-Signature is not a 64-char lowercase hex string"

    body_digest = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(["CHCAv3", method, path, timestamp, body_digest])

    secret = FILER_SECRETS[filer_id].encode("utf-8")
    expected = hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, signature):
        return False, filer_id, "HMAC signature does not match"

    return True, filer_id, None


# ---------------------------------------------------------------------------
# Manifest validation pipeline (schema only — no business rules; those are
# the candidate's responsibility per the spec)
# ---------------------------------------------------------------------------


def validate_manifest(body: bytes) -> Tuple[str, list]:
    """
    Returns (status, errors). status is 'ACCEPTED' or 'REJECTED'.
    errors is a list of {code, message} dicts.
    """
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        return "REJECTED", [{"code": "M-101", "message": f"manifest body is not valid JSON: {exc}"}]

    errors = []
    for err in SCHEMA_VALIDATOR.iter_errors(doc):
        path = "/" + "/".join(str(p) for p in err.absolute_path) if err.absolute_path else "/"
        # Map a few common schema violations to spec rejection codes
        msg = err.message
        if "Additional properties" in msg:
            code = "M-103"
        elif "is not one of" in msg or "is not valid" in msg or "does not match" in msg:
            code = "M-102"
        elif "is a required property" in msg:
            code = "R-602"
        else:
            code = "M-102"
        errors.append({"code": code, "message": f"{path}: {msg[:200]}"})

    if errors:
        return "REJECTED", errors[:20]
    return "ACCEPTED", []


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class CustomsHandler(http.server.BaseHTTPRequestHandler):
    server_version = "CrescentCustomsMock/3.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(
            f"[customs] {self.address_string()} {self.command} {self.path} - {fmt % args}\n"
        )

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _headers_dict(self) -> Dict[str, str]:
        # http.client.HTTPMessage is case-insensitive, but a plain dict is
        # not. Normalize to a case-insensitive lookup by lowercasing and
        # then re-exposing the canonical X-Crescent-* names.
        normalized: Dict[str, str] = {}
        for canonical in (
            "X-Crescent-FilerId",
            "X-Crescent-Timestamp",
            "X-Crescent-Signature",
        ):
            v = self.headers.get(canonical)
            if v is not None:
                normalized[canonical] = v
        return normalized

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -------- POST /v3/manifests ------------------------------------------

    def do_POST(self):
        if self.path != "/v3/manifests":
            self._send_json(404, {"error": "not found"})
            return

        body = self._read_body()
        ok, filer_id, err = verify_hmac("POST", self.path, self._headers_dict(), body)
        if not ok:
            self._send_json(401, {"error": err})
            return

        # Parse just enough of the body to extract manifestId for duplicate
        # tracking, but defer full validation to validate_manifest below.
        try:
            doc = json.loads(body)
            manifest_id = doc.get("manifestId", "<missing>")
        except json.JSONDecodeError:
            manifest_id = "<unparseable>"

        # §3.4 duplicate detection within rolling 90-day window. The case
        # study uses a minutes-long process; we treat any prior receipt of
        # the same (filerId, manifestId) tuple as a duplicate.
        key = (filer_id, manifest_id)
        if key in SEEN_MANIFESTS:
            self._send_json(
                409,
                {
                    "error": "duplicate manifestId",
                    "code": "M-104",
                    "manifestId": manifest_id,
                },
            )
            return
        SEEN_MANIFESTS[key] = time.time()

        receipt_id = secrets.token_hex(8).upper()
        status, errors = validate_manifest(body)

        with ACKS_LOCK:
            ACKS[receipt_id] = {
                "receiptId": receipt_id,
                "manifestId": manifest_id,
                "status": status,
                "errors": errors,
                "createdAt": time.time(),
                "filerId": filer_id,
            }

        self._send_json(
            202,
            {
                "receiptId": receipt_id,
                "manifestId": manifest_id,
                "status": "RECEIVED",
            },
        )

    # -------- GET /v3/acks/{receiptId} ------------------------------------

    def do_GET(self):
        if not self.path.startswith("/v3/acks/"):
            self._send_json(404, {"error": "not found"})
            return

        receipt_id = self.path[len("/v3/acks/") :]
        ok, filer_id, err = verify_hmac("GET", self.path, self._headers_dict(), b"")
        if not ok:
            self._send_json(401, {"error": err})
            return

        with ACKS_LOCK:
            ack = ACKS.get(receipt_id)
        if ack is None:
            self._send_json(404, {"error": "unknown receiptId"})
            return
        if ack["filerId"] != filer_id:
            self._send_json(403, {"error": "receiptId does not belong to this filer"})
            return

        # The spec says the Authority guarantees a terminal state within 30
        # seconds of RECEIVED. We simulate this by returning PENDING for the
        # first second, then the terminal state.
        if time.time() - ack["createdAt"] < 1.0:
            self._send_json(200, {"status": "PENDING"})
            return

        if ack["status"] == "ACCEPTED":
            self._send_json(
                200,
                {
                    "status": "ACCEPTED",
                    "manifestId": ack["manifestId"],
                    "receiptId": ack["receiptId"],
                },
            )
        else:
            self._send_json(
                200,
                {
                    "status": "REJECTED",
                    "manifestId": ack["manifestId"],
                    "receiptId": ack["receiptId"],
                    "errors": ack["errors"],
                },
            )


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    addr = ("0.0.0.0", PORT)
    server = ThreadedHTTPServer(addr, CustomsHandler)
    print(
        f"[startup] Mock Crescent Harbor Customs listening on http://0.0.0.0:{PORT}",
        flush=True,
    )
    print(
        f"[startup] Endpoints: POST /v3/manifests, GET /v3/acks/{{receiptId}}",
        flush=True,
    )
    print(
        f"[startup] Authorized filers: {', '.join(sorted(FILER_SECRETS.keys()))}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[shutdown] interrupted", flush=True)


if __name__ == "__main__":
    main()
