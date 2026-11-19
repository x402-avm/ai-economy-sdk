# ai-economy-sdk

[![CI](https://github.com/x402-avm/ai-economy-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/x402-avm/ai-economy-sdk/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/ai-economy-sdk.svg)](https://pypi.org/project/ai-economy-sdk/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Payment-gated Model Context Protocol (MCP) server and client SDK for Algorand.

`ai-economy-sdk` turns Model Context Protocol (MCP) tools into monetized services on Algorand using the official [x402](https://github.com/x402-foundation/x402) payment standard. On the server side, `PaymentGate` wraps FastMCP applications to enforce per-call micropayments, dynamic rates, and vendor passes with settle-on-success protection. On the client side, `paid_mcp_session` connects to payment-gated servers and automatically settles transactions only when tool execution succeeds.

## Requirements

- Python 3.10+
- `x402` >= 2.28, < 3.0
- `x402-algorand` >= 0.2.0
- `fastmcp` >= 3.4, < 4.0
- `py-algorand-sdk` >= 2.6

## Installation

```bash
pip install ai-economy-sdk
```

Installing `ai-economy-sdk` automatically installs `x402-algorand` and the official `x402` package.

## Quickstart

### 1. Server: Monetize FastMCP Tools

```python
from fastmcp import FastMCP
from ai_economy_sdk import PaymentGate, DefaultAvmSigner

mcp = FastMCP("paid-service")


@mcp.tool()
def summarize(text: str) -> str:
    return f"Summary: {text[:50]}..."


gate = PaymentGate(
    signer=DefaultAvmSigner(server_private_key),
    network="localnet",
    pay_to=server_address,
    asset_id=0,  # Native ALGO (or ASA ID such as USDC)
    prices={"summarize": 5_000},  # 5,000 microALGO per call
)

app = gate.wrap(mcp)
# Run with: uvicorn server:app --port 8000
```

### 2. Client: Invoke Paid Tools

```python
import asyncio
from ai_economy_sdk import paid_mcp_session


async def main():
    async with paid_mcp_session(
        "http://127.0.0.1:8000/sse",
        buyer_private_key,
        "localnet",
    ) as session:
        result = await session.call_tool("summarize", {"text": "Hello Algorand x402"})
        print(result.content[0].text)


asyncio.run(main())
```

## Key Capabilities

- **Settle-on-Success**: Payments are verified before tool execution, but committed on-chain only after the tool completes successfully. If a tool raises an error, the payment is never submitted and the buyer is not charged.
- **Identity Verification**: Built-in `X-Identity-Txn` cryptographic proofs verify client account ownership without on-chain execution or transaction fees.
- **Spend Limits & Controls**: Direct integration with official `x402Client` spend controls allows setting per-payment caps or whitelisting custom ASAs.
- **Vendor Passes & Subscriptions**: Support for period-based access passes and subscription ASA tokens.
- **Replay Protection**: Built-in replay prevention using memory or SQLite stores across single-process and multi-worker deployments.

## Ecosystem

- [Documentation](https://x402-avm.github.io): Architecture guides, tutorials, and API references.
- [x402-avm/examples](https://github.com/x402-avm/examples): Runnable examples for LocalNet, TestNet, and MainNet.
- [x402-algorand](https://github.com/x402-avm/x402-algorand): Algorand exact payment scheme plugin for official `x402`.
- [x402-avm-js](https://github.com/x402-avm/x402-avm-js): TypeScript client for web browsers and Node.js.

## Development

Clone the repository and install development dependencies:

```bash
git clone https://github.com/x402-avm/ai-economy-sdk.git
cd ai-economy-sdk
uv pip install -e ".[dev]"
```

Run tests across test tiers:

```bash
# Tier 1: Offline unit tests (default)
pytest

# Tier 2: AlgoKit LocalNet integration tests (requires `algokit localnet start`)
LOCALNET=1 pytest

# Tier 3: Public network simulations (TestNet/MainNet, simulate only)
PUBLIC_NETS=1 pytest
```

Check formatting and linting:

```bash
ruff check .
ruff format --check .
```

## License

MIT License. Copyright (c) AI Economy Team.
