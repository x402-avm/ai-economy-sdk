"""Public type aliases and protocols used in ``ai_economy_sdk`` signatures.

These exist for static type checkers and IDEs. They add no runtime checks: any object
with the right methods is accepted, exactly as in earlier releases.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol

from x402 import SpendControls

__all__ = [
    "ServerSigner",
    "ChainLookup",
    "ToolArguments",
    "GatePriceFunction",
    "Prices",
    "ToolPriceFunction",
    "SpendControls",
    "SpendControlsArg",
    "Transport",
]

#: Arguments of an MCP ``tools/call`` request, as decoded from JSON.
ToolArguments = dict[str, Any]

#: ``PaymentGate`` pricing callback: ``(tool name, arguments, verified sender or None) -> price``.
GatePriceFunction = Callable[[str, ToolArguments, str | None], int]

#: ``PaymentGate(prices=...)``: a ``{tool name: price}`` mapping or a :data:`GatePriceFunction`.
Prices = Mapping[str, int] | GatePriceFunction

#: ``AIEconomySDK.add_tool(pricing_func=...)``: ``(arguments, sender) -> price`` or ``(arguments) -> price``.
ToolPriceFunction = Callable[[ToolArguments, str | None], int] | Callable[[ToolArguments], int]

#: ``paid_mcp_session(spend_controls=...)``: x402 spend controls, ``False`` to disable, or ``None`` for defaults.
SpendControlsArg = SpendControls | Literal[False] | None

#: HTTP transports :meth:`PaymentGate.wrap` can serve. ``"http"`` is an alias of ``"streamable-http"``.
Transport = Literal["streamable-http", "http", "sse"]


class ServerSigner(Protocol):
    """Signer accepted by :class:`~ai_economy_sdk.PaymentGate` and ``AIEconomySDK``.

    Structurally the facilitator signer x402-algorand calls during verification and
    settlement (it calls these methods positionally). :class:`~ai_economy_sdk.DefaultAvmSigner`
    implements it. If the object also has an ``algod_client`` attribute, it is used to
    resolve ``"localnet"`` to its CAIP-2 id.
    """

    def get_addresses(self) -> list[str]: ...

    def sign_transaction(self, txn_bytes: bytes, fee_payer: Any, network: str, /) -> bytes: ...

    def sign_group(
        self, group_bytes: list[bytes], fee_payer: Any, indexes: list[int], network: str, /
    ) -> list[bytes]: ...

    def simulate_group(self, group_bytes: list[bytes], network: str, /) -> object: ...

    def send_group(self, group_bytes: list[bytes], network: str, /) -> str: ...

    def confirm_transaction(self, txid: str, network: str, /) -> object: ...


class ChainLookup(Protocol):
    """On-chain lookups used by ``PaymentGate`` for subscriptions, passes and bulk pricing.

    :class:`~ai_economy_sdk.ChainReader` implements it. Only the methods for the
    features you enable are called: ``has_active_subscription`` when
    ``subscription_asset_id`` is set, ``has_pass`` when ``pass_scopes`` is set, and
    ``past_usage`` when ``bulk_threshold`` is set.
    """

    def has_active_subscription(
        self,
        address: str,
        asset_id: int | str | None,
        max_age_days: float = ...,
        now: float | int | None = ...,
        issuer: str | None = ...,
    ) -> bool: ...

    def has_pass(
        self,
        address: str,
        pay_to: str,
        asset_id: int | str,
        scopes: dict[str, int],
        days: float = ...,
        now: float | int | None = ...,
    ) -> bool: ...

    def past_usage(
        self,
        address: str,
        pay_to: str,
        asset_id: int | str,
        days: int = ...,
        now: float | int | None = ...,
    ) -> int: ...
