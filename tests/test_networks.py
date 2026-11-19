"""The SDK on every Algorand network.

Offline tests always run. Tests marked `public` talk to the real TestNet and MainNet through the
public AlgoNode endpoints (run: PUBLIC_NETS=1 pytest -m public). They never sign anything with a
funded key and never submit a transaction: real funded accounts are only used inside `simulate`
with empty signatures, which the network evaluates against live balances without moving funds.
"""

import base64

import httpx
import pytest
import x402_algorand.constants as C
import x402_algorand.utils as U
from algosdk import account, encoding, transaction
from algosdk.v2client import algod
from algosdk.v2client.models import SimulateRequest, SimulateRequestTransactionGroup
from x402.schemas import PaymentPayload, PaymentRequirements
from x402_algorand.exact.client import ExactAvmScheme as ExactAvmClient
from x402_algorand.exact.facilitator import ExactAvmScheme as ExactAvmFacilitator

from ai_economy_sdk import (
    MAINNET,
    TESTNET,
    ChainReader,
    DefaultAvmSigner,
    PaymentGate,
    endpoints_for,
    genesis_hash_for,
    is_public,
    make_identity_txn,
    resolve_network,
    sign_identity_txn,
    usdc_asset_id,
    verify_identity_txn,
)

PUBLIC = [("testnet", TESTNET), ("mainnet", MAINNET)]


# ---------------------------------------------------------------- offline


@pytest.mark.parametrize(
    "name,caip",
    [
        ("mainnet", MAINNET),
        ("MainNet", MAINNET),
        ("algorand-mainnet", MAINNET),
        ("testnet", TESTNET),
        ("algorand-testnet", TESTNET),
        (MAINNET, MAINNET),
    ],
)
def test_names_resolve_to_caip2(name, caip):
    assert resolve_network(name) == caip


@pytest.mark.parametrize(
    "legacy,short",
    [
        ("algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=", MAINNET),
        ("algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=", TESTNET),
    ],
)
def test_legacy_long_id_resolves_to_short(legacy, short):
    assert resolve_network(legacy) == short


def test_genesis_hash_for_mainnet_and_testnet():
    assert genesis_hash_for("mainnet") == C.MAINNET_GENESIS_HASH
    assert genesis_hash_for(MAINNET) == C.MAINNET_GENESIS_HASH
    assert genesis_hash_for("testnet") == C.TESTNET_GENESIS_HASH
    assert genesis_hash_for(TESTNET) == C.TESTNET_GENESIS_HASH
    assert (
        genesis_hash_for("algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=")
        == C.MAINNET_GENESIS_HASH
    )
    assert (
        genesis_hash_for("algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=")
        == C.TESTNET_GENESIS_HASH
    )


def test_custom_caip2_is_accepted_and_registered():
    raw_b64 = base64.b64encode(b"x" * 32).decode()
    net = "algorand:" + raw_b64
    short_id = resolve_network(net)
    assert short_id.startswith("algorand:")
    assert short_id in C.SUPPORTED_NETWORKS
    assert genesis_hash_for(short_id) == raw_b64


@pytest.mark.parametrize("bad", ["", "ethereum", "eip155:1", "algorand:"])
def test_unknown_network_is_rejected(bad):
    with pytest.raises(ValueError):
        resolve_network(bad)


def test_localnet_name_reads_genesis_from_algod():
    class Algod:
        def suggested_params(self):
            return transaction.SuggestedParams(
                1000, 1, 2, base64.b64encode(b"L" * 32).decode(), "dockernet-v1"
            )

    gh_b64 = base64.b64encode(b"L" * 32).decode()
    expected = U.caip2_from_genesis_hash(gh_b64)
    assert resolve_network("localnet", Algod()) == expected


def test_default_endpoints():
    assert endpoints_for("mainnet").algod_url.startswith("https://mainnet-api")
    assert endpoints_for(TESTNET).indexer_url.startswith("https://testnet-idx")
    assert endpoints_for("localnet").algod_url.endswith(":4001")
    assert is_public(MAINNET) and is_public(TESTNET) and not is_public("algorand:abc")
    assert usdc_asset_id("mainnet") == 31566704 and usdc_asset_id("testnet") == 10458941


@pytest.mark.parametrize("name,caip", PUBLIC)
def test_gate_accepts_network_names(name, caip):
    gate = PaymentGate(
        signer=DefaultAvmSigner(account.generate_account()[0]),
        network=name,
        pay_to=account.generate_account()[1],
        asset_id=usdc_asset_id(name),
        prices={"t": 1000},
    )
    assert gate.network == caip
    assert gate.genesis_hash_b64 == genesis_hash_for(caip)


def test_payment_requirements_validates_with_gate():
    gate = PaymentGate(
        signer=DefaultAvmSigner(account.generate_account()[0]),
        network="mainnet",
        pay_to=account.generate_account()[1],
        asset_id=31566704,
        prices={"t": 1000},
    )
    req = PaymentRequirements(
        scheme="exact",
        network=gate.network,
        asset="31566704",
        amount="1000",
        pay_to=gate.pay_to,
        max_timeout_seconds=300,
    )
    assert req.network == gate.network
    assert req.network == MAINNET


def test_usdc_prices_are_not_compared_with_algo_fees():
    # 1000 = 0.001 USDC. The ALGO fee floor (2000 microalgos) must not reject or bump it.
    gate = PaymentGate(
        signer=DefaultAvmSigner(account.generate_account()[0]),
        network="mainnet",
        pay_to=account.generate_account()[1],
        asset_id=31566704,
        prices={"t": 1000},
    )
    assert gate.price_for("t", {}, None) == 1000
    with pytest.raises(ValueError):
        PaymentGate(
            signer=DefaultAvmSigner(account.generate_account()[0]),
            network="mainnet",
            pay_to=account.generate_account()[1],
            asset_id=0,
            prices={"t": 1000},
        )


def test_identity_proof_is_bound_to_its_network():
    sk, _ = account.generate_account()
    hdr = sign_identity_txn(sk, C.TESTNET_GENESIS_HASH)["X-Identity-Txn"]
    assert verify_identity_txn(hdr, C.TESTNET_GENESIS_HASH)
    assert verify_identity_txn(hdr, C.MAINNET_GENESIS_HASH) is None
    # never submittable: zero fee and a round window that closed long ago
    t = make_identity_txn(account.address_from_private_key(sk), C.MAINNET_GENESIS_HASH)
    assert t.fee == 0 and t.last_valid_round == 1000


def test_chain_reader_for_network_uses_public_indexer():
    assert "mainnet-idx" in ChainReader.for_network("mainnet").indexer.indexer_address


# ---------------------------------------------------------------- live, read-only


def _algod(caip):
    ep = endpoints_for(caip)
    return algod.AlgodClient(ep.algod_token, ep.algod_url)


def _funded_usdc_holders(caip, n=3):
    """Real accounts that hold ALGO and USDC, found through the public indexer."""
    a = _algod(caip)
    rows = httpx.get(
        f"{endpoints_for(caip).indexer_url}/v2/assets/{usdc_asset_id(caip)}/balances",
        params={"currency-greater-than": 5_000_000, "limit": 40},
        timeout=20,
    ).json()["balances"]
    out = []
    for r in rows:
        info = a.account_info(r["address"])
        if info["amount"] - info["min-balance"] > 2_000_000 and not info.get("auth-addr"):
            out.append(r["address"])
        if len(out) == n:
            return out
    pytest.skip("not enough funded USDC holders found")


def _simulate(a, group_b64):
    stx = []
    for b in group_b64:
        d = encoding.msgpack_decode(b)
        stx.append(
            d
            if isinstance(d, transaction.SignedTransaction)
            else transaction.SignedTransaction(d, None)
        )
    res = a.simulate_transactions(
        SimulateRequest(
            txn_groups=[SimulateRequestTransactionGroup(txns=stx)], allow_empty_signatures=True
        )
    )
    return res["txn-groups"][0].get("failure-message")


class _WatchOnly:
    """Builds payments for an address without its key; simulate runs them with empty signatures."""

    def __init__(self, address):
        self.address = address

    def sign_transactions(self, txns, indexes):
        return [None] * len(txns)


@pytest.mark.public
@pytest.mark.parametrize("name,caip", PUBLIC)
def test_live_genesis_matches_constants(name, caip):
    sp = _algod(caip).suggested_params()
    assert resolve_network(f"algorand:{sp.gh}") == caip
    assert genesis_hash_for(caip) == sp.gh
    assert sp.gen == ("mainnet-v1.0" if name == "mainnet" else "testnet-v1.0")


@pytest.mark.public
@pytest.mark.parametrize("name,caip", PUBLIC)
@pytest.mark.parametrize("asset", ["ALGO", "USDC"])
@pytest.mark.parametrize("fee_payer", [True, False])
def test_live_payment_group_is_accepted_by_the_network(name, caip, asset, fee_payer):
    payer, pay_to, fp = _funded_usdc_holders(caip)
    req = PaymentRequirements(
        scheme="exact",
        network=caip,
        asset="0" if asset == "ALGO" else str(usdc_asset_id(caip)),
        amount="10000",
        pay_to=pay_to,
        max_timeout_seconds=300,
        extra={"feePayer": fp} if fee_payer else {},
    )
    payload = ExactAvmClient(_WatchOnly(payer)).create_payment_payload(req)
    assert _simulate(_algod(caip), payload["paymentGroup"]) is None


@pytest.mark.public
@pytest.mark.parametrize("name,caip", PUBLIC)
def test_live_facilitator_checks_pass_and_only_funds_are_missing(name, caip):
    """Full sign + verify with fresh keys: every check before the balance check must pass."""
    a = _algod(caip)
    buyer_sk, _ = account.generate_account()
    fee_sk, fee_addr = account.generate_account()
    _, vendor = account.generate_account()
    req = PaymentRequirements(
        scheme="exact",
        network=caip,
        asset="0",
        amount="10000",
        pay_to=vendor,
        max_timeout_seconds=300,
        extra={"feePayer": fee_addr},
    )
    inner = ExactAvmClient(DefaultAvmSigner(buyer_sk)).create_payment_payload(req)
    res = ExactAvmFacilitator(DefaultAvmSigner(fee_sk, algod_client=a)).verify(
        PaymentPayload(payload=inner, accepted=req), req
    )
    assert not res.is_valid
    assert res.invalid_reason == C.ERR_SIMULATION_FAILED
    assert (
        "overspend" in (res.invalid_message or "").lower()
        or "balance" in (res.invalid_message or "").lower()
    ), res.invalid_message


@pytest.mark.public
def test_live_testnet_payment_is_rejected_by_a_mainnet_facilitator():
    buyer_sk, _ = account.generate_account()
    fee_sk, fee_addr = account.generate_account()
    _, vendor = account.generate_account()
    t_req = PaymentRequirements(
        scheme="exact",
        network=TESTNET,
        asset="0",
        amount="10000",
        pay_to=vendor,
        max_timeout_seconds=300,
        extra={"feePayer": fee_addr},
    )
    inner = ExactAvmClient(DefaultAvmSigner(buyer_sk)).create_payment_payload(t_req)
    m_req = t_req.model_copy(update={"network": MAINNET})
    res = ExactAvmFacilitator(DefaultAvmSigner(fee_sk, algod_client=_algod(MAINNET))).verify(
        PaymentPayload(payload=inner, accepted=m_req), m_req
    )
    assert not res.is_valid
    assert res.invalid_reason == C.ERR_GENESIS_HASH_MISMATCH


@pytest.mark.public
def test_live_chain_reader_reads_mainnet():
    holder = _funded_usdc_holders(MAINNET, 1)[0]
    reader = ChainReader.for_network("mainnet")
    assert reader.past_usage(holder, "A" * 58, 0, days=1) == 0
    assert reader.active_passes(holder, "A" * 58, 0, {"listing:x": 1}) == []
