"""Chain interaction for subscriptions and usage via Algorand Indexer.

Queries transaction history and asset holdings to support:
- Past usage tracking for volume/bulk tiered discounts.
- Subscription verification based on ASA ownership and recent transfer recency.
- Vendor and listing passes identified by the ``x402-pass:<scope>`` payment note prefix.
"""

import base64
import time
from datetime import datetime, timezone
from typing import Any

import algosdk.v2client.indexer

PASS_NOTE_PREFIX = "x402-pass:"


class ChainReader:
    """Reader for Algorand blockchain state via an Indexer client.

    Provides high-level queries for account payment history, recurring subscription
    asset holdings, and time-bounded vendor passes.

    Parameters
    ----------
    indexer_url : str, default: "http://localhost:8980"
        Base URL of the Algorand Indexer REST API.
    indexer_token : str, default: ""
        Authentication token header for the indexer API.
    indexer : Any, optional
        Pre-configured ``algosdk.v2client.indexer.IndexerClient`` instance. If provided,
        overrides ``indexer_url`` and ``indexer_token``.

    Attributes
    ----------
    indexer : algosdk.v2client.indexer.IndexerClient
        Underlying indexer client used for network queries.

    See Also
    --------
    for_network : Factory constructor resolving indexer endpoints by network name.

    Examples
    --------
    >>> from ai_economy_sdk.chain import ChainReader, PASS_NOTE_PREFIX
    >>> PASS_NOTE_PREFIX
    'x402-pass:'
    >>> reader = ChainReader("http://localhost:8980")
    >>> reader.indexer is not None
    True
    """

    def __init__(
        self,
        indexer_url: str = "http://localhost:8980",
        indexer_token: str = "",
        indexer: Any = None,
    ):
        """Initialize the ChainReader instance.

        Parameters
        ----------
        indexer_url : str, default: "http://localhost:8980"
            Base URL for the indexer.
        indexer_token : str, default: ""
            API token for the indexer.
        indexer : Any, optional
            Existing IndexerClient instance. Default is None.
        """
        self.indexer = indexer or algosdk.v2client.indexer.IndexerClient(indexer_token, indexer_url)

    @classmethod
    def for_network(
        cls, network: str, indexer_url: str | None = None, indexer_token: str | None = None
    ) -> "ChainReader":
        """Construct a ChainReader configured for a specific Algorand network.

        Resolves public indexer endpoints for ``"mainnet"`` and ``"testnet"`` (AlgoNode),
        or localhost for ``"localnet"``. Custom URLs/tokens override network defaults.

        Parameters
        ----------
        network : str
            Network name, alias, or CAIP-2 identifier (e.g., ``"mainnet"``, ``"testnet"``, ``"localnet"``).
        indexer_url : str, optional
            Custom indexer endpoint URL. If None, uses default endpoint for the network.
        indexer_token : str, optional
            Custom indexer token. If None, uses default token for the network.

        Returns
        -------
        ChainReader
            Instantiated reader pointing to the appropriate indexer.

        See Also
        --------
        endpoints_for : Resolves network default endpoints.

        Examples
        --------
        >>> from ai_economy_sdk.chain import ChainReader
        >>> reader = ChainReader.for_network("mainnet")
        >>> reader.indexer is not None
        True
        """
        from .network import endpoints_for

        ep = endpoints_for(network)
        return cls(
            indexer_url=indexer_url or ep.indexer_url,
            indexer_token=ep.indexer_token if indexer_token is None else indexer_token,
        )

    def past_usage(
        self,
        address: str,
        pay_to: str,
        asset_id: int | str,
        days: int = 30,
        now: float | int | None = None,
    ) -> int:
        """Calculate total amount paid by an address to a recipient within a time window.

        Searches indexed transactions where ``address`` was the sender and ``pay_to``
        was the receiver for payment (ALGO) or asset transfer (ASA) transactions.

        Parameters
        ----------
        address : str
            Algorand address of the sender/buyer.
        pay_to : str
            Algorand address of the service recipient/vendor.
        asset_id : int or str
            Asset ID: 0 for native ALGO (microAlgos), or positive ASA ID.
        days : int, default: 30
            Number of days prior to ``now`` to inspect.
        now : float or int, optional
            Current unix timestamp. If None, uses system time (``time.time()``).

        Returns
        -------
        int
            Sum of transaction amounts transferred within the window, in base units
            (microAlgos for ALGO, or base atomic units for ASAs). Returns 0 on query errors.

        Notes
        -----
        Paginates through all matching indexer transactions and tallies successful
        payments matching the target recipient and asset ID.

        Examples
        --------
        ```python
        # Needs LocalNet Indexer:
        # total_microalgos = reader.past_usage(buyer_addr, vendor_addr, 0, days=30)
        ```
        """
        try:
            curr_now = now or time.time()
            start = datetime.fromtimestamp(curr_now - days * 86400, tz=timezone.utc)
            start_time = start.strftime("%Y-%m-%dT%H:%M:%SZ")

            aid = int(asset_id)
            kwargs: dict[str, Any] = {
                "address": address,
                "address_role": "sender",
                "start_time": start_time,
                "txn_type": "pay" if aid == 0 else "axfer",
            }
            if aid != 0:
                kwargs["asset_id"] = aid

            total = 0
            next_page = None
            while True:
                if next_page:
                    kwargs["next_page"] = next_page
                resp = self.indexer.search_transactions(**kwargs)
                txns = resp.get("transactions", [])
                for tx in txns:
                    if aid == 0:
                        pay_info = tx.get("payment-transaction", {})
                        if pay_info.get("receiver") == pay_to:
                            total += pay_info.get("amount", 0)
                    else:
                        axfer_info = tx.get("asset-transfer-transaction", {})
                        if (
                            axfer_info.get("receiver") == pay_to
                            and axfer_info.get("asset-id") == aid
                        ):
                            total += axfer_info.get("amount", 0)
                next_page = resp.get("next-token")
                if not next_page:
                    break
            return total
        except Exception:
            return 0

    def has_active_subscription(
        self,
        address: str,
        asset_id: int | str | None,
        max_age_days: float = 30.0,
        now: float | int | None = None,
        issuer: str | None = None,
    ) -> bool:
        """Check whether an account holds a subscription asset received within a time window.

        Verifies that ``address`` currently holds a non-zero balance of ``asset_id``,
        and that the most recent incoming transfer of that asset occurred within the last
        ``max_age_days`` days.

        Parameters
        ----------
        address : str
            Algorand address of the subscriber.
        asset_id : int, str, or None
            ASA ID of the subscription token. Returns False if 0 or None.
        max_age_days : float, default: 30.0
            Maximum age in days of the latest incoming asset transfer.
        now : float or int, optional
            Reference unix timestamp. Defaults to ``time.time()``.
        issuer : str, optional
            If provided, only transfers originating from this sender address count
            as valid subscription issuances.

        Returns
        -------
        bool
            True if the subscriber currently holds the asset and the latest transfer
            is within ``max_age_days``, False otherwise.

        Examples
        --------
        >>> from ai_economy_sdk.chain import ChainReader
        >>> reader = ChainReader("http://localhost:8980")
        >>> reader.has_active_subscription("", None)
        False
        """
        if not address or not asset_id:
            return False
        try:
            aid = int(asset_id)
            if aid == 0:
                return False

            curr_now = now or time.time()
            acct_resp = self.indexer.account_info(address)
            account_data = acct_resp.get("account", {})
            assets = account_data.get("assets", [])

            has_balance = False
            for asset in assets:
                if asset.get("asset-id") == aid and asset.get("amount", 0) > 0:
                    has_balance = True
                    break

            if not has_balance:
                return False

            kwargs: dict[str, Any] = {
                "address": address,
                "address_role": "receiver",
                "asset_id": aid,
                "txn_type": "axfer",
            }
            newest_round_time = None
            next_page = None
            while True:
                if next_page:
                    kwargs["next_page"] = next_page
                resp = self.indexer.search_transactions(**kwargs)
                txns = resp.get("transactions", [])
                for tx in txns:
                    if issuer is not None:
                        sender = tx.get("sender")
                        if sender != issuer:
                            continue
                    rt = tx.get("round-time")
                    if rt is not None:
                        if newest_round_time is None or rt > newest_round_time:
                            newest_round_time = rt
                next_page = resp.get("next-token")
                if not next_page:
                    break

            if newest_round_time is None:
                return False

            return (curr_now - newest_round_time) / 86400.0 <= max_age_days
        except Exception:
            return False

    def active_passes(
        self,
        address: str,
        pay_to: str,
        asset_id: int | str,
        scopes: dict[str, int],
        days: float = 30.0,
        now: float | int | None = None,
    ) -> list[dict[str, Any]]:
        """Find valid passes purchased by an address from a vendor.

        A pass is an on-chain payment from the buyer to the vendor's ``pay_to`` address
        with note prefix ``x402-pass:<scope>`` whose transfer amount is at least the
        price specified for that scope in ``scopes``.

        Parameters
        ----------
        address : str
            Algorand address of the pass holder (buyer).
        pay_to : str
            Vendor receiver address.
        asset_id : int or str
            Asset ID used to purchase the pass (0 for ALGO, or ASA ID).
        scopes : dict[str, int]
            Mapping of pass scope names (e.g., ``{"listing:tts-1": 50000}``) to their
            minimum purchase prices.
        days : float, default: 30.0
            Pass validity window in days from the round time of payment.
        now : float or int, optional
            Reference unix timestamp. Defaults to ``time.time()``.

        Returns
        -------
        list of dict[str, Any]
            Sorted list of active pass records ordered by round time. Each dictionary contains:
            ``"scope"`` (str), ``"txid"`` (str), ``"amount"`` (int),
            ``"round_time"`` (int), and ``"expires"`` (float).

        Notes
        -----
        If multiple payments exist for the same scope, the newest payment within
        the validity window takes precedence.

        Examples
        --------
        >>> from ai_economy_sdk.chain import ChainReader
        >>> reader = ChainReader("http://localhost:8980")
        >>> reader.active_passes("", "", 0, {})
        []
        """
        if not address or not pay_to or not scopes:
            return []
        try:
            curr_now = float(now or time.time())
            start = datetime.fromtimestamp(curr_now - days * 86400, tz=timezone.utc)
            aid = int(asset_id)
            kwargs: dict[str, Any] = {
                "address": address,
                "address_role": "sender",
                "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "note_prefix": PASS_NOTE_PREFIX.encode(),
                "txn_type": "pay" if aid == 0 else "axfer",
            }
            if aid != 0:
                kwargs["asset_id"] = aid
            found: dict[str, dict[str, Any]] = {}
            next_page = None
            while True:
                if next_page:
                    kwargs["next_page"] = next_page
                resp = self.indexer.search_transactions(**kwargs)
                for tx in resp.get("transactions", []):
                    info = (
                        tx.get("payment-transaction")
                        if aid == 0
                        else tx.get("asset-transfer-transaction")
                    )
                    info = info or {}
                    if info.get("receiver") != pay_to:
                        continue
                    if aid != 0 and info.get("asset-id") != aid:
                        continue
                    try:
                        note = base64.b64decode(tx.get("note", "")).decode()
                    except Exception:
                        continue
                    if not note.startswith(PASS_NOTE_PREFIX):
                        continue
                    scope = note[len(PASS_NOTE_PREFIX) :]
                    price = scopes.get(scope)
                    if price is None or info.get("amount", 0) < price:
                        continue
                    rt = tx.get("round-time")
                    if rt is None or curr_now - rt > days * 86400:
                        continue
                    if scope not in found or rt > found[scope]["round_time"]:
                        found[scope] = {
                            "scope": scope,
                            "txid": tx.get("id"),
                            "amount": info.get("amount", 0),
                            "round_time": rt,
                            "expires": rt + days * 86400,
                        }
                next_page = resp.get("next-token")
                if not next_page:
                    break
            return sorted(found.values(), key=lambda x: x["round_time"])
        except Exception:
            return []

    def has_pass(
        self,
        address: str,
        pay_to: str,
        asset_id: int | str,
        scopes: dict[str, int],
        days: float = 30.0,
        now: float | int | None = None,
    ) -> bool:
        """Check whether an account holds at least one active pass for the specified vendor.

        Parameters
        ----------
        address : str
            Algorand address of the buyer/caller.
        pay_to : str
            Vendor receiver address.
        asset_id : int or str
            Asset ID used for pass payment (0 for ALGO, or positive ASA ID).
        scopes : dict[str, int]
            Mapping of eligible pass scope names to minimum prices.
        days : float, default: 30.0
            Pass validity duration in days.
        now : float or int, optional
            Reference unix timestamp. Defaults to ``time.time()``.

        Returns
        -------
        bool
            True if any active pass matches the criteria, False otherwise.

        See Also
        --------
        active_passes : Returns details of all active passes.

        Examples
        --------
        >>> from ai_economy_sdk.chain import ChainReader
        >>> reader = ChainReader("http://localhost:8980")
        >>> reader.has_pass("", "", 0, {})
        False
        """
        return bool(self.active_passes(address, pay_to, asset_id, scopes, days, now))
