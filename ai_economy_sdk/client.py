"""Paid MCP client session using x402 on Algorand.

Opens an MCP client session over Streamable HTTP or the legacy SSE transport with an
``x402HttpxClient`` and optional identity headers. When an MCP tool call returns HTTP
402 Payment Required, the client parses the requirement, signs an atomic Algorand payment
transaction (or 2-txn fee-payer group), and retries the request with the signed payment.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import AsyncGenerator
from typing import Any, Literal
from urllib.parse import urlsplit

import anyio
import httpx
import mcp.client.sse
from algosdk.v2client import algod
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from x402 import x402Client
from x402.http.clients.httpx import x402HttpxClient
from x402.schemas.errors import NoMatchingRequirementsError
from x402_algorand.exact.register import register_exact_avm_client

from .exceptions import ConfigurationError, SpendLimitExceededError
from .identity import _sign_legacy_identity
from .network import endpoints_for, resolve_network
from .signer import DefaultAvmSigner
from .types import SpendControlsArg

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup

ClientTransport = Literal["streamable-http", "sse"]


def _infer_transport(url: str) -> ClientTransport:
    """Streamable HTTP for URLs whose path ends in ``/mcp``; SSE otherwise (the 0.4 behaviour)."""
    path = urlsplit(url).path.rstrip("/")
    return "streamable-http" if path.endswith("/mcp") else "sse"


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """Flatten (possibly nested) exception groups into their leaf exceptions."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaf_exceptions(sub)]
    return [exc]


def _spend_refusal_cause(exc: BaseException) -> NoMatchingRequirementsError | None:
    """Return x402's spend-control refusal if ``exc`` is one (directly or as ``__cause__``)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, NoMatchingRequirementsError) and "spend_controls" in str(cur):
            return cur
        cur = cur.__cause__
    return None


def _describe_offers(payment_required: Any) -> str:
    """Summarise the server's offers as ``amount of asset`` for an error message."""
    try:
        offers = [f"{a.amount} of asset {a.asset} on {a.network}" for a in payment_required.accepts]
    except AttributeError:
        return ""
    return "; ".join(offers)


def _translate_spend_refusal(
    exc: BaseException, payment_required: Any = None
) -> SpendLimitExceededError | None:
    """Turn a spend-control refusal into :class:`SpendLimitExceededError`, or return None.

    ``exc`` may be the refusal itself or an ``ExceptionGroup`` (anyio task groups wrap errors
    raised inside them, possibly several levels deep). Only a refusal that is the sole leaf
    error is translated: if any other error is present the caller must re-raise ``exc``
    unchanged so nothing unrelated is hidden.
    """
    leaves = _leaf_exceptions(exc)
    causes = [_spend_refusal_cause(leaf) for leaf in leaves]
    if not leaves or any(c is None for c in causes):
        return None
    cause = causes[0]
    assert cause is not None
    offered = _describe_offers(payment_required) if payment_required is not None else ""
    msg = "payment refused by this client's spend_controls; nothing was signed or sent"
    if offered:
        msg += f" (server asked for {offered})"
    msg += f". {cause}"
    err = SpendLimitExceededError(msg)
    err.__cause__ = cause
    return err


@contextlib.asynccontextmanager
async def paid_mcp_session(
    sse_url: str,
    private_key: str,
    network: str,
    algod_url: str | None = None,
    algod_token: str | None = None,
    identity: bool = True,
    timeout: float = 30.0,
    spend_controls: SpendControlsArg = None,
    *,
    transport: ClientTransport | None = None,
) -> AsyncGenerator[ClientSession, None]:
    """Open an MCP session that pays for tool calls with x402 on Algorand.

    Connects to a remote Model Context Protocol server over Streamable HTTP or the
    legacy SSE transport, initializing a ClientSession configured with an x402-aware
    HTTP client. Outgoing requests automatically carry identity headers (if enabled)
    and auto-sign 402 payment challenges for exact Algorand scheme payments.

    Parameters
    ----------
    sse_url : str
        URL of the server's MCP endpoint: ``"http://localhost:8000/mcp"`` for Streamable
        HTTP or ``"http://localhost:8000/sse"`` for SSE. (The parameter keeps its 0.4
        name for compatibility; it accepts either kind of URL.)
    private_key : str
        Base64-encoded 32-byte Algorand private key used by the buyer to sign payments
        and identity proofs.
    network : str
        Target Algorand network name (``"mainnet"``, ``"testnet"``, ``"localnet"``),
        canonical CAIP-2 identifier (``"algorand:<32-char-prefix>"``), or legacy long ID.
    algod_url : str, optional
        Custom URL for the algod node. If None, defaults to the standard endpoint
        for the given network (AlgoNode for public networks, localhost for localnet).
    algod_token : str, optional
        Custom authentication token header for the algod node. Default is None.
    identity : bool, default: True
        Whether to attach cryptographic identity headers (``x-sender-*``) proving
        account ownership on every request to enable vendor discounts and passes.
    timeout : float, default: 30.0
        HTTP request timeout in seconds for normal requests. Stream reads allow up
        to 300 seconds.
    spend_controls : dict or False, optional
        Client-side spending limits enforced by x402Client before payment authorization:
        - None (default): official x402 defaults apply — only default assets (native ALGO
          and USDC) are allowed, capped at 1,000,000 base atomic units (1 ALGO / 1 USDC) per payment.
        - False: disables all spend controls (any asset, unlimited amount).
        - dict: custom spend controls following the x402 SpendControls schema, e.g.:
          ``{"max_amount_per_payment": False}`` removes the amount cap while keeping default assets,
          ``{"allowed_assets": True}`` allows any asset while keeping default payment cap, or
          ``{"allowed_assets": [{"network": network, "asset": "12345", "max_amount_per_payment": "5000000"}]}``
          whitelists a specific ASA.
    transport : {"streamable-http", "sse"}, optional
        MCP transport to use. When omitted, Streamable HTTP is used if the URL path ends
        in ``/mcp`` and SSE otherwise, which keeps 0.4 behaviour for SSE URLs.

    Yields
    ------
    mcp.ClientSession
        Active, initialized MCP client session capable of invoking tools via
        ``session.call_tool(tool_name, arguments)``.

    Notes
    -----
    On receiving HTTP 402 with ``PAYMENT-REQUIRED``, the client verifies requirements,
    constructs an Algorand transaction or 2-txn atomic group (where the server sponsors
    transaction fees as fee payer), signs it, and resubmits under ``PAYMENT-SIGNATURE``.
    Funds are only deducted from the buyer's account if the tool execution succeeds.

    See Also
    --------
    PaymentGate : Server-side payment middleware.
    AIEconomySDK : High-level server framework.

    Raises
    ------
    ConfigurationError
        If ``transport`` is not ``"streamable-http"`` or ``"sse"``.
    SpendLimitExceededError
        If a tool call needs a payment that ``spend_controls`` forbid (amount above the cap,
        or an asset that is not allowed). Nothing is signed and no funds move. The x402 error
        is its ``__cause__``. Other errors propagate unchanged.

    Examples
    --------
    ```python
    # Needs running paid MCP server:
    import asyncio
    from ai_economy_sdk import paid_mcp_session

    async def run():
        async with paid_mcp_session(
            "http://localhost:8000/mcp",
            private_key=buyer_pk,
            network="localnet",
        ) as session:
            result = await session.call_tool("echo", {"text": "hello"})
            print(result)

    asyncio.run(run())
    ```
    """
    if transport is None:
        transport = _infer_transport(sse_url)
    elif transport not in ("streamable-http", "sse"):
        raise ConfigurationError(
            f"unsupported transport {transport!r}; use 'streamable-http' or 'sse'"
        )
    default_timeout = timeout
    ep = endpoints_for(network)
    if algod_url is None:
        algod_url = ep.algod_url
    if algod_token is None:
        algod_token = ep.algod_token if algod_url == ep.algod_url else ""
    algod_client = algod.AlgodClient(algod_token, algod_url)
    network = resolve_network(network, algod_client)
    x402_client = x402Client()
    if spend_controls is not None:
        x402_client.set_spend_controls(spend_controls)
    offers: list[Any] = []
    refusals: list[BaseException] = []
    scope = anyio.CancelScope()
    _create_payload = x402_client.create_payment_payload

    async def _recording_create_payload(payment_required: Any, *a: Any, **kw: Any) -> Any:
        offers.append(payment_required)
        try:
            return await _create_payload(payment_required, *a, **kw)
        except Exception as e:
            if _spend_refusal_cause(e) is not None and not refusals:
                # Over SSE the MCP client swallows errors raised while posting a request, so the
                # caller would wait forever for a reply. Record the refusal and cancel the session.
                refusals.append(e)
                scope.cancel()
            raise

    x402_client.create_payment_payload = _recording_create_payload  # type: ignore[method-assign]
    signer = DefaultAvmSigner(private_key, algod_client=algod_client)
    register_exact_avm_client(
        x402_client,
        signer,
        networks=network,
        algod_url=algod_url,
        algod_token=algod_token,
    )

    async def add_identity_headers(request: httpx.Request) -> None:
        if identity:
            for k, v in _sign_legacy_identity(private_key).items():
                request.headers[k] = v

    def factory(
        headers: dict[str, Any] | None = None,
        timeout: httpx.Timeout | float | None = None,
        auth: Any = None,
    ) -> x402HttpxClient:
        if isinstance(timeout, httpx.Timeout):
            eff_timeout = timeout
        elif isinstance(timeout, (int, float)):
            eff_timeout = httpx.Timeout(timeout, read=300.0)
        else:
            eff_timeout = httpx.Timeout(default_timeout, read=300.0)

        hooks = {"request": [add_identity_headers]} if identity else {}
        return x402HttpxClient(
            x402_client,
            headers=headers,
            timeout=eff_timeout,
            auth=auth,
            event_hooks=hooks,
        )

    def _refusal_error(exc: BaseException) -> SpendLimitExceededError | None:
        return _translate_spend_refusal(exc, offers[-1] if offers else None)

    try:
        with scope:
            if transport == "streamable-http":
                async with factory() as http_client:
                    async with streamable_http_client(sse_url, http_client=http_client) as (
                        r,
                        w,
                        _,
                    ):
                        async with ClientSession(r, w) as s:
                            await s.initialize()
                            yield s
            else:
                async with mcp.client.sse.sse_client(sse_url, httpx_client_factory=factory) as (
                    r,
                    w,
                ):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        yield s
    except Exception as exc:
        translated = _refusal_error(exc)
        if translated is None:
            raise
        raise translated from translated.__cause__
    if refusals and scope.cancelled_caught:
        translated = _refusal_error(refusals[0])
        if translated is not None:
            raise translated from translated.__cause__
