"""Tests for atomic batched usage persistence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from persist.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(db_path=tmp_path / "usage.db")


def test_record_usage_batch_inserts_all_modalities_in_one_transaction(store: Store) -> None:
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)
    rows = [(name, count, cost) for name, count, cost in (
        ("audio_in", 11, 0.1),
        ("audio_out", 12, 0.2),
        ("text_in", 13, 0.3),
        ("text_out", 14, 0.4),
        ("video_in", 15, 0.5),
    )]

    store.record_usage_batch("run", rows, "stamp")

    records = store._conn.execute(
        "SELECT modality, tokens, usd, recorded_at FROM usage ORDER BY rowid"
    ).fetchall()
    assert [tuple(record) for record in records] == [
        (modality, tokens, usd, "stamp") for modality, tokens, usd in rows
    ]
    txn = [statement.strip().upper() for statement in statements]
    begins = [statement for statement in txn if statement in {"BEGIN", "BEGIN TRANSACTION"}]
    assert begins == ["BEGIN"]
    assert [statement for statement in txn if statement == "COMMIT"] == ["COMMIT"]


def test_record_usage_batch_empty_does_not_open_transaction(store: Store) -> None:
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)

    store.record_usage_batch("run", [], "stamp")

    assert not any(statement.strip().upper().startswith("BEGIN") for statement in statements)
    assert not any(statement.strip().upper() == "COMMIT" for statement in statements)
    assert store._conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0


def test_record_usage_batch_rolls_back_all_rows_if_any_row_is_invalid(store: Store) -> None:
    rows = [("audio_in", 11, 0.1), ("audio_out", 12, 0.2), ("broken", 13, 0.3)]
    invalid_rows = [rows[0], (None, 12, 0.2), rows[2]]  # type: ignore[list-item]

    with pytest.raises(sqlite3.IntegrityError):
        store.record_usage_batch("run", invalid_rows, "stamp")

    assert store._conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0


def test_record_usage_keeps_single_row_behavior(store: Store) -> None:
    store.record_usage("run", "text_in", 7, 0.125, "stamp")

    assert store.get_usage("run") == [("text_in", 7, 0.125, "stamp")]
