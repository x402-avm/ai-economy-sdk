"""Contract: ai_economy_sdk.paid_mcp_session - buyer client on both MCP HTTP transports (offline).

The server here only serves free tools, so no payment is signed and no algod is needed;
paid calls are covered by tests/test_e2e_localnet.py.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from algosdk import account
from fastmcp import FastMCP

from ai_economy_sdk import ConfigurationError, DefaultAvmSigner, PaymentGate, paid_mcp_session
from ai_economy_sdk.client import _infer_transport

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
