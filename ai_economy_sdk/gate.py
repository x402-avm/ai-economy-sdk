"""Payment gate middleware for FastMCP using x402 on Algorand.

``PaymentGate`` prices every MCP ``tools/call`` request that reaches the ASGI app,
answers HTTP 402 Payment Required with x402 requirements when a payment is due,
verifies the buyer's payment (signature, fee-payer group, replay), and settles it on
chain only after the tool succeeds.

The gate is transport-agnostic: it inspects the JSON-RPC body of every HTTP ``POST``,
whatever its path, so it covers Streamable HTTP (``POST /mcp``), the legacy SSE
transport (``POST /messages/``) and custom mount points alike. A second line of
defence inside FastMCP (:class:`SettleOnSuccessMiddleware`) refuses any priced tool
call that did not pass through the HTTP gate, so a transport the gate does not see
fails closed instead of serving tools unpaid.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import threading
import time
import uuid
import warnings
from dataclasses import dataclass, field
from typing import Any, cast

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware as FastMCPMiddleware
from starlette.middleware import Middleware as StarletteMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from x402 import x402Facilitator, x402ResourceServer
from x402.http.middleware.fastapi import FastAPIAdapter
from x402.http.types import HTTPRequestContext
from x402.http.x402_http_server import x402HTTPResourceServer
from x402_algorand.exact.register import (
    register_exact_avm_facilitator,
    register_exact_avm_server,
)

from .chain import LOOKUP_ERRORS
from .exceptions import ConfigurationError, PaymentRequiredError, SettlementError
from .identity import verify_identity
from .network import genesis_hash_for, resolve_network
from .replay import ReplayStore
from .types import ChainLookup, Prices, ServerSigner, ToolArguments, Transport

logger = logging.getLogger("ai_economy_sdk")

#: Key under ``scope["state"]`` where the HTTP gate records its decision for a request.
_STATE_KEY = "ai_economy_sdk.gate"
#: Decision value for a tool call the gate priced at zero.
_FREE = "free"

_TRANSPORTS = ("streamable-http", "http", "sse")


@dataclass
class PendingPayment:
    """Internal container for in-flight payment state between verification and settlement.

    Parameters
    ----------
    payment_payload : Any
        Decoded x402 payment payload object received from the client.
    payment_requirements : Any
        x402 payment requirement definition.
    hash : str
        SHA-256 hash string identifying the payment payload for replay tracking.
    created_at : float, default: time.time
        Timestamp when reservation was recorded.
    done : asyncio.Event, default: field(default_factory=asyncio.Event)
        Event signaled when tool execution and settlement finishes.
    settle_headers : dict[str, str], default: field(default_factory=dict)
        HTTP headers returned by facilitator settlement (e.g. PAYMENT-RESPONSE).
    """

    payment_payload: Any
    payment_requirements: Any
    hash: str
    created_at: float = field(default_factory=time.time)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    settle_headers: dict[str, str] = field(default_factory=dict)


def _current_gate_decision() -> str | None:
    """Return the HTTP gate's decision for the request behind the current tool call.

    ``None`` means the tool call did not arrive through an HTTP request the gate saw.
    """
    try:
        request = get_http_request()
    except RuntimeError:
        return None
    state = request.scope.get("state")
    if not isinstance(state, dict):
        return None
    decision = state.get(_STATE_KEY)
    return decision if isinstance(decision, str) else None


class SettleOnSuccessMiddleware(FastMCPMiddleware):
    """FastMCP middleware that settles payments on chain only upon tool success.

    If the tool raises an exception or returns an error result (``is_error=True``),
    the payment reservation in ReplayStore is released and the buyer is not charged.

    It also refuses (with :class:`~ai_economy_sdk.exceptions.PaymentRequiredError`)
    any tool call with a positive price that did not pass through the gate's HTTP
    middleware, for example a call made over a transport the gate does not wrap.
    """

    def __init__(self, gate: PaymentGate) -> None:
        self.gate = gate
        gate._settlement_attached = True

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        decision = _current_gate_decision()

        if decision == _FREE:
            return await call_next(context)

        if decision is None:
            message = getattr(context, "message", None)
            tool = getattr(message, "name", None)
            args = getattr(message, "arguments", None) or {}
            price = await asyncio.to_thread(self.gate.price_for, cast(str, tool), args, None)
            if price > 0:
                logger.warning(
                    "refused tool call %r: it did not pass through the payment gate "
                    "(transport not wrapped by PaymentGate?)",
                    tool,
                )
                raise PaymentRequiredError(
                    f"payment required: tool {tool!r} must be called through the payment gate"
                )
            return await call_next(context)

        pending = self.gate._take_pending(decision)
        if pending is None:
            logger.warning(
                "refused tool call: payment reservation %s expired or was already used", decision
            )
            raise PaymentRequiredError("payment required: the payment reservation has expired")

        settled = False
        try:
            res = await call_next(context)
            if getattr(res, "is_error", False) or getattr(res, "isError", False):
                logger.info("tool returned an error; payment %s not settled", pending.hash[:16])
                return res

            try:
                settle_result = await self.gate.http_server.process_settlement(
                    pending.payment_payload,
                    pending.payment_requirements,
                )
            except Exception as e:  # facilitator boundary: any failure means "not settled"
                logger.warning(
                    "settlement of payment %s raised; tool output withheld",
                    pending.hash[:16],
                    exc_info=True,
                )
                raise SettlementError(f"payment settlement failed: {e}") from e

            if not settle_result.success:
                reason = getattr(settle_result, "error_reason", None) or "Settlement failed"
                logger.warning(
                    "settlement of payment %s failed: %s; tool output withheld",
                    pending.hash[:16],
                    reason,
                )
                raise SettlementError(f"payment settlement failed: {reason}")

            self.gate.replay_store.commit(pending.hash)
            settled = True
            if getattr(settle_result, "headers", None):
                pending.settle_headers.update(settle_result.headers)
            return res
        finally:
            if not settled:
                self.gate.replay_store.release(pending.hash)
            pending.done.set()


class PaymentGate:
    """Payment gate protecting FastMCP server tools with x402 on Algorand.

    Integrates payment challenges, verification, and settlement into FastMCP's HTTP
    transports (Streamable HTTP and legacy SSE). Supports fixed, dynamic,
    subscription-based, pass-based, and tiered bulk pricing.

    Parameters
    ----------
    signer : ServerSigner
        Signer (e.g., ``DefaultAvmSigner``) providing merchant keys and node
        connection to sponsor transaction fees as fee payer.
    network : str
        Network name (``"mainnet"``, ``"testnet"``, ``"localnet"``) or CAIP-2 identifier.
    pay_to : str
        Algorand address where service payments are collected.
    asset_id : int or str, default: 0
        Asset ID accepted for payments (0 for native ALGO in microAlgos, or ASA ID).
    prices : Mapping[str, int] or callable, optional
        Tool pricing mapping or callable with signature ``(tool, args, sender) -> int``.
    default_price : int, default: 0
        Fallback price in base units for tools not explicitly listed in ``prices``.
    chain : ChainLookup, optional
        ``ChainReader`` (or compatible object) for subscriptions, past usage, and passes.
    subscription_asset_id : int, str, or None, optional
        ASA ID of the subscription token. If held by caller, tools are free.
    subscription_max_age_days : float, default: 30.0
        Maximum age in days of latest subscription token transfer.
    bulk_threshold : int, optional
        Cumulative spend threshold triggering the bulk discount rate.
    bulk_discount : float, default: 0.5
        Multiplier applied to base price when bulk threshold is exceeded.
    bulk_days : int, default: 30
        Window in days for computing cumulative past spend.
    replay_cache_size : int, default: 5000
        Maximum number of entries maintained in memory.
    min_fee_per_txn : int, default: 1000
        Network minimum transaction fee in microAlgos.
    allow_loss : bool, default: False
        If False, rejects or clamps ALGO prices in (0, 2000] that fail to cover
        the 2-transaction fee-payer cost.
    replay_db : str, optional
        Path to SQLite database file for multi-process replay protection.
    subscription_issuer : str, optional
        Restricts valid subscription tokens to those received from this address.
    pass_scopes : dict[str, int], optional
        Mapping of eligible pass scope names to minimum prices.
    pass_days : float, default: 30.0
        Validity window in days for vendor passes.

    Raises
    ------
    ConfigurationError
        (a ``ValueError``) If ``allow_loss=False``, ``asset_id=0``, and any configured
        price is in the loss-making range ``(0, 2 * min_fee_per_txn]``; or if
        ``subscription_issuer`` is set but ``chain.has_active_subscription`` has no
        ``issuer`` parameter.

    Notes
    -----
    When a tool is invoked, the gate checks identity proofs to identify the caller.
    If the caller holds an unexpired pass or active subscription, price is 0.
    The fee payer pays the 2,000 microALGO group fee in ALGO whatever asset the price is in.
    Settles on chain only if tool succeeds.

    See Also
    --------
    AIEconomySDK : High-level builder encapsulating PaymentGate.
    DefaultAvmSigner : Default signing implementation.

    Examples
    --------
    >>> from algosdk import account
    >>> from ai_economy_sdk.signer import DefaultAvmSigner
    >>> from ai_economy_sdk.gate import PaymentGate
    >>> sk, addr = account.generate_account()
    >>> signer = DefaultAvmSigner(sk)
    >>> gate = PaymentGate(signer=signer, network="mainnet", pay_to=addr, asset_id=0, prices={"echo": 5000})
    >>> gate.price_for("echo")
    5000
    >>> gate.price_for("unknown")
    0
    >>> gate.close()
    """

    #: Seconds a verified payment may wait for its tool call before the reservation is released.
    pending_ttl_seconds: float = 300.0
    #: Seconds the SSE ``POST`` waits for the tool call (delivered on the event stream) to settle.
    settle_wait_seconds: float = 30.0

    def __init__(
        self,
        signer: ServerSigner,
        network: str,
        pay_to: str,
        asset_id: int | str = 0,
        prices: Prices | None = None,
        default_price: int = 0,
        chain: ChainLookup | None = None,
        subscription_asset_id: int | str | None = None,
        subscription_max_age_days: float = 30.0,
        bulk_threshold: int | None = None,
        bulk_discount: float = 0.5,
        bulk_days: int = 30,
        replay_cache_size: int = 5000,
        min_fee_per_txn: int = 1000,
        allow_loss: bool = False,
        replay_db: str | None = None,
        subscription_issuer: str | None = None,
        pass_scopes: dict[str, int] | None = None,
        pass_days: float = 30.0,
    ) -> None:
        """Initialize the PaymentGate middleware with network, pricing, and key configuration."""
        self.signer = signer
        network = resolve_network(network, getattr(signer, "algod_client", None))
        self.network = network
        self.genesis_hash_b64 = genesis_hash_for(network)
        self.pay_to = pay_to
        self.asset_id = asset_id
        self.prices: Prices = prices or {}
        self.default_price = default_price
        self.chain = chain
        self.subscription_asset_id = subscription_asset_id
        self.subscription_max_age_days = subscription_max_age_days
        self.subscription_issuer = subscription_issuer
        # Vendor-scoped passes: {scope name: price}. A receipt for any scope of THIS gate makes calls free for the buyer.
        self.pass_scopes = dict(pass_scopes or {})
        self.pass_days = pass_days
        self._pass_cache: dict[str, float] = {}
        self.bulk_threshold = bulk_threshold
        self.bulk_discount = bulk_discount
        self.bulk_days = bulk_days
        self.replay_cache_size = replay_cache_size
        self.min_fee_per_txn = min_fee_per_txn
        self.allow_loss = allow_loss
        # Facilitator pays fees for the payment txn and its own fee-payer txn in the atomic group
        self.fee_cost = 2 * min_fee_per_txn
        self._warned_tools: set[str] = set()
        # Fees are paid in ALGO; prices in another asset (e.g. USDC) cannot be compared with them.
        self._floor_applies = str(asset_id) == "0"

        if not self.allow_loss and self._floor_applies:
            if isinstance(self.prices, dict):
                for tool, v in self.prices.items():
                    if 0 < v <= self.fee_cost:
                        raise ConfigurationError(
                            f"price {v} for tool '{tool}' does not cover facilitator fees ({self.fee_cost} microalgos); raise it or pass allow_loss=True"
                        )
            if 0 < self.default_price <= self.fee_cost:
                raise ConfigurationError(
                    f"price {self.default_price} for tool 'default' does not cover facilitator fees ({self.fee_cost} microalgos); raise it or pass allow_loss=True"
                )

        if subscription_issuer is not None and chain is not None and subscription_asset_id:
            if not _accepts_keyword(chain.has_active_subscription, "issuer"):
                raise ConfigurationError(
                    "subscription_issuer is set but chain.has_active_subscription() has no "
                    "'issuer' parameter; the issuer restriction could not be enforced"
                )

        facilitator = x402Facilitator()
        register_exact_avm_facilitator(facilitator, signer, network)

        resource_server = x402ResourceServer(facilitator_clients=facilitator)
        register_exact_avm_server(resource_server)

        self.replay_store = ReplayStore(replay_db)
        self._settlement_attached = False
        self._pending: dict[str, PendingPayment] = {}
        self._pending_lock = threading.Lock()

        async def _price_cb(ctx: HTTPRequestContext) -> dict[str, str]:
            req = getattr(ctx.adapter, "_request", None)
            price = getattr(getattr(req, "state", None), "x402_price", 0) if req else 0
            return {"amount": str(price), "asset": str(self.asset_id)}

        self._price_cb = _price_cb

        # A single route config applies to every path: the HTTP middleware decides which
        # requests are tool calls, so the x402 route table must never skip one (a
        # path-specific pattern would let a call on another mount point through unpaid).
        routes: dict[str, Any] = {
            "accepts": [
                {
                    "network": self.network,
                    "scheme": "exact",
                    "price": self._price_cb,
                    "pay_to": self.pay_to,
                }
            ],
            "description": "Paid MCP tool call",
        }
        self.http_server = x402HTTPResourceServer(resource_server, routes)
        self.http_server.initialize()

    # ------------------------------------------------------------------ pricing

    def _has_pass(self, sender: str) -> bool:
        """True when `sender` holds an unexpired pass for this vendor/service. Positive answers are cached 20 s."""
        now = time.time()
        if self._pass_cache.get(sender, 0) > now:
            return True
        assert self.chain is not None
        try:
            ok = self.chain.has_pass(
                sender, self.pay_to, self.asset_id, self.pass_scopes, self.pass_days
            )
        except LOOKUP_ERRORS:
            logger.warning(
                "pass lookup for %s failed; charging the normal price", sender, exc_info=True
            )
            ok = False
        if ok:
            self._pass_cache[sender] = now + 20
        return ok

    def _is_subscriber(self, sender: str) -> bool:
        assert self.chain is not None
        kwargs: dict[str, Any] = {}
        if self.subscription_issuer is not None:
            kwargs["issuer"] = self.subscription_issuer
        try:
            return bool(
                self.chain.has_active_subscription(
                    sender,
                    self.subscription_asset_id,
                    self.subscription_max_age_days,
                    **kwargs,
                )
            )
        except LOOKUP_ERRORS:
            logger.warning(
                "subscription lookup for %s failed; charging the normal price",
                sender,
                exc_info=True,
            )
            return False

    def price_for(
        self,
        tool: str,
        args: ToolArguments | None = None,
        sender: str | None = None,
    ) -> int:
        """Compute the effective invocation price for a tool call.

        Evaluates pricing based on pass holdings, subscription status, caller
        identity, argument contents, and cumulative usage volume.

        Parameters
        ----------
        tool : str
            Name of the MCP tool requested.
        args : dict[str, Any], optional
            Invocation argument mapping supplied to the tool.
        sender : str, optional
            Verified Algorand address of the caller.

        Returns
        -------
        int
            Effective price in base units (microAlgos for ALGO, base units for ASA).
            Returns 0 if the call is free, covered by a pass, or covered by subscription.

        Notes
        -----
        Clamps positive ALGO prices to 2001 microAlgos (2 * minFee + 1) if ``allow_loss=False``.

        Examples
        --------
        >>> from algosdk import account
        >>> from ai_economy_sdk.signer import DefaultAvmSigner
        >>> from ai_economy_sdk.gate import PaymentGate
        >>> sk, addr = account.generate_account()
        >>> gate = PaymentGate(signer=DefaultAvmSigner(sk), network="mainnet", pay_to=addr, asset_id=0, prices={"ping": 0, "calc": 3000})
        >>> gate.price_for("ping")
        0
        >>> gate.price_for("calc")
        3000
        >>> gate.close()
        """
        args = args or {}
        if callable(self.prices):
            base = self.prices(tool, args, sender)
        elif isinstance(self.prices, dict):
            base = self.prices.get(tool, self.default_price)
        else:
            base = self.default_price

        if base <= 0:
            return 0

        if sender and self.chain and self.pass_scopes and self._has_pass(sender):
            logger.debug("tool %r free for %s: active pass", tool, sender)
            return 0

        if sender and self.chain and self.subscription_asset_id and self._is_subscriber(sender):
            logger.debug("tool %r free for %s: active subscription", tool, sender)
            return 0

        price = base
        if sender and self.chain and self.bulk_threshold is not None:
            try:
                usage = self.chain.past_usage(sender, self.pay_to, self.asset_id, self.bulk_days)
            except LOOKUP_ERRORS:
                logger.warning(
                    "usage lookup for %s failed; no bulk discount applied", sender, exc_info=True
                )
                usage = 0
            if usage > self.bulk_threshold:
                price = int(base * self.bulk_discount)

        if self._floor_applies and 0 < price <= self.fee_cost and not self.allow_loss:
            if tool not in self._warned_tools:
                self._warned_tools.add(tool)
                logger.warning(
                    "price %d for tool '%s' does not cover facilitator fees (%d microalgos); clamped to %d",
                    price,
                    tool,
                    self.fee_cost,
                    self.fee_cost + 1,
                )
            return self.fee_cost + 1

        return price

    # ------------------------------------------------------- pending payments

    def _take_pending(self, token: str) -> PendingPayment | None:
        with self._pending_lock:
            return self._pending.pop(token, None)

    def _discard_pending(self, token: str, reason: str) -> None:
        """Drop a verified-but-unsettled payment and release its replay reservation."""
        pending = self._take_pending(token)
        if pending is not None:
            self.replay_store.release(pending.hash)
            pending.done.set()
            logger.info("payment %s not settled: %s", pending.hash[:16], reason)

    def _expire_pending(self) -> None:
        now = time.time()
        with self._pending_lock:
            expired = [
                k for k, p in self._pending.items() if now - p.created_at > self.pending_ttl_seconds
            ]
            stale = [self._pending.pop(k) for k in expired]
        for p in stale:
            self.replay_store.release(p.hash)
            p.done.set()
            logger.warning(
                "payment %s expired after %.0f s without a tool call; reservation released",
                p.hash[:16],
                self.pending_ttl_seconds,
            )

    # ------------------------------------------------------------- ASGI layer

    def middleware(self) -> list[StarletteMiddleware]:
        """Generate Starlette middleware definitions for an ASGI application.

        The middleware inspects the JSON-RPC body of every HTTP ``POST`` (any path,
        any transport) and gates each ``tools/call``. Use it directly when you build
        the FastMCP HTTP app yourself, together with :class:`SettleOnSuccessMiddleware`
        (priced calls are refused with HTTP 500 until that middleware exists)::

            mcp.add_middleware(SettleOnSuccessMiddleware(gate))
            app = mcp.http_app(middleware=gate.middleware())

        :meth:`wrap` does both steps for you.

        Returns
        -------
        list of StarletteMiddleware
            Middleware stack handling x402 payment challenges and verification.
        """
        return [StarletteMiddleware(_GateASGIMiddleware, gate=self)]

    def wrap(
        self,
        mcp: FastMCP,
        transport: Transport | None = None,
        *,
        path: str | None = None,
        **http_app_kwargs: Any,
    ) -> Any:
        """Wrap a FastMCP instance into an ASGI application protected by this PaymentGate.

        Adds :class:`SettleOnSuccessMiddleware` to ``mcp`` and builds the FastMCP HTTP
        app with the gate's ASGI middleware.

        Parameters
        ----------
        mcp : FastMCP
            FastMCP application instance defining MCP tools.
        transport : {"streamable-http", "http", "sse"}, optional
            HTTP transport to serve. ``"streamable-http"`` (alias ``"http"``) serves the
            MCP Streamable HTTP transport at ``/mcp``; ``"sse"`` serves the legacy SSE
            transport at ``/sse`` + ``/messages/``. When omitted, SSE is used (the
            0.4 behaviour) and a ``FutureWarning`` is emitted: the default will change
            to ``"streamable-http"`` in a future release.
        path : str, optional
            Endpoint path, passed to ``FastMCP.http_app`` (``/mcp`` or ``/sse`` by default).
        **http_app_kwargs
            Extra keyword arguments for ``FastMCP.http_app``. For Streamable HTTP,
            ``stateless_http`` defaults to True (each request is independent, as with
            the gated SSE transport); pass ``stateless_http=False`` for MCP sessions.

        Returns
        -------
        starlette.applications.Starlette
            ASGI application ready to serve with uvicorn.

        Raises
        ------
        ConfigurationError
            If ``transport`` is not one of the supported values.

        Examples
        --------
        ```python
        app = gate.wrap(mcp, transport="streamable-http")  # POST /mcp
        app = gate.wrap(mcp, transport="sse")  # GET /sse + POST /messages/
        ```
        """
        if transport is None:
            warnings.warn(
                "PaymentGate.wrap() was called without transport=; it serves the legacy "
                "SSE transport for compatibility. Pass transport='streamable-http' "
                "(recommended) or transport='sse' explicitly: the default will change "
                "to 'streamable-http' in a future release.",
                FutureWarning,
                stacklevel=2,
            )
            transport = "sse"
        if transport not in _TRANSPORTS:
            raise ConfigurationError(
                f"unsupported transport {transport!r}; use 'streamable-http' or 'sse'"
            )

        if not any(isinstance(m, SettleOnSuccessMiddleware) for m in mcp.middleware):
            mcp.add_middleware(SettleOnSuccessMiddleware(self))

        if transport == "sse":
            _allow_sse_calls_without_handshake(mcp)
            return mcp.http_app(
                path=path, transport="sse", middleware=self.middleware(), **http_app_kwargs
            )

        http_app_kwargs.setdefault("stateless_http", True)
        return mcp.http_app(
            path=path,
            transport="streamable-http",
            middleware=self.middleware(),
            **http_app_kwargs,
        )

    def close(self) -> None:
        """Close resources used by this PaymentGate, including the replay store.

        Returns
        -------
        None

        Examples
        --------
        >>> from algosdk import account
        >>> from ai_economy_sdk.signer import DefaultAvmSigner
        >>> from ai_economy_sdk.gate import PaymentGate
        >>> sk, addr = account.generate_account()
        >>> gate = PaymentGate(signer=DefaultAvmSigner(sk), network="mainnet", pay_to=addr, asset_id=0)
        >>> gate.close()
        """
        self.replay_store.close()


def _accepts_keyword(fn: Any, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True  # cannot introspect (e.g. a C callable); assume it does
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def _allow_sse_calls_without_handshake(mcp: FastMCP) -> None:
    """Run SSE sessions of ``mcp`` in MCP "stateless" mode.

    Why this exists: since 0.2 the gated SSE app has accepted ``tools/call`` messages
    on a session that never sent ``initialize`` (raw JSON-RPC clients and the replay
    tests rely on it). The MCP low-level server only offers that through
    ``Server.run(..., stateless=True)``, and FastMCP's SSE app calls ``run`` without
    exposing the flag, so the bound method on the private ``mcp._mcp_server`` is
    replaced. FastMCP 3.4 has no public hook for this; Streamable HTTP does not need
    it because ``http_app(stateless_http=True)`` is public. Only the SSE transport uses
    this function, it is idempotent, and ``tests/test_transports.py`` covers it.
    """
    server = mcp._mcp_server
    if getattr(server, "_ai_economy_stateless", False):
        return
    orig_run = server.run

    async def run_stateless(
        read_stream: Any,
        write_stream: Any,
        initialization_options: Any,
        raise_exceptions: bool = False,
        stateless: bool = True,
    ) -> Any:
        return await orig_run(
            read_stream,
            write_stream,
            initialization_options,
            raise_exceptions=raise_exceptions,
            stateless=True,
        )

    server.run = run_stateless  # type: ignore[method-assign]
    server._ai_economy_stateless = True  # type: ignore[attr-defined]


def _jsonrpc_error(status: int, message: str, req_id: Any = None) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32600, "message": message}},
        status_code=status,
    )


class _GateASGIMiddleware:
    """Pure ASGI middleware enforcing x402 payment on MCP ``tools/call`` requests."""

    def __init__(self, app: ASGIApp, gate: PaymentGate) -> None:
        self.app = app
        self.gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        gate = self.gate
        gate._expire_pending()

        if scope.get("method") != "POST":
            return await self.app(scope, receive, send)

        request = Request(scope, receive)
        body_bytes = await request.body()
        body_sent = False

        async def replay_receive() -> Message:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            # After the body, defer to the server so http.disconnect is still delivered.
            return await receive()

        try:
            body = json.loads(body_bytes)
        except ValueError:
            # Not JSON: the MCP server rejects it as a parse error, so no tool can run.
            return await self.app(scope, replay_receive, send)

        if isinstance(body, list):
            if any(isinstance(m, dict) and m.get("method") == "tools/call" for m in body):
                logger.warning("rejected a JSON-RPC batch containing tools/call")
                resp = _jsonrpc_error(400, "batched tools/call requests are not supported")
                return await resp(scope, receive, send)
            return await self.app(scope, replay_receive, send)

        if not isinstance(body, dict) or body.get("method") != "tools/call":
            return await self.app(scope, replay_receive, send)

        params = body.get("params")
        params = params if isinstance(params, dict) else {}
        tool_name = params.get("name")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}

        sender = verify_identity(request.headers, genesis_hash_b64=gate.genesis_hash_b64)
        price = await asyncio.to_thread(gate.price_for, cast(str, tool_name), arguments, sender)
        state = scope.setdefault("state", {})

        if price <= 0:
            state[_STATE_KEY] = _FREE
            return await self.app(scope, replay_receive, send)

        if not gate._settlement_attached:
            # Without SettleOnSuccessMiddleware a verified payment would never be
            # settled while the tool still ran: refuse instead of serving it unpaid.
            logger.error(
                "refused priced tool %r: SettleOnSuccessMiddleware is not installed on the "
                "FastMCP server (use PaymentGate.wrap() or add it with mcp.add_middleware())",
                tool_name,
            )
            resp = _jsonrpc_error(500, "payment gate misconfigured", body.get("id"))
            return await resp(scope, receive, send)

        request.state.x402_price = price
        adapter = FastAPIAdapter(request)
        context = HTTPRequestContext(
            adapter=adapter,
            path=request.url.path,
            method=request.method,
            payment_header=(
                adapter.get_header("payment-signature") or adapter.get_header("x-payment")
            ),
        )

        result = await gate.http_server.process_http_request(context)

        if result.type == "payment-error":
            return await _payment_error_response(result.response)(scope, receive, send)

        if result.type != "payment-verified":
            # The route table covers every path, so this is unreachable unless x402
            # changes; never let a priced call through without a verified payment.
            logger.error(
                "x402 returned %r for priced tool %r; refusing the call", result.type, tool_name
            )
            resp = _jsonrpc_error(500, "payment gate error", body.get("id"))
            return await resp(scope, receive, send)

        payload_obj = result.payment_payload
        if hasattr(payload_obj, "payload"):
            payload_data = getattr(payload_obj, "payload")  # noqa: B009
        elif isinstance(payload_obj, dict):
            payload_data = payload_obj.get("payload", payload_obj)
        else:
            payload_data = payload_obj
        h = hashlib.sha256(json.dumps(payload_data, sort_keys=True).encode()).hexdigest()

        if not gate.replay_store.reserve(h):
            logger.warning("rejected replayed payment %s for tool %r", h[:16], tool_name)
            resp = JSONResponse({"error": "Replay detected"}, status_code=402)
            return await resp(scope, receive, send)

        token = uuid.uuid4().hex
        pending = PendingPayment(
            payment_payload=result.payment_payload,
            payment_requirements=result.payment_requirements,
            hash=h,
        )
        with gate._pending_lock:
            gate._pending[token] = pending
        state[_STATE_KEY] = token

        # Buffer the downstream response so the settlement headers (PAYMENT-RESPONSE)
        # can be added after the tool has run and been settled.
        start: Message | None = None
        chunks: list[Message] = []

        async def capture_send(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            else:
                chunks.append(message)

        try:
            await self.app(scope, replay_receive, capture_send)
        except BaseException:
            gate._discard_pending(token, "the MCP app raised before responding")
            raise

        status = start["status"] if start else 500
        if status >= 400:
            gate._discard_pending(token, f"the MCP app answered HTTP {status}")
        elif not pending.done.is_set():
            if status == 202:
                # SSE transport: the POST is only acknowledged; the tool runs and its
                # result is delivered on the event stream. Wait for settlement.
                try:
                    await asyncio.wait_for(pending.done.wait(), timeout=gate.settle_wait_seconds)
                except asyncio.TimeoutError:
                    logger.warning(
                        "tool %r did not finish within %.0f s; responding without "
                        "settlement headers (settlement still runs when it finishes)",
                        tool_name,
                        gate.settle_wait_seconds,
                    )
            else:
                # Streamable HTTP: the response is complete, so the tool call never
                # reached the settlement middleware (e.g. a protocol error).
                gate._discard_pending(token, "the request finished without a tool call")

        if start is None:
            resp = _jsonrpc_error(500, "MCP app sent no response", body.get("id"))
            return await resp(scope, receive, send)

        headers = list(start.get("headers", []))
        for k, v in pending.settle_headers.items():
            headers.append((k.lower().encode("latin-1"), str(v).encode("latin-1")))
        await send({**start, "headers": headers})
        for message in chunks:
            await send(message)


def _payment_error_response(response: Any) -> Any:
    if response is None:
        return JSONResponse(content={"error": "Payment required"}, status_code=402)
    if response.is_html:
        return HTMLResponse(
            content=response.body, status_code=response.status, headers=response.headers
        )
    return JSONResponse(
        content=response.body or {}, status_code=response.status, headers=response.headers
    )
