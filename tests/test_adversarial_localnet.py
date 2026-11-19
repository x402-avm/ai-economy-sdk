"""Adversarial payment tests on LocalNet.

Proves the PaymentGate rejects every dishonest payment on LocalNet:
1. underpay: amount = price - 1
2. wrong receiver: pay client2 instead of pay_to
3. wrong asset: pay ASA 1010 (subscription ASA) instead of ALGO
4. forged signature: signed with client2 key while client1 is sender
5. tampered after signing: amount changed after signing
6. expired: last_valid round in the past
7. wrong genesis hash: different network
8. group broken: dropped fee payer transaction
9. insufficient funds: fresh unfunded account
10. replay across proxies: same payment reused across proxies sharing replay_db
"""

import asyncio
import base64
import json
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from algosdk import account, encoding, transaction
from algosdk.v2client import algod
from conftest import get_localnet_context
from fastmcp import FastMCP
from x402.http.x402_http_client_base import encode_payment_signature_header
from x402.schemas import PaymentPayload, PaymentRequirements

from ai_economy_sdk import DefaultAvmSigner, PaymentGate
from ai_economy_sdk.chain import ChainReader

pytestmark = pytest.mark.localnet
PRICE = 5000


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _run_sse_listener(
    sse_url: str, queue: asyncio.Queue, msg_list: list | None = None
) -> None:
    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("GET", sse_url) as r:
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if msg_list is not None:
                            msg_list.append(data)
                        if "/messages" in data and queue.empty():
                            await queue.put(data)
    except asyncio.CancelledError:
        pass


@pytest.fixture(scope="module")
def server_env():
    cfg = get_localnet_context()
    algod_client = algod.AlgodClient(cfg["algod_token"], cfg["algod_url"])
    server_pk = cfg["accounts"]["server"]["private_key"]
    server_addr = cfg["accounts"]["server"]["address"]

    m = FastMCP("adversarial_test")

    @m.tool()
    def ok() -> str:
        return "SECRET_TOOL_OUTPUT"

    gate = PaymentGate(
        signer=DefaultAvmSigner(server_pk, algod_client=algod_client),
        network=cfg["network"],
        pay_to=server_addr,
        asset_id=0,
        prices={"ok": PRICE},
        chain=ChainReader(indexer_url=cfg["indexer_url"]),
        subscription_asset_id=cfg["subscription_asset_id"],
    )

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
        "port": port,
        "sse_url": f"http://127.0.0.1:{port}/sse",
        "algod": algod_client,
        "server_addr": server_addr,
        "server_pk": server_pk,
        "cfg": cfg,
    }

    server.should_exit = True
    t.join(5)


async def _get_session_and_requirements(
    sse_url: str, port: int, msg_list: list | None = None
) -> tuple[str, PaymentRequirements, asyncio.Task]:
    q = asyncio.Queue()
    task = asyncio.create_task(_run_sse_listener(sse_url, q, msg_list))
    endpoint = await q.get()
    msg_url = f"http://127.0.0.1:{port}{endpoint}"

    call_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "ok", "arguments": {}},
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(msg_url, json=call_body)
        assert r.status_code == 402, f"Initial call expected 402, got {r.status_code}"
        req_hdr = r.headers["payment-required"]
        req_data = json.loads(base64.b64decode(req_hdr))["accepts"][0]
        req = PaymentRequirements(
            scheme=req_data["scheme"],
            network=req_data["network"],
            asset=str(req_data.get("asset", "0")),
            amount=str(req_data["amount"]),
            pay_to=req_data["payTo"],
            max_timeout_seconds=req_data.get("maxTimeoutSeconds", 300),
            extra=req_data.get("extra", {}),
        )
    return msg_url, req, task


def _check_no_txns_on_chain(
    algod_client: algod.AlgodClient, payer_addr: str, start_round: int, end_round: int
) -> None:
    for r in range(start_round + 1, end_round + 1):
        blk = algod_client.block_info(r)
        txns = blk.get("block", {}).get("txns", [])
        for t in txns:
            tb = t.get("txn", {})
            snd = tb.get("snd")
            if isinstance(snd, bytes):
                snd = (
                    encoding.encode_address(snd)
                    if len(snd) == 32
                    else snd.decode("utf-8", errors="replace")
                )
            assert snd != payer_addr, (
                f"Dishonest payment transaction appeared in round {r} from {payer_addr}"
            )


def _get_refusal_reason(response: httpx.Response) -> str:
    if "payment-required" in response.headers:
        try:
            decoded = json.loads(base64.b64decode(response.headers["payment-required"]))
            if "error" in decoded:
                return str(decoded["error"])
        except Exception:
            pass
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("error") or body.get("details") or body)
    except Exception:
        pass
    return response.text or f"HTTP {response.status_code}"


async def _run_attack_test(
    server_env: dict,
    payer_key_name: str,
    tamper_fn: Any,
) -> tuple[str, int, int]:
    port = server_env["port"]
    sse_url = server_env["sse_url"]
    algod_client: algod.AlgodClient = server_env["algod"]
    server_addr = server_env["server_addr"]
    payer_pk = server_env["cfg"]["accounts"][payer_key_name]["private_key"]
    payer_addr = server_env["cfg"]["accounts"][payer_key_name]["address"]

    msg_url, req, sse_task = await _get_session_and_requirements(sse_url, port)
    try:
        payload = tamper_fn(req, payer_pk, payer_addr, server_addr, algod_client)
        header_val = encode_payment_signature_header(payload)

        payer_before = algod_client.account_info(payer_addr)["amount"]
        server_before = algod_client.account_info(server_addr)["amount"]
        start_round = algod_client.status()["last-round"]

        call_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "ok", "arguments": {}},
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                msg_url, json=call_body, headers={"payment-signature": header_val}
            )

        # Verification of refusal
        assert resp.status_code == 402, f"Expected status 402, got {resp.status_code}: {resp.text}"
        assert "SECRET_TOOL_OUTPUT" not in resp.text, "Tool output must NOT be delivered"

        end_round = algod_client.status()["last-round"]
        payer_after = algod_client.account_info(payer_addr)["amount"]
        server_after = algod_client.account_info(server_addr)["amount"]

        payer_diff = payer_after - payer_before
        server_diff = server_after - server_before

        assert payer_diff == 0, f"Payer balance changed: {payer_before} -> {payer_after}"
        assert server_diff == 0, f"Server balance changed: {server_before} -> {server_after}"

        _check_no_txns_on_chain(algod_client, payer_addr, start_round, end_round)
        reason = _get_refusal_reason(resp)
        return reason, payer_diff, server_diff
    finally:
        sse_task.cancel()


def test_attack_1_underpay(server_env):
    """Attack 1: underpay: amount = price - 1."""

    def tamper(req, payer_pk, payer_addr, server_addr, algod_client):
        sp = algod_client.suggested_params()
        fp_sp = transaction.SuggestedParams(2000, sp.first, sp.last, sp.gh, sp.gen, True)
        as_sp = transaction.SuggestedParams(0, sp.first, sp.last, sp.gh, sp.gen, True)
        fp = transaction.PaymentTxn(server_addr, fp_sp, server_addr, 0, note=b"fp")
        p = transaction.PaymentTxn(payer_addr, as_sp, server_addr, PRICE - 1, note=b"pay")
        txns = transaction.assign_group_id([fp, p])
        stxn = txns[1].sign(payer_pk)
        pg = [
            base64.b64encode(base64.b64decode(encoding.msgpack_encode(txns[0]))).decode(),
            base64.b64encode(base64.b64decode(encoding.msgpack_encode(stxn))).decode(),
        ]
        return PaymentPayload(
            x402_version=2, payload={"paymentGroup": pg, "paymentIndex": 1}, accepted=req
        )

    reason, p_diff, s_diff = asyncio.run(_run_attack_test(server_env, "client1", tamper))
    print(f"Attack 1 refusal reason: {reason}, payer diff: {p_diff}, server diff: {s_diff}")


def test_attack_2_wrong_receiver(server_env):
    """Attack 2: wrong receiver: pay a different address (client2) instead of pay_to."""
    client2_addr = server_env["cfg"]["accounts"]["client2"]["address"]

    def tamper(req, payer_pk, payer_addr, server_addr, algod_client):
        sp = algod_client.suggested_params()
        fp_sp = transaction.SuggestedParams(2000, sp.first, sp.last, sp.gh, sp.gen, True)
        as_sp = transaction.SuggestedParams(0, sp.first, sp.last, sp.gh, sp.gen, True)
        fp = transaction.PaymentTxn(server_addr, fp_sp, server_addr, 0, note=b"fp")
        p = transaction.PaymentTxn(payer_addr, as_sp, client2_addr, PRICE, note=b"pay")
        txns = transaction.assign_group_id([fp, p])
        stxn = txns[1].sign(payer_pk)
        pg = [
            base64.b64encode(base64.b64decode(encoding.msgpack_encode(txns[0]))).decode(),
            base64.b64encode(base64.b64decode(encoding.msgpack_encode(stxn))).decode(),
        ]
        return PaymentPayload(
            x402_version=2, payload={"paymentGroup": pg, "paymentIndex": 1}, accepted=req
        )

    reason, p_diff, s_diff = asyncio.run(_run_attack_test(server_env, "client1", tamper))
    print(f"Attack 2 refusal reason: {reason}, payer diff: {p_diff}, server diff: {s_diff}")


def test_attack_3_wrong_asset(server_env):
    """Attack 3: wrong asset: pay 5000 units of ASA (subscription asset) instead of ALGO."""
    sub_asset_id = server_env["cfg"]["subscription_asset_id"]

    def tamper(req, payer_pk, payer_addr, server_addr, algod_client):
        sp = algod_client.suggested_params()
        fp_sp = transaction.SuggestedParams(2000, sp.first, sp.last, sp.gh, sp.gen, True)
        as_sp = transaction.SuggestedParams(0, sp.first, sp.last, sp.gh, sp.gen, True)
        fp = transaction.PaymentTxn(server_addr, fp_sp, server_addr, 0, note=b"fp")
        p = transaction.AssetTransferTxn(
            payer_addr, as_sp, server_addr, PRICE, index=sub_asset_id, note=b"pay"
        )
        txns = transaction.assign_group_id([fp, p])
        stxn = txns[1].sign(payer_pk)
        pg = [
            base64.b64encode(base64.b64decode(encoding.msgpack_encode(txns[0]))).decode(),
            base64.b64encode(base64.b64decode(encoding.msgpack_encode(stxn))).decode(),
        ]
        return PaymentPayload(
            x402_version=2, payload={"paymentGroup": pg, "paymentIndex": 1}, accepted=req
        )

    reason, p_diff, s_diff = asyncio.run(_run_attack_test(server_env, "client1", tamper))
    print(f"Attack 3 refusal reason: {reason}, payer diff: {p_diff}, server diff: {s_diff}")


def test_attack_4_forged_signature(server_env):
    """Attack 4: forged signature: sign the payment txn with client2 key but keep client1 as sender."""
    client2_pk = server_env["cfg"]["accounts"]["client2"]["private_key"]

    def tamper(req, payer_pk, payer_addr, server_addr, algod_client):
        sp = algod_client.suggested_params()
        fp_sp = transaction.SuggestedParams(2000, sp.first, sp.last, sp.gh, sp.gen, True)
        as_sp = transaction.SuggestedParams(0, sp.first, sp.last, sp.gh, sp.gen, True)
        fp = transaction.PaymentTxn(server_addr, fp_sp, server_addr, 0, note=b"fp")
        p = transaction.PaymentTxn(payer_addr, as_sp, server_addr, PRICE, note=b"pay")
        txns = transaction.assign_group_id([fp, p])
        stxn = txns[1].sign(client2_pk)
        pg = [
            base64.b64encode(base64.b64decode(encoding.msgpack_encode(txns[0]))).decode(),
            base64.b64encode(base64.b64decode(encoding.msgpack_encode(stxn))).decode(),
        ]
        return PaymentPayload(
            x402_version=2, payload={"paymentGroup": pg, "paymentIndex": 1}, accepted=req
        )

    reason, p_diff, s_diff = asyncio.run(_run_attack_test(server_env, "client1", tamper))
    print(f"Attack 4 refusal reason: {reason}, payer diff: {p_diff}, server diff: {s_diff}")


def test_attack_5_tampered_after_signing(server_env):
    """Attack 5: tampered after signing: change bytes in signed txn so signature no longer matches."""

    def tamper(req, payer_pk, payer_addr, server_addr, algod_client):
        sp = algod_client.suggested_params()
        fp_sp = transaction.SuggestedParams(2000, sp.first, sp.last, sp.gh, sp.gen, True)
        as_sp = transaction.SuggestedParams(0, sp.first, sp.last, sp.gh, sp.gen, True)
        fp = transaction.PaymentTxn(server_addr, fp_sp, server_addr, 0, note=b"fp")
        p = transaction.PaymentTxn(payer_addr, as_sp, server_addr, PRICE, note=b"pay")
        txns = transaction.assign_group_id([fp, p])
        stxn = txns[1].sign(payer_pk)
        d = stxn.dictify()
        # Tamper note after signing: amount matches exact requirement so amount check passes, signature check fails
        d["txn"]["note"] = b"tampered_after_signing"
        tampered_b64 = base64.b64encode(base64.b64decode(encoding.msgpack_encode(d))).decode()
        pg = [
            base64.b64encode(base64.b64decode(encoding.msgpack_encode(txns[0]))).decode(),
            tampered_b64,
        ]
        return PaymentPayload(
            x402_version=2, payload={"paymentGroup": pg, "paymentIndex": 1}, accepted=req
        )

    reason, p_diff, s_diff = asyncio.run(_run_attack_test(server_env, "client1", tamper))
    assert "signature" in reason.lower(), (
        f"Expected refusal reason to contain 'signature', got: {reason}"
    )
    print(f"Attack 5 refusal reason: {reason}, payer diff: {p_diff}, server diff: {s_diff}")


def test_attack_6_expired(server_env):
    """Attack 6: expired: last_valid = current round - 1."""

    def tamper(req, payer_pk, payer_addr, server_addr, algod_client):
        cur_round = algod_client.status()["last-round"]
        sp = algod_client.suggested_params()
        first_r = max(1, cur_round - 100)
        last_r = max(1, cur_round - 1)
        fp_sp = transaction.SuggestedParams(2000, first_r, last_r, sp.gh, sp.gen, True)
        as_sp = transaction.SuggestedParams(0, first_r, last_r, sp.gh, sp.gen, True)
        fp = transaction.PaymentTxn(server_addr, fp_sp, server_addr, 0, note=b"fp")
        p = transaction.PaymentTxn(payer_addr, as_sp, server_addr, PRICE, note=b"pay")
        txns = transaction.assign_group_id([fp, p])
        stxn = txns[1].sign(payer_pk)
        pg = [
            base64.b64encode(base64.b64decode(encoding.msgpack_encode(txns[0]))).decode(),
            base64.b64encode(base64.b64decode(encoding.msgpack_encode(stxn))).decode(),
        ]
        return PaymentPayload(
            x402_version=2, payload={"paymentGroup": pg, "paymentIndex": 1}, accepted=req
        )

    reason, p_diff, s_diff = asyncio.run(_run_attack_test(server_env, "client1", tamper))
    print(f"Attack 6 refusal reason: {reason}, payer diff: {p_diff}, server diff: {s_diff}")


def test_attack_7_wrong_genesis_hash(server_env):
    """Attack 7: wrong genesis hash (another network)."""
    fake_gh = base64.b64decode("dGVzdHRlc3R0ZXN0dGVzdHRlc3R0ZXN0dGVzdHRlc3Q=")

    def tamper(req, payer_pk, payer_addr, server_addr, algod_client):
        sp = algod_client.suggested_params()
        fp_sp = transaction.SuggestedParams(2000, sp.first, sp.last, fake_gh, "othernet", True)
        as_sp = transaction.SuggestedParams(0, sp.first, sp.last, fake_gh, "othernet", True)
        fp = transaction.PaymentTxn(server_addr, fp_sp, server_addr, 0, note=b"fp")
        p = transaction.PaymentTxn(payer_addr, as_sp, server_addr, PRICE, note=b"pay")
        txns = transaction.assign_group_id([fp, p])
        stxn = txns[1].sign(payer_pk)
        pg = [
            base64.b64encode(base64.b64decode(encoding.msgpack_encode(txns[0]))).decode(),
            base64.b64encode(base64.b64decode(encoding.msgpack_encode(stxn))).decode(),
        ]
        return PaymentPayload(
            x402_version=2, payload={"paymentGroup": pg, "paymentIndex": 1}, accepted=req
        )

    reason, p_diff, s_diff = asyncio.run(_run_attack_test(server_env, "client1", tamper))
    print(f"Attack 7 refusal reason: {reason}, payer diff: {p_diff}, server diff: {s_diff}")


def test_attack_8_group_broken(server_env):
    """Attack 8: group broken: drop the fee-payer txn."""

    def tamper(req, payer_pk, payer_addr, server_addr, algod_client):
        sp = algod_client.suggested_params()
        as_sp = transaction.SuggestedParams(0, sp.first, sp.last, sp.gh, sp.gen, True)
        p = transaction.PaymentTxn(payer_addr, as_sp, server_addr, PRICE, note=b"pay")
        stxn = p.sign(payer_pk)
        pg = [base64.b64encode(base64.b64decode(encoding.msgpack_encode(stxn))).decode()]
        return PaymentPayload(
            x402_version=2, payload={"paymentGroup": pg, "paymentIndex": 0}, accepted=req
        )

    reason, p_diff, s_diff = asyncio.run(_run_attack_test(server_env, "client1", tamper))
    print(f"Attack 8 refusal reason: {reason}, payer diff: {p_diff}, server diff: {s_diff}")


def test_attack_9_insufficient_funds(server_env):
    """Attack 9: insufficient funds: a fresh, unfunded account pays."""
    unfunded_sk, unfunded_addr = account.generate_account()

    port = server_env["port"]
    sse_url = server_env["sse_url"]
    algod_client: algod.AlgodClient = server_env["algod"]
    server_addr = server_env["server_addr"]

    async def _test():
        msg_url, req, sse_task = await _get_session_and_requirements(sse_url, port)
        try:
            sp = algod_client.suggested_params()
            fp_sp = transaction.SuggestedParams(2000, sp.first, sp.last, sp.gh, sp.gen, True)
            as_sp = transaction.SuggestedParams(0, sp.first, sp.last, sp.gh, sp.gen, True)
            fp = transaction.PaymentTxn(server_addr, fp_sp, server_addr, 0, note=b"fp")
            p = transaction.PaymentTxn(unfunded_addr, as_sp, server_addr, PRICE, note=b"pay")
            txns = transaction.assign_group_id([fp, p])
            stxn = txns[1].sign(unfunded_sk)
            pg = [
                base64.b64encode(base64.b64decode(encoding.msgpack_encode(txns[0]))).decode(),
                base64.b64encode(base64.b64decode(encoding.msgpack_encode(stxn))).decode(),
            ]
            payload = PaymentPayload(
                x402_version=2, payload={"paymentGroup": pg, "paymentIndex": 1}, accepted=req
            )
            header_val = encode_payment_signature_header(payload)

            server_before = algod_client.account_info(server_addr)["amount"]
            start_round = algod_client.status()["last-round"]

            call_body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ok", "arguments": {}},
            }
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    msg_url, json=call_body, headers={"payment-signature": header_val}
                )

            assert resp.status_code == 402
            assert "SECRET_TOOL_OUTPUT" not in resp.text

            end_round = algod_client.status()["last-round"]
            server_after = algod_client.account_info(server_addr)["amount"]
            server_diff = server_after - server_before
            assert server_diff == 0

            _check_no_txns_on_chain(algod_client, unfunded_addr, start_round, end_round)
            reason = _get_refusal_reason(resp)
            return reason, 0, server_diff
        finally:
            sse_task.cancel()

    reason, p_diff, s_diff = asyncio.run(_test())
    print(f"Attack 9 refusal reason: {reason}, payer diff: {p_diff}, server diff: {s_diff}")


def test_attack_10a_replay_after_settle():
    """Attack 10a: replay across proxies: valid payment used on proxy A succeeds once, then reused on proxy B sharing replay_db -> refused."""
    cfg = get_localnet_context()
    algod_client = algod.AlgodClient(cfg["algod_token"], cfg["algod_url"])
    server_pk = cfg["accounts"]["server"]["private_key"]
    server_addr = cfg["accounts"]["server"]["address"]
    client1_pk = cfg["accounts"]["client1"]["private_key"]
    client1_addr = cfg["accounts"]["client1"]["address"]

    with tempfile.TemporaryDirectory() as td:
        shared_replay_db = str(Path(td) / "shared_replay.sqlite")

        def make_app():
            m = FastMCP("proxy_app")

            @m.tool()
            def ok() -> str:
                return "SECRET_TOOL_OUTPUT"

            gate = PaymentGate(
                signer=DefaultAvmSigner(server_pk, algod_client=algod_client),
                network=cfg["network"],
                pay_to=server_addr,
                asset_id=0,
                prices={"ok": PRICE},
                chain=ChainReader(indexer_url=cfg["indexer_url"]),
                subscription_asset_id=cfg["subscription_asset_id"],
                replay_db=shared_replay_db,
            )
            return gate.wrap(m)

        p1 = _free_port()
        p2 = _free_port()

        srv1 = uvicorn.Server(
            uvicorn.Config(make_app(), host="127.0.0.1", port=p1, log_level="error")
        )
        srv2 = uvicorn.Server(
            uvicorn.Config(make_app(), host="127.0.0.1", port=p2, log_level="error")
        )

        t1 = threading.Thread(target=srv1.run, daemon=True)
        t2 = threading.Thread(target=srv2.run, daemon=True)
        t1.start()
        t2.start()
        time.sleep(0.5)

        async def _test():
            msg_url_1, req, sse_task_1 = await _get_session_and_requirements(
                f"http://127.0.0.1:{p1}/sse", p1
            )
            msg_url_2, _, sse_task_2 = await _get_session_and_requirements(
                f"http://127.0.0.1:{p2}/sse", p2
            )
            try:
                # Build valid payment
                sp = algod_client.suggested_params()
                fp_sp = transaction.SuggestedParams(2000, sp.first, sp.last, sp.gh, sp.gen, True)
                as_sp = transaction.SuggestedParams(0, sp.first, sp.last, sp.gh, sp.gen, True)
                fp = transaction.PaymentTxn(
                    server_addr, fp_sp, server_addr, 0, note=b"fp_replay_test"
                )
                p = transaction.PaymentTxn(
                    client1_addr, as_sp, server_addr, PRICE, note=b"pay_replay_test"
                )
                txns = transaction.assign_group_id([fp, p])
                stxn = txns[1].sign(client1_pk)
                pg = [
                    base64.b64encode(base64.b64decode(encoding.msgpack_encode(txns[0]))).decode(),
                    base64.b64encode(base64.b64decode(encoding.msgpack_encode(stxn))).decode(),
                ]
                payload = PaymentPayload(
                    x402_version=2, payload={"paymentGroup": pg, "paymentIndex": 1}, accepted=req
                )
                header_val = encode_payment_signature_header(payload)

                payer_before = algod_client.account_info(client1_addr)["amount"]
                server_before = algod_client.account_info(server_addr)["amount"]

                call_body = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "ok", "arguments": {}},
                }
                async with httpx.AsyncClient(timeout=15) as client:
                    # 1. Use payment on Proxy A -> succeeds
                    resp_a = await client.post(
                        msg_url_1, json=call_body, headers={"payment-signature": header_val}
                    )
                    assert resp_a.status_code in (200, 202)

                    # 2. Replay same header on Proxy B -> refused
                    resp_b = await client.post(
                        msg_url_2, json=call_body, headers={"payment-signature": header_val}
                    )
                    assert resp_b.status_code == 402
                    assert "SECRET_TOOL_OUTPUT" not in resp_b.text

                await asyncio.sleep(2)
                payer_after = algod_client.account_info(client1_addr)["amount"]
                server_after = algod_client.account_info(server_addr)["amount"]

                payer_paid = payer_before - payer_after
                server_gained = server_after - server_before

                # Payer paid exactly PRICE once
                assert payer_paid == PRICE, f"Payer paid {payer_paid}, expected {PRICE}"
                # Server gained PRICE minus fee-payer fees (2000)
                assert server_gained == PRICE - 2000, (
                    f"Server gained {server_gained}, expected {PRICE - 2000}"
                )

                reason = _get_refusal_reason(resp_b)
                return reason, payer_paid, server_gained
            finally:
                sse_task_1.cancel()
                sse_task_2.cancel()

        try:
            reason, p_paid, s_gained = asyncio.run(_test())
            print(
                f"Attack 10a refusal reason: {reason}, total payer paid: {p_paid}, total server gained: {s_gained}"
            )
        finally:
            srv1.should_exit = True
            srv2.should_exit = True
            t1.join(5)
            t2.join(5)


def test_attack_10b_concurrent_replay_shared_db():
    """Attack 10b: concurrent replay across proxies sharing replay_db.
    One call succeeds; the other is refused with 'Replay detected' while the first is in-flight.
    """
    cfg = get_localnet_context()
    algod_client = algod.AlgodClient(cfg["algod_token"], cfg["algod_url"])
    server_pk = cfg["accounts"]["server"]["private_key"]
    server_addr = cfg["accounts"]["server"]["address"]
    client1_pk = cfg["accounts"]["client1"]["private_key"]
    client1_addr = cfg["accounts"]["client1"]["address"]

    with tempfile.TemporaryDirectory() as td:
        shared_replay_db = str(Path(td) / "shared_replay.sqlite")

        def make_app():
            m = FastMCP("proxy_app")

            @m.tool()
            async def ok() -> str:
                await asyncio.sleep(2)
                return "SECRET_TOOL_OUTPUT"

            gate = PaymentGate(
                signer=DefaultAvmSigner(server_pk, algod_client=algod_client),
                network=cfg["network"],
                pay_to=server_addr,
                asset_id=0,
                prices={"ok": PRICE},
                chain=ChainReader(indexer_url=cfg["indexer_url"]),
                subscription_asset_id=cfg["subscription_asset_id"],
                replay_db=shared_replay_db,
            )
            return gate.wrap(m)

        p1 = _free_port()
        p2 = _free_port()

        srv1 = uvicorn.Server(
            uvicorn.Config(make_app(), host="127.0.0.1", port=p1, log_level="error")
        )
        srv2 = uvicorn.Server(
            uvicorn.Config(make_app(), host="127.0.0.1", port=p2, log_level="error")
        )

        t1 = threading.Thread(target=srv1.run, daemon=True)
        t2 = threading.Thread(target=srv2.run, daemon=True)
        t1.start()
        t2.start()
        time.sleep(0.5)

        async def _test():
            msg_url_1, req, sse_task_1 = await _get_session_and_requirements(
                f"http://127.0.0.1:{p1}/sse", p1
            )
            msg_url_2, _, sse_task_2 = await _get_session_and_requirements(
                f"http://127.0.0.1:{p2}/sse", p2
            )
            try:
                sp = algod_client.suggested_params()
                fp_sp = transaction.SuggestedParams(2000, sp.first, sp.last, sp.gh, sp.gen, True)
                as_sp = transaction.SuggestedParams(0, sp.first, sp.last, sp.gh, sp.gen, True)
                fp = transaction.PaymentTxn(server_addr, fp_sp, server_addr, 0, note=b"fp_10b")
                p = transaction.PaymentTxn(client1_addr, as_sp, server_addr, PRICE, note=b"pay_10b")
                txns = transaction.assign_group_id([fp, p])
                stxn = txns[1].sign(client1_pk)
                pg = [
                    base64.b64encode(base64.b64decode(encoding.msgpack_encode(txns[0]))).decode(),
                    base64.b64encode(base64.b64decode(encoding.msgpack_encode(stxn))).decode(),
                ]
                payload = PaymentPayload(
                    x402_version=2, payload={"paymentGroup": pg, "paymentIndex": 1}, accepted=req
                )
                header_val = encode_payment_signature_header(payload)

                payer_before = algod_client.account_info(client1_addr)["amount"]
                server_before = algod_client.account_info(server_addr)["amount"]

                call_body = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "ok", "arguments": {}},
                }
                async with httpx.AsyncClient(timeout=20) as client:
                    resp_a, resp_b = await asyncio.gather(
                        client.post(
                            msg_url_1, json=call_body, headers={"payment-signature": header_val}
                        ),
                        client.post(
                            msg_url_2, json=call_body, headers={"payment-signature": header_val}
                        ),
                    )

                # Exactly one succeeds (200/202), other is refused (402)
                statuses = [resp_a.status_code, resp_b.status_code]
                assert 402 in statuses, f"Expected one 402, got {statuses}"
                assert any(s in (200, 202) for s in statuses), (
                    f"Expected one 200/202, got {statuses}"
                )

                refused_resp = resp_b if resp_a.status_code in (200, 202) else resp_a
                reason = _get_refusal_reason(refused_resp)
                assert "replay" in reason.lower(), (
                    f"Expected refusal to contain 'Replay', got: {reason}"
                )
                assert "transaction_simulation_failed" not in reason.lower(), (
                    f"Did not expect simulation failure, got: {reason}"
                )

                await asyncio.sleep(2)
                payer_after = algod_client.account_info(client1_addr)["amount"]
                server_after = algod_client.account_info(server_addr)["amount"]

                payer_paid = payer_before - payer_after
                server_gained = server_after - server_before

                assert payer_paid == PRICE, f"Payer paid {payer_paid}, expected {PRICE}"
                assert server_gained == PRICE - 2000, (
                    f"Server gained {server_gained}, expected {PRICE - 2000}"
                )

                return reason, payer_paid, server_gained
            finally:
                sse_task_1.cancel()
                sse_task_2.cancel()

        try:
            reason, p_paid, s_gained = asyncio.run(_test())
            print(
                f"Attack 10b refusal reason: {reason}, total payer paid: {p_paid}, total server gained: {s_gained}"
            )
        finally:
            srv1.should_exit = True
            srv2.should_exit = True
            t1.join(5)
            t2.join(5)


def test_attack_10c_concurrent_replay_separate_db():
    """Attack 10c: concurrent replay across proxies with separate replay_db instances.
    Both accept initial payment and run tool concurrently, but Algod settles only one;
    the other fails settlement and its tool output is not delivered.
    """
    cfg = get_localnet_context()
    algod_client = algod.AlgodClient(cfg["algod_token"], cfg["algod_url"])
    server_pk = cfg["accounts"]["server"]["private_key"]
    server_addr = cfg["accounts"]["server"]["address"]
    client1_pk = cfg["accounts"]["client1"]["private_key"]
    client1_addr = cfg["accounts"]["client1"]["address"]

    with tempfile.TemporaryDirectory() as td:
        replay_db_a = str(Path(td) / "replay_a.sqlite")
        replay_db_b = str(Path(td) / "replay_b.sqlite")

        def make_app(db_path: str):
            m = FastMCP("proxy_app")

            @m.tool()
            async def ok() -> str:
                await asyncio.sleep(2)
                return "SECRET_TOOL_OUTPUT"

            gate = PaymentGate(
                signer=DefaultAvmSigner(server_pk, algod_client=algod_client),
                network=cfg["network"],
                pay_to=server_addr,
                asset_id=0,
                prices={"ok": PRICE},
                chain=ChainReader(indexer_url=cfg["indexer_url"]),
                subscription_asset_id=cfg["subscription_asset_id"],
                replay_db=db_path,
            )
            return gate.wrap(m)

        p1 = _free_port()
        p2 = _free_port()

        srv1 = uvicorn.Server(
            uvicorn.Config(make_app(replay_db_a), host="127.0.0.1", port=p1, log_level="error")
        )
        srv2 = uvicorn.Server(
            uvicorn.Config(make_app(replay_db_b), host="127.0.0.1", port=p2, log_level="error")
        )

        t1 = threading.Thread(target=srv1.run, daemon=True)
        t2 = threading.Thread(target=srv2.run, daemon=True)
        t1.start()
        t2.start()
        time.sleep(0.5)

        async def _test():
            sse_msgs_a = []
            sse_msgs_b = []
            msg_url_1, req, sse_task_1 = await _get_session_and_requirements(
                f"http://127.0.0.1:{p1}/sse", p1, sse_msgs_a
            )
            msg_url_2, _, sse_task_2 = await _get_session_and_requirements(
                f"http://127.0.0.1:{p2}/sse", p2, sse_msgs_b
            )
            try:
                sp = algod_client.suggested_params()
                fp_sp = transaction.SuggestedParams(2000, sp.first, sp.last, sp.gh, sp.gen, True)
                as_sp = transaction.SuggestedParams(0, sp.first, sp.last, sp.gh, sp.gen, True)
                fp = transaction.PaymentTxn(server_addr, fp_sp, server_addr, 0, note=b"fp_10c")
                p = transaction.PaymentTxn(client1_addr, as_sp, server_addr, PRICE, note=b"pay_10c")
                txns = transaction.assign_group_id([fp, p])
                stxn = txns[1].sign(client1_pk)
                pg = [
                    base64.b64encode(base64.b64decode(encoding.msgpack_encode(txns[0]))).decode(),
                    base64.b64encode(base64.b64decode(encoding.msgpack_encode(stxn))).decode(),
                ]
                payload = PaymentPayload(
                    x402_version=2, payload={"paymentGroup": pg, "paymentIndex": 1}, accepted=req
                )
                header_val = encode_payment_signature_header(payload)

                payer_before = algod_client.account_info(client1_addr)["amount"]
                server_before = algod_client.account_info(server_addr)["amount"]

                call_body = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "ok", "arguments": {}},
                }
                async with httpx.AsyncClient(timeout=20) as client:
                    resp_a, resp_b = await asyncio.gather(
                        client.post(
                            msg_url_1, json=call_body, headers={"payment-signature": header_val}
                        ),
                        client.post(
                            msg_url_2, json=call_body, headers={"payment-signature": header_val}
                        ),
                    )

                # Both accept downstream call initially (202 Accepted)
                assert resp_a.status_code in (200, 202)
                assert resp_b.status_code in (200, 202)

                # Wait for tool execution and settlement
                await asyncio.sleep(4)

                payer_after = algod_client.account_info(client1_addr)["amount"]
                server_after = algod_client.account_info(server_addr)["amount"]
                payer_paid = payer_before - payer_after
                server_gained = server_after - server_before

                all_sse = sse_msgs_a + sse_msgs_b
                delivered_count = sum(1 for m in all_sse if "SECRET_TOOL_OUTPUT" in m)
                error_msgs = [m for m in all_sse if "payment settlement failed" in m]

                assert payer_paid == PRICE, f"Expected payer to pay {PRICE}, got {payer_paid}"
                assert server_gained == PRICE - 2000, (
                    f"Expected server to gain {PRICE - 2000}, got {server_gained}"
                )
                assert delivered_count <= 1, (
                    f"Expected at most 1 result delivered, got {delivered_count}"
                )
                assert delivered_count == 1, (
                    f"Expected exactly 1 successful result delivery, got {delivered_count}"
                )
                assert len(error_msgs) == 1, f"Expected 1 settlement failure, got {len(error_msgs)}"

                reason = error_msgs[0]
                return reason, payer_paid, server_gained
            finally:
                sse_task_1.cancel()
                sse_task_2.cancel()

        try:
            reason, p_paid, s_gained = asyncio.run(_test())
            print(
                f"Attack 10c refusal reason: {reason}, total payer paid: {p_paid}, total server gained: {s_gained}"
            )
        finally:
            srv1.should_exit = True
            srv2.should_exit = True
            t1.join(5)
            t2.join(5)
