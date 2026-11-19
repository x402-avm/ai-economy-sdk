"""Contract: PaymentGate gates MCP tool calls on every HTTP transport and fails closed.

Offline. The x402 facilitator is replaced by stubs (payment "verified" when a
PAYMENT-SIGNATURE header is present, settlement recorded), so these tests cover the
gate's routing, settle-on-success and fail-closed behaviour, not the Algorand payment
itself (see tests/test_e2e_localnet.py for that).
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
import warnings

import httpx
import pytest
import uvicorn
from algosdk import account
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient
from x402.http.types import HTTPProcessResult, ProcessSettleResult
from x402.schemas import PaymentRequirements

from ai_economy_sdk import (
    MAINNET,
    ConfigurationError,
    DefaultAvmSigner,
    PaymentGate,
    PaymentRequiredError,
    SettleOnSuccessMiddleware,
)

SPK, PAY_TO = account.generate_account()
ACCEPT = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
PAID = {**ACCEPT, "payment-signature": "stub-signature"}


def make_mcp() -> FastMCP:
    m = FastMCP("transports")

    @m.tool()
    def ok(text: str = "hi") -> str:
        return f"ok:{text}"

    @m.tool()
    def boom() -> str:
        raise ValueError("deliberate tool failure")

    @m.tool()
    def free_tool() -> str:
        return "free"

    return m


class StubbedGate:
    """A PaymentGate whose facilitator accepts any payment header and records settlements."""

    def __init__(self, settle_ok: bool = True, **kw):
        kw.setdefault("prices", {"ok": 5000, "boom": 5000, "free_tool": 0})
        self.gate = PaymentGate(
            signer=DefaultAvmSigner(SPK), network=MAINNET, pay_to=PAY_TO, asset_id=0, **kw
        )
        self.settled: list = []
        self.settle_ok = settle_ok
        original = self.gate.http_server.process_http_request

        async def process_http_request(context, paywall_config=None):
            if not context.payment_header:
                return await original(context, paywall_config)
            reqs = PaymentRequirements(
                scheme="exact",
                network=MAINNET,
                asset="0",
                amount="5000",
                pay_to=PAY_TO,
                max_timeout_seconds=300,
            )
            return HTTPProcessResult(
                type="payment-verified",
                payment_payload={"signature": context.payment_header},
                payment_requirements=reqs,
            )

        async def process_settlement(payload, requirements):
            self.settled.append(payload)
            if not self.settle_ok:
                return ProcessSettleResult(success=False, error_reason="insufficient balance")
            return ProcessSettleResult(success=True, headers={"PAYMENT-RESPONSE": "settled"})

        self.gate.http_server.process_http_request = process_http_request
        self.gate.http_server.process_settlement = process_settlement


def rpc(name, args=None, req_id=1, method="tools/call"):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if method == "tools/call":
        body["params"] = {"name": name, "arguments": args or {}}
    return body


def sse_json(resp: httpx.Response) -> dict:
    """Decode a Streamable HTTP response (JSON or a single-event SSE stream)."""
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise AssertionError(f"no JSON-RPC message in response: {resp.text!r}")


def required_amount(resp: httpx.Response) -> str:
    hdr = resp.headers["payment-required"]
    return str(json.loads(base64.b64decode(hdr))["accepts"][0]["amount"])


# --------------------------------------------------------------------------- Streamable HTTP


def test_streamable_http_unpaid_call_gets_402():
    app = StubbedGate().gate.wrap(make_mcp(), transport="streamable-http")
    with TestClient(app) as c:
        r = c.post("/mcp", json=rpc("ok", {"text": "x"}), headers=ACCEPT)
    assert r.status_code == 402
    assert required_amount(r) == "5000"


def test_streamable_http_paid_call_settles_and_returns_result():
    s = StubbedGate()
    app = s.gate.wrap(make_mcp(), transport="streamable-http")
    with TestClient(app) as c:
        r = c.post("/mcp", json=rpc("ok", {"text": "paid"}), headers=PAID)
    assert r.status_code == 200
    msg = sse_json(r)
    assert msg["result"]["content"][0]["text"] == "ok:paid"
    assert r.headers["payment-response"] == "settled"
    assert len(s.settled) == 1
    assert s.gate._pending == {}


def test_streamable_http_free_tool_and_list_need_no_payment():
    s = StubbedGate()
    app = s.gate.wrap(make_mcp(), transport="http")  # "http" is an alias
    with TestClient(app) as c:
        r = c.post("/mcp", json=rpc("free_tool"), headers=ACCEPT)
        assert r.status_code == 200
        assert sse_json(r)["result"]["content"][0]["text"] == "free"
        r = c.post("/mcp", json=rpc(None, method="tools/list"), headers=ACCEPT)
        assert r.status_code == 200
    assert s.settled == []


def test_streamable_http_failing_tool_is_not_settled_and_payment_is_reusable():
    s = StubbedGate()
    app = s.gate.wrap(make_mcp(), transport="streamable-http")
    with TestClient(app) as c:
        r = c.post("/mcp", json=rpc("boom"), headers=PAID)
        assert r.status_code == 200
        assert sse_json(r)["result"]["isError"] is True
        assert "payment-response" not in r.headers
        # The reservation was released, so the same payment can pay for a working call.
        r = c.post("/mcp", json=rpc("ok"), headers=PAID)
        assert sse_json(r)["result"]["content"][0]["text"] == "ok:hi"
    assert len(s.settled) == 1


def test_streamable_http_settlement_failure_withholds_output():
    s = StubbedGate(settle_ok=False)
    app = s.gate.wrap(make_mcp(), transport="streamable-http")
    with TestClient(app) as c:
        r = c.post("/mcp", json=rpc("ok", {"text": "secret"}), headers=PAID)
    msg = sse_json(r)
    assert msg["result"]["isError"] is True
    text = msg["result"]["content"][0]["text"]
    assert "payment settlement failed" in text and "secret" not in text
    assert s.gate._pending == {}
    # The reservation was released: the same payment is accepted once settlement works.
    s.settle_ok = True
    with TestClient(app) as c:
        r = c.post("/mcp", json=rpc("ok"), headers=PAID)
    assert sse_json(r)["result"]["content"][0]["text"] == "ok:hi"


def test_replayed_payment_is_rejected():
    s = StubbedGate()
    app = s.gate.wrap(make_mcp(), transport="streamable-http")
    with TestClient(app) as c:
        assert c.post("/mcp", json=rpc("ok"), headers=PAID).status_code == 200
        r = c.post("/mcp", json=rpc("ok"), headers=PAID)
    assert r.status_code == 402
    assert r.json() == {"error": "Replay detected"}
    assert len(s.settled) == 1


def test_batched_tool_calls_are_rejected():
    app = StubbedGate().gate.wrap(make_mcp(), transport="streamable-http")
    with TestClient(app) as c:
        r = c.post("/mcp", json=[rpc("ok"), rpc("free_tool", req_id=2)], headers=PAID)
    assert r.status_code == 400


def test_non_json_body_passes_to_server_which_rejects_it():
    s = StubbedGate()
    app = s.gate.wrap(make_mcp(), transport="streamable-http")
    with TestClient(app) as c:
        r = c.post("/mcp", content=b"not json", headers=ACCEPT)
    assert r.status_code >= 400
    assert s.settled == []


@pytest.mark.parametrize(
    ("transport", "path"),
    [
        ("streamable-http", "/svc/mcp"),
        ("sse", "/svc/messages/?session_id=0123456789abcdef0123456789abcdef"),
    ],
)
def test_custom_mount_path_is_still_gated(transport, path):
    """The x402 route table matches every path, so a mounted app cannot skip payment.

    0.4.x registered only "POST /messages*", so an app mounted under a prefix passed
    priced calls to the server without a payment.
    """
    inner = StubbedGate().gate.wrap(make_mcp(), transport=transport)
    outer = Starlette(routes=[Mount("/svc", app=inner)], lifespan=inner.router.lifespan_context)
    with TestClient(outer) as c:
        r = c.post(path, json=rpc("ok"), headers=ACCEPT)
    assert r.status_code == 402


def test_manual_middleware_on_streamable_http_app_is_gated():
    """0.4.x only gated paths ending in /messages; a Streamable HTTP app went unpaid."""
    s = StubbedGate()
    mcp = make_mcp()
    mcp.add_middleware(SettleOnSuccessMiddleware(s.gate))
    app = mcp.http_app(transport="http", stateless_http=True, middleware=s.gate.middleware())
    with TestClient(app) as c:
        assert c.post("/mcp", json=rpc("ok"), headers=ACCEPT).status_code == 402
        r = c.post("/mcp", json=rpc("ok"), headers=PAID)
        assert sse_json(r)["result"]["content"][0]["text"] == "ok:hi"
    assert len(s.settled) == 1


def test_middleware_without_settlement_refuses_priced_calls():
    s = StubbedGate()
    app = make_mcp().http_app(transport="http", stateless_http=True, middleware=s.gate.middleware())
    with TestClient(app) as c:
        r = c.post("/mcp", json=rpc("ok"), headers=PAID)
        assert r.status_code == 500
        assert c.post("/mcp", json=rpc("free_tool"), headers=ACCEPT).status_code == 200
    assert s.settled == []


def test_stateful_streamable_http_is_supported():
    s = StubbedGate()
    app = s.gate.wrap(make_mcp(), transport="streamable-http", stateless_http=False)
    with TestClient(app) as c:
        init = rpc(None, method="initialize")
        init["params"] = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        }
        r = c.post("/mcp", json=init, headers=ACCEPT)
        sid = r.headers["mcp-session-id"]
        hdr = {"mcp-session-id": sid}
        c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**ACCEPT, **hdr},
        )
        assert (
            c.post("/mcp", json=rpc("ok", req_id=2), headers={**ACCEPT, **hdr}).status_code == 402
        )
        r = c.post("/mcp", json=rpc("ok", req_id=3), headers={**PAID, **hdr})
        assert sse_json(r)["result"]["content"][0]["text"] == "ok:hi"
    assert len(s.settled) == 1


# --------------------------------------------------------------------------- fail closed


def test_tool_call_that_bypasses_http_gate_is_refused():
    """A priced tool reached without the HTTP gate (here: in-memory transport) is refused."""
    s = StubbedGate()
    mcp = make_mcp()
    s.gate.wrap(mcp, transport="streamable-http")

    async def run():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="payment required"):
                await client.call_tool("ok", {"text": "x"})
            res = await client.call_tool("free_tool", {})
            assert res.content[0].text == "free"

    asyncio.run(run())
    assert s.settled == []
    assert issubclass(PaymentRequiredError, ToolError)


def test_missing_reservation_is_refused():
    """A gated call whose reservation is gone (expired/used) is refused, not served free."""
    s = StubbedGate()
    s.gate._take_pending = lambda token: None
    app = s.gate.wrap(make_mcp(), transport="streamable-http")
    with TestClient(app) as c:
        r = c.post("/mcp", json=rpc("ok", {"text": "secret"}), headers=PAID)
    result = sse_json(r)["result"]
    assert result["isError"] is True
    assert "payment required" in result["content"][0]["text"]
    assert s.settled == []


def test_stale_reservations_expire_and_release(caplog):
    from ai_economy_sdk.gate import PendingPayment

    s = StubbedGate()
    assert s.gate.replay_store.reserve("h1")
    s.gate._pending["tok"] = PendingPayment(None, None, "h1", created_at=time.time() - 1000)
    with caplog.at_level("WARNING", logger="ai_economy_sdk"):
        s.gate._expire_pending()
    assert s.gate._pending == {}
    assert s.gate.replay_store.reserve("h1")  # released
    assert "expired" in caplog.text


# --------------------------------------------------------------------------- wrap() options


def test_wrap_default_transport_is_sse_with_future_warning():
    with pytest.warns(FutureWarning, match="streamable-http"):
        app = StubbedGate().gate.wrap(make_mcp())
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/sse" in paths


def test_wrap_explicit_transports_do_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        sse = StubbedGate().gate.wrap(make_mcp(), transport="sse")
        http = StubbedGate().gate.wrap(make_mcp(), transport="streamable-http")
    assert "/sse" in {getattr(r, "path", None) for r in sse.routes}
    assert "/mcp" in {getattr(r, "path", None) for r in http.routes}


def test_wrap_rejects_unknown_transport():
    with pytest.raises(ConfigurationError):
        StubbedGate().gate.wrap(make_mcp(), transport="websocket")  # type: ignore[arg-type]
    with pytest.raises(ValueError):  # ConfigurationError is a ValueError
        StubbedGate().gate.wrap(make_mcp(), transport="stdio")  # type: ignore[arg-type]


def test_sse_handshake_compat_is_only_installed_for_sse():
    m_http, m_sse = make_mcp(), make_mcp()
    StubbedGate().gate.wrap(m_http, transport="streamable-http")
    StubbedGate().gate.wrap(m_sse, transport="sse")
    StubbedGate().gate.wrap(m_sse, transport="sse")  # idempotent
    assert not getattr(m_http._mcp_server, "_ai_economy_stateless", False)
    assert m_sse._mcp_server._ai_economy_stateless is True


def test_wrap_adds_settlement_middleware_once():
    s = StubbedGate()
    mcp = make_mcp()
    s.gate.wrap(mcp, transport="streamable-http")
    s.gate.wrap(mcp, transport="streamable-http")
    assert sum(isinstance(m, SettleOnSuccessMiddleware) for m in mcp.middleware) == 1


# --------------------------------------------------------------------------- SSE over a socket


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def sse_server():
    s = StubbedGate()
    app = s.gate.wrap(make_mcp(), transport="sse")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield s, f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


def test_sse_paid_call_without_initialize_settles(sse_server):
    """SSE keeps the 0.4 behaviour: raw JSON-RPC clients may skip the MCP handshake."""
    s, base = sse_server

    async def run():
        async with httpx.AsyncClient(timeout=10) as c:
            async with c.stream("GET", base + "/sse") as stream:
                lines = stream.aiter_lines()
                endpoint = None
                async for line in lines:
                    if line.startswith("data: ") and "/messages" in line:
                        endpoint = line[6:].strip()
                        break
                url = base + endpoint
                r = await c.post(url, json=rpc("ok", {"text": "sse"}))
                assert r.status_code == 402
                r = await c.post(url, json=rpc("ok", {"text": "sse"}, req_id=7), headers=PAID)
                assert r.status_code == 202
                assert r.headers["payment-response"] == "settled"
                async for line in lines:
                    if line.startswith("data: ") and '"id":7' in line.replace(" ", ""):
                        assert "ok:sse" in line
                        break

    asyncio.run(run())
    assert len(s.settled) == 1
