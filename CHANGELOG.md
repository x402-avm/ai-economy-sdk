# Changelog

All notable changes to `ai-economy-sdk` are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.1] - 2026-11-19

### Added
- Project URLs (`Homepage`, `Documentation`, `Repository`, `Issues`, `Changelog`), PyPI classifiers, and dev optional dependencies (`pytest`, `pytest-asyncio`, `ruff`, `build`, `twine`) in `pyproject.toml`.
- Self-contained LocalNet test infrastructure in `tests/conftest.py` with dynamic KMD dispenser funding, ephemeral account creation, and dynamic asset generation, removing external file and private path dependencies.
- Public repository documentation and metadata: comprehensive `README.md` with working FastMCP quickstart, `AGENTS.md` coding agent specification, `ruff.toml`, `.editorconfig`, GitHub Actions CI pipeline, and Dependabot weekly updates with PR grouping.

### Changed
- Code formatted with Ruff (`ruff check` and `ruff format`) for standard import ordering and styling without behavioral changes.
- Dependency policy: Kept stable ranges `fastmcp>=3.4,<4` and `mcp>=1.29,<2`; major versions `mcp 2.x` and `fastmcp 4.x` are pinned/ignored in Dependabot configuration due to upstream `httpx2` breaking changes affecting `x402` compatibility.

## [0.4.0] - 2026-11-18

### Added
- Integration with the official `x402>=2.28` framework and `x402-algorand>=0.2.0` plugin.
- Added `spend_controls` parameter to `paid_mcp_session` (`spend_controls: dict | Literal[False] | None = None`), allowing clients to enforce per-payment caps or whitelist custom ASAs before signing.
- Comprehensive NumPy-style docstrings across public API methods (`PaymentGate`, `paid_mcp_session`, `ChainReader`, `DefaultAvmSigner`).

### Changed
- Removed monkeypatches in favor of native `find_default_asset` and spend control filtering provided by `x402-algorand`.
- Updated CAIP-2 network identifiers to adhere to Algorand CAIP-2 profile (first 32 characters of base64 genesis hash).

## [0.3.1] - 2026-11-18

### Added
- Individual vendor passes support: `ChainReader.active_passes()`, `ChainReader.has_pass()`, and `PaymentGate(pass_scopes=..., pass_days=...)`.
- Included official MIT `LICENSE` file in package distribution.

### Changed
- Updated dependency requirement to `x402-algorand>=0.1.4`.

## [0.3.0] - 2026-11-18

### Added
- Multi-network resolution: `PaymentGate`, `AIEconomySDK`, and `paid_mcp_session` accept `"mainnet"`, `"testnet"`, `"localnet"`, and CAIP-2 identifiers (`algorand:<genesis_hash>`).
- New network utilities: `resolve_network`, `endpoints_for`, `is_public`, and `usdc_asset_id`.
- Browser wallet identity verification: `make_identity_txn`, `sign_identity_txn`, and `verify_identity_txn` generating zero-fee `X-Identity-Txn` proofs.
- Subscription issuer enforcement: `PaymentGate(subscription_issuer=...)` and `has_active_subscription(issuer=...)` to prevent secondary token transfer exploitation.

### Changed
- `paid_mcp_session` automatically resolves algod endpoints according to network selection (AlgoNode for MainNet/TestNet).
- Fee floor enforcement (2,000 microALGO minimum) restricted to native ALGO pricing, avoiding interference with ASA/USDC rates.

## [0.2.0] - 2026-11-17

### Added
- Settle-on-Success guarantee: `PaymentGate` verifies payments before tool execution but broadcasts settlement transactions strictly after tool success.
- Persistent replay protection: `ReplayStore` with in-memory and SQLite WAL implementations for distributed multi-process servers.
- Fee floor enforcement: startup validation rejects ALGO prices under 2,000 microALGO unless `allow_loss=True`.
- Off-chain caller identity verification (`sign_identity` and `verify_identity`).
