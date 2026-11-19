"""End-to-end replay protection test on AlgoKit LocalNet."""

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from algosdk.v2client import algod
from conftest import get_localnet_context
from fastmcp import FastMCP
from x402 import x402Client
from x402.http.clients.httpx import x402HttpxClient
from x402_algorand.exact.register import register_exact_avm_client

from ai_economy_sdk import DefaultAvmSigner, PaymentGate, resolve_network
from ai_economy_sdk.chain import ChainReader

pytestmark = pytest.mark.localnet
PRICE = 7777


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def env():
    cfg = get_localnet_context()
    algod_client = algod.AlgodClient(cfg["algod_token"], cfg["algod_url"])
    server_pk = cfg["accounts"]["server"]["private_key"]
    m = FastMCP("e2e_replay")

    @m.tool()
    def echo(text: str) -> str:
        return f"echo:{text}"

    gate = PaymentGate(
        signer=DefaultAvmSigner(server_pk, algod_client=algod_client),
        network=cfg["network"],
        pay_to=cfg["accounts"]["server"]["address"],
        asset_id=0,
        prices={"echo": PRICE},
        chain=ChainReader(indexer_url=cfg["indexer_url"]),
        subscription_asset_id=cfg["subscription_asset_id"],
    )
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            gate.wrap(m),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
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
        "base_url": f"http://127.0.0.1:{port}",
        "algod": algod_client,
        "gate": gate,
        "cfg": cfg,
    }
    server.should_exit = True
    t.join(5)


async def _run_listener(sse_url: str, queue: asyncio.Queue):
    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("GET", sse_url) as r:
                async for line in r.aiter_lines():
                    if line.startswith("data: ") and "/messages" in line:
                        await queue.put(line[6:].strip())
    except asyncio.CancelledError:
        pass


def test_replay_protection(env):
    async def _test():
        algod_client = env["algod"]
        cfg = env["cfg"]
        server_addr = cfg["accounts"]["server"]["address"]
        client1_pk = cfg["accounts"]["client1"]["private_key"]

        # 1. Open an SSE GET to /sse with plain httpx (stream) and read the "endpoint" event
        q1 = asyncio.Queue()
        task1 = asyncio.create_task(_run_listener(env["url"], q1))
        endpoint1 = await q1.get()
        msg_url1 = env["base_url"] + endpoint1

        # 2. Hook and client setup
        captured = {}

        async def hook(request):
            v = request.headers.get("payment-signature") or request.headers.get("x-payment")
            if v:
                captured["h"] = v
                captured["name"] = (
                    "payment-signature" if request.headers.get("payment-signature") else "x-payment"
                )

        x402_client = x402Client()
        signer = DefaultAvmSigner(client1_pk, algod_client=algod_client)
        register_exact_avm_client(
            x402_client,
            signer,
            networks=resolve_network(cfg["network"], algod_client),
            algod_url=cfg["algod_url"],
            algod_token=cfg["algod_token"],
        )
        paid_client = x402HttpxClient(x402_client, event_hooks={"request": [hook]}, timeout=30)
        # Ensure hook runs on retry request as well
        orig_send = paid_client._transport._transport.handle_async_request

        async def hooked_send(req):
            await hook(req)
            return await orig_send(req)

        paid_client._transport._transport.handle_async_request = hooked_send

        # 3. POST tools/call JSON to messages URL with paid_client
        client1_addr = cfg["accounts"]["client1"]["address"]
        client1_bal_before = algod_client.account_info(client1_addr)["amount"]
        bal_before = algod_client.account_info(server_addr)["amount"]
        call_json = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hello"}},
        }
        r_paid = await paid_client.post(msg_url1, json=call_json)
        assert r_paid.status_code in (200, 202)
        assert "h" in captured
        assert captured.get("name") in ("payment-signature", "x-payment")

        bal_after = algod_client.account_info(server_addr)["amount"]
        client1_bal_after = algod_client.account_info(client1_addr)["amount"]
        assert bal_after - bal_before >= PRICE - 10_000
        assert bal_after - bal_before <= PRICE
        assert client1_bal_before - client1_bal_after >= PRICE

        # 4. REPLAY: with a PLAIN httpx client POST same JSON to same messages URL with captured header
        async with httpx.AsyncClient(timeout=30) as plain:
            r_replay1 = await plain.post(
                msg_url1,
                json=call_json,
                headers={captured["name"]: captured["h"]},
            )
            assert r_replay1.status_code == 402

        await asyncio.sleep(3)
        bal_replay1 = algod_client.account_info(server_addr)["amount"]
        client1_bal_replay1 = algod_client.account_info(client1_addr)["amount"]
        assert client1_bal_replay1 == client1_bal_after
        assert (bal_replay1 <= bal_after) or (client1_bal_replay1 == client1_bal_after)

        # 5. REPLAY to a NEW session (open second SSE stream, new messages URL)
        q2 = asyncio.Queue()
        task2 = asyncio.create_task(_run_listener(env["url"], q2))
        endpoint2 = await q2.get()
        msg_url2 = env["base_url"] + endpoint2

        async with httpx.AsyncClient(timeout=30) as plain:
            r_replay2 = await plain.post(
                msg_url2,
                json=call_json,
                headers={captured["name"]: captured["h"]},
            )
            assert r_replay2.status_code == 402

        await asyncio.sleep(3)
        bal_replay2 = algod_client.account_info(server_addr)["amount"]
        client1_bal_replay2 = algod_client.account_info(client1_addr)["amount"]
        assert client1_bal_replay2 == client1_bal_after
        assert (bal_replay2 <= bal_after) or (client1_bal_replay2 == client1_bal_after)

        # Cleanup background SSE listeners
        task1.cancel()
        await task1
        task2.cancel()
        await task2

    asyncio.run(_test())
