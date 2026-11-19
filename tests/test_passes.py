"""Contract: vendor-scoped passes. A pass is a receipt (payment to THIS gate's pay_to with note x402-pass:<scope>).

Only a receipt for one of the scopes the gate knows makes calls free; a receipt for another vendor/service does not.
"""

import base64

from algosdk import account

from ai_economy_sdk import MAINNET, PASS_NOTE_PREFIX, ChainReader, DefaultAvmSigner, PaymentGate

NET = MAINNET
SPK, PAY_TO = account.generate_account()
_, OTHER_VENDOR = account.generate_account()
_, BUYER = account.generate_account()
NOW = 1_800_000_000


def receipt(scope, amount, receiver=PAY_TO, age_days=1.0, txid="T"):
    return {
        "id": txid,
        "round-time": NOW - age_days * 86400,
        "tx-type": "pay",
        "note": base64.b64encode(f"{PASS_NOTE_PREFIX}{scope}".encode()).decode(),
        "payment-transaction": {"receiver": receiver, "amount": amount},
    }


class FakeIndexer:
    def __init__(self, txns):
        self.txns = txns

    def search_transactions(self, **kw):
        return {"transactions": self.txns}


class FrozenClockReader(ChainReader):
    """The gate uses the wall clock; pin it so the fixed receipts above have a known age."""

    def active_passes(self, address, pay_to, asset_id, scopes, days=30.0, now=None):
        return super().active_passes(address, pay_to, asset_id, scopes, days, NOW)


def gate_with(txns, scopes=None):
    chain = FrozenClockReader(indexer=FakeIndexer(txns))
    return PaymentGate(
        signer=DefaultAvmSigner(SPK),
        network=NET,
        pay_to=PAY_TO,
        asset_id=0,
        prices={"echo": 5000},
        default_price=5000,
        chain=chain,
        pass_scopes=scopes or {"listing:tts-1": 1_000_000, "vendor:vendorA": 4_000_000},
        allow_loss=True,
    )


def test_service_pass_receipt_makes_the_call_free():
    g = gate_with([receipt("listing:tts-1", 1_000_000)])
    assert g.price_for("echo", {}, BUYER) == 0


def test_vendor_pass_receipt_makes_the_call_free():
    g = gate_with([receipt("vendor:vendorA", 4_000_000)])
    assert g.price_for("echo", {}, BUYER) == 0


def test_receipt_for_another_service_does_not_count():
    g = gate_with([receipt("listing:sandbox_mcp-1", 1_000_000)])
    assert g.price_for("echo", {}, BUYER) == 5000


def test_receipt_for_another_vendor_does_not_count():
    g = gate_with([receipt("vendor:vendorB", 4_000_000)])
    assert g.price_for("echo", {}, BUYER) == 5000


def test_receipt_paid_to_a_different_vendor_does_not_count():
    g = gate_with([receipt("listing:tts-1", 1_000_000, receiver=OTHER_VENDOR)])
    assert g.price_for("echo", {}, BUYER) == 5000


def test_underpaid_or_expired_receipts_do_not_count():
    g = gate_with(
        [receipt("listing:tts-1", 999_999), receipt("vendor:vendorA", 4_000_000, age_days=31)]
    )
    assert g.price_for("echo", {}, BUYER) == 5000


def test_no_sender_means_no_pass():
    g = gate_with([receipt("listing:tts-1", 1_000_000)])
    assert g.price_for("echo", {}, None) == 5000


def test_gate_without_pass_scopes_ignores_receipts():
    g = gate_with([receipt("listing:tts-1", 1_000_000)])
    g.pass_scopes = {}
    assert g.price_for("echo", {}, BUYER) == 5000
