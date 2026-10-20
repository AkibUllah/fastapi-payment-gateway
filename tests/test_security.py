import pytest

from app.core.security import (
    SignatureError,
    api_key_prefix,
    build_signature_header,
    generate_api_key,
    hash_api_key,
    parse_signature_header,
    sign_payload,
    verify_api_key,
    verify_plain_hmac,
    verify_signature_header,
)


def test_api_key_generation_and_verification():
    plaintext, prefix, key_hash = generate_api_key("test")
    assert plaintext.startswith("pk_test_")
    assert api_key_prefix(plaintext) == prefix
    assert key_hash == hash_api_key(plaintext)
    assert verify_api_key(plaintext, key_hash)
    assert not verify_api_key(plaintext[:-1] + "x", key_hash)
    assert api_key_prefix("not-a-key") is None
    assert api_key_prefix("pk_test_short") is None


def test_signature_header_roundtrip():
    body = b'{"id":"pay_1","status":"captured"}'
    header = build_signature_header("secret", body, timestamp=1_700_000_000)
    ts, sigs = parse_signature_header(header)
    assert ts == 1_700_000_000
    assert sigs == [sign_payload("secret", 1_700_000_000, body)]
    verify_signature_header("secret", body, header, now=1_700_000_010)


def test_signature_rejects_tampered_body_and_wrong_secret():
    body = b'{"amount_minor":150000}'
    header = build_signature_header("secret", body, timestamp=1_700_000_000)
    with pytest.raises(SignatureError, match="mismatch"):
        verify_signature_header("secret", b'{"amount_minor":1}', header, now=1_700_000_000)
    with pytest.raises(SignatureError, match="mismatch"):
        verify_signature_header("other", body, header, now=1_700_000_000)


def test_signature_rejects_stale_timestamp():
    body = b"{}"
    header = build_signature_header("secret", body, timestamp=1_700_000_000)
    with pytest.raises(SignatureError, match="tolerance"):
        verify_signature_header("secret", body, header, tolerance_seconds=300, now=1_700_000_301)
    # Clock skew within tolerance is accepted in both directions.
    verify_signature_header("secret", body, header, tolerance_seconds=300, now=1_699_999_800)


@pytest.mark.parametrize("header", [None, "", "garbage", "t=abc,v1=00", "v1=00", "t=1"])
def test_signature_rejects_malformed_headers(header):
    with pytest.raises(SignatureError):
        verify_signature_header("secret", b"{}", header, now=1)


def test_plain_hmac_verification():
    from app.core.security import hmac_hex

    body = b'{"trxID":"TRX1"}'
    verify_plain_hmac("s", body, hmac_hex("s", body))
    verify_plain_hmac("s", body, hmac_hex("s", body).upper())
    with pytest.raises(SignatureError):
        verify_plain_hmac("s", body, "deadbeef")
    with pytest.raises(SignatureError):
        verify_plain_hmac("s", body, None)
