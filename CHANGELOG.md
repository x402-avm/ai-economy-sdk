# Changelog

All notable changes to `ai-economy-sdk` are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1] - 2026-11-19

### Fixed
- When the buyer's own spend controls refused a payment inside `paid_mcp_session`, the caller got an `ExceptionGroup` wrapping x402's `PaymentError` (cause `NoMatchingRequirementsError`), so `except AIEconomyError` missed it. The refusal now surfaces as `SpendLimitExceededError`, with the x402 error as `__cause__`. Other errors, and groups that contain anything besides the refusal, propagate unchanged.

### Added
- `SpendLimitExceededError(AIEconomyError)`, exported from the package and documented in `ai_economy_sdk.exceptions`. Its message includes the amount and asset the server asked for.

## [0.5.0] - 2026-11-19

### Security
- **Unpaid tool calls over Streamable HTTP and under mount prefixes.** In 0.4.x the gate's ASGI middleware only inspected `POST` requests whose path ended in `/messages`, and its x402 route table only matched `POST /messages*`. A FastMCP Streamable HTTP app wrapped with `PaymentGate.middleware()` (`POST /mcp`), or a gated SSE app mounted under a path prefix, passed priced `tools/call` requests to the server without a payment. The gate now inspects the JSON-RPC body of every HTTP `POST`, whatever its path or transport, and its route table matches every path.
- Fail closed: `SettleOnSuccessMiddleware` now refuses (with `PaymentRequiredError`) any priced tool call that did not pass through the HTTP gate, for example a call over a transport the gate does not wrap. A gated call whose payment reservation has expired is refused instead of being served without settlement. JSON-RPC batches containing `tools/call` are rejected with HTTP 400, and the HTTP middleware refuses priced calls with HTTP 500 if `SettleOnSuccessMiddleware` was never installed.
- `subscription_issuer` could be silently ignored: when `chain.has_active_subscription()` raised `TypeError`, 0.4.x retried the lookup without `issuer=`. The issuer is now always passed, and `PaymentGate` raises `ConfigurationError` at construction if the chain's method has no `issuer` parameter.
- `DefaultAvmSigner` no longer stores the key in a public attribute, and its `repr()` shows only the address.

### Added
- Streamable HTTP support: `PaymentGate.wrap(mcp, transport="streamable-http")` (alias `"http"`) serves the MCP Streamable HTTP transport at `POST /mcp`, stateless by default (`stateless_http=False` enables MCP sessions). `wrap()` also accepts `path=` and other `FastMCP.http_app()` keyword arguments. `AIEconomySDK.build_app(transport=...)` forwards to it.
- `paid_mcp_session(..., transport=...)`: connects over Streamable HTTP or SSE. Without `transport=`, URLs whose path ends in `/mcp` use Streamable HTTP and all others use SSE, as in 0.4.
- Exception hierarchy in `ai_economy_sdk.exceptions`, exported from the package: `AIEconomyError`, `ConfigurationError` (a `ValueError`), `UnknownNetworkError`, `AlgodClientRequiredError` (also a `RuntimeError`), `PaymentVerificationError` (a `RuntimeError`), `PaymentRequiredError` and `SettlementError` (FastMCP `ToolError`s). Each also subclasses the type raised at the same place in 0.4.x, so existing handlers keep working.
- Public typing module `ai_economy_sdk.types` (`ServerSigner`, `ChainLookup`, `Prices`, `GatePriceFunction`, `ToolPriceFunction`, `SpendControlsArg`, `Transport`), used in the signatures of `PaymentGate`, `AIEconomySDK` and `paid_mcp_session`. Annotations only; no runtime checks.
- `py.typed` marker, so type checkers use the package's annotations (the `Typing :: Typed` classifier was already declared).
- `ai_economy_sdk.__version__`.
- `SettleOnSuccessMiddleware` is exported for apps that build the FastMCP HTTP app themselves.
- Logging to the `ai_economy_sdk` logger (with a `NullHandler`): refused calls, settlement failures, expired reservations and failed chain lookups at WARNING.
- Python 3.14 support (classifier and CI).
- Release workflow (`.github/workflows/release.yml`): build, `twine check`, PyPI trusted publishing with attestations, GitHub Release. Manual trigger only.

### Changed
- `PaymentGate.wrap()` without `transport=` still serves SSE but emits a `FutureWarning`; the default will change to `"streamable-http"` in a future release.
- The SSE compatibility patch (running `mcp._mcp_server.run` with `stateless=True`, so raw JSON-RPC clients can call tools without the MCP `initialize` handshake) is now applied only for the SSE transport. Correction to the 0.4.0 entry "Removed monkeypatches": this FastMCP patch was still present in 0.4.0 and 0.4.1. FastMCP 3.4 has no public hook for it; Streamable HTTP uses the public `stateless_http` option instead.
- `AIEconomySDK.add_tool()` decides once, from the pricing function's signature, whether to call it as `(args, sender)` or `(args)`. **Potentially breaking:** a pricing function accepting neither raises `ConfigurationError` at registration (0.4.x failed per request).
- `ChainReader` and `PaymentGate` catch only lookup errors (algod/indexer HTTP errors, connection errors, malformed responses) and log them; the lookup then reports no usage, subscription or pass, as before. **Potentially breaking:** other exceptions (programming errors) now propagate instead of being swallowed.
- **Potentially breaking:** `DefaultAvmSigner.private_key` is now a read-only property; assigning to it raises `AttributeError`.
- Signing uses `AccountTransactionSigner` instead of the deprecated `Transaction.sign()`.
- On the SSE transport the `POST` response is the server's own response plus settlement headers (0.4.x replaced the body with `Accepted`, which is what the server sends).
- Requires `x402-algorand>=0.2.1`.
- Package metadata: authors are "x402-avm contributors" (no email address); `LICENSE` copyright holder is "x402-avm contributors". Builds use Hatchling; the sdist contains the package, tests, `README.md`, `CHANGELOG.md`, `LICENSE` and `pyproject.toml` (plus `.gitignore`, which Hatchling always adds), without `.egg-info` or `setup.cfg`.

### Deprecated
- `sign_identity()` (legacy `x-sender-*` headers): use `sign_identity_txn()`, which binds the proof to one network. It still works and emits a `DeprecationWarning`; servers keep accepting legacy headers, and `paid_mcp_session` still sends them.
- `DefaultAvmSigner.private_key`: reading it emits a `DeprecationWarning`. Keep your own reference to the key.

### Fixed
- A `TypeError` raised inside a two-argument pricing function was caught and the function was called again with one argument.
- `pyproject.toml` now declares the `Repository` URL; the 0.4.1 entry listed it but it was missing.

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
