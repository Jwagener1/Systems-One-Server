"""
Entry point for the MQTT ingestor service.
Loads .env, sets up structured logging, then starts the pipeline.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


# ── .env loader (no external deps) ───────────────────────────────────────────


def _load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Remove optional surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ.setdefault(key, value)


# ── structured logging ────────────────────────────────────────────────────────

_STANDARD_LOG_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
    }
)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json

        record.message = record.getMessage()
        data: dict = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.message,
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        for key, val in record.__dict__.items():
            if key not in _STANDARD_LOG_ATTRS and not key.startswith("_"):
                data[key] = val
        return json.dumps(data, default=str)


def _setup_logging(level: str, fmt: str) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if root.handlers:
        root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    if fmt.lower() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
        )
    root.addHandler(handler)


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    _load_env_file(".env")

    from ingestor.config import load_config

    cfg = load_config()
    _setup_logging(cfg.obs.log_level, cfg.obs.log_format)

    logger = logging.getLogger(__name__)
    logger.info(
        "MQTT ingestor starting",
        extra={
            "mqtt_host": cfg.mqtt.host,
            "mqtt_topic": cfg.mqtt.topic_filter,
            "db_host": cfg.db.host,
            "db_name": cfg.db.name,
        },
    )

    from ingestor.pipeline import Pipeline

    Pipeline(cfg).run()


if __name__ == "__main__":
    main()
