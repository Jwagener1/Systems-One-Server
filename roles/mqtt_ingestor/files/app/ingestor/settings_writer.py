"""
Settings file writer.

For topics matching App/settings, write the JSON payload's Value field to the
filesystem under a structured path:

  {base_dir}/{customer}/{location}/{machine_name}/{id_leaf}.json

where id_leaf is the last dot-separated segment of the payload Id field
(e.g. "app.settings.watchdog_settings" -> "watchdog_settings").

Writes are atomic: content is written to {path}.tmp first, then os.replace()
swaps it into place so readers never see a partial file.

File-system errors are logged but do NOT dead-letter the entry -- the entry
is considered processed and removed from the spool in all cases.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .models import SpoolEntry

logger = logging.getLogger(__name__)


def _parse_topic_segments(
    topic: str, prefix_depth: int
) -> tuple[str, str, str] | None:
    """Return (customer, location, machine_name) or None if topic is too short."""
    parts = topic.split("/")
    needed = prefix_depth + 3
    if len(parts) < needed:
        return None
    return parts[prefix_depth], parts[prefix_depth + 1], parts[prefix_depth + 2]


def _is_safe_path_component(value: str) -> bool:
    """Return True if `value` is safe to use as a single filesystem path segment.

    Rejects values that are empty (after stripping), equal to "." or "..",
    or contain a path separator -- any of which could let untrusted MQTT
    topic segments or JSON payload fields escape the intended base
    directory (path traversal).
    """
    stripped = value.strip()
    if not stripped:
        return False
    if stripped in (".", ".."):
        return False
    if "/" in stripped or "\\" in stripped:
        return False
    return True


class SettingsWriter:
    def __init__(self, base_dir: Path, prefix_depth: int = 1) -> None:
        self._base_dir = base_dir
        self._prefix_depth = prefix_depth

    def write_batch(
        self, entries: list[SpoolEntry]
    ) -> tuple[list[int], list[SpoolEntry]]:
        """
        Write each settings entry to the filesystem.

        Returns (committed_ids, failed_entries).
        failed_entries is always empty -- file errors do not dead-letter.
        """
        committed_ids: list[int] = []

        for entry in entries:
            try:
                self._write_entry(entry)
                committed_ids.append(entry.id)
            except Exception as exc:
                logger.error(
                    "SettingsWriter: failed to write entry -- marking as processed",
                    extra={"topic": entry.topic, "id": entry.id, "error": str(exc)},
                )
                # Still commit so the entry leaves the spool.
                committed_ids.append(entry.id)

        return committed_ids, []

    def _write_entry(self, entry: SpoolEntry) -> None:
        segments = _parse_topic_segments(entry.topic, self._prefix_depth)
        if segments is None:
            logger.debug(
                "SettingsWriter: topic too short -- skipping",
                extra={"topic": entry.topic},
            )
            return

        customer, location, machine_name = segments

        for field_name, value in (
            ("customer", customer),
            ("location", location),
            ("machine_name", machine_name),
        ):
            if not _is_safe_path_component(value):
                logger.warning(
                    "SettingsWriter: unsafe path component in topic -- skipping",
                    extra={"topic": entry.topic, "field": field_name, "value": value},
                )
                return

        if not entry.payload_json:
            logger.debug(
                "SettingsWriter: no JSON payload -- skipping",
                extra={"topic": entry.topic},
            )
            return

        try:
            payload = json.loads(entry.payload_json)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "SettingsWriter: invalid JSON payload -- skipping",
                extra={"topic": entry.topic, "error": str(exc)},
            )
            return

        if not isinstance(payload, dict):
            logger.debug(
                "SettingsWriter: payload is not a dict -- skipping",
                extra={"topic": entry.topic},
            )
            return

        raw_id = payload.get("Id", "")
        id_leaf = str(raw_id).rsplit(".", 1)[-1] if raw_id else "settings"
        if not id_leaf:
            id_leaf = "settings"

        if not _is_safe_path_component(id_leaf):
            logger.warning(
                "SettingsWriter: unsafe path component in payload Id -- skipping",
                extra={"topic": entry.topic, "field": "id_leaf", "value": id_leaf},
            )
            return

        value = payload.get("Value")

        out_dir = self._base_dir / customer / location / machine_name
        out_dir.mkdir(parents=True, exist_ok=True)

        out_path = out_dir / f"{id_leaf}.json"
        tmp_path = out_dir / f"{id_leaf}.json.tmp"

        content = json.dumps(value, indent=2, ensure_ascii=False)
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, out_path)

        logger.debug(
            "SettingsWriter: wrote settings file",
            extra={"path": str(out_path), "topic": entry.topic},
        )
