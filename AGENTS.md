# AGENTS.md

Practical guidance for AI coding agents and human contributors working on `ai-economy-sdk`.

## Project Overview

`ai-economy-sdk` is a Python SDK that monetizes Model Context Protocol (MCP) servers and clients on Algorand using the official [x402](https://github.com/x402-foundation/x402) payment standard and the `x402-algorand` scheme plugin.

Core architectural components:
- **`PaymentGate`**: Wraps FastMCP applications as ASGI/Starlette middleware, intercepts tool calls, challenges callers with HTTP 402, verifies payments via `x402_algorand`, and guarantees settle-on-success.
- **`paid_mcp_session`**: Asynchronous context manager client connecting to payment-gated MCP servers via SSE, resolving network endpoints, attaching identity headers, enforcing spend controls, and executing paid tool calls.
- **`ChainReader`**: Queries Algorand Indexer for payment receipts, active subscription passes, and historical volume for dynamic discounts.
- **`DefaultAvmSigner`**: Implements the `ClientAvmSigner` and `FacilitatorAvmSigner` protocols for local transaction signing and group composition.
- **`ReplayStore`**: Persistent replay prevention using in-memory dicts or SQLite WAL mode across multi-process deployments.
- **`Identity`**: Cryptographic identity proofs (`X-Identity-Txn`) verifying account ownership without on-chain fees.

## Environment Setup

Create an isolated virtual environment with Python 3.10+ (Python 3.12 recommended):

```bash
uv venv -q --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

## Running Tests

Tests are divided into three tiers:

```bash
# 1. Offline tests (default, fast, no external network needed):
pytest

# 2. LocalNet tests (requires AlgoKit LocalNet running on localhost:4001/4002):
LOCALNET=1 pytest

# 3. Public network simulation tests (read-only simulations against TestNet/MainNet):
PUBLIC_NETS=1 pytest

# Run all test tiers:
LOCALNET=1 PUBLIC_NETS=1 pytest
```

## Code Style and Linting

Linting and formatting are configured via `ruff`:

```bash
# Check code style:
ruff check .

# Check formatting:
ruff format --check .

# Auto-fix lint issues:
ruff check --fix .
```

## Repository Structure

```
.
├── .github/
│   ├── dependabot.yml          # Weekly dependency update configuration
│   └── workflows/
│       └── ci.yml              # Multi-tier GitHub Actions workflow
├── ai_economy_sdk/             # Core package source
│   ├── __init__.py             # Public exports (PaymentGate, paid_mcp_session, etc.)
│   ├── chain.py                # ChainReader for Indexer queries and pass verification
│   ├── client.py               # paid_mcp_session client implementation
│   ├── gate.py                 # PaymentGate ASGI middleware and FastMCP wrapper
│   ├── identity.py             # X-Identity-Txn generation and verification
│   ├── network.py              # CAIP-2 network resolution and endpoint defaults
│   ├── replay.py               # In-memory and SQLite ReplayStore
│   ├── sdk.py                  # High-level AIEconomySDK wrapper
│   └── signer.py               # DefaultAvmSigner implementation
├── tests/                      # Self-contained pytest test suite
│   ├── conftest.py             # Shared LocalNet fixtures (KMD dispenser, account creation)
│   ├── test_adversarial_localnet.py # Security attack vectors on LocalNet
│   ├── test_chain.py           # ChainReader unit tests
│   ├── test_e2e_localnet.py    # End-to-end LocalNet tool execution and spend controls
│   ├── test_e2e_replay.py      # Replay attack prevention tests
│   ├── test_fee_floor.py       # Fee floor clamping and loss allowance
│   ├── test_gate.py            # PaymentGate middleware unit tests
│   ├── test_identity.py        # Ed25519 identity verification unit tests
│   ├── test_identity_txn.py    # X-Identity-Txn format and validity tests
│   ├── test_networks.py        # CAIP-2 network resolution and public simulate tests
│   ├── test_passes.py          # Vendor pass verification tests
│   ├── test_replay_store.py    # ReplayStore SQLite persistence tests
│   ├── test_settle_on_success.py # Settle-on-success guarantee tests
│   ├── test_signer.py          # Signer unit tests
│   └── test_subscription_issuer.py # Issuer enforcement tests
├── AGENTS.md                   # This agent guidance document
├── CHANGELOG.md                # Version history (Keep a Changelog format)
├── LICENSE                     # MIT License
├── README.md                   # Public documentation and quickstart
├── pyproject.toml              # Build metadata, dependencies, and URLs
├── ruff.toml                   # Ruff linter and formatter configuration
├── .editorconfig               # Editor whitespace and encoding rules
└── .gitignore                  # Git ignore rules for Python builds and artifacts
```

## Conventions and Rules

1. **Self-Contained Tests**: Tests must never rely on local machine paths, absolute host directory paths, specific usernames, or hardcoded account credential files. LocalNet tests must create and fund fresh accounts from the LocalNet KMD dispenser via `conftest.py`.
2. **Settle-on-Success Guarantee**: If a tool returns an error (`isError=True`) or raises an exception, the payment must NEVER be committed to the blockchain.
3. **Network Identifiers**: Use the 32-character canonical CAIP-2 network identifiers:
   - MainNet: `algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73k`
   - TestNet: `algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDe`
   - LocalNet: Dynamically derived from the local algod node's genesis hash.
4. **Units**: Native ALGO values are expressed in base microALGO units (1 ALGO = 1,000,000 microALGO). USDC and 6-decimal ASAs are expressed in base atomic units (1 USDC = 1,000,000 units).
5. **No Secrets**: Never commit private keys, mnemonics, tokens, or sensitive credentials. All test accounts must be generated ephemerally or funded from the local dispenser.
6. **Package Versioning**: Do not modify the package version in `pyproject.toml` unless explicitly instructed; versions are cut and tagged as part of scheduled releases.

## Pull Request Checklist

Before submitting a pull request, ensure:
- [ ] Code style and formatting checks pass (`ruff check .`, `ruff format --check .`).
- [ ] Offline test suite passes (`pytest`).
- [ ] LocalNet test suite passes (`LOCALNET=1 pytest`).
- [ ] Public network simulation suite passes (`PUBLIC_NETS=1 pytest`).
- [ ] Distribution package builds cleanly (`python -m build` and `twine check dist/*`).
- [ ] No hardcoded machine paths, local usernames, or credentials exist in the diff.
- [ ] `CHANGELOG.md` is updated with all public-facing changes under the unreleased or current release header.
