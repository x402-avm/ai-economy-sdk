"""LocalNet tests for subscription issuer verification."""

import time

import pytest
from algosdk import transaction
from algosdk.atomic_transaction_composer import AccountTransactionSigner
from algosdk.v2client import algod, indexer
from conftest import get_localnet_context

from ai_economy_sdk import ChainReader, DefaultAvmSigner, PaymentGate

pytestmark = pytest.mark.localnet


def _get_asset_balance(algod_client: algod.AlgodClient, addr: str, asset_id: int) -> int:
    info = algod_client.account_info(addr)
    for a in info.get("assets", []):
        if a.get("asset-id") == asset_id:
            return a.get("amount", 0)
    return 0


def _transfer_asset(
    algod_client: algod.AlgodClient,
    sender_sk: str,
    sender_addr: str,
    receiver_addr: str,
    asset_id: int,
    amt: int,
) -> str:
    sp = algod_client.suggested_params()
    txn = transaction.AssetTransferTxn(
        sender=sender_addr,
        sp=sp,
        receiver=receiver_addr,
        amt=amt,
        index=asset_id,
    )
    signer = AccountTransactionSigner(sender_sk)
    stxn = signer.sign_transactions([txn], [0])[0]
    txid = algod_client.send_transaction(stxn)
    transaction.wait_for_confirmation(algod_client, txid, 4)
    return txid


def _wait_for_indexer(
    indexer_client: indexer.IndexerClient,
    txid: str,
    address: str,
    asset_id: int,
    expected_min_balance: int | None = None,
    timeout: float = 15.0,
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            tx_resp = indexer_client.search_transactions(txid=txid)
            txns = tx_resp.get("transactions", [])
            if txns and txns[0].get("round-time") is not None:
                if expected_min_balance is not None:
                    acct = indexer_client.account_info(address).get("account", {})
                    assets = acct.get("assets", [])
                    bal = next(
                        (a.get("amount", 0) for a in assets if a.get("asset-id") == asset_id), 0
                    )
                    if bal >= expected_min_balance:
                        return
                else:
                    return
        except Exception:
            pass
        time.sleep(0.2)
    raise TimeoutError(
        f"Indexer did not index txid {txid} or update balance for {address} within {timeout}s"
    )


def test_subscription_issuer_verification():
    cfg = get_localnet_context()
    algod_client = algod.AlgodClient(cfg["algod_token"], cfg["algod_url"])
    chain = ChainReader(indexer_url=cfg["indexer_url"])
    asset_id = cfg["subscription_asset_id"]

    server_addr = cfg["accounts"]["server"]["address"]
    server_sk = cfg["accounts"]["server"]["private_key"]

    client1_addr = cfg["accounts"]["client1"]["address"]
    client1_sk = cfg["accounts"]["client1"]["private_key"]

    client2_addr = cfg["accounts"]["client2"]["address"]
    client2_sk = cfg["accounts"]["client2"]["private_key"]

    init_server_bal = _get_asset_balance(algod_client, server_addr, asset_id)
    init_client1_bal = _get_asset_balance(algod_client, client1_addr, asset_id)
    init_client2_bal = _get_asset_balance(algod_client, client2_addr, asset_id)

    assert init_client1_bal == 0, (
        f"Expected client1 to have 0 AI-SUB initially, got {init_client1_bal}"
    )

    tokens_server_to_client2 = 0
    tokens_client2_to_client1 = 0

    try:
        # Step 1: Server (issuer) sends 2 AI-SUB to client2
        txid1 = _transfer_asset(algod_client, server_sk, server_addr, client2_addr, asset_id, 2)
        tokens_server_to_client2 += 2
        _wait_for_indexer(
            chain.indexer, txid1, client2_addr, asset_id, expected_min_balance=init_client2_bal + 2
        )

        # client2 must have active subscription with issuer=server
        active_c2 = chain.has_active_subscription(client2_addr, asset_id, issuer=server_addr)
        assert active_c2 is True, "Expected client2 to have active subscription with issuer=server"

        # Step 2: client2 transfers 1 AI-SUB to client1 (client1 had 0)
        txid2 = _transfer_asset(algod_client, client2_sk, client2_addr, client1_addr, asset_id, 1)
        tokens_client2_to_client1 += 1
        _wait_for_indexer(chain.indexer, txid2, client1_addr, asset_id, expected_min_balance=1)

        # client1 has balance > 0, so active with issuer=None
        active_c1_no_issuer = chain.has_active_subscription(client1_addr, asset_id, issuer=None)
        assert active_c1_no_issuer is True, (
            "Expected client1 to have active subscription with issuer=None"
        )

        # client1's recent receipt is from client2, NOT server -> NOT active with issuer=server
        active_c1_with_issuer = chain.has_active_subscription(
            client1_addr, asset_id, issuer=server_addr
        )
        assert active_c1_with_issuer is False, (
            "Expected client1 NOT to have active subscription with issuer=server"
        )

        # Step 3: PaymentGate configured with subscription_issuer=server charges client1 normally
        gate = PaymentGate(
            signer=DefaultAvmSigner(server_sk, algod_client=algod_client),
            network=cfg["network"],
            pay_to=server_addr,
            prices={"ok": 5000},
            subscription_asset_id=asset_id,
            subscription_issuer=server_addr,
            chain=chain,
        )

        price_c1 = gate.price_for("ok", sender=client1_addr)
        price_c2 = gate.price_for("ok", sender=client2_addr)

        assert price_c1 == 5000, f"Expected client1 to be charged 5000, got {price_c1}"
        assert price_c2 == 0, f"Expected client2 to be free (price 0), got {price_c2}"

    finally:
        # Cleanup: restore all transferred tokens
        if tokens_client2_to_client1 > 0:
            _transfer_asset(
                algod_client,
                client1_sk,
                client1_addr,
                client2_addr,
                asset_id,
                tokens_client2_to_client1,
            )
        if tokens_server_to_client2 > 0:
            _transfer_asset(
                algod_client,
                client2_sk,
                client2_addr,
                server_addr,
                asset_id,
                tokens_server_to_client2,
            )

        final_server_bal = _get_asset_balance(algod_client, server_addr, asset_id)
        final_client1_bal = _get_asset_balance(algod_client, client1_addr, asset_id)
        final_client2_bal = _get_asset_balance(algod_client, client2_addr, asset_id)

        assert final_server_bal == init_server_bal, (
            f"Server balance not restored: {final_server_bal} != {init_server_bal}"
        )
        assert final_client1_bal == init_client1_bal, (
            f"Client1 balance not restored: {final_client1_bal} != {init_client1_bal}"
        )
        assert final_client2_bal == init_client2_bal, (
            f"Client2 balance not restored: {final_client2_bal} != {init_client2_bal}"
        )
        print(
            f"Balances successfully restored: server={final_server_bal}, client1={final_client1_bal}, client2={final_client2_bal}"
        )
