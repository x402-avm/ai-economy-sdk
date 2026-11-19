"""End-to-end on AlgoKit LocalNet (run: LOCALNET=1 pytest -m localnet).

Proves the full loop: gated FastMCP server -> 402 -> client pays on-chain -> tool result -> funds arrived,
on both HTTP transports (Streamable HTTP at /mcp and legacy SSE at /sse).
"""

import asyncio
import socket
import sys
import threading
import time

import pytest
import uvicorn
from algosdk.v2client import algod
from conftest import get_localnet_context
from fastmcp import FastMCP

from ai_economy_sdk import DefaultAvmSigner, PaymentGate
from ai_economy_sdk.chain import ChainReader
from ai_economy_sdk.client import paid_mcp_session

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup, ExceptionGroup  # noqa: F401

pytestmark = pytest.mark.localnet
PRICE = 12_345
MICRO_PRICE = 5_000


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module", params=["streamable-http", "sse"])
def env(request):
    transport = request.param
    cfg = get_localnet_context()
    algod_client = algod.AlgodClient(cfg["algod_token"], cfg["algod_url"])
    server_pk = cfg["accounts"]["server"]["private_key"]
    m = FastMCP("e2e")

    @m.tool()
    def echo(text: str) -> str:
        return f"echo:{text}"

    @m.tool()
    def micro(text: str) -> str:
        return f"micro:{text}"

    gate = PaymentGate(
        signer=DefaultAvmSigner(server_pk, algod_client=algod_client),
        network=cfg["network"],
        pay_to=cfg["accounts"]["server"]["address"],
        asset_id=0,
        prices={"echo": PRICE, "micro": MICRO_PRICE},
        chain=ChainReader(indexer_url=cfg["indexer_url"]),
        subscription_asset_id=cfg["subscription_asset_id"],
    )
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            gate.wrap(m, transport=transport), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            conn = socket.create_connection(("127.0.0.1", port), 0.2)
            conn.close()
            break
        except OSError:
            time.sleep(0.1)
    path = "/mcp" if transport == "streamable-http" else "/sse"
    yield {
        "url": f"http://127.0.0.1:{port}{path}",
        "transport": transport,
        "algod": algod_client,
        "gate": gate,
        "cfg": cfg,
    }
    server.should_exit = True
    t.join(5)


async def _call(url, who, text, tool="echo", spend_controls=None):
    cfg = get_localnet_context()
    pk = cfg["accounts"][who]["private_key"]
    async with paid_mcp_session(
        url,
        pk,
        cfg["network"],
        algod_url=cfg["algod_url"],
        algod_token=cfg["algod_token"],
        spend_controls=spend_controls,
    ) as s:
        res = await s.call_tool(tool, {"text": text})
        return res.content[0].text


def test_paid_call_settles_on_chain(env):
    pay_to = env["cfg"]["accounts"]["server"]["address"]
    before = env["algod"].account_info(pay_to)["amount"]
    out = asyncio.run(_call(env["url"], "client1", "hello"))
    assert out == "echo:hello"
    after = env["algod"].account_info(pay_to)["amount"]
    # server receives PRICE; it may also pay fees as fee-payer of the group, so allow fee slack only downward
    assert after - before >= PRICE - 10_000, (before, after)
    assert after - before <= PRICE


def test_spend_controls_default_pays_5000_microalgo(env):
    pay_to = env["cfg"]["accounts"]["server"]["address"]
    before = env["algod"].account_info(pay_to)["amount"]
    out = asyncio.run(_call(env["url"], "client1", "hello_5000", tool="micro"))
    assert out == "micro:hello_5000"
    after = env["algod"].account_info(pay_to)["amount"]
    assert after - before >= MICRO_PRICE - 10_000, (before, after)
    assert after - before <= MICRO_PRICE


def test_spend_controls_unlimited_max_amount_pays(env):
    pay_to = env["cfg"]["accounts"]["server"]["address"]
    before = env["algod"].account_info(pay_to)["amount"]
    out = asyncio.run(
        _call(
            env["url"],
            "client2",
            "hello_unlimited",
            tool="micro",
            spend_controls={"max_amount_per_payment": False},
        )
    )
    assert out == "micro:hello_unlimited"
    after = env["algod"].account_info(pay_to)["amount"]
    assert after - before >= MICRO_PRICE - 10_000, (before, after)
    assert after - before <= MICRO_PRICE


def test_unpaid_plain_client_is_refused(env):
    import httpx

    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "x"}},
    }
    if env["transport"] == "streamable-http":
        r = httpx.post(
            env["url"],
            json=call,
            headers={"accept": "application/json, text/event-stream"},
            timeout=10,
        )
        assert r.status_code == 402
        assert r.headers.get("payment-required")
        return

    with httpx.Client(timeout=10) as c, c.stream("GET", env["url"]) as sse:
        endpoint = None
        for line in sse.iter_lines():
            if line.startswith("data: ") and "/messages" in line:
                endpoint = line[6:].strip()
                break
        r = httpx.post(
            env["url"].rsplit("/sse", 1)[0] + endpoint,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "x"}},
            },
        )
        assert r.status_code == 402


def test_spend_cap_refusal_raises_spend_limit_error_and_moves_no_funds(env):
    from ai_economy_sdk import AIEconomyError, SpendLimitExceededError

    cfg = env["cfg"]
    pay_to = cfg["accounts"]["server"]["address"]
    buyer = cfg["accounts"]["client2"]["address"]
    low_cap = {
        "allowed_assets": [
            {"network": cfg["network"], "asset": "0", "max_amount_per_payment": str(PRICE - 1)}
        ]
    }
    server_before = env["algod"].account_info(pay_to)["amount"]
    buyer_before = env["algod"].account_info(buyer)["amount"]
    with pytest.raises(SpendLimitExceededError) as ei:
        asyncio.run(_call(env["url"], "client2", "too dear", spend_controls=low_cap))
    assert isinstance(ei.value, AIEconomyError)
    assert not isinstance(ei.value, BaseExceptionGroup)
    assert str(PRICE) in str(ei.value)
    assert ei.value.__cause__ is not None
    assert env["algod"].account_info(pay_to)["amount"] == server_before
    assert env["algod"].account_info(buyer)["amount"] == buyer_before
