"""Public exception hierarchy for ``ai_economy_sdk``.

Every exception raised by the SDK on purpose derives from :class:`AIEconomyError`, so
``except AIEconomyError`` catches all of them. Each class also derives from the built-in
(or FastMCP) exception type that the same code raised before 0.5.0, so existing
``except ValueError`` / ``except RuntimeError`` / ``except ToolError`` handlers keep working.

Hierarchy::

    AIEconomyError
    ├── ConfigurationError (ValueError)
    │   ├── UnknownNetworkError
    │   └── AlgodClientRequiredError (also RuntimeError)
    ├── PaymentVerificationError (RuntimeError)
    ├── PaymentRequiredError (fastmcp ToolError)
    └── SettlementError (fastmcp ToolError)
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

__all__ = [
    "AIEconomyError",
    "ConfigurationError",
    "UnknownNetworkError",
    "AlgodClientRequiredError",
    "PaymentVerificationError",
    "PaymentRequiredError",
    "SettlementError",
]


class AIEconomyError(Exception):
    """Base class for every exception the SDK raises deliberately."""


class ConfigurationError(AIEconomyError, ValueError):
    """Invalid arguments or configuration, detected before any payment is attempted.

    Raised, for example, when an ALGO price does not cover the fee-payer cost and
    ``allow_loss`` is False, when a transport name is not supported, or when a
    pricing function has an unsupported signature.
    """


class UnknownNetworkError(ConfigurationError):
    """A network name or CAIP-2 identifier could not be resolved."""


class AlgodClientRequiredError(ConfigurationError, RuntimeError):
    """An operation needs an algod connection but the signer was created without one.

    Subclasses ``RuntimeError`` as well as ``ValueError`` because 0.4.x raised a plain
    ``RuntimeError`` here.
    """


class PaymentVerificationError(AIEconomyError, RuntimeError):
    """A payment transaction group failed verification (for example, algod simulation)."""


class PaymentRequiredError(AIEconomyError, ToolError):
    """A priced tool was invoked without a verified payment.

    Raised by the server-side settlement middleware when a tool call reaches the
    FastMCP server without having passed through the payment gate (for example over a
    transport the gate does not cover). The call is refused rather than served unpaid.
    """


class SettlementError(AIEconomyError, ToolError):
    """A verified payment could not be settled on chain after the tool succeeded.

    The tool's output is withheld from the caller and the payment reservation is
    released, so the buyer is not charged.
    """
