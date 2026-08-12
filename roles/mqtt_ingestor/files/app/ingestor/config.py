"""
Configuration loading from environment variables.
All runtime secrets come exclusively from the environment — nothing hardcoded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


# ── helpers ───────────────────────────────────────────────────────────────────


def _env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(f"Required environment variable {key!r} is not set")
    return val or ""


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"Environment variable {key!r} must be an integer, got: {raw!r}"
        ) from exc


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ── config dataclasses ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MqttConfig:
    connection_name: str
    protocol: str
    host: str
    port: int
    basepath: str
    username: str
    password: str
    use_tls: bool
    validate_cert: bool
    url: str
    topics: tuple[str, ...]
    client_id: str
    keepalive_sec: int
    clean_session: bool
    topic_prefix_depth: (
        int  # number of leading topic segments before customer/location/machine
    )


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    name: str
    user: str
    password: str
    driver: str
    trust_server_certificate: bool
    connect_timeout_sec: int
    command_timeout_sec: int


@dataclass(frozen=True)
class SpoolConfig:
    dir: Path
    max_bytes: int
    critical_pct: int
    full_mode: str  # drop_oldest | reject_new
    drop_log_interval_sec: int


@dataclass(frozen=True)
class AppConfig:
    batch_size: int
    flush_interval_ms: int
    max_retries: int
    retry_base_ms: int
    retry_max_ms: int
    deadletter_enabled: bool
    dedupe_mode: str  # off | hash_topic_time
    settings_dir: Path
    broker_flush_interval_sec: int


@dataclass(frozen=True)
class ObsConfig:
    log_level: str
    log_format: str
    health_stale_db_write_sec: int
    metrics_enabled: bool
    metrics_port: int
    health_port: int


@dataclass(frozen=True)
class Config:
    mqtt: MqttConfig
    db: DbConfig
    spool: SpoolConfig
    app: AppConfig
    obs: ObsConfig


# ── loader ────────────────────────────────────────────────────────────────────


def load_config() -> Config:
    import uuid

    mqtt_host = _env("MQTT_HOST", "")
    mqtt_port = _env_int("MQTT_PORT", 1883)
    mqtt_protocol = _env("MQTT_PROTOCOL", "tcp")
    mqtt_basepath = _env("MQTT_BASEPATH", "")
    mqtt_url = _env("MQTT_URL", "")

    if mqtt_url:
        parsed = urlparse(mqtt_url)
        if parsed.scheme:
            mqtt_protocol = parsed.scheme
        if parsed.hostname:
            mqtt_host = parsed.hostname
        if parsed.port:
            mqtt_port = parsed.port
        if parsed.path and parsed.path != "/":
            mqtt_basepath = parsed.path

    if not mqtt_host:
        raise RuntimeError("MQTT_HOST is required (set directly or via MQTT_URL)")

    client_id = _env("MQTT_CLIENT_ID") or f"mqtt-ingestor-{uuid.uuid4().hex[:8]}"

    mqtt = MqttConfig(
        connection_name=_env("MQTT_CONNECTION_NAME", "mqtt-ingestor"),
        protocol=mqtt_protocol,
        host=mqtt_host,
        port=mqtt_port,
        basepath=mqtt_basepath,
        username=_env("MQTT_USERNAME", ""),
        password=_env("MQTT_PASSWORD", ""),
        use_tls=_env_bool("MQTT_USE_TLS", False),
        validate_cert=_env_bool("MQTT_VALIDATE_CERT", True),
        url=mqtt_url,
        topics=tuple(
            t.strip() for t in _env("MQTT_TOPICS", required=True).split(",") if t.strip()
        ),
        client_id=client_id,
        keepalive_sec=_env_int("MQTT_KEEPALIVE_SEC", 60),
        clean_session=_env_bool("MQTT_CLEAN_SESSION", False),
        topic_prefix_depth=_env_int("MQTT_TOPIC_PREFIX_DEPTH", 1),
    )

    db = DbConfig(
        host=_env("DB_HOST", required=True),
        port=_env_int("DB_PORT", 1433),
        name=_env("DB_NAME", required=True),
        user=_env("DB_USER", required=True),
        password=_env("DB_PASSWORD", required=True),
        driver=_env("DB_DRIVER", "ODBC Driver 18 for SQL Server"),
        trust_server_certificate=_env_bool("DB_TRUST_SERVER_CERTIFICATE", False),
        connect_timeout_sec=_env_int("DB_CONNECT_TIMEOUT_SEC", 5),
        command_timeout_sec=_env_int("DB_COMMAND_TIMEOUT_SEC", 30),
    )

    spool = SpoolConfig(
        dir=Path(_env("SPOOL_DIR", "./spool")),
        max_bytes=_env_int("SPOOL_MAX_BYTES", 1_073_741_824),
        critical_pct=_env_int("SPOOL_CRITICAL_PCT", 95),
        full_mode=_env("SPOOL_FULL_MODE", "drop_oldest"),
        drop_log_interval_sec=_env_int("SPOOL_DROP_LOG_INTERVAL_SEC", 30),
    )

    app = AppConfig(
        batch_size=_env_int("APP_BATCH_SIZE", 100),
        flush_interval_ms=_env_int("APP_FLUSH_INTERVAL_MS", 500),
        max_retries=_env_int("APP_MAX_RETRIES", 8),
        retry_base_ms=_env_int("APP_RETRY_BASE_MS", 250),
        retry_max_ms=_env_int("APP_RETRY_MAX_MS", 30_000),
        deadletter_enabled=_env_bool("APP_DEADLETTER_ENABLED", True),
        dedupe_mode=_env("APP_DEDUPE_MODE", "off"),
        settings_dir=Path(_env("SETTINGS_DIR", "./settings")),
        broker_flush_interval_sec=_env_int("BROKER_FLUSH_INTERVAL_SEC", 60),
    )

    obs = ObsConfig(
        log_level=_env("LOG_LEVEL", "INFO"),
        log_format=_env("LOG_FORMAT", "json"),
        health_stale_db_write_sec=_env_int("HEALTH_STALE_DB_WRITE_SEC", 120),
        metrics_enabled=_env_bool("METRICS_ENABLED", True),
        metrics_port=_env_int("METRICS_PORT", 9108),
        health_port=_env_int("HEALTH_PORT", 8080),
    )

    return Config(mqtt=mqtt, db=db, spool=spool, app=app, obs=obs)
