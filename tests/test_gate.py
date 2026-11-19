"""Contract: ai_economy_sdk.gate.PaymentGate - x402 payment gate in front of ANY FastMCP server (SSE transport).

Offline: no blockchain needed. A request is only *priced* here; settlement is covered by tests/test_e2e_localnet.py.
"""

import base64
import json

import pytest
from algosdk import account
from fastmcp import FastMCP
from starlette.testclient import TestClient

from ai_economy_sdk import MAINNET, DefaultAvmSigner, PaymentGate
from ai_economy_sdk.identity import sign_identity

NET = MAINNET
SPK, PAY_TO = account.generate_account()
UPK, USER = account.generate_account()
SESSION = "/messages/?session_id=0123456789abcdef0123456789abcdef"


def call(name, args=None, method="tools/call"):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if method == "tools/call":
        body["params"] = {"name": name, "arguments": args or {}}
    return body


def make_mcp():
    m = FastMCP("t")

    @m.tool()
    def echo(text: str) -> str:
        return text

    @m.tool()
    def free_tool() -> str:
        return "free"

    return m


class FakeChain:
    def __init__(self, sub=False, usage=0):
        self.sub, self.usage, self.sub_calls = sub, usage, []

    def has_active_subscription(self, address, asset_id, max_age_days=30.0, now=None):
        self.sub_calls.append((address, asset_id))
        return self.sub

    def past_usage(self, address, pay_to, asset_id, days=30, now=None):
        return self.usage


def gate(**kw):
    kw.setdefault("prices", {"echo": 1000, "free_tool": 0})
    kw.setdefault("allow_loss", True)
    return PaymentGate(signer=DefaultAvmSigner(SPK), network=NET, pay_to=PAY_TO, asset_id=0, **kw)


def required(resp):
    hdr = resp.headers.get("payment-required")
    assert hdr, (
        f"no payment-required header; status={resp.status_code} headers={dict(resp.headers)}"
    )
    return json.loads(base64.b64decode(hdr))["accepts"][0]


def post(app, body, headers=None):
    with TestClient(app) as c:
        return c.post(SESSION, json=body, headers=headers or {})


def test_paid_tool_returns_402_with_exact_requirements():
    r = post(gate().wrap(make_mcp()), call("echo", {"text": "hi"}))
    assert r.status_code == 402
    req = required(r)
    assert str(req["amount"]) == "1000"
    assert req["payTo"] == PAY_TO
    assert str(req["asset"]) == "0"
    assert req["network"] == NET


@pytest.mark.parametrize(
    "body",
    [
        call("free_tool"),
        call(None, method="tools/list"),
        call(None, method="initialize"),
        call("not_listed_tool"),
    ],
)
def test_free_or_non_tool_requests_pass_through(body):
    assert post(gate().wrap(make_mcp()), body).status_code != 402


def test_sse_endpoint_not_gated():
    app = gate().wrap(make_mcp())
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/sse" in paths


def test_callable_pricing():
    g = gate(
        prices=lambda tool, args, sender: 42 if tool == "echo" and args.get("text") == "x" else 7
    )
    assert g.price_for("echo", {"text": "x"}, None) == 42
    assert g.price_for("echo", {"text": "y"}, None) == 7
    assert str(required(post(g.wrap(make_mcp()), call("echo", {"text": "x"})))["amount"]) == "42"


def test_verified_subscriber_is_free():
    chain = FakeChain(sub=True)
    g = gate(chain=chain, subscription_asset_id=77)
    r = post(g.wrap(make_mcp()), call("echo", {"text": "hi"}), headers=sign_identity(UPK))
    assert r.status_code != 402
    assert chain.sub_calls and chain.sub_calls[-1] == (USER, 77)


def test_spoofed_subscriber_header_still_pays():
    chain = FakeChain(sub=True)
    g = gate(chain=chain, subscription_asset_id=77)
    r = post(g.wrap(make_mcp()), call("echo", {"text": "hi"}), headers={"x-sender-address": USER})
    assert r.status_code == 402
    assert chain.sub_calls == []  # unverified identity must never reach the chain lookups


def test_bulk_discount_for_verified_power_user():
    g = gate(chain=FakeChain(usage=60_000), bulk_threshold=50_000, bulk_discount=0.5)
    assert g.price_for("echo", {}, USER) == 500
    assert g.price_for("echo", {}, None) == 1000
    r = post(g.wrap(make_mcp()), call("echo", {"text": "hi"}), headers=sign_identity(UPK))
    assert str(required(r)["amount"]) == "500"


def test_below_threshold_no_discount():
    g = gate(chain=FakeChain(usage=10), bulk_threshold=50_000)
    assert g.price_for("echo", {}, USER) == 1000
