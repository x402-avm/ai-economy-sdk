"""Tests for ReplayStore (in-memory and SQLite modes)."""

import tempfile
from pathlib import Path

from ai_economy_sdk.replay import ReplayStore


def test_memory_replay_store():
    store = ReplayStore(None)
    # reserve twice -> True then False
    assert store.reserve("hash1") is True
    assert store.reserve("hash1") is False

    # commit does not remove reservation
    store.commit("hash1")
    assert store.reserve("hash1") is False

    # release -> reserve True again
    store.release("hash1")
    assert store.reserve("hash1") is True


def test_sqlite_replay_store():
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "replay.sqlite")
        store = ReplayStore(db_path)

        # reserve twice -> True then False
        assert store.reserve("hashA") is True
        assert store.reserve("hashA") is False

        # commit
        store.commit("hashA")
        assert store.reserve("hashA") is False

        # release -> reserve True again
        store.release("hashA")
        assert store.reserve("hashA") is True
        store.close()


def test_sqlite_shared_between_two_stores():
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "shared.sqlite")
        store1 = ReplayStore(db_path)
        store2 = ReplayStore(db_path)

        # store1 reserves hashX
        assert store1.reserve("hashX") is True
        # store2 sees the reservation and cannot reserve
        assert store2.reserve("hashX") is False

        # store1 releases hashX
        store1.release("hashX")
        # store2 can now reserve hashX
        assert store2.reserve("hashX") is True

        store1.close()
        store2.close()
