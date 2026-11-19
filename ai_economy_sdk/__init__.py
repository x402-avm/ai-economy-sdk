"""AI Economy SDK — Payment-gated MCP server for Algorand.

Turns any Model Context Protocol (MCP) server into an autonomous, paid service
monetized with x402 payment requirements on the Algorand blockchain.

Exports
-------
AIEconomySDK : High-level builder for FastMCP servers with dynamic per-tool pricing.
PaymentGate : Low-level ASGI/Starlette middleware enforcing x402 payment challenges.
DefaultAvmSigner : Algorand Virtual Machine signer implementation.
ChainReader : Indexer client for queries on past usage, subscriptions, and passes.
PASS_NOTE_PREFIX : Note prefix identifying on-chain vendor passes (``"x402-pass:"``).
sign_identity : Generate legacy Ed25519 identity headers for account ownership.
verify_identity : Validate modern (X-Identity-Txn) or legacy identity headers.
make_identity_txn : Build unsigned zero-amount self-payment identity transaction.
sign_identity_txn : Sign zero-amount self-payment into an ``X-Identity-Txn`` header.
verify_identity_txn : Cryptographically verify an ``X-Identity-Txn`` header.
paid_mcp_session : Client-side async context manager auto-signing 402 challenges.
register_network : Register custom network genesis hashes and node endpoints.
network_from_algod : Derive and register network CAIP-2 id from an active AlgodClient.
genesis_hash_for : Return full 44-character base64 genesis hash for a network.
resolve_network : Resolve network names or aliases into canonical CAIP-2 strings.
endpoints_for : Get default algod and indexer endpoints for a given network.
is_public : Check whether a network identifier represents MainNet or TestNet.
usdc_asset_id : Look up Circle USDC ASA ID for a network.
MAINNET : Canonical CAIP-2 string for Algorand MainNet.
TESTNET : Canonical CAIP-2 string for Algorand TestNet.
IDENTITY_HEADERS : Tuple of legacy identity header names.
AIEconomyError : Base class of every exception the SDK raises (see ``ai_economy_sdk.exceptions``).
SpendLimitExceededError : Raised by ``paid_mcp_session`` when the buyer's spend controls refuse a payment.
__version__ : Installed package version.

Logging
-------
The SDK logs to the ``"ai_economy_sdk"`` logger: payment refusals, settlement failures
and failed chain lookups at WARNING, routine payment decisions at INFO/DEBUG.
"""

from __future__ import annotations

import inspect
import logging
import warnings
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from typing import Any, TypeVar

from fastmcp import FastMCP

from .chain import PASS_NOTE_PREFIX, ChainReader
from .client import paid_mcp_session
from .exceptions import (
    AIEconomyError,
    AlgodClientRequiredError,
    ConfigurationError,
    PaymentRequiredError,
    PaymentVerificationError,
    SettlementError,
    SpendLimitExceededError,
    UnknownNetworkError,
)
from .gate import PaymentGate, SettleOnSuccessMiddleware
from .identity import (
    IDENTITY_HEADERS,
    make_identity_txn,
    sign_identity,
    sign_identity_txn,
    verify_identity,
    verify_identity_txn,
)
from .network import (
    MAINNET,
    TESTNET,
    endpoints_for,
    genesis_hash_for,
    is_public,
    network_from_algod,
    register_network,
    resolve_network,
    usdc_asset_id,
)
from .signer import DefaultAvmSigner
from .types import ChainLookup, Prices, ServerSigner, ToolPriceFunction, Transport

try:
    __version__ = _dist_version("ai-economy-sdk")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"

logger = logging.getLogger("ai_economy_sdk")
logger.addHandler(logging.NullHandler())

__all__ = [
    "__version__",
    "AIEconomySDK",
    "PaymentGate",
    "SettleOnSuccessMiddleware",
    "DefaultAvmSigner",
    "ChainReader",
    "PASS_NOTE_PREFIX",
    "sign_identity",
    "verify_identity",
    "make_identity_txn",
    "sign_identity_txn",
    "verify_identity_txn",
    "paid_mcp_session",
    "register_network",
    "network_from_algod",
    "genesis_hash_for",
    "resolve_network",
    "endpoints_for",
    "is_public",
    "usdc_asset_id",
    "MAINNET",
    "TESTNET",
    "IDENTITY_HEADERS",
    "AIEconomyError",
    "ConfigurationError",
    "UnknownNetworkError",
    "AlgodClientRequiredError",
    "PaymentVerificationError",
    "PaymentRequiredError",
    "SettlementError",
    "SpendLimitExceededError",
    "ServerSigner",
    "ChainLookup",
    "Prices",
    "ToolPriceFunction",
    "Transport",
]

_F = TypeVar("_F", bound=Callable[..., Any])


def _normalize_pricing(
    name: str, fn: ToolPriceFunction
) -> Callable[[dict[str, Any], str | None], int]:
    """Return ``fn`` as a two-argument ``(args, sender)`` function.

    The arity is decided once, here, from the signature. Calling ``fn`` and retrying
    with fewer arguments on ``TypeError`` would also swallow a ``TypeError`` raised
    inside ``fn`` and call it again with the wrong arguments.
    """
    if not callable(fn):
        raise ConfigurationError(f"pricing_func for tool {name!r} must be callable")
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn  # type: ignore[return-value]  # no introspectable signature: use (args, sender)
    try:
        sig.bind(None, None)
        return fn  # type: ignore[return-value]
    except TypeError:
        pass
    try:
        sig.bind(None)
    except TypeError:
        raise ConfigurationError(
            f"pricing_func for tool {name!r} must accept (args, sender) or (args); got {sig}"
        ) from None
    one_arg: Callable[[dict[str, Any]], int] = fn  # type: ignore[assignment]

    def two_arg(args: dict[str, Any], sender: str | None) -> int:
        return one_arg(args)

    return two_arg


class AIEconomySDK:
    """Payment-gated MCP server SDK.

    High-level framework for building Model Context Protocol servers on Algorand.
    Provides decorator-based tool registration, dynamic per-tool pricing callbacks,
    subscription checking, and automatic PaymentGate ASGI integration.

    Parameters
    ----------
    server_signer : ServerSigner
        Signer instance (e.g., ``DefaultAvmSigner``) providing private keys and
        algod connection to sponsor fees as fee payer.
    network : str
        Network name (``"mainnet"``, ``"testnet"``, ``"localnet"``) or CAIP-2 identifier.
    asset_id : str or int
        Asset ID accepted for payments (0 for native ALGO in microAlgos, or ASA ID).
    pay_to_address : str
        Algorand address where service payments are deposited.
    indexer_url : str, optional
        URL of the Algorand Indexer API for checking past usage and subscriptions.
    subscription_asset_id : str, int, or None, optional
        ASA ID required for subscription-based free access.
    name : str, default: "ai_economy_server"
        Name of the FastMCP server application.

    Attributes
    ----------
    mcp : FastMCP
        Underlying FastMCP instance managing registered tools.
    chain : ChainReader or None
        ChainReader instance if ``indexer_url`` is provided.

    See Also
    --------
    PaymentGate : Middleware powering the x402 payment enforcement.
    DefaultAvmSigner : Default signing implementation.

    Examples
    --------
    >>> from algosdk import account
    >>> from ai_economy_sdk.signer import DefaultAvmSigner
    >>> from ai_economy_sdk import AIEconomySDK
    >>> sk, addr = account.generate_account()
    >>> signer = DefaultAvmSigner(sk)
    >>> sdk = AIEconomySDK(server_signer=signer, network="mainnet", asset_id=0, pay_to_address=addr)
    >>> @sdk.add_tool(name="calc", description="Run calc", pricing_func=lambda args, sender: 5000)
    ... def calc(x: int) -> int:
    ...     return x * 2
    """

    def __init__(
        self,
        server_signer: ServerSigner,
        network: str,
        asset_id: str | int,
        pay_to_address: str,
        indexer_url: str | None = None,
        subscription_asset_id: str | int | None = None,
        name: str = "ai_economy_server",
    ) -> None:
        """Initialize the AIEconomySDK server builder."""
        self.server_signer = server_signer
        network = resolve_network(network, getattr(server_signer, "algod_client", None))
        self.network = network
        self.asset_id = str(asset_id)
        self.pay_to_address = pay_to_address
        self.subscription_asset_id = subscription_asset_id
        self.indexer_url = indexer_url

        self.mcp = FastMCP(name)
        self.chain = ChainReader(indexer_url=indexer_url) if indexer_url else None
        self._pricing: dict[str, Callable[[dict[str, Any], str | None], int]] = {}

    def add_tool(
        self,
        name: str,
        description: str,
        pricing_func: ToolPriceFunction,
    ) -> Callable[[_F], _F]:
        """Decorator — register an MCP tool with dynamic pricing.

        Parameters
        ----------
        name : str
            Unique tool name recognized by MCP clients.
        description : str
            Human-readable description of tool behavior.
        pricing_func : ToolPriceFunction
            Pricing function calculating cost in base units. May accept
            ``(args, sender)`` or ``(args)``; which one is decided from its signature
            when the tool is registered.

        Returns
        -------
        Callable
            Decorator wrapping the target tool function.

        Raises
        ------
        ConfigurationError
            If ``pricing_func`` accepts neither ``(args, sender)`` nor ``(args)``.

        Examples
        --------
        >>> from algosdk import account
        >>> from ai_economy_sdk.signer import DefaultAvmSigner
        >>> from ai_economy_sdk import AIEconomySDK
        >>> sk, addr = account.generate_account()
        >>> sdk = AIEconomySDK(server_signer=DefaultAvmSigner(sk), network="mainnet", asset_id=0, pay_to_address=addr)
        >>> @sdk.add_tool("greet", "Greeting tool", lambda args, sender: 3000)
        ... def greet(name: str) -> str:
        ...     return f"Hello, {name}"
        """

        pricing = _normalize_pricing(name, pricing_func)

        def decorator(func: _F) -> _F:
            self._pricing[name] = pricing
            self.mcp.tool(name=name, description=description)(func)
            return func

        return decorator

    def build_app(self, transport: Transport | None = None, **wrap_kwargs: Any) -> Any:
        """Build and wrap the FastMCP application with PaymentGate.

        Constructs a ``PaymentGate`` instance wired with registered pricing
        functions and wraps the FastMCP application as an ASGI app.

        Parameters
        ----------
        transport : {"streamable-http", "http", "sse"}, optional
            Passed to :meth:`PaymentGate.wrap`. When omitted, the legacy SSE transport is
            served and a ``FutureWarning`` is emitted (the default will change to
            ``"streamable-http"``).
        **wrap_kwargs
            Further keyword arguments for :meth:`PaymentGate.wrap` (``path``, ...).

        Returns
        -------
        starlette.applications.Starlette
            ASGI application ready to serve with uvicorn.

        Examples
        --------
        ```python
        # Needs configured server:
        # app = sdk.build_app(transport="streamable-http")
        # uvicorn.run(app, host="127.0.0.1", port=8000)
        ```
        """

        def _get_price(tool: str, args: dict[str, Any], sender: str | None) -> int:
            fn = self._pricing.get(tool)
            return fn(args, sender) if fn is not None else 0

        gate = PaymentGate(
            signer=self.server_signer,
            network=self.network,
            pay_to=self.pay_to_address,
            asset_id=self.asset_id,
            prices=_get_price,
            chain=self.chain,
            subscription_asset_id=self.subscription_asset_id,
        )
        if transport is None:
            # Warn here (not in wrap) so the warning points at the caller of build_app.
            warnings.warn(
                "AIEconomySDK.build_app() was called without transport=; it serves the "
                "legacy SSE transport for compatibility. Pass transport='streamable-http' "
                "(recommended) or transport='sse' explicitly: the default will change to "
                "'streamable-http' in a future release.",
                FutureWarning,
                stacklevel=2,
            )
            transport = "sse"
        return gate.wrap(self.mcp, transport, **wrap_kwargs)

    def has_active_subscription(self, sender_address: str, max_age_days: float = 30.0) -> bool:
        """Check if the sender holds the subscription asset and received it recently.

        Parameters
        ----------
        sender_address : str
            Algorand address of the sender.
        max_age_days : float, default: 30.0
            Maximum age in days of the latest subscription token transfer.

        Returns
        -------
        bool
            True if sender holds the subscription asset and received it within
            ``max_age_days``, False otherwise.

        Examples
        --------
        >>> from algosdk import account
        >>> from ai_economy_sdk.signer import DefaultAvmSigner
        >>> from ai_economy_sdk import AIEconomySDK
        >>> sk, addr = account.generate_account()
        >>> sdk = AIEconomySDK(server_signer=DefaultAvmSigner(sk), network="mainnet", asset_id=0, pay_to_address=addr)
        >>> sdk.has_active_subscription("")
        False
        """
        if not self.chain or not self.subscription_asset_id or not sender_address:
            return False
        return self.chain.has_active_subscription(
            address=sender_address,
            asset_id=self.subscription_asset_id,
            max_age_days=max_age_days,
        )

    def get_past_usage_amount(self, sender_address: str, days: int = 30) -> int:
        """Calculate how much the sender has paid to this server in the last N days.

        Parameters
        ----------
        sender_address : str
            Algorand address of the sender.
        days : int, default: 30
            Number of days prior to current time to tally.

        Returns
        -------
        int
            Total spend in base units (microAlgos for ALGO, or base ASA units).

        Examples
        --------
        >>> from algosdk import account
        >>> from ai_economy_sdk.signer import DefaultAvmSigner
        >>> from ai_economy_sdk import AIEconomySDK
        >>> sk, addr = account.generate_account()
        >>> sdk = AIEconomySDK(server_signer=DefaultAvmSigner(sk), network="mainnet", asset_id=0, pay_to_address=addr)
        >>> sdk.get_past_usage_amount("")
        0
        """
        if not self.chain or not sender_address:
            return 0
        return self.chain.past_usage(
            address=sender_address,
            pay_to=self.pay_to_address,
            asset_id=self.asset_id,
            days=days,
        )
