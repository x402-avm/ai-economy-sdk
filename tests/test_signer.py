"""Contract: ai_economy_sdk.signer.DefaultAvmSigner."""

import base64

import pytest
from algosdk import account, encoding, transaction

from ai_economy_sdk.signer import DefaultAvmSigner

PK, ADDR = account.generate_account()


def _txn_bytes():
    sp = transaction.SuggestedParams(fee=1000, first=1, last=1000, gh="dGVzdA==", flat_fee=True)
    t = transaction.PaymentTxn(ADDR, sp, ADDR, 1)
    return base64.b64decode(encoding.msgpack_encode(t))


def test_addresses():
    assert DefaultAvmSigner(PK).get_addresses() == [ADDR]


def test_sign_transactions_only_requested_indexes():
    s = DefaultAvmSigner(PK)
    out = s.sign_transactions([_txn_bytes(), _txn_bytes()], [1])
    assert out[0] is None
    signed = encoding.msgpack_decode(base64.b64encode(out[1]).decode())
    assert isinstance(signed, transaction.SignedTransaction) and signed.transaction.sender == ADDR


def test_network_ops_without_algod_raise():
    s = DefaultAvmSigner(PK)
    with pytest.raises(RuntimeError):
        s.send_group([_txn_bytes()], "algorand:x")
    with pytest.raises(RuntimeError):
        s.simulate_group([_txn_bytes()], "algorand:x")
