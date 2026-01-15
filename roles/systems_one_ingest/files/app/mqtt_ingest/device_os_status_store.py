from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .db_client import MSSQLConnector
from .ingest_models import IngestEvent


@dataclass(frozen=True)
class DeviceOsStatusUpsertResult:
    device_os_status_id: int
    created: bool


class DeviceOsStatusStore:
    """Upserts current device OS version into dbo.device_os_status.

    Expected table columns:
      - id (identity)
      - device_id (FK -> dbo.devices.id)
      - os_version
      - ts_epoch (epoch seconds)
      - ts_datetime (UTC datetime)
      - created_at
      - updated_at

    Behavior:
      - Only processes events with subtype == "status" and payload.device_os_version.
      - One row per device_id (assumed). If missing, inserts.
      - Updates updated_at on every status packet that includes device_os_version.
    """

    def __init__(self, connector: MSSQLConnector) -> None:
        self._logger = logging.getLogger("mqtt_ingest.device_os_status")
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

    def ensure_from_event(
        self, event: IngestEvent, *, device_id: int
    ) -> Optional[DeviceOsStatusUpsertResult]:
        if (event.subtype or "") != "status":
            return None

        payload = event.payload
        if not isinstance(payload, Mapping):
            return None

        os_raw = payload.get("device_os_version")
        os_version = _safe_str(os_raw)
        if not os_version:
            return None

        os_version = os_version.strip()
        if not os_version:
            return None

        ts_epoch, ts_dt = _event_timestamp(event)

        return self.upsert_os_version(
            device_id=int(device_id),
            os_version=os_version,
            ts_epoch=ts_epoch,
            ts_datetime=ts_dt,
        )

    def upsert_os_version(
        self,
        *,
        device_id: int,
        os_version: str,
        ts_epoch: int,
        ts_datetime: datetime,
    ) -> DeviceOsStatusUpsertResult:
        conn = self._get_conn()

        row = self._select_by_device_id(conn, device_id)
        if row is None:
            os_status_id = self._insert_os_status(
                conn,
                device_id=device_id,
                os_version=os_version,
                ts_epoch=ts_epoch,
                ts_datetime=ts_datetime,
            )
            self._logger.info(
                "Device OS inserted id=%s device_id=%s",
                os_status_id,
                device_id,
            )
            return DeviceOsStatusUpsertResult(
                device_os_status_id=os_status_id, created=True
            )

        os_status_id = int(row[0])
        prev_os = _safe_str(row[1])

        self._update_os_status(
            conn,
            os_status_id=os_status_id,
            os_version=os_version,
            ts_epoch=ts_epoch,
            ts_datetime=ts_datetime,
        )

        if (prev_os or "") != os_version:
            self._logger.info(
                "Device OS changed device_id=%s",
                device_id,
            )
        else:
            self._logger.debug(
                "Device OS updated device_id=%s",
                device_id,
            )

        return DeviceOsStatusUpsertResult(
            device_os_status_id=os_status_id, created=False
        )

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

    def _select_by_device_id(self, conn: Any, device_id: int) -> Any:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, os_version FROM dbo.device_os_status WHERE device_id = ?",
            (int(device_id),),
        )
        return cur.fetchone()

    def _insert_os_status(
        self,
        conn: Any,
        *,
        device_id: int,
        os_version: str,
        ts_epoch: int,
        ts_datetime: datetime,
    ) -> int:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dbo.device_os_status (
                device_id,
                os_version,
                ts_epoch,
                ts_datetime,
                created_at,
                updated_at
            )
            OUTPUT Inserted.id
            VALUES (?, ?, ?, ?, SYSUTCDATETIME(), SYSUTCDATETIME())
            """,
            (int(device_id), os_version, int(ts_epoch), ts_datetime),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            raise RuntimeError("Insert succeeded but no id returned")
        return int(row[0])

    def _update_os_status(
        self,
        conn: Any,
        *,
        os_status_id: int,
        os_version: str,
        ts_epoch: int,
        ts_datetime: datetime,
    ) -> None:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE dbo.device_os_status
            SET os_version = ?,
                ts_epoch = ?,
                ts_datetime = ?,
                updated_at = SYSUTCDATETIME()
            WHERE id = ?
            """,
            (os_version, int(ts_epoch), ts_datetime, int(os_status_id)),
        )


def _event_timestamp(event: IngestEvent) -> tuple[int, datetime]:
    if event.ts_ms is not None:
        ts_dt = datetime.fromtimestamp(event.ts_ms / 1000.0, tz=timezone.utc)
        ts_epoch = int(event.ts_ms // 1000)
        return ts_epoch, ts_dt

    now = datetime.now(timezone.utc)
    return int(now.timestamp()), now


def _safe_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return None
