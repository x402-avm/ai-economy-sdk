"""Offline and LocalNet tests for browser identity proof transaction."""

import base64
import time

import pytest
from algosdk import account, encoding, transaction
from algosdk.atomic_transaction_composer import AccountTransactionSigner
from algosdk.v2client import algod
from conftest import get_localnet_context

from ai_economy_sdk.identity import (
    make_identity_txn,
    sign_identity_txn,
    verify_identity,
    verify_identity_txn,
)

GENESIS_HASH = "qNF74lmmiHUCf7s3oewhYsMzOCOEptuJ9ytCk5gBPGI="
OTHER_GENESIS = "dGVzdHRlc3R0ZXN0dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="


def test_valid_roundtrip():
    sk, addr = account.generate_account()
    now_ts = 1700000000
    headers = sign_identity_txn(sk, GENESIS_HASH, ts=now_ts)
    assert "X-Identity-Txn" in headers

    # Verify with matching timestamp
    res = verify_identity_txn(
        headers["X-Identity-Txn"], GENESIS_HASH, max_skew=120, now=now_ts + 60
    )
    assert res == addr

    # Verify with default now (wall clock)
    fresh_headers = sign_identity_txn(sk, GENESIS_HASH)
    res_fresh = verify_identity_txn(fresh_headers["X-Identity-Txn"], GENESIS_HASH)
    assert res_fresh == addr


def test_wrong_genesis():
    sk, addr = account.generate_account()
    headers = sign_identity_txn(sk, GENESIS_HASH)
    res = verify_identity_txn(headers["X-Identity-Txn"], OTHER_GENESIS)
    assert res is None


def test_fee_non_zero():
    sk, addr = account.generate_account()
    sp = transaction.SuggestedParams(
        fee=1000, first=1, last=1000, gh=GENESIS_HASH, gen="", flat_fee=True
    )
    txn = transaction.PaymentTxn(
        sender=addr,
        sp=sp,
        receiver=addr,
        amt=0,
        note=f"x402-identity:{addr}:{int(time.time())}".encode(),
    )
    signer = AccountTransactionSigner(sk)
    stxn = signer.sign_transactions([txn], [0])[0]
    hdr = encoding.msgpack_encode(stxn)
    assert verify_identity_txn(hdr, GENESIS_HASH) is None


def test_amount_non_zero():
    sk, addr = account.generate_account()
    sp = transaction.SuggestedParams(
        fee=0, first=1, last=1000, gh=GENESIS_HASH, gen="", flat_fee=True
    )
    txn = transaction.PaymentTxn(
        sender=addr,
        sp=sp,
        receiver=addr,
        amt=1,
        note=f"x402-identity:{addr}:{int(time.time())}".encode(),
    )
    signer = AccountTransactionSigner(sk)
    stxn = signer.sign_transactions([txn], [0])[0]
    hdr = encoding.msgpack_encode(stxn)
    assert verify_identity_txn(hdr, GENESIS_HASH) is None


def test_receiver_not_sender():
    sk, addr = account.generate_account()
    _, other_addr = account.generate_account()
    sp = transaction.SuggestedParams(
        fee=0, first=1, last=1000, gh=GENESIS_HASH, gen="", flat_fee=True
    )
    txn = transaction.PaymentTxn(
        sender=addr,
        sp=sp,
        receiver=other_addr,
        amt=0,
        note=f"x402-identity:{addr}:{int(time.time())}".encode(),
    )
    signer = AccountTransactionSigner(sk)
    stxn = signer.sign_transactions([txn], [0])[0]
    hdr = encoding.msgpack_encode(stxn)
    assert verify_identity_txn(hdr, GENESIS_HASH) is None


def test_rekey_set():
    sk, addr = account.generate_account()
    _, rekey_addr = account.generate_account()
    sp = transaction.SuggestedParams(
        fee=0, first=1, last=1000, gh=GENESIS_HASH, gen="", flat_fee=True
    )
    txn = transaction.PaymentTxn(
        sender=addr,
        sp=sp,
        receiver=addr,
        amt=0,
        note=f"x402-identity:{addr}:{int(time.time())}".encode(),
        rekey_to=rekey_addr,
    )
    signer = AccountTransactionSigner(sk)
    stxn = signer.sign_transactions([txn], [0])[0]
    hdr = encoding.msgpack_encode(stxn)
    assert verify_identity_txn(hdr, GENESIS_HASH) is None


def test_close_to_set():
    sk, addr = account.generate_account()
    _, close_addr = account.generate_account()
    sp = transaction.SuggestedParams(
        fee=0, first=1, last=1000, gh=GENESIS_HASH, gen="", flat_fee=True
    )
    txn = transaction.PaymentTxn(
        sender=addr,
        sp=sp,
        receiver=addr,
        amt=0,
        note=f"x402-identity:{addr}:{int(time.time())}".encode(),
        close_remainder_to=close_addr,
    )
    signer = AccountTransactionSigner(sk)
    stxn = signer.sign_transactions([txn], [0])[0]
    hdr = encoding.msgpack_encode(stxn)
    assert verify_identity_txn(hdr, GENESIS_HASH) is None


def test_note_for_another_address():
    sk, addr = account.generate_account()
    _, other_addr = account.generate_account()
    sp = transaction.SuggestedParams(
        fee=0, first=1, last=1000, gh=GENESIS_HASH, gen="", flat_fee=True
    )
    txn = transaction.PaymentTxn(
        sender=addr,
        sp=sp,
        receiver=addr,
        amt=0,
        note=f"x402-identity:{other_addr}:{int(time.time())}".encode(),
    )
    signer = AccountTransactionSigner(sk)
    stxn = signer.sign_transactions([txn], [0])[0]
    hdr = encoding.msgpack_encode(stxn)
    assert verify_identity_txn(hdr, GENESIS_HASH) is None


def test_stale_timestamp():
    sk, addr = account.generate_account()
    now_ts = 1700000000
    stale_ts = now_ts - 200  # max_skew = 120
    headers = sign_identity_txn(sk, GENESIS_HASH, ts=stale_ts)
    assert (
        verify_identity_txn(headers["X-Identity-Txn"], GENESIS_HASH, max_skew=120, now=now_ts)
        is None
    )


def test_future_timestamp():
    sk, addr = account.generate_account()
    now_ts = 1700000000
    future_ts = now_ts + 200  # max_skew = 120
    headers = sign_identity_txn(sk, GENESIS_HASH, ts=future_ts)
    assert (
        verify_identity_txn(headers["X-Identity-Txn"], GENESIS_HASH, max_skew=120, now=now_ts)
        is None
    )


def test_tampered_note_after_signing():
    sk, addr = account.generate_account()
    now_ts = 1700000000
    headers = sign_identity_txn(sk, GENESIS_HASH, ts=now_ts)
    raw_b64 = headers["X-Identity-Txn"]
    stxn = encoding.msgpack_decode(raw_b64)

    # Tamper the note inside the signed transaction dictionary
    d = stxn.dictify()
    d["txn"]["note"] = f"x402-identity:{addr}:{now_ts + 1}".encode()
    tampered_b64 = encoding.msgpack_encode(d)

    # Signature verification must fail and return None
    assert verify_identity_txn(tampered_b64, GENESIS_HASH, now=now_ts) is None


def test_garbage_rejected():
    for garbage in ["", "not-base64-msgpack", "{}", base64.b64encode(b"invalid-bytes").decode()]:
        assert verify_identity_txn(garbage, GENESIS_HASH) is None


def test_signed_by_different_key_than_sender():
    _, addr1 = account.generate_account()
    sk2, _ = account.generate_account()
    now_ts = int(time.time())
    sp = transaction.SuggestedParams(
        fee=0, first=1, last=1000, gh=GENESIS_HASH, gen="", flat_fee=True
    )
    txn = transaction.PaymentTxn(
        sender=addr1, sp=sp, receiver=addr1, amt=0, note=f"x402-identity:{addr1}:{now_ts}".encode()
    )
    signer = AccountTransactionSigner(sk2)
    stxn = signer.sign_transactions([txn], [0])[0]
    hdr = encoding.msgpack_encode(stxn)
    assert verify_identity_txn(hdr, GENESIS_HASH) is None


def test_verify_identity_wrapper_supports_identity_txn():
    sk, addr = account.generate_account()
    headers = sign_identity_txn(sk, GENESIS_HASH)
    # Case-insensitive header check
    res = verify_identity(
        {"x-identity-txn": headers["X-Identity-Txn"]}, genesis_hash_b64=GENESIS_HASH
    )
    assert res == addr

    # Legacy header fallback
    from ai_economy_sdk.identity import _sign_legacy_identity as sign_identity

    legacy_headers = sign_identity(sk)
    res_legacy = verify_identity(legacy_headers, genesis_hash_b64=GENESIS_HASH)
    assert res_legacy == addr


@pytest.mark.localnet
def test_localnet_algod_rejects_identity_txn_fee_too_low():
    """LocalNet test: submitting the identity txn to algod is REJECTED (fee too low)."""
    cfg = get_localnet_context()
    algod_client = algod.AlgodClient(cfg["algod_token"], cfg["algod_url"])
    client1_pk = cfg["accounts"]["client1"]["private_key"]
    client1_addr = cfg["accounts"]["client1"]["address"]

    # Build identity txn with current round parameters to isolate the fee rejection
    sp = algod_client.suggested_params()
    txn = make_identity_txn(client1_addr, cfg["genesis_hash"], sp=sp)
    assert txn.fee == 0

    signer = AccountTransactionSigner(client1_pk)
    stxn = signer.sign_transactions([txn], [0])[0]

    with pytest.raises(Exception) as exc_info:
        algod_client.send_transaction(stxn)

    err_text = str(exc_info.value)
    print(f"Algod rejection error: {err_text}")
    assert (
        "fees is less than" in err_text
        or "TransactionPool.Remember" in err_text
        or "fee" in err_text.lower()
    )


def test_mutated_proofs_never_raise():
    """verify_identity_txn() gets untrusted input: corrupted proofs return None, never raise."""
    import base64
    import random

    sk, addr = account.generate_account()
    raw = base64.b64decode(sign_identity_txn(sk, GENESIS_HASH, ts=1700000000)["X-Identity-Txn"])
    rng = random.Random(1234)
    for _ in range(1500):
        b = bytearray(raw)
        for _ in range(rng.randint(1, 4)):
            op = rng.random()
            if op < 0.5:
                b[rng.randrange(len(b))] = rng.randrange(256)
            elif op < 0.75 and len(b) > 1:
                del b[rng.randrange(len(b))]
            else:
                b.insert(rng.randrange(len(b) + 1), rng.randrange(256))
        out = verify_identity_txn(base64.b64encode(bytes(b)).decode(), GENESIS_HASH, now=1700000000)
        assert out in (None, addr)
