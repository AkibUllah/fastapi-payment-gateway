"""API key handling and HMAC-SHA256 webhook signatures.

Signature header format (shared by outgoing merchant webhooks and the Stripe-style
inbound verifier):

    X-Gateway-Signature: t=<unix-ts>,v1=<hex hmac of "<ts>.<raw body>">

The timestamp is part of the signed payload, so a captured request cannot be replayed
after the tolerance window.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

API_KEY_PREFIX_LENGTH = 8


class SignatureError(Exception):
    """Raised when a webhook signature is missing, malformed, stale or wrong."""


def generate_api_key(environment: str = "test") -> tuple[str, str, str]:
    """Return (plaintext_key, prefix, sha256_hash).

    The plaintext is shown to the merchant exactly once; only the hash is stored.
    Format: pk_<env>_<prefix><secret>, where the prefix is used for lookup.
    """
    prefix = secrets.token_hex(API_KEY_PREFIX_LENGTH // 2)
    secret = secrets.token_urlsafe(32)
    plaintext = f"pk_{environment}_{prefix}{secret}"
    return plaintext, prefix, hash_api_key(plaintext)


def api_key_prefix(plaintext: str) -> str | None:
    parts = plaintext.split("_", 2)
    if len(parts) != 3 or parts[0] != "pk" or len(parts[2]) <= API_KEY_PREFIX_LENGTH:
        return None
    return parts[2][:API_KEY_PREFIX_LENGTH]


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def verify_api_key(plaintext: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(plaintext), stored_hash)


def hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def sign_payload(secret: str, timestamp: int, body: bytes) -> str:
    return hmac_hex(secret, f"{timestamp}.".encode() + body)


def build_signature_header(secret: str, body: bytes, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    return f"t={ts},v1={sign_payload(secret, ts, body)}"


def parse_signature_header(header: str) -> tuple[int, list[str]]:
    timestamp: int | None = None
    signatures: list[str] = []
    for item in header.split(","):
        key, sep, value = item.strip().partition("=")
        if not sep:
            raise SignatureError("Malformed signature header")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise SignatureError("Malformed signature timestamp") from exc
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        raise SignatureError("Signature header missing timestamp or v1 signature")
    return timestamp, signatures


def verify_signature_header(
    secret: str,
    body: bytes,
    header: str | None,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> None:
    """Raise SignatureError unless `header` is a valid, fresh signature of `body`."""
    if not header:
        raise SignatureError("Missing signature header")
    timestamp, signatures = parse_signature_header(header)
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        raise SignatureError("Signature timestamp outside tolerance window")
    expected = sign_payload(secret, timestamp, body)
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise SignatureError("Signature mismatch")


def verify_plain_hmac(secret: str, body: bytes, header: str | None) -> None:
    """Verify a bare hex HMAC-SHA256 header (bKash / SSLCommerz sandbox style)."""
    if not header:
        raise SignatureError("Missing signature header")
    if not hmac.compare_digest(hmac_hex(secret, body), header.strip().lower()):
        raise SignatureError("Signature mismatch")
