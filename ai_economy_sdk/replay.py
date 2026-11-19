"""Shared replay protection store using in-memory dict or SQLite with WAL mode.

Provides state tracking for payment transaction hashes through the three phases of
settlement: ``reserved`` (during verification and execution), ``settled`` (committed
on chain), or released (on failure or tool error).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("ai_economy_sdk")


class ReplayStore:
    """Store for tracking payment transaction hashes to prevent replay attacks.

    Supports an in-memory dictionary (when path is None) or a shared SQLite database
    configured with WAL mode and thread locks across write operations.

    Parameters
    ----------
    path : str or None, default: None
        Path to SQLite database file. If None, an in-memory dictionary is used.

    See Also
    --------
    PaymentGate : Middleware utilizing ReplayStore for request deduping.

    Examples
    --------
    >>> from ai_economy_sdk.replay import ReplayStore
    >>> store = ReplayStore()
    >>> store.reserve("txhash_abc123")
    True
    >>> store.reserve("txhash_abc123")
    False
    >>> store.commit("txhash_abc123")
    >>> store.release("txhash_abc123")
    >>> store.reserve("txhash_abc123")
    True
    >>> store.close()
    """

    def __init__(self, path: str | None = None) -> None:
        """Initialize the replay store in memory or with an SQLite database file.

        Parameters
        ----------
        path : str or None, default: None
            Path to SQLite database file. If None, uses an in-memory dictionary.
        """
        self.path = path
        self._lock = threading.Lock()
        if path is not None:
            p = Path(path).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(p), check_same_thread=False, timeout=30.0)
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL;")
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS seen (hash TEXT PRIMARY KEY, state TEXT, ts REAL)"
                )
                self._conn.commit()
        else:
            self._mem: dict[str, tuple[str, float]] = {}

    def reserve(self, h: str) -> bool:
        """Atomically reserve a payment hash before tool execution.

        Parameters
        ----------
        h : str
            Hex-encoded hash (typically SHA-256 of payment payload).

        Returns
        -------
        bool
            True if reservation succeeded (hash was not previously seen).
            False if hash is already reserved or committed (replay detected).

        Notes
        -----
        Reservation protects against concurrent requests attempting to use
        the same payment before on-chain settlement occurs.

        Examples
        --------
        >>> from ai_economy_sdk.replay import ReplayStore
        >>> s = ReplayStore()
        >>> s.reserve("test_hash_1")
        True
        >>> s.reserve("test_hash_1")
        False
        """
        with self._lock:
            if self.path is not None:
                try:
                    self._conn.execute(
                        "INSERT INTO seen (hash, state, ts) VALUES (?, ?, ?)",
                        (h, "reserved", time.time()),
                    )
                    self._conn.commit()
                    return True
                except sqlite3.IntegrityError:
                    self._conn.rollback()
                    return False
            else:
                if h in self._mem:
                    return False
                self._mem[h] = ("reserved", time.time())
                return True

    def commit(self, h: str) -> None:
        """Mark a reserved hash as settled after successful on-chain transaction.

        Parameters
        ----------
        h : str
            Hex-encoded payment transaction hash to mark as settled.

        Returns
        -------
        None

        Notes
        -----
        Once committed, future attempts to reserve this hash will fail, permanently
        preventing replay of this payment transaction.

        Examples
        --------
        >>> from ai_economy_sdk.replay import ReplayStore
        >>> s = ReplayStore()
        >>> s.reserve("h2")
        True
        >>> s.commit("h2")
        """
        with self._lock:
            if self.path is not None:
                self._conn.execute(
                    "UPDATE seen SET state = ?, ts = ? WHERE hash = ?",
                    ("settled", time.time(), h),
                )
                self._conn.commit()
            else:
                if h in self._mem:
                    self._mem[h] = ("settled", time.time())

    def release(self, h: str) -> None:
        """Delete a reservation or settled entry so it can be reused or retried.

        Parameters
        ----------
        h : str
            Hex-encoded payment transaction hash to release.

        Returns
        -------
        None

        Notes
        -----
        Called when a tool execution fails or an unhandled exception occurs prior
        to settlement, ensuring the client is not falsely locked out.

        Examples
        --------
        >>> from ai_economy_sdk.replay import ReplayStore
        >>> s = ReplayStore()
        >>> s.reserve("h3")
        True
        >>> s.release("h3")
        >>> s.reserve("h3")
        True
        """
        with self._lock:
            if self.path is not None:
                self._conn.execute("DELETE FROM seen WHERE hash = ?", (h,))
                self._conn.commit()
            else:
                self._mem.pop(h, None)

    def close(self) -> None:
        """Close the underlying SQLite database connection if one was opened.

        Returns
        -------
        None

        Examples
        --------
        >>> from ai_economy_sdk.replay import ReplayStore
        >>> s = ReplayStore()
        >>> s.close()
        """
        with self._lock:
            if self.path is not None and hasattr(self, "_conn"):
                try:
                    self._conn.close()
                except sqlite3.Error:
                    logger.warning("closing replay store %s failed", self.path, exc_info=True)
