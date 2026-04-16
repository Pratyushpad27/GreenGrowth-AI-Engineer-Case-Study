"""
HMAC-signed transmission client for the Crescent Harbor Customs Authority API.

Implements the submission protocol from §10 and §11 of the specification:
  - Computes HMAC-SHA256 signatures per §10.3
  - POSTs manifests to /v3/manifests
  - Polls /v3/acks/{receiptId} until terminal state (ACCEPTED or REJECTED)
"""

import hashlib
import hmac
import json
import time
from typing import Optional

import httpx

# Default base URL (override via env or constructor)
DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_FILER_ID = "CHC100001"


class AuthorityClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        filer_id: str = DEFAULT_FILER_ID,
        secret: str = "",
        poll_interval_s: float = 2.0,
        poll_timeout_s: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.filer_id = filer_id
        self._secret = secret
        self.poll_interval_s = poll_interval_s
        self.poll_timeout_s = poll_timeout_s

    def _sign(self, method: str, path: str, body_bytes: bytes) -> tuple[int, str]:
        """
        Compute HMAC-SHA256 signature per §10.3.
        Returns (timestamp, signature_hex).

        Signing string (newline-separated, no trailing newline):
          1. "CHCAv3"
          2. HTTP method (e.g. "POST")
          3. Request path (e.g. "/v3/manifests")
          4. Unix timestamp as integer string
          5. Lowercase hex SHA-256 digest of request body bytes
        """
        timestamp = int(time.time())
        body_digest = hashlib.sha256(body_bytes).hexdigest()
        message = "\n".join([
            "CHCAv3",
            method,
            path,
            str(timestamp),
            body_digest,
        ])
        signature = hmac.new(
            self._secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return timestamp, signature

    def _auth_headers(self, method: str, path: str, body_bytes: bytes) -> dict:
        timestamp, signature = self._sign(method, path, body_bytes)
        return {
            "X-Crescent-FilerId": self.filer_id,
            "X-Crescent-Timestamp": str(timestamp),
            "X-Crescent-Signature": signature,
            "Content-Type": "application/json",
        }

    def submit(self, manifest: dict) -> dict:
        """
        POST a manifest to /v3/manifests.

        Returns the parsed 202 response body:
          { "receiptId": "...", "manifestId": "...", "status": "RECEIVED" }

        Raises httpx.HTTPStatusError on non-2xx responses.
        """
        path = "/v3/manifests"
        body_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
        headers = self._auth_headers("POST", path, body_bytes)

        with httpx.Client(base_url=self.base_url) as client:
            response = client.post(path, content=body_bytes, headers=headers)

        if response.status_code != 202:
            # Surface the error body for diagnostics
            try:
                error_body = response.json()
            except Exception:
                error_body = response.text
            raise httpx.HTTPStatusError(
                f"POST {path} returned {response.status_code}: {error_body}",
                request=response.request,
                response=response,
            )

        return response.json()

    def poll_ack(self, receipt_id: str) -> dict:
        """
        Poll GET /v3/acks/{receiptId} until a terminal state is reached.

        Returns the final ack body (status: ACCEPTED or REJECTED).
        Raises TimeoutError if no terminal state within poll_timeout_s.
        Raises httpx.HTTPStatusError on non-200 responses.
        """
        path = f"/v3/acks/{receipt_id}"
        deadline = time.time() + self.poll_timeout_s

        while time.time() < deadline:
            body_bytes = b""  # GET has no body
            headers = self._auth_headers("GET", path, body_bytes)

            with httpx.Client(base_url=self.base_url) as client:
                response = client.get(path, headers=headers)

            if response.status_code != 200:
                try:
                    error_body = response.json()
                except Exception:
                    error_body = response.text
                raise httpx.HTTPStatusError(
                    f"GET {path} returned {response.status_code}: {error_body}",
                    request=response.request,
                    response=response,
                )

            ack = response.json()
            status = ack.get("status")

            if status in ("ACCEPTED", "REJECTED"):
                return ack

            if status == "PENDING":
                time.sleep(self.poll_interval_s)
                continue

            raise ValueError(f"Unexpected ack status: {status}")

        raise TimeoutError(
            f"Polling /v3/acks/{receipt_id} timed out after {self.poll_timeout_s}s"
        )

    def transmit(self, manifest: dict) -> dict:
        """
        Full transmission flow: submit → poll → return final ack.

        Returns a dict with keys:
          outcome: "accepted" | "rejected_by_authority" | "error"
          receipt_id: str (if submitted successfully)
          errors: list[dict] (if rejected by authority)
          detail: str (error description if outcome == "error")
        """
        try:
            receipt = self.submit(manifest)
            receipt_id = receipt["receiptId"]
        except Exception as exc:
            return {"outcome": "error", "detail": f"Submission failed: {exc}"}

        try:
            ack = self.poll_ack(receipt_id)
        except Exception as exc:
            return {
                "outcome": "error",
                "receipt_id": receipt_id,
                "detail": f"Polling failed: {exc}",
            }

        if ack["status"] == "ACCEPTED":
            return {"outcome": "accepted", "receipt_id": receipt_id}
        else:
            return {
                "outcome": "rejected_by_authority",
                "receipt_id": receipt_id,
                "errors": ack.get("errors", []),
            }


def load_secret(secrets_path: str, filer_id: str) -> str:
    """Load the HMAC secret for a given filer from a secrets JSON file."""
    with open(secrets_path) as f:
        secrets = json.load(f)
    if filer_id not in secrets:
        raise KeyError(f"No secret found for filer '{filer_id}' in {secrets_path}")
    return secrets[filer_id]
