"""
Canonical internal data models for the ingestor pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class CanonicalEvent:
    """Normalised representation of one received MQTT message."""

    received_utc: datetime
    topic: str
    qos: int
    retain: bool
    payload_hash_sha256: str
    payload_text: str | None
    payload_json: Any  # parsed Python object, or None
    source_id: str | None  # Id field from JSON payload
    source_timestamp_utc: datetime | None  # Timestamp field from JSON payload
    payload_bytes_len: int
    serial_number: (
        str | None
    )  # SerialNumber field from JSON payload (added by MQTT server)


@dataclass
class SpoolEntry:
    """Row as read back from the SQLite spool."""

    id: int
    enqueued_utc: str
    topic: str
    qos: int
    retain: bool
    payload_hash_sha256: str
    payload_text: str | None
    payload_json: str | None  # raw JSON string (already serialised)
    source_id: str | None
    source_timestamp_utc: str | None
    payload_bytes_len: int


@dataclass
class BrokerSnapshot:
    """Aggregated Mosquitto $SYS broker-health snapshot."""

    collected_utc: datetime
    clients_connected: int | None = None
    clients_total: int | None = None
    clients_inactive: int | None = None
    clients_max: int | None = None
    msgs_received: int | None = None
    msgs_sent: int | None = None
    msgs_stored: int | None = None
    bytes_received: int | None = None
    bytes_sent: int | None = None
    subscriptions: int | None = None
    uptime_seconds: int | None = None
    version: str | None = None
    load_msgs_recv_1min: float | None = None
    load_msgs_sent_1min: float | None = None
