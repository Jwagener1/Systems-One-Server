"""
Pipeline orchestrator — wires Spool, DbWriter, MqttIngestor, HealthServer,
and Prometheus metrics together and manages the writer loop.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .db import DbWriter
from .health import HealthServer
from .metrics import Metrics
from .models import BrokerSnapshot, CanonicalEvent, SpoolEntry
from .mqtt_client import MqttIngestor
from .settings_writer import SettingsWriter
from .spool import Spool

logger = logging.getLogger(__name__)

# $SYS topic -> BrokerSnapshot field mapping (from broker-ingestor's pipeline.py)
_BROKER_TOPIC_MAP: dict[str, str] = {
    "$SYS/broker/clients/connected": "clients_connected",
    "$SYS/broker/clients/total": "clients_total",
    "$SYS/broker/clients/inactive": "clients_inactive",
    "$SYS/broker/clients/maximum": "clients_max",
    "$SYS/broker/messages/received": "msgs_received",
    "$SYS/broker/messages/sent": "msgs_sent",
    "$SYS/broker/messages/stored": "msgs_stored",
    "$SYS/broker/bytes/received": "bytes_received",
    "$SYS/broker/bytes/sent": "bytes_sent",
    "$SYS/broker/subscriptions/count": "subscriptions",
    "$SYS/broker/uptime": "uptime_seconds",
    "$SYS/broker/version": "version",
    "$SYS/broker/load/messages/received/1min": "load_msgs_recv_1min",
    "$SYS/broker/load/messages/sent/1min": "load_msgs_sent_1min",
}
_BROKER_INT_FIELDS = {
    "clients_connected", "clients_total", "clients_inactive", "clients_max",
    "msgs_received", "msgs_sent", "msgs_stored", "bytes_received", "bytes_sent",
    "subscriptions", "uptime_seconds",
}
_BROKER_FLOAT_FIELDS = {"load_msgs_recv_1min", "load_msgs_sent_1min"}
_UPTIME_RE = re.compile(r"(\d+)\s*seconds?", re.IGNORECASE)


def is_broker_topic(topic: str) -> bool:
    """True for Mosquitto's own $SYS/# broker-health topic space."""
    return topic.startswith("$SYS/")


def _coerce_broker_field(field: str, raw: str):
    if field in _BROKER_INT_FIELDS:
        if field == "uptime_seconds":
            m = _UPTIME_RE.match(raw)
            if m:
                return int(m.group(1))
        try:
            return int(raw)
        except ValueError:
            try:
                return int(float(raw))
            except ValueError:
                return None
    elif field in _BROKER_FLOAT_FIELDS:
        try:
            return float(raw)
        except ValueError:
            return None
    elif field == "version":
        # broker.broker_stats.version is NVARCHAR(100) -- truncate to avoid a
        # SQL truncation error (which would burn all retries and drop the
        # whole snapshot) on an unexpectedly long broker version string.
        return raw[:100]
    return raw


def _split_entries(
    entries: list[SpoolEntry], prefix_depth: int
) -> tuple[list[SpoolEntry], list[SpoolEntry]]:
    """
    Partition entries into (settings_entries, db_entries).
    Settings entries have type_path starting with 'App/settings'.
    """
    settings: list[SpoolEntry] = []
    db: list[SpoolEntry] = []
    for entry in entries:
        parts = entry.topic.split("/")
        type_path = "/".join(parts[prefix_depth + 3:]) if len(parts) > prefix_depth + 3 else ""
        if type_path.startswith("App/settings"):
            settings.append(entry)
        else:
            db.append(entry)
    return settings, db


class Pipeline:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._stop_event = threading.Event()
        self._last_db_write_utc: datetime | None = None
        self._metrics = Metrics()

        # Spool
        spool_path = cfg.spool.dir / "spool.db"
        cfg.spool.dir.mkdir(parents=True, exist_ok=True)
        self._spool = Spool(
            db_path=spool_path,
            max_bytes=cfg.spool.max_bytes,
            full_mode=cfg.spool.full_mode,
            drop_log_interval_sec=cfg.spool.drop_log_interval_sec,
            metrics=self._metrics,
        )

        # DB writer
        self._db = DbWriter(
            cfg=cfg.db,
            prefix_depth=cfg.mqtt.topic_prefix_depth,
            max_retries=cfg.app.max_retries,
            retry_base_ms=cfg.app.retry_base_ms,
            retry_max_ms=cfg.app.retry_max_ms,
            metrics=self._metrics,
        )

        # Settings writer
        cfg.app.settings_dir.mkdir(parents=True, exist_ok=True)
        self._settings_writer = SettingsWriter(
            base_dir=cfg.app.settings_dir,
            prefix_depth=cfg.mqtt.topic_prefix_depth,
        )

        # MQTT
        self._mqtt = MqttIngestor(
            cfg=cfg.mqtt,
            on_event=self._handle_event,
            metrics=self._metrics,
        )

        self._broker_buffer: dict[str, Any] = {}
        self._broker_buffer_lock = threading.Lock()
        self._broker_last_flush_utc: datetime | None = None
        self._broker_flush_interval_sec = cfg.app.broker_flush_interval_sec

        # Health
        self._health = HealthServer(
            port=cfg.obs.health_port,
            get_status=self._get_health_status,
        )

    # ── event ingestion ───────────────────────────────────────────────────────

    def _handle_event(self, event: CanonicalEvent) -> None:
        if is_broker_topic(event.topic):
            self._handle_broker_event(event)
        else:
            self._spool.enqueue(event)

    def _handle_broker_event(self, event: CanonicalEvent) -> None:
        field = _BROKER_TOPIC_MAP.get(event.topic)
        if field is None or event.payload_text is None:
            return
        value = _coerce_broker_field(field, event.payload_text)
        if value is None:
            return
        with self._broker_buffer_lock:
            self._broker_buffer[field] = value

    # ── writer loop ───────────────────────────────────────────────────────────

    def _writer_loop(self) -> None:
        flush_interval_sec = self._cfg.app.flush_interval_ms / 1000.0
        batch_size = self._cfg.app.batch_size

        logger.info(
            "Writer loop started",
            extra={"flush_interval_sec": flush_interval_sec, "batch_size": batch_size},
        )

        while not self._stop_event.is_set():
            try:
                entries = self._spool.dequeue_batch(batch_size)
                if entries:
                    settings_entries, db_entries = _split_entries(
                        entries, self._cfg.mqtt.topic_prefix_depth
                    )

                    # Route settings entries to the filesystem writer
                    if settings_entries:
                        s_committed, _ = self._settings_writer.write_batch(settings_entries)
                        if s_committed:
                            self._spool.commit_batch(s_committed)

                    # Route DB entries to the SQL writer
                    if db_entries:
                        committed_ids, failed_entries = self._db.write_batch(db_entries)

                        if committed_ids:
                            self._spool.commit_batch(committed_ids)
                            self._last_db_write_utc = datetime.now(UTC)
                            self._db.update_pipeline_state(
                                "last_db_write_utc",
                                self._last_db_write_utc.isoformat(),
                            )

                        if failed_entries and self._cfg.app.deadletter_enabled:
                            self._db.write_deadletter(
                                failed_entries,
                                reason="max_retries_exceeded",
                                retry_count=self._cfg.app.max_retries,
                            )
                            # Remove from spool regardless -- they've been dead-lettered
                            self._spool.commit_batch([e.id for e in failed_entries])

                    self._update_metrics()

            except Exception as exc:
                logger.error(
                    "Writer loop unhandled exception",
                    extra={"error": str(exc)},
                    exc_info=True,
                )

            self._stop_event.wait(timeout=flush_interval_sec)

        logger.info("Writer loop stopping — draining remaining spool entries")
        self._drain_on_shutdown()

    def _drain_on_shutdown(self) -> None:
        """Best-effort final flush before exit."""
        try:
            entries = self._spool.dequeue_batch(self._cfg.app.batch_size * 10)
            if entries:
                settings_entries, db_entries = _split_entries(
                    entries, self._cfg.mqtt.topic_prefix_depth
                )
                if settings_entries:
                    s_committed, _ = self._settings_writer.write_batch(settings_entries)
                    if s_committed:
                        self._spool.commit_batch(s_committed)
                if db_entries:
                    committed_ids, failed_entries = self._db.write_batch(db_entries)
                    if committed_ids:
                        self._spool.commit_batch(committed_ids)
                    if failed_entries and self._cfg.app.deadletter_enabled:
                        self._db.write_deadletter(
                            failed_entries,
                            reason="shutdown_drain_max_retries_exceeded",
                            retry_count=self._cfg.app.max_retries,
                        )
                        self._spool.commit_batch([e.id for e in failed_entries])
        except Exception as exc:
            logger.error("Drain on shutdown failed", extra={"error": str(exc)})

    # ── broker flush loop ─────────────────────────────────────────────────────

    def _broker_flush_loop(self) -> None:
        interval = self._broker_flush_interval_sec
        logger.info("Broker flush loop started", extra={"interval_sec": interval})

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=interval)
            if self._stop_event.is_set():
                break
            self._flush_broker_snapshot()

        logger.info("Broker flush loop stopping — flushing final broker snapshot")
        self._flush_broker_snapshot()

    def _flush_broker_snapshot(self) -> None:
        with self._broker_buffer_lock:
            snapshot_data = dict(self._broker_buffer)

        if not snapshot_data:
            return

        snapshot = BrokerSnapshot(collected_utc=datetime.now(UTC), **snapshot_data)
        if self._db.write_broker_snapshot(snapshot):
            self._broker_last_flush_utc = datetime.now(UTC)
            self._metrics.broker_snapshots_flushed_total.inc()
            # Deliberately NOT cleared: mosquitto publishes many $SYS topics
            # (clients/connected, subscriptions/count, version, ...) only on
            # change, so clearing here leaves those columns NULL in every
            # quiet minute and empties the broker-health dashboard's
            # latest-row stat panels. The buffer holds one value per known
            # $SYS field, so carrying it forward is bounded.

    # ── metrics update ────────────────────────────────────────────────────────

    def _update_metrics(self) -> None:
        try:
            count = self._spool.current_count()
            current_bytes = self._spool.current_bytes()
            util_pct = self._spool.utilization_pct()
            self._metrics.spool_messages_current.set(count)
            self._metrics.spool_bytes_current.set(current_bytes)
            self._metrics.spool_utilization_pct.set(util_pct)

            if self._last_db_write_utc:
                age = (datetime.now(UTC) - self._last_db_write_utc).total_seconds()
                self._metrics.last_successful_db_write_age_seconds.set(age)

            if self._broker_last_flush_utc:
                broker_age = (
                    datetime.now(UTC) - self._broker_last_flush_utc
                ).total_seconds()
                self._metrics.broker_last_flush_age_seconds.set(broker_age)

            oldest_utc = self._spool.oldest_enqueued_utc()
            if oldest_utc:
                try:
                    oldest_dt = datetime.fromisoformat(oldest_utc)
                    backlog_age = (datetime.now(UTC) - oldest_dt).total_seconds()
                    self._metrics.replay_backlog_age_seconds.set(backlog_age)
                except ValueError:
                    pass
        except Exception as exc:
            logger.debug("Metrics update failed", extra={"error": str(exc)})

    # ── health status ─────────────────────────────────────────────────────────

    def _get_health_status(self) -> dict[str, Any]:
        current_bytes = self._spool.current_bytes()
        util_pct = self._spool.utilization_pct()
        count = self._spool.current_count()
        is_connected = self._mqtt.is_connected
        critical_pct = self._cfg.spool.critical_pct
        stale_threshold = self._cfg.obs.health_stale_db_write_sec

        last_write_age: float | None = None
        if self._last_db_write_utc:
            last_write_age = (
                datetime.now(UTC) - self._last_db_write_utc
            ).total_seconds()

        db_stale = last_write_age is None or last_write_age > stale_threshold
        spool_critical = util_pct >= critical_pct
        healthy = is_connected and not spool_critical

        return {
            "healthy": healthy,
            "mqtt_connected": is_connected,
            "spool_utilization_pct": round(util_pct, 1),
            "spool_messages_current": count,
            "spool_bytes_current": current_bytes,
            "last_db_write_age_sec": (
                round(last_write_age, 1) if last_write_age is not None else None
            ),
            "db_stale": db_stale,
            "spool_critical": spool_critical,
            "timestamp_utc": datetime.now(UTC).isoformat(),
        }

    # ── entry point ───────────────────────────────────────────────────────────

    def run(self) -> None:
        def _handle_signal(signum: int, frame: Any) -> None:
            logger.info("Shutdown signal received", extra={"signal": signum})
            self._stop_event.set()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        # Run DB migrations
        migrations_file = (
            Path(__file__).parent.parent / "migrations" / "001_init_schema.sql"
        )
        if migrations_file.exists():
            try:
                self._db.run_migrations(str(migrations_file))
            except Exception as exc:
                logger.warning(
                    "Migration failed — continuing (schema may already exist)",
                    extra={"error": str(exc)},
                )
        else:
            logger.warning(
                "Migration file not found — skipping",
                extra={"path": str(migrations_file)},
            )

        # Start observability
        if self._cfg.obs.metrics_enabled:
            self._metrics.start_server(self._cfg.obs.metrics_port)

        self._health.start()

        # Start MQTT
        self._mqtt.start()

        # Start writer loop in daemon thread
        writer_thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name="writer-loop",
        )
        writer_thread.start()

        broker_flush_thread = threading.Thread(
            target=self._broker_flush_loop,
            daemon=True,
            name="broker-flush-loop",
        )
        broker_flush_thread.start()

        logger.info(
            "Pipeline running",
            extra={
                "health_port": self._cfg.obs.health_port,
                "metrics_port": self._cfg.obs.metrics_port,
                "topic": list(self._cfg.mqtt.topics),
            },
        )

        # Block until stop signal
        self._stop_event.wait()

        logger.info("Pipeline shutting down")
        self._mqtt.stop()
        writer_thread.join(timeout=15)
        broker_flush_thread.join(timeout=10)
        self._db.close()
        self._spool.close()
        self._health.stop()
        logger.info("Pipeline stopped cleanly")
