"""
MQTT client wrapper with:
  - paho-mqtt v1 and v2 callback API compatibility
  - Exponential backoff + jitter reconnect (guarded against duplicate threads)
  - Deterministic re-subscribe on every successful connect
  - Source-timestamp and source-id extraction from JSON payloads
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import ssl
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Callable

import paho.mqtt.client as mqtt

from .config import MqttConfig
from .models import CanonicalEvent

if TYPE_CHECKING:
    from .metrics import Metrics

logger = logging.getLogger(__name__)

_RECONNECT_BASE_SEC = 1.0
_RECONNECT_MAX_SEC = 60.0
_RECONNECT_JITTER_PCT = 0.2


class MqttIngestor:
    def __init__(
        self,
        cfg: MqttConfig,
        on_event: Callable[[CanonicalEvent], None],
        metrics: "Metrics | None" = None,
    ) -> None:
        self._cfg = cfg
        self._on_event = on_event
        self._metrics = metrics
        self._connected = False
        self._reconnect_attempt = 0
        self._reconnecting = False
        self._state_lock = threading.Lock()
        self._client = self._build_client()

    # ── client construction ───────────────────────────────────────────────────

    def _build_client(self) -> mqtt.Client:
        transport = (
            "websockets" if self._cfg.protocol.lower().startswith("ws") else "tcp"
        )

        callback_api = getattr(mqtt, "CallbackAPIVersion", None)
        if callback_api is not None:
            client: mqtt.Client = mqtt.Client(
                callback_api_version=callback_api.VERSION2,
                client_id=self._cfg.client_id,
                transport=transport,
                clean_session=self._cfg.clean_session,
            )
        else:
            client = mqtt.Client(
                client_id=self._cfg.client_id,
                transport=transport,
                clean_session=self._cfg.clean_session,
            )

        if self._cfg.username:
            client.username_pw_set(self._cfg.username, self._cfg.password)

        if transport == "websockets":
            ws_path = self._cfg.basepath or "/"
            if not ws_path.startswith("/"):
                ws_path = f"/{ws_path}"
            client.ws_set_options(path=ws_path)

        if self._cfg.use_tls:
            cert_reqs = ssl.CERT_REQUIRED if self._cfg.validate_cert else ssl.CERT_NONE
            client.tls_set(cert_reqs=cert_reqs)
            if not self._cfg.validate_cert:
                client.tls_insecure_set(True)

        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        return client

    # ── paho callbacks ────────────────────────────────────────────────────────

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        if reason_code != 0:
            logger.error(
                "MQTT connect rejected by broker",
                extra={"reason_code": str(reason_code)},
            )
            return

        with self._state_lock:
            self._connected = True
            self._reconnect_attempt = 0
            self._reconnecting = False

        if self._metrics:
            self._metrics.mqtt_connected.set(1)

        logger.info(
            "MQTT connected — subscribing",
            extra={"topics": list(self._cfg.topics)},
        )
        for topic in self._cfg.topics:
            client.subscribe(topic, qos=1)

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        payload_bytes = bytes(msg.payload)
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()

        try:
            payload_text: str | None = payload_bytes.decode("utf-8")
        except UnicodeDecodeError:
            payload_text = payload_bytes.decode("utf-8", errors="replace")

        payload_json: Any = None
        source_id: str | None = None
        source_timestamp_utc: datetime | None = None
        serial_number: str | None = None

        if payload_text:
            try:
                parsed = json.loads(payload_text)
                payload_json = parsed
                if isinstance(parsed, dict):
                    source_id = parsed.get("Id")
                    # Support both casing variants used across firmware generations
                    serial_number = parsed.get("SerialNumber") or parsed.get("serial_number")
                    raw_ts = parsed.get("Timestamp")
                    if isinstance(raw_ts, (int, float)):
                        # Legacy numeric (ms or s epoch)
                        try:
                            ts_sec = raw_ts / 1000.0 if raw_ts > 1e10 else raw_ts
                            source_timestamp_utc = datetime.fromtimestamp(ts_sec, tz=UTC)
                        except (ValueError, OSError, OverflowError):
                            pass
                    elif isinstance(raw_ts, str):
                        # New-generation devices send an ISO-8601 string
                        try:
                            source_timestamp_utc = datetime.fromisoformat(raw_ts).astimezone(UTC)
                        except (ValueError, TypeError):
                            pass
                    # Final fallback: TimestampUnix integer (seconds epoch)
                    if source_timestamp_utc is None:
                        raw_ts_unix = parsed.get("TimestampUnix")
                        if isinstance(raw_ts_unix, (int, float)):
                            try:
                                source_timestamp_utc = datetime.fromtimestamp(raw_ts_unix, tz=UTC)
                            except (ValueError, OSError, OverflowError):
                                pass
            except (json.JSONDecodeError, ValueError):
                pass

        event = CanonicalEvent(
            received_utc=datetime.now(UTC),
            topic=msg.topic,
            qos=msg.qos,
            retain=bool(msg.retain),
            payload_hash_sha256=payload_hash,
            payload_text=payload_text,
            payload_json=payload_json,
            source_id=source_id,
            source_timestamp_utc=source_timestamp_utc,
            payload_bytes_len=len(payload_bytes),
            serial_number=serial_number,
        )
        self._on_event(event)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        with self._state_lock:
            self._connected = False

        if self._metrics:
            self._metrics.mqtt_connected.set(0)

        logger.warning(
            "MQTT disconnected",
            extra={"reason_code": str(reason_code)},
        )

        # Schedule reconnect only for unexpected disconnects.
        if reason_code != 0:
            self._maybe_schedule_reconnect()

    # ── reconnect logic ───────────────────────────────────────────────────────

    def _maybe_schedule_reconnect(self) -> None:
        with self._state_lock:
            if self._reconnecting:
                return
            self._reconnecting = True
            self._reconnect_attempt += 1
            attempt = self._reconnect_attempt

        threading.Thread(
            target=self._reconnect_worker,
            args=(attempt,),
            daemon=True,
            name=f"mqtt-reconnect-{attempt}",
        ).start()

    def _reconnect_worker(self, attempt: int) -> None:
        delay = min(_RECONNECT_BASE_SEC * (2 ** (attempt - 1)), _RECONNECT_MAX_SEC)
        jitter = delay * _RECONNECT_JITTER_PCT * (2 * random.random() - 1)
        wait = max(1.0, delay + jitter)

        logger.info(
            "MQTT scheduling reconnect",
            extra={"attempt": attempt, "delay_sec": round(wait, 1)},
        )
        time.sleep(wait)

        try:
            self._client.reconnect()
            logger.info("MQTT reconnect initiated", extra={"attempt": attempt})
        except Exception as exc:
            logger.error(
                "MQTT reconnect failed",
                extra={"attempt": attempt, "error": str(exc)},
            )
            with self._state_lock:
                self._reconnecting = False
            # Try again
            self._maybe_schedule_reconnect()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        logger.info(
            "MQTT ingestor connecting",
            extra={
                "host": self._cfg.host,
                "port": self._cfg.port,
                "topic": list(self._cfg.topics),
                "client_id": self._cfg.client_id,
            },
        )
        self._client.connect(
            self._cfg.host, self._cfg.port, keepalive=self._cfg.keepalive_sec
        )
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
        with self._state_lock:
            self._connected = False
        if self._metrics:
            self._metrics.mqtt_connected.set(0)
        logger.info("MQTT ingestor stopped")

    @property
    def is_connected(self) -> bool:
        with self._state_lock:
            return self._connected
