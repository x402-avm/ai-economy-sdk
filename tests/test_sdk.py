"""Contract: ai_economy_sdk.AIEconomySDK - decorator-based paid FastMCP server builder (offline)."""

from __future__ import annotations

import base64
import json
import warnings

import pytest
from algosdk import account
from starlette.testclient import TestClient

from ai_economy_sdk import MAINNET, AIEconomySDK, ConfigurationError, DefaultAvmSigner

SPK, PAY_TO = account.generate_account()
USER = account.generate_account()[1]
ACCEPT = {"accept": "application/json, text/event-stream"}


def sdk(**kw) -> AIEconomySDK:
    return AIEconomySDK(
        server_signer=DefaultAvmSigner(SPK),
        network="mainnet",
        asset_id=0,
        pay_to_address=PAY_TO,
        **kw,
    )


def call(name, args=None):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    }


def quoted_amount(app, name, args=None) -> str | None:
    with TestClient(app) as c:
        r = c.post("/mcp", json=call(name, args), headers=ACCEPT)
    if r.status_code != 402:
        return None
    return str(json.loads(base64.b64decode(r.headers["payment-required"]))["accepts"][0]["amount"])


def test_constructor_resolves_network_and_normalizes_asset():
    s = sdk(subscription_asset_id=77, name="svc")
    assert s.network == MAINNET
    assert s.asset_id == "0"
    assert s.chain is None
    assert s.mcp.name == "svc"


def test_two_and_one_argument_pricing_functions():
    s = sdk()
    seen = {}

    @s.add_tool("two", "two-arg pricing", lambda args, sender: seen.setdefault("s", sender) or 5000)
    def two(x: int = 1) -> int:
        return x

    @s.add_tool("one", "one-arg pricing", lambda args: 3000 + args.get("x", 0))
    def one(x: int = 0) -> int:
        return x

    def with_default(args, sender=None):
        return 4000

    @s.add_tool("dflt", "default sender", with_default)
    def dflt() -> int:
        return 0

    app = s.build_app(transport="streamable-http")
    assert quoted_amount(app, "two") == "5000"
    assert "s" in seen and seen["s"] is None  # unverified caller
    assert quoted_amount(app, "one", {"x": 7}) == "3007"
    assert quoted_amount(app, "dflt") == "4000"


def test_type_error_inside_pricing_function_is_not_retried_with_fewer_args():
    """0.4.x caught TypeError and called fn(args): a bug inside fn was hidden and fn ran twice."""
    s = sdk()
    calls = []

    def buggy(args, sender):
        calls.append((args, sender))
        raise TypeError("bug inside pricing")

    @s.add_tool("t", "buggy pricing", buggy)
    def t() -> str:
        return "x"

    app = s.build_app(transport="streamable-http")
    with TestClient(app, raise_server_exceptions=True) as c:
        with pytest.raises(TypeError, match="bug inside pricing"):
            c.post("/mcp", json=call("t"), headers=ACCEPT)
    assert len(calls) == 1


def test_pricing_function_with_unsupported_signature_is_rejected_at_registration():
    s = sdk()
    with pytest.raises(ConfigurationError, match="pricing_func"):
        s.add_tool("bad", "no args", lambda: 5000)
    with pytest.raises(ConfigurationError):
        s.add_tool("bad2", "three args", lambda a, b, c: 5000)
    with pytest.raises(ConfigurationError):
        s.add_tool("bad3", "not callable", 5000)  # type: ignore[arg-type]


def test_builtin_pricing_callable_without_signature_is_called_with_two_args():
    from ai_economy_sdk import _normalize_pricing

    fn = _normalize_pricing("x", max)  # max(args, sender) has no introspectable signature
    assert fn is max


def test_unpriced_tool_is_free():
    s = sdk()

    @s.mcp.tool()
    def plain() -> str:
        return "free"

    assert quoted_amount(s.build_app(transport="streamable-http"), "plain") is None


def test_build_app_default_transport_warns_and_serves_sse():
    s = sdk()
    with pytest.warns(FutureWarning, match="build_app"):
        app = s.build_app()
    assert "/sse" in {getattr(r, "path", None) for r in app.routes}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        app = sdk().build_app(transport="sse")
    assert "/sse" in {getattr(r, "path", None) for r in app.routes}


class FakeChain:
    def __init__(self):
        self.calls = []

    def has_active_subscription(self, address, asset_id, max_age_days=30.0, now=None, issuer=None):
        self.calls.append(("sub", address, asset_id, max_age_days))
        return True

    def past_usage(self, address, pay_to, asset_id, days=30, now=None):
        self.calls.append(("usage", address, pay_to, asset_id, days))
        return 1234


def test_subscription_and_usage_helpers():
    s = sdk(indexer_url="http://127.0.0.1:1", subscription_asset_id=77)
    assert s.chain is not None
    s.chain = FakeChain()
    assert s.has_active_subscription(USER, max_age_days=7) is True
    assert s.get_past_usage_amount(USER, days=3) == 1234
    assert s.chain.calls == [("sub", USER, 77, 7), ("usage", USER, PAY_TO, "0", 3)]
    assert s.has_active_subscription("") is False
    assert s.get_past_usage_amount("") == 0


def test_helpers_without_chain():
    s = sdk()
    assert s.has_active_subscription(USER) is False
    assert s.get_past_usage_amount(USER) == 0
