"""Algorand networks: names, CAIP-2 ids and default endpoints.

Every public entry point of the SDK accepts a network as any of:

- a short name: ``"mainnet"``, ``"testnet"``, ``"localnet"``
- an x402 v1 name: ``"algorand-mainnet"``, ``"algorand-testnet"``
- a CAIP-2 id: ``"algorand:<32-char prefix>"`` or legacy ``"algorand:<genesis hash>"``
  (any network, including a private one)

MainNet and TestNet default to the public AlgoNode endpoints. Override them with the
``ALGOD_MAINNET_URL`` / ``ALGOD_TESTNET_URL`` / ``INDEXER_MAINNET_URL`` / ``INDEXER_TESTNET_URL``
environment variables or by passing URLs explicitly. A LocalNet's genesis hash changes on every
reset, so ``"localnet"`` is resolved by asking its algod.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import x402_algorand.constants as c
import x402_algorand.utils as u
from algosdk.error import AlgodHTTPError

from .exceptions import ConfigurationError, UnknownNetworkError

logger = logging.getLogger("ai_economy_sdk")

LOCALNET_TOKEN = "a" * 64


@dataclass(frozen=True)
class Endpoints:
    """Connection parameters for Algorand node services.

    Parameters
    ----------
    algod_url : str
        URL of the algod daemon endpoint.
    algod_token : str
        Authentication token header value for algod.
    indexer_url : str
        URL of the Algorand indexer service endpoint.
    indexer_token : str
        Authentication token header value for indexer.

    Examples
    --------
    >>> from ai_economy_sdk.network import Endpoints
    >>> ep = Endpoints("http://localhost:4001", "a" * 64, "http://localhost:8980", "")
    >>> ep.algod_url
    'http://localhost:4001'
    """

    algod_url: str
    algod_token: str
    indexer_url: str
    indexer_token: str


LOCALNET = Endpoints(
    os.environ.get("ALGOD_LOCALNET_URL", "http://localhost:4001"),
    LOCALNET_TOKEN,
    os.environ.get("INDEXER_LOCALNET_URL", "http://localhost:8980"),
    "",
)

_PUBLIC = {
    c.ALGORAND_MAINNET_CAIP2: Endpoints(c.MAINNET_ALGOD_URL, "", c.MAINNET_INDEXER_URL, ""),
    c.ALGORAND_TESTNET_CAIP2: Endpoints(c.TESTNET_ALGOD_URL, "", c.TESTNET_INDEXER_URL, ""),
}

_ALIASES = {
    "mainnet": c.ALGORAND_MAINNET_CAIP2,
    "algorand-mainnet": c.ALGORAND_MAINNET_CAIP2,
    "algorand": c.ALGORAND_MAINNET_CAIP2,
    "testnet": c.ALGORAND_TESTNET_CAIP2,
    "algorand-testnet": c.ALGORAND_TESTNET_CAIP2,
}
_LOCAL_NAMES = {"localnet", "algorand-localnet", "dockernet", "sandnet"}

MAINNET = c.ALGORAND_MAINNET_CAIP2
TESTNET = c.ALGORAND_TESTNET_CAIP2


def is_public(network: str) -> bool:
    """Check whether a network identifier represents public MainNet or TestNet.

    Parameters
    ----------
    network : str
        Network name, canonical short CAIP-2 identifier, or legacy long ID.

    Returns
    -------
    bool
        True if the network is Algorand MainNet or TestNet, False otherwise.

    See Also
    --------
    resolve_network : Resolve any alias to a canonical CAIP-2 identifier.

    Examples
    --------
    >>> from ai_economy_sdk.network import is_public, MAINNET, TESTNET
    >>> is_public(MAINNET)
    True
    >>> is_public(TESTNET)
    True
    >>> is_public("localnet")
    False
    """
    if not isinstance(network, str):
        return False
    key = network.strip().lower()
    if key in _ALIASES:
        return _ALIASES[key] in _PUBLIC
    try:
        return u.normalize_network(network.strip()) in _PUBLIC
    except ValueError:
        return False


def resolve_network(network: str, algod_client: Any = None) -> str:
    """Return the CAIP-2 id for a network name or id, and register it with x402.

    Resolves short aliases such as ``"mainnet"`` or ``"testnet"`` to canonical
    short CAIP-2 identifiers (``"algorand:<32-char-prefix>"``). Normalizes legacy
    long IDs (``"algorand:<44-char-hash>"``). When given ``"localnet"`` or its aliases,
    queries the provided ``algod_client`` (or the default localnet endpoint) to
    derive and register the network configuration dynamically.

    Parameters
    ----------
    network : str
        Network name, alias, canonical short CAIP-2 ID, or legacy long ID.
    algod_client : Any, optional
        Pre-configured ``algosdk.v2client.algod.AlgodClient`` instance. Used
        to query genesis parameters when resolving ``"localnet"``.

    Returns
    -------
    str
        Canonical short CAIP-2 network identifier (e.g. ``"algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73k"``).

    Raises
    ------
    UnknownNetworkError
        (a ``ValueError``) If the network name cannot be resolved or is invalid.

    See Also
    --------
    endpoints_for : Resolves connection endpoints for the network.
    genesis_hash_for : Returns the full 44-character base64 genesis hash.

    Examples
    --------
    >>> from ai_economy_sdk.network import resolve_network, MAINNET, TESTNET
    >>> resolve_network("mainnet") == MAINNET
    True
    >>> resolve_network("testnet") == TESTNET
    True
    >>> resolve_network("algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=") == MAINNET
    True
    """
    if not isinstance(network, str) or not network.strip():
        raise UnknownNetworkError(
            "network must be a non-empty string such as 'mainnet', 'testnet', 'localnet' or 'algorand:<genesis hash>'"
        )
    name = network.strip()
    key = name.lower()
    if key in _ALIASES:
        return _ALIASES[key]
    if key in _LOCAL_NAMES:
        from algosdk.v2client import algod

        return network_from_algod(
            algod_client or algod.AlgodClient(LOCALNET.algod_token, LOCALNET.algod_url)
        )
    try:
        norm = u.normalize_network(name)
        if norm not in u.NETWORK_CONFIGS:
            try:
                from algosdk.v2client import algod

                client = algod_client or algod.AlgodClient(LOCALNET.algod_token, LOCALNET.algod_url)
                sp = client.suggested_params()
                if u.caip2_from_genesis_hash(sp.gh) == norm:
                    u.register_network(sp.gh, sp.gen)
            except (AlgodHTTPError, OSError, ValueError):
                # No local algod (or a different one): the id stays unregistered and
                # x402 reports it as unsupported when it is used.
                logger.debug("could not register %s from the local algod", norm, exc_info=True)
        return norm
    except ValueError:
        raise UnknownNetworkError(
            f"unknown network {network!r}: use 'mainnet', 'testnet', 'localnet' or 'algorand:<genesis hash>'"
        ) from None


def endpoints_for(network: str) -> Endpoints:
    """Default algod/indexer endpoints: AlgoNode for MainNet/TestNet, LocalNet for anything else.

    Parameters
    ----------
    network : str
        Network name, alias, canonical short CAIP-2 ID, or legacy long ID.

    Returns
    -------
    Endpoints
        Named tuple containing algod and indexer URL and token strings.

    See Also
    --------
    resolve_network : Normalizes network identifiers.

    Examples
    --------
    >>> from ai_economy_sdk.network import endpoints_for
    >>> ep = endpoints_for("mainnet")
    >>> "algonode.cloud" in ep.algod_url
    True
    >>> ep = endpoints_for("localnet")
    >>> ep.algod_url
    'http://localhost:4001'
    """
    if not isinstance(network, str):
        return LOCALNET
    key = network.strip().lower()
    if key in _ALIASES:
        caip = _ALIASES[key]
    else:
        try:
            caip = u.normalize_network(network.strip())
        except ValueError:
            caip = network
    return _PUBLIC.get(caip, LOCALNET)


def register_network(
    network: str,
    genesis_id: str = "dockernet-v1",
    algod_url: str | None = None,
    indexer_url: str | None = None,
) -> None:
    """Register an Algorand network in x402's network tables (no-op for MainNet/TestNet, which are built in).

    Parameters
    ----------
    network : str
        Network name, bare 44-character genesis hash, legacy long CAIP-2 ID,
        or canonical short CAIP-2 ID.
    genesis_id : str, default: "dockernet-v1"
        Genesis ID string associated with the network (e.g. "mainnet-v1.0").
    algod_url : str, optional
        Custom Algod REST endpoint URL. If omitted, uses default endpoint.
    indexer_url : str, optional
        Custom Indexer REST endpoint URL. If omitted, uses default endpoint.

    Raises
    ------
    ConfigurationError
        (a ``ValueError``) If ``network`` is not a string.
    UnknownNetworkError
        (a ``ValueError``) If an unknown short CAIP-2 ID is passed without the full
        genesis hash; use ``network_from_algod`` instead.

    See Also
    --------
    network_from_algod : Discovers and registers network parameters from Algod.

    Examples
    --------
    >>> from ai_economy_sdk.network import register_network
    >>> register_network("mainnet")
    """
    if not isinstance(network, str):
        raise ConfigurationError("network must be a string")
    net = network.strip()
    # If network is a legacy long id or bare 44-char genesis hash
    raw_hash = net[len("algorand:") :] if net.startswith("algorand:") else net
    if len(raw_hash) == 44:
        u.register_network(
            raw_hash, genesis_id=genesis_id, algod_url=algod_url, indexer_url=indexer_url
        )
        return

    # If it is a known short id or alias
    resolved = _ALIASES.get(net.lower(), net)
    if resolved in u.NETWORK_CONFIGS:
        return

    # Short unknown id:
    raise UnknownNetworkError("pass the genesis hash or use network_from_algod")


def network_from_algod(algod_client: Any) -> str:
    """Derive the CAIP-2 network id from an algod client and register it.

    Parameters
    ----------
    algod_client : Any
        Active ``algosdk.v2client.algod.AlgodClient`` instance.

    Returns
    -------
    str
        Registered canonical short CAIP-2 network identifier.

    See Also
    --------
    register_network : Manually register network parameters.

    Examples
    --------
    ```python
    # Requires running Algod node:
    from algosdk.v2client import algod
    from ai_economy_sdk.network import network_from_algod

    client = algod.AlgodClient("a" * 64, "http://localhost:4001")
    caip = network_from_algod(client)
    print(caip)
    ```
    """
    sp = algod_client.suggested_params()
    return u.register_network(sp.gh, sp.gen)


def usdc_asset_id(network: str) -> int | None:
    """Circle USDC's asset id on MainNet/TestNet; None elsewhere.

    Parameters
    ----------
    network : str
        Network name, alias, canonical short CAIP-2 ID, or legacy long ID.

    Returns
    -------
    int or None
        Circle USDC ASA ID (31566704 on MainNet, 10458941 on TestNet), or None
        if the network does not have an official Circle USDC deployment.

    See Also
    --------
    resolve_network : Resolves network aliases to CAIP-2 identifiers.

    Examples
    --------
    >>> from ai_economy_sdk.network import usdc_asset_id
    >>> usdc_asset_id("mainnet")
    31566704
    >>> usdc_asset_id("testnet")
    10458941
    >>> usdc_asset_id("localnet") is None
    True
    """
    if not isinstance(network, str):
        return None
    key = network.strip().lower()
    if key in _ALIASES:
        caip = _ALIASES[key]
    else:
        try:
            caip = u.normalize_network(network.strip())
        except ValueError:
            caip = network
    return {
        c.ALGORAND_MAINNET_CAIP2: c.USDC_MAINNET_ASA_ID,
        c.ALGORAND_TESTNET_CAIP2: c.USDC_TESTNET_ASA_ID,
    }.get(caip)


def genesis_hash_for(network: str) -> str:
    """Return the full 44-character base64 genesis hash for a network.

    Parameters
    ----------
    network : str
        Network name (``"mainnet"``, ``"testnet"``, ``"localnet"``), canonical
        short CAIP-2 identifier (``"algorand:<32-char-prefix>"``), or legacy long ID.

    Returns
    -------
    str
        Full 44-character base64-encoded genesis hash including trailing ``=``.

    See Also
    --------
    resolve_network : Resolves network names and aliases to short CAIP-2 IDs.

    Examples
    --------
    >>> from ai_economy_sdk.network import genesis_hash_for
    >>> genesis_hash_for("mainnet")
    'wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8='
    >>> genesis_hash_for("testnet")
    'SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI='
    """
    return u.get_genesis_hash(resolve_network(network))
