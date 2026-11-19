"""End-to-end on AlgoKit LocalNet (run: LOCALNET=1 pytest -m localnet).

Proves the full loop: gated FastMCP server -> 402 -> client pays on-chain -> tool result -> funds arrived.
"""

import asyncio
import socket
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

pytestmark = pytest.mark.localnet
PRICE = 12_345
MICRO_PRICE = 5_000


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
        uvicorn.Config(gate.wrap(m), host="127.0.0.1", port=port, log_level="warning")
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
    yield {"url": f"http://127.0.0.1:{port}/sse", "algod": algod_client, "gate": gate, "cfg": cfg}
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
