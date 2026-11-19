"""Default Algorand Virtual Machine (AVM) signer implementation.

Implements the ``AvmSigner`` protocol required by ``x402`` for signing, simulating,
broadcasting, and confirming Algorand transactions and atomic transaction groups.
"""

import base64

from algosdk import account, encoding, transaction
from algosdk.v2client.models import SimulateRequest, SimulateRequestTransactionGroup


class DefaultAvmSigner:
    """A default implementation of AvmSigner for convenience.

    Wraps an Algorand account private key and an optional AlgodClient connection
    to handle signing and submitting single transactions or atomic transaction groups.

    Parameters
    ----------
    private_key : str
        Base64-encoded 32-byte Algorand private key.
    algod_client : algosdk.v2client.algod.AlgodClient, optional
        Connected algod client used for group simulation, transmission, and confirmation.
        Default is None.

    Attributes
    ----------
    private_key : str
        The signing key.
    address : str
        The corresponding Algorand public address.
    algod_client : algosdk.v2client.algod.AlgodClient or None
        Client used for on-chain interactions.

    See Also
    --------
    PaymentGate : Accepts a signer to sign fee-payer and settlement transactions.

    Examples
    --------
    >>> from algosdk import account
    >>> from ai_economy_sdk.signer import DefaultAvmSigner
    >>> sk, addr = account.generate_account()
    >>> signer = DefaultAvmSigner(sk)
    >>> signer.address == addr
    True
    >>> signer.get_addresses() == [addr]
    True
    """

    def __init__(self, private_key: str, algod_client=None):
        """Initialize the signer with a private key and optional algod client.

        Parameters
        ----------
        private_key : str
            Base64-encoded Algorand private key.
        algod_client : algosdk.v2client.algod.AlgodClient, optional
            Connected algod client. Default is None.
        """
        self.private_key = private_key
        self.address = account.address_from_private_key(private_key)
        self.algod_client = algod_client

    def get_addresses(self):
        """Return the list of public addresses managed by this signer.

        Returns
        -------
        list of str
            Single-element list containing ``self.address``.

        Examples
        --------
        >>> from algosdk import account
        >>> from ai_economy_sdk.signer import DefaultAvmSigner
        >>> sk, addr = account.generate_account()
        >>> signer = DefaultAvmSigner(sk)
        >>> signer.get_addresses() == [addr]
        True
        """
        return [self.address]

    def sign_transactions(self, unsigned_txns, indexes):
        """Sign selected transactions in a list.

        Parameters
        ----------
        unsigned_txns : list of bytes
            List of raw msgpack-encoded unsigned transaction bytes.
        indexes : list or set of int
            0-based indices in ``unsigned_txns`` to sign.

        Returns
        -------
        list of bytes or None
            List of the same length as ``unsigned_txns``, containing raw signed
            transaction bytes at designated indexes and None elsewhere.

        Examples
        --------
        >>> import base64, algosdk.encoding
        >>> from algosdk import account, transaction
        >>> from ai_economy_sdk.signer import DefaultAvmSigner
        >>> sk, addr = account.generate_account()
        >>> signer = DefaultAvmSigner(sk)
        >>> sp = transaction.SuggestedParams(1000, 1, 1000, "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=", flat_fee=True)
        >>> txn = transaction.PaymentTxn(addr, sp, addr, 0)
        >>> raw_bytes = base64.b64decode(algosdk.encoding.msgpack_encode(txn))
        >>> signed = signer.sign_transactions([raw_bytes], [0])
        >>> len(signed)
        1
        >>> signed[0] is not None
        True
        """
        signed = []
        for i, txn_bytes in enumerate(unsigned_txns):
            if i in indexes:
                signed.append(self._sign_raw(txn_bytes))
            else:
                signed.append(None)
        return signed

    def sign_transaction(self, txn_bytes, fee_payer, network):
        """Sign a single raw transaction.

        Parameters
        ----------
        txn_bytes : bytes
            Raw msgpack-encoded unsigned transaction bytes.
        fee_payer : bool
            Whether this signer acts as fee payer for the transaction.
        network : str
            Target network identifier (unused during raw local signing).

        Returns
        -------
        bytes
            Raw msgpack-encoded signed transaction bytes.

        Examples
        --------
        >>> import base64, algosdk.encoding
        >>> from algosdk import account, transaction
        >>> from ai_economy_sdk.signer import DefaultAvmSigner
        >>> sk, addr = account.generate_account()
        >>> signer = DefaultAvmSigner(sk)
        >>> sp = transaction.SuggestedParams(1000, 1, 1000, "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=", flat_fee=True)
        >>> txn = transaction.PaymentTxn(addr, sp, addr, 0)
        >>> raw_bytes = base64.b64decode(algosdk.encoding.msgpack_encode(txn))
        >>> stxn = signer.sign_transaction(raw_bytes, False, "mainnet")
        >>> len(stxn) > 0
        True
        """
        return self._sign_raw(txn_bytes)

    def sign_group(self, group_bytes, fee_payer, indexes, network):
        """Sign specified transactions within an atomic transaction group.

        Parameters
        ----------
        group_bytes : list of bytes
            List of raw transaction bytes representing the atomic group.
        fee_payer : bool
            Whether this signer acts as fee payer for the group.
        indexes : iterable of int
            Indices within ``group_bytes`` to be signed by this signer.
        network : str
            Target network identifier.

        Returns
        -------
        list of bytes
            New list where transactions at ``indexes`` have been replaced with
            their signed counterparts.

        Examples
        --------
        >>> import base64, algosdk.encoding
        >>> from algosdk import account, transaction
        >>> from ai_economy_sdk.signer import DefaultAvmSigner
        >>> sk, addr = account.generate_account()
        >>> signer = DefaultAvmSigner(sk)
        >>> sp = transaction.SuggestedParams(1000, 1, 1000, "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=", flat_fee=True)
        >>> txn = transaction.PaymentTxn(addr, sp, addr, 0)
        >>> raw = base64.b64decode(algosdk.encoding.msgpack_encode(txn))
        >>> group = signer.sign_group([raw], True, [0], "mainnet")
        >>> len(group)
        1
        """
        signed = list(group_bytes)
        for i in indexes:
            signed[i] = self._sign_raw(group_bytes[i])
        return signed

    def simulate_group(self, group_bytes, network):
        """Simulate execution of an atomic transaction group against algod.

        Decodes the transactions, constructs a SimulateRequest with
        ``allow_empty_signatures=True``, and calls ``algod_client.simulate_transactions``.

        Parameters
        ----------
        group_bytes : list of bytes
            List of raw msgpack-encoded transaction bytes in the group.
        network : str
            Target network identifier.

        Returns
        -------
        dict
            Raw simulation response dictionary from the algod node.

        Raises
        ------
        RuntimeError
            If ``self.algod_client`` is None, or if simulation fails with a
            failure message returned by algod.

        Examples
        --------
        ```python
        # Needs connected AlgodClient:
        # sim_result = signer.simulate_group(group_bytes, "localnet")
        ```
        """
        if self.algod_client is None:
            raise RuntimeError("algod_client required")
        decoded = [self._decode(b) for b in group_bytes]
        req = SimulateRequest(
            txn_groups=[SimulateRequestTransactionGroup(txns=decoded)],
            allow_empty_signatures=True,
        )
        result = self.algod_client.simulate_transactions(req)
        txn_groups = result.get("txn-groups", [])
        if txn_groups and txn_groups[0].get("failure-message"):
            raise RuntimeError(txn_groups[0]["failure-message"])
        return result

    def send_group(self, group_bytes, network):
        """Broadcast a signed atomic transaction group to the network.

        Parameters
        ----------
        group_bytes : list of bytes
            List of signed raw transaction bytes.
        network : str
            Target network identifier.

        Returns
        -------
        str
            First transaction ID (txid) of the submitted atomic group.

        Raises
        ------
        RuntimeError
            If ``self.algod_client`` is None.

        Notes
        -----
        Broadcasts the atomic transaction group to the Algorand blockchain. Once confirmed,
        the on-chain state transition (transfers, fees, note payloads) becomes irreversible.

        Examples
        --------
        ```python
        # Needs connected AlgodClient:
        # txid = signer.send_group(signed_group_bytes, "localnet")
        ```
        """
        if not self.algod_client:
            raise RuntimeError("algod_client required")
        decoded = [self._decode(b) for b in group_bytes]
        return self.algod_client.send_transactions(decoded)

    def confirm_transaction(self, txid, network, rounds=4):
        """Wait for an on-chain transaction to be confirmed by algod.

        Parameters
        ----------
        txid : str
            Transaction ID to wait for.
        network : str
            Target network identifier.
        rounds : int, default: 4
            Number of rounds to wait before raising an error.

        Returns
        -------
        dict
            Confirmed transaction information dictionary from algod.

        Raises
        ------
        RuntimeError
            If ``self.algod_client`` is None.

        Examples
        --------
        ```python
        # Needs connected AlgodClient:
        # info = signer.confirm_transaction(txid, "localnet")
        ```
        """
        if not self.algod_client:
            raise RuntimeError("algod_client required")
        return transaction.wait_for_confirmation(self.algod_client, txid, rounds)

    def _decode(self, raw_bytes):
        return encoding.msgpack_decode(base64.b64encode(raw_bytes).decode())

    def _sign_raw(self, txn_bytes):
        txn = self._decode(txn_bytes)
        return base64.b64decode(encoding.msgpack_encode(txn.sign(self.private_key)))
