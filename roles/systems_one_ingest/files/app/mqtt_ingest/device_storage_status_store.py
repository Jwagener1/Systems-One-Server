from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, cast

from .db_client import MSSQLConnector
from .ingest_models import IngestEvent


@dataclass(frozen=True)
class DeviceStorageUpsertResult:
    device_storage_id: int
    created: bool


class DeviceStorageStatusStore:
    """Upserts per-drive storage status into dbo.device_storage_status.

    Expected table columns:
      - id (identity)
      - device_id
      - drive
      - drive_type
      - format
      - total_gb
      - free_gb
      - used_gb
      - usage_percent
      - ts_epoch
      - ts_datetime
      - created_at
      - updated_at

    Behavior:
      - Only processes events with subtype == "storage" and payload.storage.
      - Each drive gets its own row, keyed by (device_id, drive).
      - New packets update existing rows (no history rows).
    """

    def __init__(self, connector: MSSQLConnector) -> None:
        self._logger = logging.getLogger("mqtt_ingest.device_storage")
        self._connector = connector
        self._conn: Any | None = None

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = None

    def ensure_from_event(self, event: IngestEvent, *, device_id: int) -> int:
        if (event.subtype or "") != "storage":
            return 0

        payload = event.payload
        if not isinstance(payload, Mapping):
            return 0

        storage = payload.get("storage")
        if not isinstance(storage, Mapping):
            return 0

        storage_map = cast(Mapping[str, Any], storage)

        ts_epoch, ts_dt = _event_timestamp(event)

        upserts = 0
        for drive, info in storage_map.items():
            drive_name = _normalize_drive(drive)
            if not drive_name:
                continue

            if not isinstance(info, Mapping):
                continue

            info_map = cast(Mapping[str, Any], info)

            total_gb = _safe_float(info_map.get("total_gb"))
            free_gb = _safe_float(info_map.get("free_gb"))
            used_gb = _safe_float(info_map.get("used_gb"))

            # payload uses used_pct; DB expects usage_percent
            usage_percent = _safe_float(info_map.get("used_pct"))

            # Not currently provided in payload; leave NULL.
            drive_type = _safe_str(info_map.get("drive_type"))
            fmt = _safe_str(info_map.get("format"))

            self.upsert_drive(
                device_id=int(device_id),
                drive=drive_name,
                drive_type=drive_type,
                format=fmt,
                total_gb=total_gb,
                free_gb=free_gb,
                used_gb=used_gb,
                usage_percent=usage_percent,
                ts_epoch=ts_epoch,
                ts_datetime=ts_dt,
            )
            upserts += 1

        return upserts

    def upsert_drive(
        self,
        *,
        device_id: int,
        drive: str,
        drive_type: str | None,
        format: str | None,
        total_gb: float | None,
        free_gb: float | None,
        used_gb: float | None,
        usage_percent: float | None,
        ts_epoch: int,
        ts_datetime: datetime,
    ) -> DeviceStorageUpsertResult:
        conn = self._get_conn()

        row = self._select_by_device_and_drive(conn, device_id=device_id, drive=drive)
        if row is None:
            storage_id = self._insert_drive(
                conn,
                device_id=device_id,
                drive=drive,
                drive_type=drive_type,
                format=format,
                total_gb=total_gb,
                free_gb=free_gb,
                used_gb=used_gb,
                usage_percent=usage_percent,
                ts_epoch=ts_epoch,
                ts_datetime=ts_datetime,
            )
            self._logger.info(
                "Storage inserted id=%s device_id=%s drive=%s",
                storage_id,
                device_id,
                drive,
            )
            return DeviceStorageUpsertResult(device_storage_id=storage_id, created=True)

        storage_id = int(row[0])
        self._update_drive(
            conn,
            storage_id=storage_id,
            drive_type=drive_type,
            format=format,
            total_gb=total_gb,
            free_gb=free_gb,
            used_gb=used_gb,
            usage_percent=usage_percent,
            ts_epoch=ts_epoch,
            ts_datetime=ts_datetime,
        )

        self._logger.debug(
            "Storage updated device_id=%s drive=%s",
            device_id,
            drive,
        )

        return DeviceStorageUpsertResult(device_storage_id=storage_id, created=False)

    def _get_conn(self) -> Any:
        if self._conn is None:
            self._conn = self._connector.connect()
            return self._conn

        try:
            self._conn.cursor()
        except Exception:
            self.close()
            self._conn = self._connector.connect()

        return self._conn

    def _select_by_device_and_drive(
        self, conn: Any, *, device_id: int, drive: str
    ) -> Any:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM dbo.device_storage_status WHERE device_id = ? AND drive = ?",
            (int(device_id), drive),
        )
        return cur.fetchone()

    def _insert_drive(
        self,
        conn: Any,
        *,
        device_id: int,
        drive: str,
        drive_type: str | None,
        format: str | None,
        total_gb: float | None,
        free_gb: float | None,
        used_gb: float | None,
        usage_percent: float | None,
        ts_epoch: int,
        ts_datetime: datetime,
    ) -> int:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dbo.device_storage_status (
                device_id,
                drive,
                drive_type,
                format,
                total_gb,
                free_gb,
                used_gb,
                usage_percent,
                ts_epoch,
                ts_datetime,
                created_at,
                updated_at
            )
            OUTPUT Inserted.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME(), SYSUTCDATETIME())
            """,
            (
                int(device_id),
                drive,
                drive_type,
                format,
                total_gb,
                free_gb,
                used_gb,
                usage_percent,
                int(ts_epoch),
                ts_datetime,
            ),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            raise RuntimeError("Insert succeeded but no id returned")
        return int(row[0])

    def _update_drive(
        self,
        conn: Any,
        *,
        storage_id: int,
        drive_type: str | None,
        format: str | None,
        total_gb: float | None,
        free_gb: float | None,
        used_gb: float | None,
        usage_percent: float | None,
        ts_epoch: int,
        ts_datetime: datetime,
    ) -> None:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE dbo.device_storage_status
            SET drive_type = ?,
                format = ?,
                total_gb = ?,
                free_gb = ?,
                used_gb = ?,
                usage_percent = ?,
                ts_epoch = ?,
                ts_datetime = ?,
                updated_at = SYSUTCDATETIME()
            WHERE id = ?
            """,
            (
                drive_type,
                format,
                total_gb,
                free_gb,
                used_gb,
                usage_percent,
                int(ts_epoch),
                ts_datetime,
                int(storage_id),
            ),
        )


def _event_timestamp(event: IngestEvent) -> tuple[int, datetime]:
    if event.ts_ms is not None:
        ts_dt = datetime.fromtimestamp(event.ts_ms / 1000.0, tz=timezone.utc)
        ts_epoch = int(event.ts_ms // 1000)
        return ts_epoch, ts_dt

    now = datetime.now(timezone.utc)
    return int(now.timestamp()), now


def _normalize_drive(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        drive = value.strip()
    else:
        try:
            drive = str(value).strip()
        except Exception:
            return None

    if not drive:
        return None

    # Common payload examples: "C", "C:", "C:\\"
    if len(drive) >= 2 and drive[1] == ":":
        drive = drive[0]

    drive = drive.upper()
    if len(drive) != 1:
        return None

    if not ("A" <= drive <= "Z"):
        return None

    return drive


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except Exception:
        return None


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return None
