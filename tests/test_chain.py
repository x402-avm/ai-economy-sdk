"""Contract: ai_economy_sdk.chain.ChainReader - indexer queries for subscriptions and usage discounts."""

import logging
import time
import urllib.error

import pytest
from algosdk.error import IndexerHTTPError

from ai_economy_sdk.chain import ChainReader

PAY_TO = "PAYTO"
USER = "USER"
NOW = 1_700_000_000


class FakeIndexer:
    """Mimics algosdk IndexerClient.search_transactions / account_info, with pagination."""

    def __init__(self, pages, account=None):
        self.pages, self.account, self.calls = pages, account, []

    def search_transactions(self, **kw):
        self.calls.append(kw)
        i = int(kw.get("next_page") or 0)
        page = {"transactions": self.pages[i]}
        if i + 1 < len(self.pages):
            page["next-token"] = str(i + 1)
        return page

    def account_info(self, address, **kw):
        return {"account": self.account or {}}


def pay(amount, receiver=PAY_TO, t=NOW - 3600):
    return {
        "tx-type": "pay",
        "round-time": t,
        "payment-transaction": {"receiver": receiver, "amount": amount},
    }


def axfer(amount, asset, receiver=PAY_TO, t=NOW - 3600):
    return {
        "tx-type": "axfer",
        "round-time": t,
        "asset-transfer-transaction": {"receiver": receiver, "amount": amount, "asset-id": asset},
    }


def test_usage_algo_paginates_and_filters():
    fake = FakeIndexer([[pay(1000), pay(500, receiver="OTHER")], [pay(2000)], [pay(7)]])
    r = ChainReader(indexer=fake)
    assert r.past_usage(USER, PAY_TO, asset_id=0, days=30, now=NOW) == 3007
    assert len(fake.calls) == 3
    first = fake.calls[0]
    assert (
        first["address"] == USER
        and first["address_role"] == "sender"
        and first["txn_type"] == "pay"
    )
    assert first["start_time"].endswith("Z") and first["start_time"].startswith(
        "2023-10-15"
    )  # NOW-30d in UTC


def test_usage_asa_filters_asset():
    fake = FakeIndexer([[axfer(100, 1003), axfer(999, 42), axfer(5, 1003, receiver="OTHER")]])
    r = ChainReader(indexer=fake)
    assert r.past_usage(USER, PAY_TO, asset_id=1003, days=30, now=NOW) == 100
    assert fake.calls[0]["txn_type"] == "axfer" and fake.calls[0]["asset_id"] == 1003


class Boom:
    """Indexer that fails like a real one: HTTP error or connection refused."""

    def __init__(self, exc):
        self.exc = exc

    def search_transactions(self, **kw):
        raise self.exc

    def account_info(self, address):
        raise self.exc


DOWN = [IndexerHTTPError("503 down"), urllib.error.URLError("connection refused")]


@pytest.mark.parametrize("exc", DOWN)
def test_usage_indexer_error_returns_zero_and_logs(exc, caplog):
    with caplog.at_level(logging.WARNING, logger="ai_economy_sdk"):
        assert ChainReader(indexer=Boom(exc)).past_usage(USER, PAY_TO, 0, now=NOW) == 0
    assert "past_usage lookup" in caplog.text


@pytest.mark.parametrize("exc", DOWN)
def test_subscription_and_pass_lookup_errors_deny_and_log(exc, caplog):
    r = ChainReader(indexer=Boom(exc))
    with caplog.at_level(logging.WARNING, logger="ai_economy_sdk"):
        assert r.has_active_subscription(USER, 77, now=NOW) is False
        assert r.active_passes(USER, PAY_TO, 0, {"svc": 1}, now=NOW) == []
    assert "subscription lookup" in caplog.text
    assert "pass lookup" in caplog.text


def test_programming_errors_are_not_swallowed():
    with pytest.raises(ZeroDivisionError):
        ChainReader(indexer=Boom(ZeroDivisionError())).past_usage(USER, PAY_TO, 0, now=NOW)


def test_subscription_requires_balance_and_recent_receipt():
    acct = {"assets": [{"asset-id": 77, "amount": 1}]}
    recent = FakeIndexer([[axfer(1, 77, receiver=USER, t=NOW - 5 * 86400)]], account=acct)
    assert (
        ChainReader(indexer=recent).has_active_subscription(USER, 77, max_age_days=30, now=NOW)
        is True
    )
    old = FakeIndexer([[axfer(1, 77, receiver=USER, t=NOW - 40 * 86400)]], account=acct)
    assert (
        ChainReader(indexer=old).has_active_subscription(USER, 77, max_age_days=30, now=NOW)
        is False
    )
    nobal = FakeIndexer(
        [[axfer(1, 77, receiver=USER, t=NOW - 86400)]],
        account={"assets": [{"asset-id": 77, "amount": 0}]},
    )
    assert ChainReader(indexer=nobal).has_active_subscription(USER, 77, now=NOW) is False


def test_subscription_disabled_cases():
    r = ChainReader(indexer=FakeIndexer([[]]))
    assert r.has_active_subscription(USER, None, now=NOW) is False
    assert r.has_active_subscription(USER, 0, now=NOW) is False
    assert r.has_active_subscription("", 77, now=NOW) is False


def test_default_now_is_wallclock():
    fake = FakeIndexer([[pay(1, t=int(time.time()) - 10)]])
    assert ChainReader(indexer=fake).past_usage(USER, PAY_TO, 0) == 1
