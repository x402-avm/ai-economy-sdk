# ai-economy-sdk

[![CI](https://github.com/x402-avm/ai-economy-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/x402-avm/ai-economy-sdk/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/ai-economy-sdk.svg)](https://pypi.org/project/ai-economy-sdk/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Sell your MCP tools per call, and let your agents pay for other people's, in ALGO or USDC on Algorand.**

Wrap a FastMCP server, put a price on each tool, and any MCP client with an Algorand wallet can pay for it. Callers don't need an account or an API key. The money goes straight to your address, and the buyer is charged only if the tool succeeds. On the other side, a few lines give your Python agent a wallet it can pay tools with, inside limits you set.

Part of [x402-avm](https://github.com/x402-avm/x402-avm). Built on the open [x402](https://www.x402.org) standard. **[Documentation](https://x402-avm.github.io/build/sell-mcp-tools)**

```bash
pip install ai-economy-sdk
```

## Sell a tool

```python
from algosdk.v2client.algod import AlgodClient
from fastmcp import FastMCP
from ai_economy_sdk import DefaultAvmSigner, PaymentGate

mcp = FastMCP("Summarizer")


@mcp.tool()
def summarize(text: str) -> str:
    return text[:200]


algod = AlgodClient("", "https://testnet-api.algonode.cloud")
gate = PaymentGate(
    signer=DefaultAvmSigner(server_private_key, algod_client=algod),  # pays the network fee
    network="testnet",
    pay_to=my_address,  # where the money goes
    prices={"summarize": 5_000},  # 5,000 microALGO = 0.005 ALGO per call
)
app = gate.wrap(mcp, transport="streamable-http")  # run with: uvicorn server:app
```

An unpaid call to `summarize` gets `402 Payment Required` with the price. A paid call runs the tool, and the payment goes on chain only if the tool returns a result.

## Pay for a tool

```python
from ai_economy_sdk import paid_mcp_session

async with paid_mcp_session("https://example.com/mcp", wallet_private_key, "testnet") as session:
    result = await session.call_tool("summarize", {"text": article})
```

The session pays every `402` on its own. It refuses any payment over 1 ALGO or 1 USDC per call, or in an asset it doesn't know, unless you pass different `spend_controls`. A refused payment raises `SpendLimitExceededError`, and nothing is signed.

## What else it does

- **Pricing your way:** fixed prices per tool, prices computed from the arguments or the caller, and USDC or any other Algorand asset. → [Pricing and passes](https://x402-avm.github.io/build/pricing)
- **Passes and subscriptions:** let holders of a pass token call tools without paying each time.
- **Caller identity:** prove who is calling with a signed proof that never touches the chain. → [Identity](https://x402-avm.github.io/build/identity)
- **Replay protection:** one payment can't be used twice, including across workers (SQLite store). → [Security](https://x402-avm.github.io/production/security)
- **Both MCP transports:** Streamable HTTP (`/mcp`, recommended) and the older SSE.
- **Typed errors:** every error is a subclass of `AIEconomyError`.

Try it on your machine with test money in five minutes: [Quickstart](https://x402-avm.github.io/quickstart). The full API is in the [reference](https://x402-avm.github.io/reference/python/ai-economy-sdk/index).

## Related packages

`ai-economy-sdk` installs [`x402-algorand`](https://github.com/x402-avm/x402-algorand), the Algorand plugin for the official x402 SDK, which you can also use on its own for HTTP APIs. To pay from a browser or Node.js, use [`x402-avm-js`](https://github.com/x402-avm/x402-avm-js). Supports Python 3.10 to 3.14.

## Development

```bash
git clone https://github.com/x402-avm/ai-economy-sdk.git && cd ai-economy-sdk
pip install -e ".[dev]"
pytest                    # offline tests
LOCALNET=1 pytest         # also against AlgoKit LocalNet (algokit localnet start)
ruff check . && ruff format --check .
```

See [CONTRIBUTING](https://github.com/x402-avm/.github/blob/main/CONTRIBUTING.md). Report security problems privately, following the [security policy](https://github.com/x402-avm/.github/blob/main/SECURITY.md).

## Licence

MIT. Copyright (c) 2026 x402-avm contributors. x402-avm is not affiliated with GoPlausible's x402-avm packages.
