from __future__ import annotations

import logging
import os
import ssl
import threading
from dataclasses import dataclass
from typing import Optional
from typing import Any, cast

import paho.mqtt.client as mqtt

from .ingest_models import build_event
from .device_store import DeviceStore
from .device_status_store import DeviceStatusStore
from .device_os_status_store import DeviceOsStatusStore
from .device_storage_status_store import DeviceStorageStatusStore
from .device_statistics_store import DeviceStatisticsStore


@dataclass(frozen=True)
class MQTTConnectionParams:
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    keepalive: int = 60
    client_id: str = "mqtt_ingest"
    clean_session: bool = True

    # "tcp" or "websockets"
    transport: str = "tcp"

    # Used only when transport="websockets". If empty/None, paho defaults to "/mqtt".
    ws_path: Optional[str] = None

    # TLS is needed for wss:// or mqtts:// style connections.
    tls: bool = False
    tls_insecure: bool = False

    # Ingest subscription configuration
    ingest_topics: tuple[str, ...] = ()
    ingest_qos: int = 0


class MQTTConnector:
    def __init__(
        self,
        params: MQTTConnectionParams,
        *,
        device_store: DeviceStore | None = None,
        device_status_store: DeviceStatusStore | None = None,
        device_os_status_store: DeviceOsStatusStore | None = None,
        device_storage_status_store: DeviceStorageStatusStore | None = None,
        device_statistics_store: DeviceStatisticsStore | None = None,
    ) -> None:
        self._logger = logging.getLogger("mqtt_ingest.mqtt")
        self._params = params
        self._device_store = device_store
        self._device_status_store = device_status_store
        self._device_os_status_store = device_os_status_store
        self._device_storage_status_store = device_storage_status_store
        self._device_statistics_store = device_statistics_store

        transport = (params.transport or "tcp").strip().lower()
        if transport in {"ws", "websocket", "websockets"}:
            transport = "websockets"
        else:
            transport = "tcp"

        self._client = mqtt.Client(
            client_id=params.client_id,
            clean_session=params.clean_session,
            transport=transport,
        )

        # Avoid tight reconnect loops when the broker drops the connection.
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

        if transport == "websockets" and params.ws_path:
            self._client.ws_set_options(path=params.ws_path)

        if params.tls:
            context: ssl.SSLContext = ssl.create_default_context()
            cast(Any, self._client).tls_set_context(context)
            self._client.tls_insecure_set(bool(params.tls_insecure))

        if params.username:
            self._client.username_pw_set(
                username=params.username, password=params.password
            )

        self._connected_event = threading.Event()
        self._connect_rc: Optional[int] = None

        # Desired subscriptions are kept for the lifetime of the connector so that
        # we can re-subscribe on reconnect (required to keep receiving messages,
        # including retained ones, after a disconnect).
        self._subscriptions_lock = threading.Lock()
        self._desired_subscriptions: dict[str, int] = {}

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe

    def connect(self, *, timeout_seconds: float = 10.0) -> None:
        self._connected_event.clear()
        self._connect_rc = None

        self._logger.info(
            "Connecting to MQTT broker %s:%s (transport=%s)",
            self._params.host,
            self._params.port,
            (self._params.transport or "tcp"),
        )

        self._client.connect_async(
            self._params.host, self._params.port, self._params.keepalive
        )
        self._client.loop_start()

        if not self._connected_event.wait(timeout=timeout_seconds):
            self.disconnect()
            raise TimeoutError("Timed out waiting for MQTT CONNACK")

        if self._connect_rc is None:
            self.disconnect()
            raise ConnectionError("MQTT connection status unknown")

        if self._connect_rc != 0:
            # paho return codes: 0=success, non-zero=refused
            rc = self._connect_rc
            self.disconnect()
            raise ConnectionError(f"MQTT connection failed (rc={rc})")

        self._logger.info("MQTT connected")

    def subscribe(
        self, topics: str | list[str] | tuple[str, ...], *, qos: int = 0
    ) -> None:
        """Subscribe to one or more topics.

        If called before the broker connection is established, subscriptions are queued
        and automatically applied on successful connect.
        """

        if isinstance(topics, str):
            normalized_topics = [topics]
        else:
            normalized_topics = list(topics)

        normalized_topics = [t.strip() for t in normalized_topics if t and t.strip()]
        if not normalized_topics:
            return

        qos_int = int(qos)
        qos_int = 0 if qos_int < 0 else 2 if qos_int > 2 else qos_int

        with self._subscriptions_lock:
            for topic in normalized_topics:
                # last one wins for qos
                self._desired_subscriptions[topic] = qos_int

        # If we're already connected, subscribe immediately.
        if self._connected_event.is_set() and (self._connect_rc == 0):
            self._subscribe_desired_subscriptions()

    def _subscribe_desired_subscriptions(self) -> None:
        with self._subscriptions_lock:
            items = list(self._desired_subscriptions.items())

        if not items:
            return

        for topic, qos in items:
            result, _mid = self._client.subscribe(topic, qos=qos)
            if result != mqtt.MQTT_ERR_SUCCESS:
                self._logger.warning(
                    "Failed to subscribe to topic '%s' (result=%s)", topic, result
                )
            else:
                self._logger.info("Subscribe sent for '%s' (qos=%s)", topic, qos)

    def disconnect(self) -> None:
        try:
            self._client.disconnect()
        except Exception:
            pass
        try:
            self._client.loop_stop()
        except Exception:
            pass

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: dict[str, object],
        rc: int,
        properties: object | None = None,
    ) -> None:
        session_present = bool(
            flags.get("session present") or flags.get("session_present")
        )
        self._logger.info(
            "MQTT on_connect rc=%s session_present=%s", int(rc), session_present
        )
        self._connect_rc = int(rc)
        self._connected_event.set()

        if int(rc) == 0:
            # Always re-subscribe on connect/reconnect.
            self._subscribe_desired_subscriptions()

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: object,
        rc: int,
        properties: object | None = None,
    ) -> None:
        # Track current connection state so callers can reason about it.
        self._connected_event.clear()
        if rc != 0:
            self._logger.warning("MQTT disconnected unexpectedly (rc=%s)", rc)
        else:
            self._logger.info("MQTT disconnected")

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: object,
        msg: mqtt.MQTTMessage,
    ) -> None:
        payload_bytes = msg.payload or b""
        event = build_event(
            topic=str(msg.topic),
            payload_bytes=payload_bytes,
            qos=getattr(msg, "qos", None),
            retain=getattr(msg, "retain", None),
        )

        if event is None:
            # Topic doesn't match the expected format.
            self._logger.info(
                "[?] topic=%s retain=%s qos=%s bytes=%s",
                msg.topic,
                getattr(msg, "retain", None),
                getattr(msg, "qos", None),
                len(payload_bytes),
            )
            return

        if self._device_store is not None:
            try:
                device_result = self._device_store.ensure_from_event(event)
            except Exception:
                # Don't kill the MQTT loop on transient DB issues.
                self._logger.exception("Device upsert failed")
                device_result = None

            if (
                device_result is not None
                and self._device_status_store is not None
                and (event.subtype == "status")
            ):
                try:
                    self._device_status_store.ensure_from_event(
                        event, device_id=device_result.device_id
                    )
                except Exception:
                    self._logger.exception("Device status upsert failed")

            if (
                device_result is not None
                and self._device_os_status_store is not None
                and (event.subtype == "status")
            ):
                try:
                    self._device_os_status_store.ensure_from_event(
                        event, device_id=device_result.device_id
                    )
                except Exception:
                    self._logger.exception("Device OS status upsert failed")

            if (
                device_result is not None
                and self._device_storage_status_store is not None
                and (event.subtype == "storage")
            ):
                try:
                    self._device_storage_status_store.ensure_from_event(
                        event, device_id=device_result.device_id
                    )
                except Exception:
                    self._logger.exception("Device storage upsert failed")

            if (
                device_result is not None
                and self._device_statistics_store is not None
                and (event.subtype == "statistics")
            ):
                try:
                    self._device_statistics_store.insert_from_event(
                        event, device_id=device_result.device_id
                    )
                except Exception:
                    self._logger.exception("Device statistics insert failed")

        self._logger.info(event.to_log_line())

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: object,
        mid: int,
        granted_qos: tuple[int, ...] | list[int],
        properties: object | None = None,
    ) -> None:
        self._logger.info("MQTT SUBACK mid=%s granted_qos=%s", mid, list(granted_qos))


def params_from_env() -> MQTTConnectionParams:
    def _get(name: str, default: str = "") -> str:
        return os.getenv(name, default).strip()

    host = _get("MQTT_HOST", "localhost")
    port_raw = _get("MQTT_PORT", "1883")
    try:
        port = int(port_raw)
    except ValueError:
        port = 1883

    ws_path = _get("MQTT_WS_PATH", "")
    ws_path = ws_path if ws_path else None

    keepalive_raw = _get("MQTT_KEEPALIVE", "60")
    try:
        keepalive = int(keepalive_raw)
    except ValueError:
        keepalive = 60

    clean_session = _get("MQTT_CLEAN_SESSION", "true").lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    tls = _get("MQTT_TLS", "false").lower() in {"1", "true", "yes", "y", "on"}
    tls_insecure = _get("MQTT_TLS_INSECURE", "false").lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }

    ingest_topics_raw = _get("INGEST_TOPICS", "")
    ingest_topics_raw = ingest_topics_raw.replace(";", ",")
    ingest_topics = tuple(
        t.strip() for t in ingest_topics_raw.split(",") if t and t.strip()
    )

    ingest_qos_raw = _get("INGEST_QOS", "0")
    try:
        ingest_qos = int(ingest_qos_raw)
    except ValueError:
        ingest_qos = 0
    ingest_qos = 0 if ingest_qos < 0 else 2 if ingest_qos > 2 else ingest_qos

    return MQTTConnectionParams(
        host=host,
        port=port,
        username=_get("MQTT_USERNAME") or None,
        password=_get("MQTT_PASSWORD") or None,
        keepalive=keepalive,
        client_id=_get("MQTT_CLIENT_ID", "mqtt_ingest") or "mqtt_ingest",
        clean_session=clean_session,
        transport=_get("MQTT_TRANSPORT", "tcp") or "tcp",
        ws_path=ws_path,
        tls=tls,
        tls_insecure=tls_insecure,
        ingest_topics=ingest_topics,
        ingest_qos=ingest_qos,
    )
