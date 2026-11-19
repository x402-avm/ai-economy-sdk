"""Contract: ai_economy_sdk.paid_mcp_session - buyer client on both MCP HTTP transports (offline).

The server here only serves free tools, so no payment is signed and no algod is needed;
paid calls are covered by tests/test_e2e_localnet.py.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time

import pytest
import uvicorn
from algosdk import account
from fastmcp import FastMCP

from ai_economy_sdk import ConfigurationError, DefaultAvmSigner, PaymentGate, paid_mcp_session
from ai_economy_sdk.client import _infer_transport

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup, ExceptionGroup  # noqa: F401

SPK, PAY_TO = account.generate_account()
BUYER_PK, BUYER = account.generate_account()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(params=["streamable-http", "sse"])
def server(request):
    senders: list[str | None] = []

    def prices(tool, args, sender):
        senders.append(sender)
        return 0

    mcp = FastMCP("client-test")

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    gate = PaymentGate(
        signer=DefaultAvmSigner(SPK), network="mainnet", pay_to=PAY_TO, prices=prices
    )
    app = gate.wrap(mcp, transport=request.param)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    path = "/mcp" if request.param == "streamable-http" else "/sse"
    yield request.param, f"http://127.0.0.1:{port}{path}", senders
    srv.should_exit = True
    t.join(5)


def test_session_calls_tools_and_proves_identity(server):
    transport, url, senders = server

    async def run():
        async with paid_mcp_session(
            url,
            BUYER_PK,
            "mainnet",
            spend_controls={"max_amount_per_payment": False},
        ) as session:
            tools = await session.list_tools()
            assert [t.name for t in tools.tools] == ["echo"]
            res = await session.call_tool("echo", {"text": f"via {transport}"})
            assert res.content[0].text == f"via {transport}"

    asyncio.run(run())
    assert senders == [BUYER]  # the gate verified the buyer's identity headers


def test_session_without_identity_and_explicit_transport(server):
    transport, url, senders = server

    async def run():
        async with paid_mcp_session(
            url,
            BUYER_PK,
            "mainnet",
            identity=False,
            transport=transport,
            spend_controls=False,
            algod_url="http://127.0.0.1:9",
            timeout=5,
        ) as session:
            res = await session.call_tool("echo", {"text": "anon"})
            assert res.content[0].text == "anon"

    asyncio.run(run())
    assert senders == [None]


def test_transport_inference():
    assert _infer_transport("http://h:1/mcp") == "streamable-http"
    assert _infer_transport("http://h:1/svc/mcp/") == "streamable-http"
    assert _infer_transport("http://h:1/sse") == "sse"
    assert _infer_transport("http://h:1/custom") == "sse"  # 0.4 behaviour


def test_unknown_transport_is_rejected():
    async def run():
        async with paid_mcp_session("http://h/mcp", BUYER_PK, "mainnet", transport="ws"):  # type: ignore[arg-type]
            pass

    with pytest.raises(ConfigurationError):
        asyncio.run(run())


# --- spend-control refusals surface as SpendLimitExceededError (offline) ---------------------------


def _refusal(
    msg="All payment requirements were rejected by spend_controls.max_amount_per_payment (1)",
):
    from x402.http.clients.httpx import PaymentError
    from x402.schemas.errors import NoMatchingRequirementsError

    cause = NoMatchingRequirementsError(msg)
    try:
        raise PaymentError(f"Failed to handle payment: {cause}") from cause
    except PaymentError as e:
        return e, cause


def _offer():
    from types import SimpleNamespace

    return SimpleNamespace(
        accepts=[SimpleNamespace(amount="2500000", asset="0", network="algorand:abc")]
    )


def test_translate_plain_and_nested_group_with_cause_and_offer():
    from ai_economy_sdk import AIEconomyError, SpendLimitExceededError
    from ai_economy_sdk.client import _translate_spend_refusal

    err, cause = _refusal()
    for exc in (err, ExceptionGroup("tg", [ExceptionGroup("inner", [err])])):
        out = _translate_spend_refusal(exc, _offer())
        assert isinstance(out, SpendLimitExceededError) and isinstance(out, AIEconomyError)
        assert out.__cause__ is cause
        assert "2500000" in str(out) and "asset 0" in str(out)
    assert _translate_spend_refusal(err) is not None  # no offers known: still translated


def test_translate_leaves_unrelated_errors_alone():
    from x402.schemas.errors import NoMatchingRequirementsError

    from ai_economy_sdk.client import _translate_spend_refusal

    err, _ = _refusal()
    assert _translate_spend_refusal(ValueError("boom")) is None
    # a no-match error that is not about spend controls (e.g. wrong network)
    assert (
        _translate_spend_refusal(NoMatchingRequirementsError("No payment requirements match"))
        is None
    )
    # a refusal mixed with another error: the group must propagate unchanged
    assert _translate_spend_refusal(ExceptionGroup("tg", [err, ValueError("other")])) is None


def test_paid_mcp_session_translates_refusal_and_keeps_other_errors(monkeypatch):
    from ai_economy_sdk import SpendLimitExceededError
    from ai_economy_sdk import client as client_mod

    err, cause = _refusal()

    def fake_sse(url, httpx_client_factory=None):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [err])

    monkeypatch.setattr(client_mod.mcp.client.sse, "sse_client", fake_sse)

    async def run(exc=None):
        async with paid_mcp_session(
            "http://127.0.0.1:1/sse", BUYER_PK, "algorand:abc", algod_url="http://127.0.0.1:1"
        ):
            pass

    monkeypatch.setattr(client_mod, "resolve_network", lambda n, c: n)
    with pytest.raises(SpendLimitExceededError) as ei:
        asyncio.run(run())
    assert ei.value.__cause__ is cause

    other = ExceptionGroup("tg", [ValueError("unrelated")])
    monkeypatch.setattr(
        client_mod.mcp.client.sse, "sse_client", lambda *a, **k: (_ for _ in ()).throw(other)
    )
    with pytest.raises(ExceptionGroup) as ei2:
        asyncio.run(run())
    assert ei2.value is other


@pytest.fixture(params=["streamable-http", "sse"])
def priced_server(request):
    mcp = FastMCP("priced")

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    gate = PaymentGate(
        signer=DefaultAvmSigner(SPK), network="mainnet", pay_to=PAY_TO, prices={"echo": 2_500_000}
    )
    port = _free_port()
    srv = uvicorn.Server(
        uvicorn.Config(
            gate.wrap(mcp, transport=request.param),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    path = "/mcp" if request.param == "streamable-http" else "/sse"
    yield f"http://127.0.0.1:{port}{path}"
    srv.should_exit = True
    t.join(5)


def test_default_spend_cap_refusal_is_spend_limit_error_on_both_transports(priced_server):
    from ai_economy_sdk import AIEconomyError, SpendLimitExceededError

    async def run():
        async with paid_mcp_session(
            priced_server, BUYER_PK, "mainnet", algod_url="http://127.0.0.1:9", timeout=10
        ) as session:
            await session.call_tool("echo", {"text": "x"})

    with pytest.raises(SpendLimitExceededError) as ei:
        asyncio.run(asyncio.wait_for(run(), 30))
    assert isinstance(ei.value, AIEconomyError)
    assert "2500000" in str(ei.value)
    assert ei.value.__cause__ is not None


def test_unrelated_error_in_body_propagates_unchanged(priced_server):
    async def run():
        async with paid_mcp_session(
            priced_server, BUYER_PK, "mainnet", algod_url="http://127.0.0.1:9"
        ):
            raise KeyError("mine")

    with pytest.raises(BaseException) as ei:
        asyncio.run(run())
    from ai_economy_sdk.client import _leaf_exceptions

    leaves = _leaf_exceptions(ei.value)
    assert [type(x) for x in leaves] == [KeyError]
