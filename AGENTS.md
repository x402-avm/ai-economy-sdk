# AGENTS.md

Practical guidance for AI coding agents and human contributors working on `ai-economy-sdk`.

## Project Overview

`ai-economy-sdk` is a Python SDK that monetizes Model Context Protocol (MCP) servers and clients on Algorand using the official [x402](https://github.com/x402-foundation/x402) payment standard and the `x402-algorand` scheme plugin.

Core architectural components:
- **`PaymentGate`**: Wraps FastMCP applications (Streamable HTTP or legacy SSE) as ASGI/Starlette middleware, intercepts every `tools/call` POST whatever its path, challenges callers with HTTP 402, verifies payments via `x402_algorand`, and guarantees settle-on-success. `SettleOnSuccessMiddleware` (inside FastMCP) settles after success and refuses priced calls that bypassed the HTTP gate.
- **`paid_mcp_session`**: Asynchronous context manager client connecting to payment-gated MCP servers over Streamable HTTP or SSE, resolving network endpoints, attaching identity headers, enforcing spend controls, and executing paid tool calls.
- **`ChainReader`**: Queries Algorand Indexer for payment receipts, active subscription passes, and historical volume for dynamic discounts.
- **`DefaultAvmSigner`**: Implements the `ClientAvmSigner` and `FacilitatorAvmSigner` protocols for local transaction signing and group composition.
- **`ReplayStore`**: Persistent replay prevention using in-memory dicts or SQLite WAL mode across multi-process deployments.
- **`Identity`**: Cryptographic identity proofs (`X-Identity-Txn`) verifying account ownership without on-chain fees.
- **`exceptions`**: `AIEconomyError` hierarchy; every class also subclasses the built-in type raised before 0.5.0.
- **`types`**: Protocols and aliases (`ServerSigner`, `ChainLookup`, `Prices`, `Transport`, ...) for public signatures.

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
# 1. Offline tests (default, fast, no external network needed), with the CI coverage floor:
pytest --cov=ai_economy_sdk --cov-fail-under=91

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
│       ├── ci.yml              # Multi-tier GitHub Actions workflow
│       └── release.yml         # PyPI trusted publishing + GitHub Release (manual trigger only)
├── ai_economy_sdk/             # Core package source
│   ├── __init__.py             # Public exports (PaymentGate, paid_mcp_session, etc.)
│   ├── chain.py                # ChainReader for Indexer queries and pass verification
│   ├── client.py               # paid_mcp_session client implementation
│   ├── exceptions.py           # AIEconomyError hierarchy
│   ├── gate.py                 # PaymentGate ASGI middleware and FastMCP wrapper
│   ├── identity.py             # X-Identity-Txn generation and verification
│   ├── network.py              # CAIP-2 network resolution and endpoint defaults
│   ├── py.typed                # PEP 561 marker
│   ├── replay.py               # In-memory and SQLite ReplayStore
│   ├── signer.py               # DefaultAvmSigner implementation
│   └── types.py                # Public Protocols and type aliases
├── tests/                      # Self-contained pytest test suite
│   ├── conftest.py             # Shared LocalNet fixtures (KMD dispenser, account creation)
│   ├── test_adversarial_localnet.py # Security attack vectors on LocalNet
│   ├── test_chain.py           # ChainReader unit tests
│   ├── test_client.py          # paid_mcp_session over both transports (offline)
│   ├── test_e2e_localnet.py    # End-to-end LocalNet tool execution and spend controls
│   ├── test_e2e_replay.py      # Replay attack prevention tests
│   ├── test_fee_floor.py       # Fee floor clamping and loss allowance
│   ├── test_gate.py            # PaymentGate middleware unit tests
│   ├── test_identity.py        # Ed25519 identity verification unit tests
│   ├── test_identity_txn.py    # X-Identity-Txn format and validity tests
│   ├── test_networks.py        # CAIP-2 network resolution and public simulate tests
│   ├── test_package.py         # Version, py.typed, exports, exception hierarchy
│   ├── test_passes.py          # Vendor pass verification tests
│   ├── test_replay_store.py    # ReplayStore SQLite persistence tests
│   ├── test_sdk.py             # AIEconomySDK builder and pricing-function tests
│   ├── test_settle_on_success.py # Settle-on-success guarantee tests
│   ├── test_signer.py          # Signer unit tests
│   ├── test_subscription_issuer.py # Issuer enforcement tests
│   └── test_transports.py      # Gate on Streamable HTTP/SSE, fail-closed behaviour
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
7. **Fail Closed**: A priced tool call must never run without a verified payment, whatever the transport or mount path. Never grant or deny a payment, pass, or subscription silently: log to `logging.getLogger("ai_economy_sdk")`. Catch specific exceptions, not `Exception`.
8. **Exceptions**: Raise classes from `ai_economy_sdk.exceptions`; a new class must also subclass the built-in type previously raised at that site.

## Pull Request Checklist

Before submitting a pull request, ensure:
- [ ] Code style and formatting checks pass (`ruff check .`, `ruff format --check .`).
- [ ] Offline test suite passes (`pytest`).
- [ ] LocalNet test suite passes (`LOCALNET=1 pytest`).
- [ ] Public network simulation suite passes (`PUBLIC_NETS=1 pytest`).
- [ ] Distribution package builds cleanly (`python -m build` and `twine check dist/*`).
- [ ] No hardcoded machine paths, local usernames, or credentials exist in the diff.
- [ ] `CHANGELOG.md` is updated with all public-facing changes under the unreleased or current release header.
