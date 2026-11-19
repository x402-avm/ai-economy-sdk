"""Pytest configuration and self-contained LocalNet fixtures for ai-economy-sdk."""

import os

import pytest
from algosdk import account, kmd, mnemonic, transaction
from algosdk.v2client import algod
from x402_algorand.utils import caip2_from_genesis_hash, register_network

LOCALNET_ALGOD_URL = os.environ.get("ALGOD_URL", "http://localhost:4001")
LOCALNET_ALGOD_TOKEN = os.environ.get("ALGOD_TOKEN", "a" * 64)
LOCALNET_KMD_URL = os.environ.get("KMD_URL", "http://localhost:4002")
LOCALNET_KMD_TOKEN = os.environ.get("KMD_TOKEN", "a" * 64)
LOCALNET_INDEXER_URL = os.environ.get("INDEXER_URL", "http://localhost:8980")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "localnet: mark test as requiring running LocalNet (LOCALNET=1)"
    )
    config.addinivalue_line(
        "markers", "public: mark test as requiring public networks (PUBLIC_NETS=1)"
    )
    config.addinivalue_line(
        "markers", "public_nets: mark test as requiring public networks (PUBLIC_NETS=1)"
    )


def pytest_collection_modifyitems(config, items):
    """Filter tests by environment tiers."""
    is_localnet = os.environ.get("LOCALNET") == "1"
    is_public = os.environ.get("PUBLIC_NETS") == "1"

    skip_localnet = pytest.mark.skip(reason="set LOCALNET=1 with algokit localnet running")
    skip_public = pytest.mark.skip(
        reason="set PUBLIC_NETS=1 to test against the real TestNet and MainNet (read-only)"
    )

    for item in items:
        if "localnet" in item.keywords and not is_localnet:
            item.add_marker(skip_localnet)
        if ("public" in item.keywords or "public_nets" in item.keywords) and not is_public:
            item.add_marker(skip_public)


def get_localnet_algod() -> algod.AlgodClient:
    return algod.AlgodClient(LOCALNET_ALGOD_TOKEN, LOCALNET_ALGOD_URL)


def get_localnet_kmd() -> kmd.KMDClient:
    return kmd.KMDClient(LOCALNET_KMD_TOKEN, LOCALNET_KMD_URL)


def get_dispenser(algod_c: algod.AlgodClient | None = None) -> tuple[str, str]:
    """Retrieve the KMD dispenser address and private key on LocalNet."""
    algod_client = algod_c or get_localnet_algod()
    k = get_localnet_kmd()
    wallets = [w for w in k.list_wallets() if w.get("name") == "unencrypted-default-wallet"]
    if not wallets:
        raise RuntimeError("No unencrypted-default-wallet found in KMD")
    h = k.init_wallet_handle(wallets[0]["id"], "")
    keys = k.list_keys(h)
    addr = max(keys, key=lambda a: algod_client.account_info(a)["amount"])
    sk = k.export_key(h, "", addr)
    return addr, sk


def fund_account(addr: str, algos: float = 100.0, algod_c: algod.AlgodClient | None = None) -> None:
    """Fund an address with ALGO from the KMD dispenser."""
    algod_client = algod_c or get_localnet_algod()
    d_addr, d_sk = get_dispenser(algod_client)
    sp = algod_client.suggested_params()
    txn = transaction.PaymentTxn(d_addr, sp, addr, int(algos * 1_000_000))
    stxn = txn.sign(d_sk)
    txid = algod_client.send_transaction(stxn)
    transaction.wait_for_confirmation(algod_client, txid, 4)


def new_funded_account(
    algos: float = 100.0, algod_c: algod.AlgodClient | None = None
) -> tuple[str, str, str]:
    """Create a new account on LocalNet and fund it. Returns (private_key, address, mnemonic)."""
    algod_client = algod_c or get_localnet_algod()
    sk, addr = account.generate_account()
    mn = mnemonic.from_private_key(sk)
    fund_account(addr, algos=algos, algod_c=algod_client)
    return sk, addr, mn


def create_asset(
    creator_sk: str,
    creator_addr: str,
    total: int = 1_000_000,
    decimals: int = 0,
    unit_name: str = "PASS",
    asset_name: str = "AI-SUB",
    algod_c: algod.AlgodClient | None = None,
) -> int:
    """Create an ASA on LocalNet and return its asset ID."""
    algod_client = algod_c or get_localnet_algod()
    sp = algod_client.suggested_params()
    txn = transaction.AssetCreateTxn(
        sender=creator_addr,
        sp=sp,
        total=total,
        decimals=decimals,
        default_frozen=False,
        unit_name=unit_name,
        asset_name=asset_name,
        manager=creator_addr,
        reserve=creator_addr,
    )
    stxn = txn.sign(creator_sk)
    txid = algod_client.send_transaction(stxn)
    res = transaction.wait_for_confirmation(algod_client, txid, 4)
    return int(res["asset-index"])


def opt_in_asset(
    account_sk: str,
    account_addr: str,
    asset_id: int,
    algod_c: algod.AlgodClient | None = None,
) -> None:
    """Opt an account into an ASA."""
    algod_client = algod_c or get_localnet_algod()
    sp = algod_client.suggested_params()
    txn = transaction.AssetOptInTxn(account_addr, sp, asset_id)
    stxn = txn.sign(account_sk)
    txid = algod_client.send_transaction(stxn)
    transaction.wait_for_confirmation(algod_client, txid, 4)


_cached_localnet_context = None


def get_localnet_context(force_refresh: bool = False) -> dict:
    """Provide a self-contained LocalNet environment with funded accounts and ASAs."""
    global _cached_localnet_context
    if _cached_localnet_context is not None and not force_refresh:
        return _cached_localnet_context

    algod_c = get_localnet_algod()
    sp = algod_c.suggested_params()
    register_network(sp.gh)
    network = caip2_from_genesis_hash(sp.gh)

    server_sk, server_addr, server_mn = new_funded_account(100.0, algod_c)
    client1_sk, client1_addr, client1_mn = new_funded_account(100.0, algod_c)
    client2_sk, client2_addr, client2_mn = new_funded_account(100.0, algod_c)

    sub_id = create_asset(
        server_sk,
        server_addr,
        total=1_000_000,
        decimals=0,
        unit_name="PASS",
        asset_name="AI-SUB",
        algod_c=algod_c,
    )
    musd_id = create_asset(
        server_sk,
        server_addr,
        total=10**15,
        decimals=6,
        unit_name="mUSD",
        asset_name="Marketplace USD",
        algod_c=algod_c,
    )

    opt_in_asset(client1_sk, client1_addr, sub_id, algod_c)
    opt_in_asset(client2_sk, client2_addr, sub_id, algod_c)
    opt_in_asset(client1_sk, client1_addr, musd_id, algod_c)
    opt_in_asset(client2_sk, client2_addr, musd_id, algod_c)

    _cached_localnet_context = {
        "network": network,
        "genesis_hash": sp.gh,
        "algod_url": LOCALNET_ALGOD_URL,
        "algod_token": LOCALNET_ALGOD_TOKEN,
        "kmd_url": LOCALNET_KMD_URL,
        "indexer_url": LOCALNET_INDEXER_URL,
        "subscription_asset_id": sub_id,
        "musd_asset_id": musd_id,
        "accounts": {
            "server": {"address": server_addr, "private_key": server_sk, "mnemonic": server_mn},
            "client1": {"address": client1_addr, "private_key": client1_sk, "mnemonic": client1_mn},
            "client2": {"address": client2_addr, "private_key": client2_sk, "mnemonic": client2_mn},
        },
    }
    return _cached_localnet_context


@pytest.fixture(scope="session")
def localnet_context():
    """Session-scoped fixture yielding self-contained LocalNet accounts and assets."""
    return get_localnet_context()
