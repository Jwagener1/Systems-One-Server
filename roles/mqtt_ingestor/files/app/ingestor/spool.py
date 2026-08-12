"""
SQLite-backed durable FIFO spool with fail-open drop-oldest policy.

Design:
  - WAL mode + NORMAL sync for crash-safe, performant writes.
  - Single writer lock so concurrent threads don't corrupt state.
  - Counters persisted in spool_meta so they survive restarts.
  - When spool exceeds SPOOL_MAX_BYTES the oldest rows are deleted first
    to make room (fail-open / drop-oldest), and a CRITICAL log is emitted.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .models import CanonicalEvent, SpoolEntry

if TYPE_CHECKING:
    from .metrics import Metrics

logger = logging.getLogger(__name__)

# Estimated per-row overhead in the SQLite file beyond raw payload bytes.
_ROW_OVERHEAD_BYTES = 256

# Maximum rows fetched in one pass when dropping oldest.
_DROP_SCAN_LIMIT = 500

# Known standard LogRecord attributes (used by JSON formatter elsewhere).
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS spool (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    enqueued_utc         TEXT    NOT NULL,
    topic                TEXT    NOT NULL,
    qos                  INTEGER NOT NULL,
    retain               INTEGER NOT NULL,
    payload_hash         TEXT    NOT NULL,
    payload_text         TEXT,
    payload_json         TEXT,
    source_id            TEXT,
    source_timestamp_utc TEXT,
    payload_bytes_len    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS spool_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO spool_meta(key, value) VALUES ('total_bytes',         '0');
INSERT OR IGNORE INTO spool_meta(key, value) VALUES ('dropped_total',       '0');
INSERT OR IGNORE INTO spool_meta(key, value) VALUES ('dropped_bytes_total', '0');
INSERT OR IGNORE INTO spool_meta(key, value) VALUES ('drop_events_total',   '0');
INSERT OR IGNORE INTO spool_meta(key, value) VALUES ('enqueued_total',      '0');
INSERT OR IGNORE INTO spool_meta(key, value) VALUES ('dequeued_total',      '0');
"""


class Spool:
    def __init__(
        self,
        db_path: Path,
        max_bytes: int,
        full_mode: str,
        drop_log_interval_sec: int,
        metrics: "Metrics | None" = None,
    ) -> None:
        self._path = db_path
        self._max_bytes = max_bytes
        self._full_mode = full_mode
        self._drop_log_interval_sec = drop_log_interval_sec
        self._metrics = metrics
        self._lock = threading.Lock()
        self._last_drop_log: float = 0.0
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-8192")
            self._conn = conn
        return self._conn

    def _init_db(self) -> None:
        with self._lock:
            self._get_conn().executescript(_SCHEMA_DDL)

    def _meta_get(self, conn: sqlite3.Connection, key: str) -> int:
        row = conn.execute(
            "SELECT value FROM spool_meta WHERE key = ?", (key,)
        ).fetchone()
        return int(row[0]) if row else 0

    def _meta_set(self, conn: sqlite3.Connection, key: str, value: int) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO spool_meta(key, value) VALUES (?, ?)",
            (key, str(value)),
        )

    # ── public API ────────────────────────────────────────────────────────────

    def enqueue(self, event: CanonicalEvent) -> None:
        payload_json_str = (
            json.dumps(event.payload_json, ensure_ascii=True)
            if event.payload_json is not None
            else None
        )
        source_ts_str = (
            event.source_timestamp_utc.isoformat()
            if event.source_timestamp_utc
            else None
        )
        enqueued_utc = datetime.now(UTC).isoformat()
        event_bytes = (event.payload_bytes_len or 0) + _ROW_OVERHEAD_BYTES

        with self._lock:
            conn = self._get_conn()
            current_bytes = self._meta_get(conn, "total_bytes")

            if current_bytes + event_bytes > self._max_bytes:
                if self._full_mode == "drop_oldest":
                    # A single _drop_oldest_to_fit pass scans at most
                    # _DROP_SCAN_LIMIT rows and may not free enough space
                    # (many small rows, or an unusually large incoming
                    # event). Retry a bounded number of times so the cap
                    # is actually enforced instead of being silently
                    # exceeded, while never looping unboundedly against a
                    # pathological spool state.
                    max_drop_passes = 10
                    for _ in range(max_drop_passes):
                        dropped = self._drop_oldest_to_fit(conn, event_bytes)
                        current_bytes = self._meta_get(conn, "total_bytes")
                        if current_bytes + event_bytes <= self._max_bytes:
                            break
                        if dropped == 0:
                            # Nothing left to drop -- further passes can't help.
                            break

                    if current_bytes + event_bytes > self._max_bytes:
                        # Drop pass(es) were insufficient -- fall back to
                        # reject_new behavior for this message rather than
                        # silently exceeding the configured cap.
                        util = (
                            int(current_bytes * 100 / self._max_bytes)
                            if self._max_bytes
                            else 0
                        )
                        logger.critical(
                            "Spool full: dropping oldest was insufficient, "
                            "rejecting new message",
                            extra={
                                "event": "spool_full_reject",
                                "spool_bytes": current_bytes,
                                "max_bytes": self._max_bytes,
                                "spool_utilization_pct": util,
                            },
                        )
                        return
                else:
                    # reject_new: log and silently discard the incoming message
                    util = (
                        int(current_bytes * 100 / self._max_bytes)
                        if self._max_bytes
                        else 0
                    )
                    logger.critical(
                        "Spool full: rejecting new message",
                        extra={
                            "event": "spool_full_reject",
                            "spool_bytes": current_bytes,
                            "max_bytes": self._max_bytes,
                            "spool_utilization_pct": util,
                        },
                    )
                    return

            conn.execute(
                """
                INSERT INTO spool (
                    enqueued_utc, topic, qos, retain, payload_hash,
                    payload_text, payload_json, source_id,
                    source_timestamp_utc, payload_bytes_len
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    enqueued_utc,
                    event.topic,
                    event.qos,
                    int(event.retain),
                    event.payload_hash_sha256,
                    event.payload_text,
                    payload_json_str,
                    event.source_id,
                    source_ts_str,
                    event.payload_bytes_len,
                ),
            )
            new_bytes = current_bytes + event_bytes
            self._meta_set(conn, "total_bytes", new_bytes)
            self._meta_set(
                conn, "enqueued_total", self._meta_get(conn, "enqueued_total") + 1
            )
            conn.commit()

            if self._metrics:
                self._metrics.spool_messages_enqueued_total.inc()
                self._metrics.spool_messages_current.inc()
                self._metrics.spool_bytes_current.set(new_bytes)

    def _drop_oldest_to_fit(self, conn: sqlite3.Connection, needed_bytes: int) -> int:
        """
        Delete oldest rows until there is room for needed_bytes, scanning at
        most _DROP_SCAN_LIMIT rows in this pass.
        Called while the spool lock is held.

        Returns the number of rows dropped in this pass (0 means the spool
        had nothing left to drop).
        """
        current_bytes = self._meta_get(conn, "total_bytes")
        rows = conn.execute(
            "SELECT id, payload_bytes_len, enqueued_utc FROM spool ORDER BY id ASC LIMIT ?",
            (_DROP_SCAN_LIMIT,),
        ).fetchall()

        freed_bytes = 0
        drop_ids: list[int] = []
        oldest_utc: str | None = None
        newest_utc: str | None = None

        for row_id, row_bytes, row_utc in rows:
            drop_ids.append(row_id)
            freed_bytes += (row_bytes or 0) + _ROW_OVERHEAD_BYTES
            if oldest_utc is None:
                oldest_utc = row_utc
            newest_utc = row_utc
            if current_bytes - freed_bytes + needed_bytes <= self._max_bytes:
                break

        if not drop_ids:
            return 0

        placeholders = ",".join("?" * len(drop_ids))
        conn.execute(f"DELETE FROM spool WHERE id IN ({placeholders})", drop_ids)

        new_bytes = max(0, current_bytes - freed_bytes)
        self._meta_set(conn, "total_bytes", new_bytes)
        self._meta_set(
            conn,
            "dropped_total",
            self._meta_get(conn, "dropped_total") + len(drop_ids),
        )
        self._meta_set(
            conn,
            "dropped_bytes_total",
            self._meta_get(conn, "dropped_bytes_total") + freed_bytes,
        )
        self._meta_set(
            conn,
            "drop_events_total",
            self._meta_get(conn, "drop_events_total") + 1,
        )

        # Rate-limited critical log
        now = time.monotonic()
        if now - self._last_drop_log >= self._drop_log_interval_sec:
            self._last_drop_log = now
            util = int(new_bytes * 100 / self._max_bytes) if self._max_bytes else 0
            logger.critical(
                "Spool full: dropping oldest messages to make room",
                extra={
                    "event": "spool_drop_oldest",
                    "dropped_count": len(drop_ids),
                    "dropped_bytes": freed_bytes,
                    "oldest_message_received_utc": oldest_utc,
                    "newest_message_received_utc": newest_utc,
                    "spool_utilization_pct": util,
                    "reason": "spool_full",
                },
            )

        if self._metrics:
            self._metrics.spool_messages_dropped_total.inc(len(drop_ids))
            self._metrics.spool_bytes_dropped_total.inc(freed_bytes)
            self._metrics.spool_drop_events_total.inc()
            self._metrics.spool_messages_current.dec(len(drop_ids))
            self._metrics.spool_bytes_current.set(new_bytes)

        return len(drop_ids)

    def dequeue_batch(self, n: int) -> list[SpoolEntry]:
        with self._lock:
            rows = (
                self._get_conn()
                .execute(
                    """
                SELECT id, enqueued_utc, topic, qos, retain, payload_hash,
                       payload_text, payload_json, source_id,
                       source_timestamp_utc, payload_bytes_len
                FROM spool ORDER BY id ASC LIMIT ?
                """,
                    (n,),
                )
                .fetchall()
            )
        return [
            SpoolEntry(
                id=r[0],
                enqueued_utc=r[1],
                topic=r[2],
                qos=r[3],
                retain=bool(r[4]),
                payload_hash_sha256=r[5],
                payload_text=r[6],
                payload_json=r[7],
                source_id=r[8],
                source_timestamp_utc=r[9],
                payload_bytes_len=r[10] or 0,
            )
            for r in rows
        ]

    def commit_batch(self, ids: list[int]) -> None:
        """Remove successfully written entries from the spool."""
        if not ids:
            return
        with self._lock:
            conn = self._get_conn()
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT payload_bytes_len FROM spool WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
            freed = sum((r[0] or 0) + _ROW_OVERHEAD_BYTES for r in rows)

            conn.execute(f"DELETE FROM spool WHERE id IN ({placeholders})", ids)
            new_bytes = max(0, self._meta_get(conn, "total_bytes") - freed)
            self._meta_set(conn, "total_bytes", new_bytes)
            self._meta_set(
                conn,
                "dequeued_total",
                self._meta_get(conn, "dequeued_total") + len(ids),
            )
            conn.commit()

            if self._metrics:
                self._metrics.spool_messages_dequeued_total.inc(len(ids))
                self._metrics.spool_messages_current.dec(len(ids))
                self._metrics.spool_bytes_current.set(new_bytes)

    # ── state queries ─────────────────────────────────────────────────────────

    def current_bytes(self) -> int:
        with self._lock:
            return self._meta_get(self._get_conn(), "total_bytes")

    def current_count(self) -> int:
        with self._lock:
            row = self._get_conn().execute("SELECT COUNT(*) FROM spool").fetchone()
            return int(row[0]) if row else 0

    def utilization_pct(self) -> float:
        if self._max_bytes <= 0:
            return 0.0
        return min(100.0, self.current_bytes() * 100.0 / self._max_bytes)

    def oldest_enqueued_utc(self) -> str | None:
        with self._lock:
            row = (
                self._get_conn()
                .execute("SELECT enqueued_utc FROM spool ORDER BY id ASC LIMIT 1")
                .fetchone()
            )
            return row[0] if row else None

    def get_counters(self) -> dict[str, int]:
        with self._lock:
            conn = self._get_conn()
            return {
                k: self._meta_get(conn, k)
                for k in (
                    "total_bytes",
                    "dropped_total",
                    "dropped_bytes_total",
                    "drop_events_total",
                    "enqueued_total",
                    "dequeued_total",
                )
            }

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
