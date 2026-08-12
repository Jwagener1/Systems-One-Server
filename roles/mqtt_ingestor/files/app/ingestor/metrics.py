"""
Prometheus metrics definitions for the ingestor service.
All counters are monotonic; gauges reflect current state.
"""

from __future__ import annotations

import logging

from prometheus_client import Counter, Gauge, start_http_server

logger = logging.getLogger(__name__)


class Metrics:
    def __init__(self) -> None:
        # ── Counters (monotonic) ──────────────────────────────────────────────
        self.spool_messages_dropped_total = Counter(
            "mqtt_ingestor_spool_messages_dropped_total",
            "Total messages dropped from spool (fail-open / drop-oldest policy)",
        )
        self.spool_bytes_dropped_total = Counter(
            "mqtt_ingestor_spool_bytes_dropped_total",
            "Total payload bytes dropped from spool",
        )
        self.spool_drop_events_total = Counter(
            "mqtt_ingestor_spool_drop_events_total",
            "Total number of spool drop events (each may drop multiple messages)",
        )
        self.spool_messages_enqueued_total = Counter(
            "mqtt_ingestor_spool_messages_enqueued_total",
            "Total messages enqueued to spool",
        )
        self.spool_messages_dequeued_total = Counter(
            "mqtt_ingestor_spool_messages_dequeued_total",
            "Total messages dequeued from spool after successful DB write",
        )
        self.db_write_success_total = Counter(
            "mqtt_ingestor_db_write_success_total",
            "Total successful DB batch write operations",
        )
        self.db_write_failure_total = Counter(
            "mqtt_ingestor_db_write_failure_total",
            "Total failed DB write attempts (individual retries)",
        )
        self.db_deadletter_total = Counter(
            "mqtt_ingestor_db_deadletter_total",
            "Total messages moved to dead-letter table",
        )
        self.broker_snapshots_flushed_total = Counter(
            "mqtt_ingestor_broker_snapshots_flushed_total",
            "Total broker-health snapshots successfully written to broker.broker_stats",
        )

        # ── Gauges (current state) ─────────────────────────────────────────────
        self.spool_messages_current = Gauge(
            "mqtt_ingestor_spool_messages_current",
            "Current number of messages pending in spool",
        )
        self.spool_bytes_current = Gauge(
            "mqtt_ingestor_spool_bytes_current",
            "Current estimated bytes in spool",
        )
        self.spool_utilization_pct = Gauge(
            "mqtt_ingestor_spool_utilization_pct",
            "Spool utilisation as a percentage of SPOOL_MAX_BYTES",
        )
        self.replay_backlog_age_seconds = Gauge(
            "mqtt_ingestor_replay_backlog_age_seconds",
            "Age in seconds of the oldest unwritten message in the spool",
        )
        self.last_successful_db_write_age_seconds = Gauge(
            "mqtt_ingestor_last_successful_db_write_age_seconds",
            "Seconds elapsed since the last successful DB write",
        )
        self.mqtt_connected = Gauge(
            "mqtt_ingestor_mqtt_connected",
            "1 if the MQTT client is currently connected, 0 otherwise",
        )
        self.broker_last_flush_age_seconds = Gauge(
            "mqtt_ingestor_broker_last_flush_age_seconds",
            "Seconds since the last successful broker-stats flush",
        )

    def start_server(self, port: int) -> None:
        start_http_server(port)
        logger.info("Metrics HTTP server started", extra={"port": port})
