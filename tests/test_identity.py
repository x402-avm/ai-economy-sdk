"""Contract: ai_economy_sdk.identity - proof that the caller owns an Algorand address (no payment needed)."""

from algosdk import account

from ai_economy_sdk.identity import IDENTITY_HEADERS, sign_identity, verify_identity

PK, ADDR = account.generate_account()


def test_headers_names():
    assert IDENTITY_HEADERS == ("x-sender-address", "x-sender-timestamp", "x-sender-signature")


def test_roundtrip():
    h = sign_identity(PK, now=1_000_000)
    assert set(h) == set(IDENTITY_HEADERS)
    assert h["x-sender-address"] == ADDR
    assert verify_identity(h, now=1_000_010) == ADDR


def test_case_insensitive_mapping():
    h = {k.upper(): v for k, v in sign_identity(PK, now=50).items()}
    assert verify_identity(h, now=50) == ADDR


def test_spoofed_address_rejected():
    _, other = account.generate_account()
    h = sign_identity(PK, now=100)
    h["x-sender-address"] = other
    assert verify_identity(h, now=100) is None


def test_address_only_header_rejected():
    assert verify_identity({"x-sender-address": ADDR}, now=100) is None


def test_stale_and_future_rejected():
    h = sign_identity(PK, now=1000)
    assert verify_identity(h, now=1000 + 121) is None  # default max_skew=120
    assert verify_identity(h, now=1000 - 121) is None
    assert verify_identity(h, now=1000 + 300, max_skew=600) == ADDR


def test_garbage_rejected():
    h = sign_identity(PK, now=100)
    for k, v in [
        ("x-sender-signature", "not-base64!!"),
        ("x-sender-timestamp", "abc"),
        ("x-sender-address", "XYZ"),
    ]:
        bad = dict(h)
        bad[k] = v
        assert verify_identity(bad, now=100) is None
