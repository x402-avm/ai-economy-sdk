"""Tests for facilitator fee floor in PaymentGate."""

import pytest
from algosdk import account

from ai_economy_sdk import MAINNET, DefaultAvmSigner, PaymentGate

NET = MAINNET
SPK, PAY_TO = account.generate_account()
UPK, USER = account.generate_account()


class FakeChain:
    def __init__(self, usage=0):
        self.usage = usage

    def has_active_subscription(self, address, asset_id, max_age_days=30.0, now=None):
        return False

    def past_usage(self, address, pay_to, asset_id, days=30, now=None):
        return self.usage


def gate(**kw):
    return PaymentGate(signer=DefaultAvmSigner(SPK), network=NET, pay_to=PAY_TO, asset_id=0, **kw)


def test_fee_floor_init_raises_value_error():
    # a) PaymentGate(prices={"t": 1500}) raises ValueError mentioning "does not cover".
    with pytest.raises(ValueError, match="does not cover"):
        gate(prices={"t": 1500})


def test_fee_floor_allow_loss():
    # b) allow_loss=True with 1500 -> no error, price_for -> 1500.
    g = gate(prices={"t": 1500}, allow_loss=True)
    assert g.price_for("t") == 1500


def test_fee_floor_bulk_discount_clamping():
    # c) prices={"t": 5000}, bulk discount 0.5 for a power user via FakeChain(usage huge) and bulk_threshold=1 -> price_for
    #    returns 2500 (above floor); with prices {"t": 3000} and discount 0.5 -> 1500 would be below floor -> returns 2001.
    chain = FakeChain(usage=1_000_000)
    g1 = gate(prices={"t": 5000}, chain=chain, bulk_threshold=1, bulk_discount=0.5)
    assert g1.price_for("t", sender=USER) == 2500

    g2 = gate(prices={"t": 3000}, chain=chain, bulk_threshold=1, bulk_discount=0.5)
    assert g2.price_for("t", sender=USER) == 2001


def test_fee_floor_zero_stays_zero():
    # d) price 0 stays 0.
    g = gate(prices={"free_tool": 0})
    assert g.price_for("free_tool") == 0
