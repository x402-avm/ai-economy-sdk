"""Contract: ai_economy_sdk.signer.DefaultAvmSigner."""

import base64
import warnings

import pytest
from algosdk import account, encoding, transaction

from ai_economy_sdk import AlgodClientRequiredError, PaymentVerificationError
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


def test_algod_errors_are_typed_and_backward_compatible():
    s = DefaultAvmSigner(PK)
    for call in (
        lambda: s.send_group([_txn_bytes()], "algorand:x"),
        lambda: s.simulate_group([_txn_bytes()], "algorand:x"),
        lambda: s.confirm_transaction("TXID", "algorand:x"),
    ):
        with pytest.raises(AlgodClientRequiredError):
            call()


def test_simulation_failure_raises_payment_verification_error():
    class FakeAlgod:
        def simulate_transactions(self, req):
            return {"txn-groups": [{"failure-message": "overspend"}]}

    s = DefaultAvmSigner(PK, algod_client=FakeAlgod())
    with pytest.raises(PaymentVerificationError, match="overspend") as ei:
        s.simulate_group([_txn_bytes()], "algorand:x")
    assert isinstance(ei.value, RuntimeError)


def test_simulate_send_confirm_delegate_to_algod(monkeypatch):
    calls = {}

    class FakeAlgod:
        def simulate_transactions(self, req):
            calls["sim"] = req
            return {"txn-groups": [{}]}

        def send_transactions(self, txns):
            calls["send"] = txns
            return "TXID"

    s = DefaultAvmSigner(PK, algod_client=FakeAlgod())
    assert s.simulate_group([_txn_bytes()], "n") == {"txn-groups": [{}]}
    assert s.send_group([_txn_bytes()], "n") == "TXID"
    monkeypatch.setattr(
        "ai_economy_sdk.signer.transaction.wait_for_confirmation",
        lambda client, txid, rounds: {"confirmed-round": 5, "txid": txid},
    )
    assert s.confirm_transaction("TXID", "n")["confirmed-round"] == 5


def test_sign_transaction_and_group():
    s = DefaultAvmSigner(PK)
    one = s.sign_transaction(_txn_bytes(), ADDR, "n")
    signed = encoding.msgpack_decode(base64.b64encode(one).decode())
    assert isinstance(signed, transaction.SignedTransaction)
    group = s.sign_group([_txn_bytes(), _txn_bytes()], ADDR, [0], "n")
    assert group[1] == _txn_bytes() and group[0] != _txn_bytes()


def test_signing_uses_no_deprecated_algosdk_api():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        DefaultAvmSigner(PK).sign_transactions([_txn_bytes()], [0])


def test_private_key_is_not_public_and_repr_does_not_leak():
    s = DefaultAvmSigner(PK)
    assert PK not in repr(s) and PK not in str(s)
    assert ADDR in repr(s)
    assert "private_key" not in vars(s)
    with pytest.deprecated_call(match="private_key"):
        assert s.private_key == PK
    with pytest.raises(AttributeError):
        s.private_key = "other"  # read-only
