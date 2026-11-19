"""Identity verification via Algorand signatures (no payment required).

Provides cryptographic proof of account ownership for zero-cost authentication,
tiered pricing, vendor passes, and subscription validation without submitting
transactions to the blockchain.

Supports two signature formats:
1. Modern ``X-Identity-Txn``: base64 msgpack-encoded signed zero-amount, zero-fee
   self-payment transaction bound to the network's genesis hash with a validity
   window <= 1000 rounds. It can never be settled or executed on chain.
2. Legacy ``x-sender-*`` headers: Ed25519 signature over ``x402-identity:<addr>:<ts>``.
"""

import base64
import copy
import time
from collections.abc import Mapping
from typing import Any

import algosdk.account
import algosdk.atomic_transaction_composer
import algosdk.encoding
import algosdk.transaction
import algosdk.util
import nacl.signing

IDENTITY_HEADERS = ("x-sender-address", "x-sender-timestamp", "x-sender-signature")


def make_identity_txn(
    address: str,
    genesis_hash_b64: str,
    sp: algosdk.transaction.SuggestedParams | None = None,
    ts: int | None = None,
) -> algosdk.transaction.PaymentTxn:
    """Create an unsigned identity proof payment transaction.

    This transaction is never submitted to the network. It proves account ownership
    by signing a zero-amount, zero-fee self-payment containing a timestamped identity note.

    Parameters
    ----------
    address : str
        Algorand public address of the sender/prover.
    genesis_hash_b64 : str or bytes
        Base64-encoded genesis hash of the target network.
    sp : algosdk.transaction.SuggestedParams, optional
        Base suggested transaction parameters. If omitted, default parameters
        with fee=0, first=1, last=1000, and flat_fee=True are generated.
    ts : int, optional
        Unix timestamp (seconds) embedded in the transaction note. Defaults
        to current system time (``int(time.time())``).

    Returns
    -------
    algosdk.transaction.PaymentTxn
        Unsigned PaymentTxn with sender=address, receiver=address, amount=0, fee=0,
        and note ``x402-identity:<address>:<ts>``.

    See Also
    --------
    sign_identity_txn : Create and sign this transaction into an X-Identity-Txn header.
    verify_identity_txn : Validate and decode the signed transaction.

    Examples
    --------
    >>> from algosdk import account
    >>> from ai_economy_sdk.identity import make_identity_txn
    >>> _, addr = account.generate_account()
    >>> txn = make_identity_txn(addr, "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=", ts=1700000000)
    >>> txn.amt
    0
    >>> txn.fee
    0
    >>> txn.sender == txn.receiver == addr
    True
    """
    if ts is None:
        ts = int(time.time())

    gh_str = (
        genesis_hash_b64
        if isinstance(genesis_hash_b64, str)
        else base64.b64encode(genesis_hash_b64).decode("utf-8")
    )

    if sp is None:
        txn_sp = algosdk.transaction.SuggestedParams(
            fee=0,
            first=1,
            last=1000,
            gh=gh_str,
            gen="",
            flat_fee=True,
        )
    else:
        txn_sp = copy.copy(sp)
        txn_sp.fee = 0
        txn_sp.flat_fee = True
        txn_sp.gh = gh_str

    note = f"x402-identity:{address}:{ts}".encode()
    return algosdk.transaction.PaymentTxn(
        sender=address,
        sp=txn_sp,
        receiver=address,
        amt=0,
        note=note,
    )


def sign_identity_txn(
    private_key: str,
    genesis_hash_b64: str,
    ts: int | None = None,
) -> dict[str, str]:
    """Create and sign an identity proof transaction, returning the X-Identity-Txn header.

    Constructs a zero-amount self-payment transaction containing the timestamped
    identity note and signs it with the provided private key.

    Parameters
    ----------
    private_key : str
        Base64-encoded Algorand private key of the sender.
    genesis_hash_b64 : str
        Base64-encoded genesis hash of the network against which identity is proven.
    ts : int, optional
        Unix timestamp (seconds) embedded in the identity note. Defaults to current time.

    Returns
    -------
    dict[str, str]
        Single-entry dictionary ``{"X-Identity-Txn": "<base64 msgpack-encoded signed txn>"}``.

    See Also
    --------
    verify_identity_txn : Server-side verification function.
    sign_identity : Legacy three-header signing mechanism.

    Examples
    --------
    >>> from algosdk import account
    >>> from ai_economy_sdk.identity import sign_identity_txn
    >>> sk, _ = account.generate_account()
    >>> hdrs = sign_identity_txn(sk, "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=")
    >>> "X-Identity-Txn" in hdrs
    True
    """
    address = algosdk.account.address_from_private_key(private_key)
    txn = make_identity_txn(address, genesis_hash_b64, ts=ts)
    signer = algosdk.atomic_transaction_composer.AccountTransactionSigner(private_key)
    stxn = signer.sign_transactions([txn], [0])[0]
    return {"X-Identity-Txn": algosdk.encoding.msgpack_encode(stxn)}


def verify_identity_txn(
    header_value: str,
    expected_genesis_hash_b64: str,
    max_skew: float | int = 120,
    now: float | int | None = None,
) -> str | None:
    """Verify an X-Identity-Txn header value against timestamp skew and Algorand signature.

    Decodes the signed transaction, checks that it is a self-payment of 0 amount with
    0 fee and no rekeying or close-to fields, verifies network genesis hash matching,
    validates round validity window <= 1000, checks timestamp within ``max_skew`` seconds,
    and cryptographically verifies the Ed25519 signature against the sender address.

    Parameters
    ----------
    header_value : str
        Value of the ``X-Identity-Txn`` HTTP request header (base64 msgpack).
    expected_genesis_hash_b64 : str or bytes
        Base64-encoded genesis hash of the expected Algorand network.
    max_skew : float or int, default: 120
        Maximum allowable clock skew in seconds between the timestamp in the
        note and ``now``.
    now : float or int, optional
        Current unix timestamp. If None, defaults to ``time.time()``.

    Returns
    -------
    str or None
        The verified Algorand sender address if all security checks pass,
        or None if verification fails. Never raises exceptions.

    Notes
    -----
    Rejects multisig, logic signatures, and rekeyed accounts. This proof cannot
    be executed on chain because fee is 0 and rounds 1..1000 are long past on live nets.

    See Also
    --------
    sign_identity_txn : Client-side proof creation.
    verify_identity : High-level verification supporting both proof formats.

    Examples
    --------
    >>> from algosdk import account
    >>> from ai_economy_sdk.identity import sign_identity_txn, verify_identity_txn
    >>> sk, addr = account.generate_account()
    >>> gh = "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="
    >>> hdrs = sign_identity_txn(sk, gh)
    >>> verify_identity_txn(hdrs["X-Identity-Txn"], gh) == addr
    True
    >>> verify_identity_txn(hdrs["X-Identity-Txn"], "wrong_genesis_hash=") is None
    True
    """
    try:
        if not header_value:
            return None

        stxn = algosdk.encoding.msgpack_decode(header_value)
        if not hasattr(stxn, "transaction") or not hasattr(stxn, "signature"):
            return None

        # Disallow rekeyed / authorized-address signers, multisig, and logic signatures
        if getattr(stxn, "authorizing_address", None) is not None:
            return None
        if getattr(stxn, "multisig", None) is not None or getattr(stxn, "logic", None) is not None:
            return None

        txn = stxn.transaction
        if getattr(txn, "type", None) != "pay":
            return None

        sender = getattr(txn, "sender", None)
        receiver = getattr(txn, "receiver", None)
        if not sender or not receiver or sender != receiver:
            return None
        if not algosdk.encoding.is_valid_address(sender):
            return None

        if getattr(txn, "amt", 0) != 0:
            return None
        if getattr(txn, "fee", 0) != 0:
            return None
        if getattr(txn, "close_remainder_to", None) is not None:
            return None
        if getattr(txn, "rekey_to", None) is not None:
            return None

        # Verify genesis hash
        txn_gh = getattr(txn, "genesis_hash", None)
        if not txn_gh:
            return None
        txn_gh_bytes = base64.b64decode(txn_gh) if isinstance(txn_gh, str) else txn_gh
        exp_gh_bytes = (
            base64.b64decode(expected_genesis_hash_b64)
            if isinstance(expected_genesis_hash_b64, str)
            else expected_genesis_hash_b64
        )
        if txn_gh_bytes != exp_gh_bytes:
            return None

        # Verify validity window <= 1000
        fv = getattr(txn, "first_valid_round", None)
        lv = getattr(txn, "last_valid_round", None)
        if fv is None or lv is None or lv < fv or (lv - fv) > 1000:
            return None

        # Verify note format: x402-identity:<sender>:<int ts>
        note = getattr(txn, "note", None)
        if not isinstance(note, (bytes, bytearray)):
            return None
        try:
            note_str = note.decode("utf-8")
        except UnicodeDecodeError:
            return None

        parts = note_str.split(":")
        if len(parts) != 3 or parts[0] != "x402-identity" or parts[1] != sender:
            return None

        try:
            ts = int(parts[2])
        except ValueError:
            return None

        curr_now = now if now is not None else time.time()
        if abs(curr_now - ts) > max_skew:
            return None

        # Verify signature with sender public key
        sig = getattr(stxn, "signature", None)
        if not sig:
            return None
        raw_sig = base64.b64decode(sig) if isinstance(sig, str) else sig
        if len(raw_sig) != 64:
            return None

        vk = nacl.signing.VerifyKey(algosdk.encoding.decode_address(sender))
        vk.verify(txn.bytes_to_sign(), raw_sig)

        return sender
    except Exception:
        return None


def sign_identity(private_key: str, now: int | None = None) -> dict[str, str]:
    """Generate legacy identity verification headers signed with an Algorand private key.

    Signs the ASCII message ``x402-identity:<sender_address>:<timestamp>`` using Ed25519
    and returns the three legacy headers ``x-sender-address``, ``x-sender-timestamp``,
    and ``x-sender-signature``.

    Parameters
    ----------
    private_key : str
        Base64-encoded 32-byte Algorand private key.
    now : int, optional
        Unix timestamp in seconds. If None, defaults to ``int(time.time())``.

    Returns
    -------
    dict[str, str]
        Dictionary with keys ``x-sender-address``, ``x-sender-timestamp``,
        and ``x-sender-signature`` (base64-encoded signature).

    See Also
    --------
    sign_identity_txn : Preferred modern transaction-based proof.
    verify_identity : Validates both modern and legacy identity headers.

    Examples
    --------
    >>> from algosdk import account
    >>> from ai_economy_sdk.identity import sign_identity
    >>> sk, addr = account.generate_account()
    >>> hdrs = sign_identity(sk, now=1700000000)
    >>> hdrs["x-sender-address"] == addr
    True
    >>> hdrs["x-sender-timestamp"]
    '1700000000'
    >>> bool(hdrs["x-sender-signature"])
    True
    """
    address = algosdk.account.address_from_private_key(private_key)
    ts = int(now if now is not None else time.time())
    message = f"x402-identity:{address}:{ts}".encode()
    signature = algosdk.util.sign_bytes(message, private_key)
    return {
        "x-sender-address": str(address),
        "x-sender-timestamp": str(ts),
        "x-sender-signature": str(signature),
    }


def verify_identity(
    headers: Mapping[str, Any],
    max_skew: int = 120,
    now: int | None = None,
    genesis_hash_b64: str | None = None,
) -> str | None:
    """Verify identity headers against timestamp skew and Algorand signature.

    First checks for the presence of the modern ``X-Identity-Txn`` header (if
    ``genesis_hash_b64`` is provided). If absent or verification fails, falls back
    to inspecting the legacy ``x-sender-address``, ``x-sender-timestamp``, and
    ``x-sender-signature`` headers.

    Parameters
    ----------
    headers : Mapping[str, Any]
        HTTP request headers mapping (case-insensitive inspection).
    max_skew : int, default: 120
        Maximum allowable clock skew in seconds between the timestamp and ``now``.
    now : int, optional
        Current unix timestamp in seconds. Defaults to ``int(time.time())``.
    genesis_hash_b64 : str, optional
        Base64-encoded network genesis hash. Required to verify ``X-Identity-Txn``.

    Returns
    -------
    str or None
        The verified Algorand sender address if signature and skew checks pass,
        or None if invalid or missing headers. Never raises exceptions.

    See Also
    --------
    verify_identity_txn : Specific verification for the X-Identity-Txn format.
    sign_identity : Client-side helper for legacy identity headers.

    Examples
    --------
    >>> from algosdk import account
    >>> from ai_economy_sdk.identity import sign_identity, verify_identity
    >>> sk, addr = account.generate_account()
    >>> hdrs = sign_identity(sk)
    >>> verify_identity(hdrs) == addr
    True
    >>> verify_identity({}) is None
    True
    """
    if not isinstance(headers, Mapping):
        return None

    headers_lower = {str(k).lower(): str(v) for k, v in headers.items()}

    # Check X-Identity-Txn if present and expected genesis hash is provided
    if "x-identity-txn" in headers_lower and genesis_hash_b64:
        verified_addr = verify_identity_txn(
            headers_lower["x-identity-txn"],
            genesis_hash_b64,
            max_skew=max_skew,
            now=now,
        )
        if verified_addr:
            return verified_addr

    # Fall back to legacy X-Sender-* headers
    addr = headers_lower.get("x-sender-address")
    ts_raw = headers_lower.get("x-sender-timestamp")
    sig = headers_lower.get("x-sender-signature")

    if not addr or not ts_raw or not sig:
        return None

    try:
        ts = int(ts_raw)
    except (ValueError, TypeError):
        return None

    curr_now = int(now if now is not None else time.time())
    if abs(curr_now - ts) > max_skew:
        return None

    if not algosdk.encoding.is_valid_address(addr):
        return None

    message = f"x402-identity:{addr}:{ts}".encode()
    try:
        if not algosdk.util.verify_bytes(message, sig, addr):
            return None
    except Exception:
        return None

    return addr
