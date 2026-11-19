"""Payment gate middleware for FastMCP using x402 on Algorand.

Intercepts incoming MCP JSON-RPC tool calls on ``/messages``, enforces HTTP 402
Payment Required challenges via x402 on Algorand, verifies buyer signatures and
fee-payer atomic groups, checks replay protection, and settles on chain only
when the tool execution completes successfully without errors.
"""

import asyncio
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware as FastMCPMiddleware
from starlette.middleware import Middleware as StarletteMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from x402 import x402Facilitator, x402ResourceServer
from x402.http.middleware.fastapi import FastAPIAdapter
from x402.http.types import HTTPRequestContext
from x402.http.x402_http_server import x402HTTPResourceServer
from x402_algorand.exact.register import (
    register_exact_avm_facilitator,
    register_exact_avm_server,
)

from .identity import verify_identity
from .network import genesis_hash_for, resolve_network
from .replay import ReplayStore

logger = logging.getLogger("ai_economy_sdk")


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


class SettleOnSuccessMiddleware(FastMCPMiddleware):
    """FastMCP middleware that settles payments on chain only upon tool success.

    If the tool raises an exception or returns an error result (``is_error=True``),
    the payment reservation in ReplayStore is released and the buyer is not charged.
    """

    def __init__(self, gate: "PaymentGate") -> None:
        self.gate = gate

    async def on_call_tool(self, context, call_next):
        sess_id = ""
        try:
            req = get_http_request()
            sess_id = req.query_params.get("session_id") or ""
        except Exception:
            pass
        if not sess_id and context.fastmcp_context and context.fastmcp_context.request_context:
            req = getattr(context.fastmcp_context.request_context, "request", None)
            if req:
                sess_id = getattr(req, "query_params", {}).get("session_id") or ""

        norm_sess = sess_id.replace("-", "").lower()

        req_id = ""
        if context.fastmcp_context:
            try:
                req_id = context.fastmcp_context.request_id
            except Exception:
                req_id = getattr(context.fastmcp_context, "origin_request_id", "")
        norm_req = str(req_id).strip() if req_id is not None else ""

        pending_key = (norm_sess, norm_req)

        with self.gate._pending_lock:
            pending = self.gate._pending.pop(pending_key, None)

        if not pending:
            return await call_next(context)

        try:
            try:
                res = await call_next(context)
            except Exception:
                self.gate.replay_store.release(pending.hash)
                raise

            if getattr(res, "is_error", False) or getattr(res, "isError", False):
                self.gate.replay_store.release(pending.hash)
                return res

            try:
                settle_result = await self.gate.http_server.process_settlement(
                    pending.payment_payload,
                    pending.payment_requirements,
                )
            except Exception as e:
                self.gate.replay_store.release(pending.hash)
                raise ToolError(f"payment settlement failed: {e}") from e

            if not settle_result.success:
                self.gate.replay_store.release(pending.hash)
                reason = getattr(settle_result, "error_reason", None) or "Settlement failed"
                raise ToolError(f"payment settlement failed: {reason}")

            self.gate.replay_store.commit(pending.hash)
            if hasattr(settle_result, "headers") and settle_result.headers:
                pending.settle_headers.update(settle_result.headers)
            return res
        finally:
            pending.done.set()


class PaymentGate:
    """Payment gate protecting FastMCP server tools with x402 on Algorand.

    Integrates payment challenges, verification, and settlement into FastMCP SSE endpoints.
    Supports fixed, dynamic, subscription-based, pass-based, and tiered bulk pricing.

    Parameters
    ----------
    signer : Any
        Signer instance (e.g., ``DefaultAvmSigner``) providing merchant keys and
        node connection to sponsor transaction fees as fee payer.
    network : str
        Network name (``"mainnet"``, ``"testnet"``, ``"localnet"``) or CAIP-2 identifier.
    pay_to : str
        Algorand address where service payments are collected.
    asset_id : int or str, default: 0
        Asset ID accepted for payments (0 for native ALGO in microAlgos, or ASA ID).
    prices : dict[str, int] or callable, optional
        Tool pricing mapping or callable with signature ``(tool, args, sender) -> int``.
    default_price : int, default: 0
        Fallback price in base units for tools not explicitly listed in ``prices``.
    chain : ChainReader, optional
        ChainReader instance for querying subscriptions, past usage, and passes.
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
    ValueError
        If ``allow_loss=False``, ``asset_id=0``, and any configured price is in
        the loss-making range ``(0, 2 * min_fee_per_txn]``.

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

    def __init__(
        self,
        signer: Any,
        network: str,
        pay_to: str,
        asset_id: int | str = 0,
        prices: Any = None,
        default_price: int = 0,
        chain: Any = None,
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
    ):
        """Initialize the PaymentGate middleware with network, pricing, and key configuration."""
        self.signer = signer
        network = resolve_network(network, getattr(signer, "algod_client", None))
        self.network = network
        self.genesis_hash_b64 = genesis_hash_for(network)
        self.pay_to = pay_to
        self.asset_id = asset_id
        self.prices = prices or {}
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
                        raise ValueError(
                            f"price {v} for tool '{tool}' does not cover facilitator fees ({self.fee_cost} microalgos); raise it or pass allow_loss=True"
                        )
            if 0 < self.default_price <= self.fee_cost:
                raise ValueError(
                    f"price {self.default_price} for tool 'default' does not cover facilitator fees ({self.fee_cost} microalgos); raise it or pass allow_loss=True"
                )

        facilitator = x402Facilitator()
        register_exact_avm_facilitator(facilitator, signer, network)

        resource_server = x402ResourceServer(facilitator_clients=facilitator)
        register_exact_avm_server(resource_server)

        self.replay_store = ReplayStore(replay_db)
        self._pending: dict[tuple[str, str], PendingPayment] = {}
        self._pending_lock = threading.Lock()

        async def _price_cb(ctx: HTTPRequestContext) -> dict[str, str]:
            req = getattr(ctx.adapter, "_request", None)
            price = getattr(getattr(req, "state", None), "x402_price", 0) if req else 0
            return {"amount": str(price), "asset": str(self.asset_id)}

        self._price_cb = _price_cb

        routes = {
            "POST /messages*": {
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
        }
        self.http_server = x402HTTPResourceServer(resource_server, routes)
        self.http_server.initialize()

    def _has_pass(self, sender: str) -> bool:
        """True when `sender` holds an unexpired pass for this vendor/service. Positive answers are cached 20 s."""
        now = time.time()
        if self._pass_cache.get(sender, 0) > now:
            return True
        try:
            ok = self.chain.has_pass(
                sender, self.pay_to, self.asset_id, self.pass_scopes, self.pass_days
            )
        except Exception:
            ok = False
        if ok:
            self._pass_cache[sender] = now + 20
        return ok

    def price_for(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
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
            return 0

        if sender and self.chain and self.subscription_asset_id:
            try:
                if self.subscription_issuer is not None:
                    is_sub = self.chain.has_active_subscription(
                        sender,
                        self.subscription_asset_id,
                        self.subscription_max_age_days,
                        issuer=self.subscription_issuer,
                    )
                else:
                    is_sub = self.chain.has_active_subscription(
                        sender,
                        self.subscription_asset_id,
                        self.subscription_max_age_days,
                    )
            except TypeError:
                is_sub = self.chain.has_active_subscription(
                    sender,
                    self.subscription_asset_id,
                    self.subscription_max_age_days,
                )
            if is_sub:
                return 0

        if (
            sender
            and self.chain
            and self.bulk_threshold is not None
            and self.chain.past_usage(sender, self.pay_to, self.asset_id, self.bulk_days)
            > self.bulk_threshold
        ):
            price = int(base * self.bulk_discount)
        else:
            price = base

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

    def middleware(self) -> list[StarletteMiddleware]:
        """Generate Starlette middleware definitions for the ASGI application.

        Returns
        -------
        list of StarletteMiddleware
            Middleware stack handling ``POST /messages`` payment challenges and verifications.
        """
        gate_self = self

        class GateMiddleware:
            def __init__(self, app: Any) -> None:
                self.app = app

            async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
                if scope["type"] != "http":
                    return await self.app(scope, receive, send)

                # Expire stale pending reservations older than 300 seconds
                now = time.time()
                with gate_self._pending_lock:
                    expired = [k for k, p in gate_self._pending.items() if now - p.created_at > 300]
                    for k in expired:
                        p = gate_self._pending.pop(k, None)
                        if p:
                            gate_self.replay_store.release(p.hash)

                raw_path = scope.get("path", "")
                if scope.get("method") != "POST" or not raw_path.rstrip("/").endswith("/messages"):
                    return await self.app(scope, receive, send)

                request = Request(scope, receive)
                body_bytes = await request.body()

                body_sent = False

                async def replay_receive():
                    nonlocal body_sent
                    if not body_sent:
                        body_sent = True
                        return {"type": "http.request", "body": body_bytes, "more_body": False}
                    return {"type": "http.request", "body": b"", "more_body": False}

                try:
                    body = json.loads(body_bytes.decode("utf-8"))
                except Exception:
                    return await self.app(scope, replay_receive, send)

                if not isinstance(body, dict) or body.get("method") != "tools/call":
                    return await self.app(scope, replay_receive, send)

                params = body.get("params", {})
                tool_name = params.get("name")
                arguments = params.get("arguments", {})

                sender = verify_identity(
                    request.headers, genesis_hash_b64=gate_self.genesis_hash_b64
                )
                price = await asyncio.to_thread(
                    gate_self.price_for, tool_name, arguments or {}, sender
                )

                if price <= 0:
                    return await self.app(scope, replay_receive, send)

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

                result = await gate_self.http_server.process_http_request(context)

                if result.type == "no-payment-required":
                    return await self.app(scope, replay_receive, send)

                if result.type == "payment-error":
                    response = result.response
                    if response is None:
                        resp = JSONResponse(
                            content={"error": "Payment required"},
                            status_code=402,
                        )
                    elif response.is_html:
                        resp = HTMLResponse(
                            content=response.body,
                            status_code=response.status,
                            headers=response.headers,
                        )
                    else:
                        resp = JSONResponse(
                            content=response.body or {},
                            status_code=response.status,
                            headers=response.headers,
                        )
                    return await resp(scope, receive, send)

                if result.type == "payment-verified":
                    payload_obj = result.payment_payload
                    if hasattr(payload_obj, "payload"):
                        payload_data = payload_obj.payload
                    elif isinstance(payload_obj, dict):
                        payload_data = payload_obj.get("payload", payload_obj)
                    else:
                        payload_data = payload_obj

                    payload_str = json.dumps(payload_data, sort_keys=True)
                    h = hashlib.sha256(payload_str.encode()).hexdigest()

                    if not gate_self.replay_store.reserve(h):
                        resp = JSONResponse({"error": "Replay detected"}, status_code=402)
                        return await resp(scope, receive, send)

                    session_id = request.query_params.get("session_id") or ""
                    norm_sess = session_id.replace("-", "").lower()
                    jsonrpc_id = body.get("id")
                    norm_req = str(jsonrpc_id).strip() if jsonrpc_id is not None else ""
                    pending_key = (norm_sess, norm_req)

                    pending = PendingPayment(
                        payment_payload=result.payment_payload,
                        payment_requirements=result.payment_requirements,
                        hash=h,
                        created_at=time.time(),
                    )
                    with gate_self._pending_lock:
                        gate_self._pending[pending_key] = pending

                    downstream_status = 202
                    downstream_headers: list[tuple[bytes, bytes]] = []

                    async def capture_send(message: Any) -> None:
                        nonlocal downstream_status, downstream_headers
                        if message["type"] == "http.response.start":
                            downstream_status = message["status"]
                            downstream_headers = list(message.get("headers", []))

                    await self.app(scope, replay_receive, capture_send)

                    if downstream_status >= 400:
                        with gate_self._pending_lock:
                            p = gate_self._pending.pop(pending_key, None)
                            if p:
                                gate_self.replay_store.release(p.hash)
                        resp = Response(
                            content=b"",
                            status_code=downstream_status,
                            headers=dict(
                                (k.decode("latin-1"), v.decode("latin-1"))
                                for k, v in downstream_headers
                            ),
                        )
                        return await resp(scope, receive, send)

                    # Await tool call & settlement completion
                    try:
                        await asyncio.wait_for(pending.done.wait(), timeout=30.0)
                    except Exception:
                        pass

                    headers_dict = {
                        k.decode("latin-1"): v.decode("latin-1") for k, v in downstream_headers
                    }
                    if pending.settle_headers:
                        headers_dict.update(pending.settle_headers)

                    resp = Response(
                        content=b"Accepted",
                        status_code=downstream_status,
                        headers=headers_dict,
                    )
                    return await resp(scope, receive, send)

                return await self.app(scope, replay_receive, send)

        return [StarletteMiddleware(GateMiddleware)]

    def wrap(self, mcp: FastMCP) -> Any:
        """Wrap a FastMCP instance into an ASGI application protected by this PaymentGate.

        Configures SettleOnSuccessMiddleware, wraps server run loop for stateless SSE,
        and mounts HTTP middleware.

        Parameters
        ----------
        mcp : FastMCP
            FastMCP application instance defining MCP tools.

        Returns
        -------
        Any
            ASGI application ready to serve with uvicorn or starlette.

        Examples
        --------
        ```python
        # Needs FastMCP instance:
        # app = gate.wrap(mcp)
        ```
        """
        if not any(isinstance(m, SettleOnSuccessMiddleware) for m in mcp.middleware):
            mcp.add_middleware(SettleOnSuccessMiddleware(self))

        orig_run = mcp._mcp_server.run
        if not getattr(mcp._mcp_server, "_wrapped_for_gate", False):

            async def wrapped_run(
                read_stream,
                write_stream,
                initialization_options,
                raise_exceptions: bool = False,
                stateless: bool = True,
            ):
                return await orig_run(
                    read_stream,
                    write_stream,
                    initialization_options,
                    raise_exceptions=raise_exceptions,
                    stateless=True,
                )

            mcp._mcp_server.run = wrapped_run
            mcp._mcp_server._wrapped_for_gate = True

        return mcp.http_app(transport="sse", middleware=self.middleware())

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
