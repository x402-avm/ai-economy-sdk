"""Paid MCP client session using x402 on Algorand.

Wraps ``mcp.client.sse.sse_client`` with an automatic ``x402HttpxClient`` payment handler
and optional Ed25519 identity headers. When an MCP tool call returns HTTP 402 Payment
Required, the client parses the requirement, signs an atomic Algorand payment transaction
(or 2-txn fee-payer group), and retries the request transparently with the signed payment.
"""

import contextlib
from collections.abc import AsyncGenerator
from typing import Any, Literal

import httpx
import mcp.client.sse
from algosdk.v2client import algod
from mcp import ClientSession
from x402 import x402Client
from x402.http.clients.httpx import x402HttpxClient
from x402_algorand.exact.register import register_exact_avm_client

from .identity import sign_identity
from .network import endpoints_for, resolve_network
from .signer import DefaultAvmSigner


@contextlib.asynccontextmanager
async def paid_mcp_session(
    sse_url: str,
    private_key: str,
    network: str,
    algod_url: str | None = None,
    algod_token: str | None = None,
    identity: bool = True,
    timeout: float = 30.0,
    spend_controls: dict | Literal[False] | None = None,
) -> AsyncGenerator[ClientSession, None]:
    """Open an MCP session that pays for tool calls with x402 on Algorand.

    Connects to a remote SSE-based Model Context Protocol server, initializing
    a ClientSession configured with an x402-aware HTTP client. Outgoing requests
    automatically carry identity headers (if enabled) and auto-sign 402 payment
    challenges for exact Algorand scheme payments.

    Parameters
    ----------
    sse_url : str
        URL of the SSE endpoint on the MCP server (e.g. ``"http://localhost:8000/sse"``).
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

    Examples
    --------
    ```python
    # Needs running paid MCP server:
    import asyncio
    from ai_economy_sdk import paid_mcp_session

    async def run():
        async with paid_mcp_session(
            sse_url="http://localhost:8000/sse",
            private_key=buyer_pk,
            network="localnet",
        ) as session:
            result = await session.call_tool("echo", {"text": "hello"})
            print(result)

    asyncio.run(run())
    ```
    """
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
            for k, v in sign_identity(private_key).items():
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

    async with mcp.client.sse.sse_client(sse_url, httpx_client_factory=factory) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield s
