"""Tests for settle-on-success payment verification and FastMCP tool execution.

Includes:
- Offline tests mocking the facilitator / settlement helpers to verify:
  1. Paid ok(): settle called once, result delivered.
  2. Paid boom(): settle NOT called, client gets an error, same payment header can be used again.
  3. Settle failure: client gets "payment settlement failed", not the ok output.
- LocalNet e2e test (marked localnet) ensuring balances are unchanged when a paid tool raises.
"""

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from algosdk import account
from algosdk.v2client import algod
from conftest import get_localnet_context
from fastmcp import FastMCP
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from x402.http.types import HTTPProcessResult, ProcessSettleResult
from x402.schemas import PaymentRequirements

from ai_economy_sdk import MAINNET, DefaultAvmSigner, PaymentGate
from ai_economy_sdk.chain import ChainReader
from ai_economy_sdk.client import paid_mcp_session


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def offline_server():
    m = FastMCP("test_settle_offline")

    @m.tool()
    def ok(text: str = "hi") -> str:
        return f"ok:{text}"

    @m.tool()
    def boom() -> str:
        raise ValueError("deliberate tool failure")

    spk, pay_to = account.generate_account()
    gate = PaymentGate(
        signer=DefaultAvmSigner(spk),
        network=MAINNET,
        pay_to=pay_to,
        prices={"ok": 1000, "boom": 1000},
        allow_loss=True,
    )

    settle_calls = []
    settle_flags = {"should_fail": False}

    original_process_req = gate.http_server.process_http_request

    async def mock_process_http_request(context, paywall_config=None):
        hdr = context.payment_header
        if not hdr:
            return await original_process_req(context, paywall_config)
        reqs = PaymentRequirements(
            scheme="exact",
            network=MAINNET,
            asset="0",
            amount="1000",
            pay_to=pay_to,
            max_timeout_seconds=300,
        )
        return HTTPProcessResult(
            type="payment-verified",
            payment_payload={"signature": hdr},
            payment_requirements=reqs,
        )

    async def mock_process_settlement(payload, requirements):
        settle_calls.append(payload)
        if settle_flags["should_fail"]:
            return ProcessSettleResult(success=False, error_reason="insufficient balance")
        return ProcessSettleResult(success=True, headers={"x-settled": "true"})

    gate.http_server.process_http_request = mock_process_http_request
    gate.http_server.process_settlement = mock_process_settlement

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(gate.wrap(m), host="127.0.0.1", port=port, log_level="error")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    yield {
        "url": f"http://127.0.0.1:{port}/sse",
        "gate": gate,
        "settle_calls": settle_calls,
        "settle_flags": settle_flags,
    }

    server.should_exit = True
    t.join(5)


def test_paid_ok_settles_and_delivers_result(offline_server):
    """Paid ok(): settle called once, result delivered."""
    url = offline_server["url"]
    settle_calls = offline_server["settle_calls"]

    def factory(headers=None, timeout=None, auth=None):
        async def add_hdr(req):
            req.headers["payment-signature"] = "sig_ok_1"

        return httpx.AsyncClient(
            headers=headers, timeout=timeout, auth=auth, event_hooks={"request": [add_hdr]}
        )

    async def _test():
        async with sse_client(url, httpx_client_factory=factory) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                res = await session.call_tool("ok", {"text": "alice"})
                assert res.content[0].text == "ok:alice"
                assert len(settle_calls) == 1
                assert settle_calls[0] == {"signature": "sig_ok_1"}

    asyncio.run(_test())


def test_paid_boom_does_not_settle_and_releases_hash(offline_server):
    """Paid boom(): settle NOT called, client gets error, same payment header can be retried."""
    url = offline_server["url"]
    settle_calls = offline_server["settle_calls"]

    hdr_val = "sig_boom_1"

    def factory(headers=None, timeout=None, auth=None):
        async def add_hdr(req):
            req.headers["payment-signature"] = hdr_val

        return httpx.AsyncClient(
            headers=headers, timeout=timeout, auth=auth, event_hooks={"request": [add_hdr]}
        )

    async def _test():
        async with sse_client(url, httpx_client_factory=factory) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                # 1. Call boom -> fails, not settled
                res = await session.call_tool("boom")
                assert res.isError is True
                assert len(settle_calls) == 0

                # 2. Re-use same payment signature for ok() -> reservation was released, so it succeeds
                res_retry = await session.call_tool("ok", {"text": "after_boom"})
                assert res_retry.isError is False
                assert res_retry.content[0].text == "ok:after_boom"
                assert len(settle_calls) == 1
                assert settle_calls[0] == {"signature": "sig_boom_1"}

    asyncio.run(_test())


def test_settle_failure_raises_error_and_does_not_deliver_output(offline_server):
    """Settle failure: client gets 'payment settlement failed', not the ok output."""
    url = offline_server["url"]
    offline_server["settle_flags"]["should_fail"] = True
    settle_calls = offline_server["settle_calls"]

    def factory(headers=None, timeout=None, auth=None):
        async def add_hdr(req):
            req.headers["payment-signature"] = "sig_fail_1"

        return httpx.AsyncClient(
            headers=headers, timeout=timeout, auth=auth, event_hooks={"request": [add_hdr]}
        )

    async def _test():
        async with sse_client(url, httpx_client_factory=factory) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                res = await session.call_tool("ok", {"text": "secret_data"})
                assert res.isError is True
                assert "payment settlement failed: insufficient balance" in res.content[0].text
                assert "secret_data" not in res.content[0].text
                assert len(settle_calls) == 1

    asyncio.run(_test())


@pytest.mark.localnet
def test_localnet_failing_tool_leaves_balances_unchanged():
    """LocalNet e2e: paying client calls a tool that raises -> payer and server balances unchanged."""
    cfg = get_localnet_context()
    algod_client = algod.AlgodClient(cfg["algod_token"], cfg["algod_url"])
    server_pk = cfg["accounts"]["server"]["private_key"]
    server_addr = cfg["accounts"]["server"]["address"]

    m = FastMCP("e2e_failing_tool")

    @m.tool()
    def boom() -> str:
        raise ValueError("failing tool deliberately raised")

    price = 15_000
    gate = PaymentGate(
        signer=DefaultAvmSigner(server_pk, algod_client=algod_client),
        network=cfg["network"],
        pay_to=server_addr,
        asset_id=0,
        prices={"boom": price},
        chain=ChainReader(indexer_url=cfg["indexer_url"]),
        subscription_asset_id=cfg["subscription_asset_id"],
    )

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(gate.wrap(m), host="127.0.0.1", port=port, log_level="warning")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    url = f"http://127.0.0.1:{port}/sse"
    client1_pk = cfg["accounts"]["client1"]["private_key"]
    client1_addr = cfg["accounts"]["client1"]["address"]

    async def _run():
        server_before = algod_client.account_info(server_addr)["amount"]
        payer_before = algod_client.account_info(client1_addr)["amount"]

        async with paid_mcp_session(
            url,
            client1_pk,
            cfg["network"],
            algod_url=cfg["algod_url"],
            algod_token=cfg["algod_token"],
        ) as sess:
            res = await sess.call_tool("boom")
            assert res.isError is True

        server_after = algod_client.account_info(server_addr)["amount"]
        payer_after = algod_client.account_info(client1_addr)["amount"]

        # Neither payer nor server had any transaction submitted or settled
        assert payer_after == payer_before, (
            f"Payer balance changed: {payer_before} -> {payer_after}"
        )
        assert server_after == server_before, (
            f"Server balance changed: {server_before} -> {server_after}"
        )

    try:
        asyncio.run(_run())
    finally:
        server.should_exit = True
        t.join(5)
