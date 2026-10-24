"""Wave 2: Store uses WAL, indexes, and an off-thread writer."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from persist.store import Store


def _index_names(store: Store) -> set[str]:
    rows = store._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    return {r[0] for r in rows}


def test_pragmas_applied(tmp_path: Path) -> None:
    with Store(tmp_path / "s.db") as store:
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store._write_conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert store._write_conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_indexes_exist(tmp_path: Path) -> None:
    with Store(tmp_path / "s.db") as store:
        names = _index_names(store)
    assert {
        "idx_turns_run_completed",
        "idx_tool_calls_run_completed",
        "idx_usage_run_recorded",
    } <= names


def test_indexes_added_to_existing_db(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE turns (id TEXT NOT NULL, run_id TEXT NOT NULL, role TEXT NOT NULL, "
        "text TEXT, completed_at TEXT, epoch INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()
    with Store(path) as store:
        names = _index_names(store)
    assert "idx_turns_run_completed" in names


def test_write_then_read_sees_data(tmp_path: Path) -> None:
    with Store(tmp_path / "s.db") as store:
        store.start_run("r1", "2026-01-01T00:00:00", "m", "mode", "/w")
        store.append_turn("t1", "r1", "user", "hi", "2026-01-01T00:00:01", 1)
        store.record_usage_batch("r1", [("audio", 5, 0.1)], "2026-01-01T00:00:02")
        assert store.get_run("r1") is not None
        assert store.get_turns("r1") == [("user", "hi", 1)]
        assert store.get_usage("r1")[0][:3] == ("audio", 5, 0.1)


def test_writes_run_on_writer_thread(tmp_path: Path) -> None:
    seen: list[str] = []
    with Store(tmp_path / "s.db") as store:
        real = store._write_conn
        gate = threading.Event()

        class Spy:
            def __enter__(self) -> Spy:
                seen.append(threading.current_thread().name)
                gate.wait(5)
                real.__enter__()
                return self

            def __exit__(self, *a: object) -> None:
                real.__exit__(*a)

            def execute(self, *a: object) -> object:
                return real.execute(*a)  # type: ignore[arg-type]

        store._write_conn = Spy()  # type: ignore[assignment]
        # Writer loop captured conn at start; restart-free check via queue instead.
        store._queue.put(None)
        store._writer.join(2)
        store._writer = threading.Thread(
            target=store._writer_loop, name="store-writer", daemon=True
        )
        store._writer.start()
        store.start_run("r1", "t", "m", "mode", "/w")  # must return while writer is gated
        assert store._queue.unfinished_tasks >= 1
        gate.set()
        store.flush()
        store._write_conn = real
    assert seen == ["store-writer"]
    assert threading.current_thread().name != "store-writer"


def test_close_drains_queue(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    store = Store(path)
    for i in range(50):
        store.append_turn(f"t{i}", "r1", "user", str(i), f"2026-01-01T00:00:{i:02d}", 1)
    store.close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 50
    conn.close()
    store.close()  # idempotent
