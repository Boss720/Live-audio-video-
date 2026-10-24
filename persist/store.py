"""SQLite persistence layer for run state, turns, tool calls, usage, and resumption.

Uses only the stdlib ``sqlite3`` module; no ORM. JSON values are serialized with
``orjson`` when available, otherwise ``json``.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import platformdirs

try:
    import orjson
except ImportError:  # pragma: no cover - orjson is a required dependency
    orjson = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
)


class _JsonSerializer:
    """Unified JSON serializer backed by ``orjson`` or ``json``."""

    def __init__(self, module: ModuleType | None) -> None:
        self._module = module

    def dumps(self, obj: Any) -> str:
        if self._module is not None:
            payload: bytes = self._module.dumps(obj)
            return payload.decode("utf-8")
        return json.dumps(obj, default=str, separators=(",", ":"))

    def loads(self, text: str) -> Any:
        if self._module is not None:
            return self._module.loads(text)
        return json.loads(text)


class Store:
    """Persistent store for agent runs.

    Parameters
    ----------
    db_path:
        Path to the SQLite database. Defaults to the user data directory for
        ``GeminiLiveAgent``.
    """

    _json = _JsonSerializer(orjson)

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = (
            db_path
            if db_path is not None
            else Path(platformdirs.user_data_dir("GeminiLiveAgent", appauthor=False)) / "state.db"
        )
        # Ensure the parent directory exists atomically before opening the DB.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        self._create_tables()
        # Single writer thread owns its own connection so commits (fsync) never
        # run on the caller's (asyncio event-loop) thread.
        self._write_conn = self._connect()
        self._queue: queue.Queue[tuple[str, Any, bool] | None] = queue.Queue()
        self._closed = False
        self._writer = threading.Thread(
            target=self._writer_loop, name="store-writer", daemon=True
        )
        self._writer.start()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        for pragma in _PRAGMAS:
            conn.execute(pragma).fetchall()
        return conn

    def _writer_loop(self) -> None:
        conn = self._write_conn
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                sql, params, many = item
                try:
                    with conn:
                        if many:
                            conn.executemany(sql, params)
                        else:
                            conn.execute(sql, params)
                except Exception:
                    logger.exception("store write failed")
            finally:
                self._queue.task_done()

    def _enqueue(self, sql: str, params: Any, many: bool = False) -> None:
        """Queue a write for the background writer (fire-and-forget)."""
        if self._closed:
            logger.warning("store write after close dropped")
            return
        self._queue.put((sql, params, many))

    def flush(self) -> None:
        """Block until every queued write has been committed."""
        if not self._closed:
            self._queue.join()

    def _create_tables(self) -> None:
        schema = """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                model TEXT,
                mode TEXT,
                workspace_root TEXT,
                ended_at TEXT
            );

            CREATE TABLE IF NOT EXISTS turns (
                id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT,
                completed_at TEXT,
                epoch INTEGER NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );

            CREATE TABLE IF NOT EXISTS tool_calls (
                id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                turn_id TEXT,
                name TEXT NOT NULL,
                args TEXT,
                result TEXT,
                ok INTEGER NOT NULL,
                orphaned INTEGER NOT NULL DEFAULT 0,
                epoch INTEGER NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );

            CREATE TABLE IF NOT EXISTS usage (
                run_id TEXT NOT NULL,
                modality TEXT NOT NULL,
                tokens INTEGER NOT NULL,
                usd REAL NOT NULL,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );

            CREATE TABLE IF NOT EXISTS resumption_handles (
                handle TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(id)
            );
        """
        self._conn.executescript(schema)
        self._conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_turns_run_completed ON turns(run_id, completed_at);
            CREATE INDEX IF NOT EXISTS idx_usage_run_recorded ON usage(run_id, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_resumption_run_created
                ON resumption_handles(run_id, created_at);
            """
        )
        self._ensure_column("tool_calls", "completed_at", "TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_calls_run_completed "
            "ON tool_calls(run_id, completed_at)"
        )
        self._conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        """Add a column to a table if it does not already exist."""
        columns = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        """Drain queued writes, stop the writer thread, and close connections."""
        if self._closed:
            return
        self._queue.join()
        self._closed = True
        self._queue.put(None)
        self._writer.join(timeout=10)
        self._write_conn.close()
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(UTC).isoformat()

    def start_run(
        self,
        run_id: str,
        started_at: str,
        model: str,
        mode: str,
        workspace_root: str,
    ) -> None:
        """Insert a new run record (idempotent for a repeated run_id)."""
        self._enqueue(
            """
            INSERT OR IGNORE INTO runs (id, started_at, model, mode, workspace_root)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, started_at, model, mode, workspace_root),
        )

    def end_run(self, run_id: str, ended_at: str | None = None) -> None:
        """Mark a run as ended."""
        when = ended_at if ended_at is not None else self._utc_now_iso()
        self._enqueue(
            "UPDATE runs SET ended_at = ? WHERE id = ?",
            (when, run_id),
        )

    def append_turn(
        self,
        turn_id: str,
        run_id: str,
        role: str,
        text: str,
        completed_at: str,
        epoch: int,
    ) -> None:
        """Append a completed turn to the transcript."""
        self._enqueue(
            """
            INSERT INTO turns (id, run_id, role, text, completed_at, epoch)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (turn_id, run_id, role, text, completed_at, epoch),
        )

    def record_tool_call(
        self,
        call_id: str,
        run_id: str,
        turn_id: str | None,
        name: str,
        args: dict[str, Any],
        result: Any,
        ok: bool,
        orphaned: bool,
        epoch: int,
    ) -> None:
        """Persist a tool call and its result."""
        from policy.redaction import redact_data

        clean_args = redact_data(args)
        clean_result = redact_data(result) if result is not None else None
        completed_at = self._utc_now_iso() if clean_result is not None else None
        result_json = self._json.dumps(clean_result) if clean_result is not None else None
        self._enqueue(
            """
            INSERT INTO tool_calls
            (id, run_id, turn_id, name, args, result, ok, orphaned, epoch, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id,
                run_id,
                turn_id,
                name,
                self._json.dumps(clean_args),
                result_json,
                int(ok),
                int(orphaned),
                epoch,
                completed_at,
            ),
        )

    def record_usage(
        self,
        run_id: str,
        modality: str,
        tokens: int,
        usd: float,
        recorded_at: str,
    ) -> None:
        """Record token usage for a modality."""
        self.record_usage_batch(run_id, [(modality, tokens, usd)], recorded_at)

    def record_usage_batch(
        self,
        run_id: str,
        rows: list[tuple[str, int, float]],
        recorded_at: str,
    ) -> None:
        """Record several modality usage rows atomically."""
        if not rows:
            return
        self._enqueue(
            """
            INSERT INTO usage (run_id, modality, tokens, usd, recorded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(run_id, modality, tokens, usd, recorded_at) for modality, tokens, usd in rows],
            many=True,
        )

    def get_usage(self, run_id: str) -> list[tuple[str, int, float, str]]:
        """Return usage rows for a run ordered by recording time."""
        self.flush()
        rows = self._conn.execute(
            """
            SELECT modality, tokens, usd, recorded_at
            FROM usage
            WHERE run_id = ?
            ORDER BY recorded_at ASC
            """,
            (run_id,),
        ).fetchall()
        return [
            (row["modality"], row["tokens"], row["usd"], row["recorded_at"])
            for row in rows
        ]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return the run record as a dict, or ``None`` if it does not exist."""
        self.flush()
        row = self._conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def save_resumption_handle(
        self,
        handle: str,
        run_id: str,
        created_at: str,
        expires_at: str | None,
    ) -> None:
        """Store a resumption handle for later session recovery."""
        self._enqueue(
            """
            INSERT INTO resumption_handles (handle, run_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (handle, run_id, created_at, expires_at),
        )

    def get_latest_resumption_handle(self, run_id: str) -> tuple[str, str, str | None] | None:
        """Return the most recent resumption handle for a run.

        Returns a tuple of ``(handle, created_at, expires_at)`` or ``None`` if
        no handle exists.
        """
        self.flush()
        row = self._conn.execute(
            """
            SELECT handle, created_at, expires_at
            FROM resumption_handles
            WHERE run_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return (row["handle"], row["created_at"], row["expires_at"])

    def delete_expired_handles(self, now: datetime | None = None) -> None:
        """Delete resumption handles whose ``expires_at`` has passed."""
        when = now.isoformat() if now is not None else self._utc_now_iso()
        self._enqueue(
            "DELETE FROM resumption_handles WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (when,),
        )

    def get_active_resumption_handle(self, run_id: str, now: datetime | None = None) -> str | None:
        """Return the most recent unexpired resumption handle for a run."""
        self.delete_expired_handles(now)
        when = now.isoformat() if now is not None else self._utc_now_iso()
        self.flush()
        row = self._conn.execute(
            """
            SELECT handle
            FROM resumption_handles
            WHERE run_id = ? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id, when),
        ).fetchone()
        if row is None:
            return None
        return str(row["handle"])

    def delete_resumption_handle(self, handle: str) -> None:
        """Delete a specific resumption handle."""
        self._enqueue(
            "DELETE FROM resumption_handles WHERE handle = ?",
            (handle,),
        )

    def get_turns(self, run_id: str, limit: int = 100) -> list[tuple[str, str, int]]:
        """Return the latest bounded turns in chronological/insertion order."""
        self.flush()
        rows = self._conn.execute(
            """
            SELECT role, text, epoch
            FROM (
                SELECT role, text, epoch, completed_at, rowid
                FROM turns
                WHERE run_id = ?
                ORDER BY completed_at DESC, rowid DESC
                LIMIT ?
            )
            ORDER BY completed_at ASC, rowid ASC
            """,
            (run_id, limit),
        ).fetchall()
        return [(row["role"], row["text"], row["epoch"]) for row in rows]

    def get_recent_tool_results(
        self, run_id: str, limit: int = 100
    ) -> list[tuple[str, str, bool, str, int]]:
        """Return completed tool results for a run ordered by completion time."""
        self.flush()
        rows = self._conn.execute(
            """
            SELECT id, name, ok, result, epoch
            FROM (
                SELECT id, name, ok, result, epoch, completed_at, rowid
                FROM tool_calls
                WHERE run_id = ? AND result IS NOT NULL
                ORDER BY completed_at DESC, rowid DESC
                LIMIT ?
            )
            ORDER BY completed_at ASC, rowid ASC
            """,
            (run_id, limit),
        ).fetchall()
        return [
            (row["id"], row["name"], bool(row["ok"]), row["result"], row["epoch"])
            for row in rows
        ]
