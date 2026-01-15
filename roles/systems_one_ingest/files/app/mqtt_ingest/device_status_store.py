from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .db_client import MSSQLConnector
from .ingest_models import IngestEvent


@dataclass(frozen=True)
class DeviceStatusUpsertResult:
    device_status_id: int
    created: bool


class DeviceStatusStore:
    """Upserts current device status into dbo.device_status.

    Expected table columns:
      - id (identity)
      - device_id (FK -> dbo.devices.id)
      - status (e.g. "online"/"offline")
      - ts_epoch (epoch seconds)
      - ts_datetime (UTC datetime)
      - offline_since (UTC datetime nullable)
      - created_at
      - updated_at

    Behavior:
      - Only processes events with subtype == "status" and payload.device_status.
      - One row per device_id (assumed). If missing, inserts.
      - Updates updated_at on every status packet.
      - Sets offline_since:
          - when going offline: set if not already set
          - when online: clear
    """

    def __init__(self, connector: MSSQLConnector) -> None:
        self._logger = logging.getLogger("mqtt_ingest.device_status")
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
    ) -> Optional[DeviceStatusUpsertResult]:
        if (event.subtype or "") != "status":
            return None

        payload = event.payload
        if not isinstance(payload, Mapping):
            return None

        status_raw = payload.get("device_status")
        status = _normalize_status(status_raw)
        if status is None:
            return None

        ts_epoch, ts_dt = _event_timestamp(event)

        return self.upsert_status(
            device_id=int(device_id),
            status=status,
            ts_epoch=ts_epoch,
            ts_datetime=ts_dt,
        )

    def upsert_status(
        self,
        *,
        device_id: int,
        status: str,
        ts_epoch: int,
        ts_datetime: datetime,
    ) -> DeviceStatusUpsertResult:
        conn = self._get_conn()

        row = self._select_by_device_id(conn, device_id)
        if row is None:
            offline_since = ts_datetime if status == "offline" else None
            status_id = self._insert_status(
                conn,
                device_id=device_id,
                status=status,
                ts_epoch=ts_epoch,
                ts_datetime=ts_datetime,
                offline_since=offline_since,
            )
            self._logger.info(
                "Device status inserted id=%s device_id=%s status=%s",
                status_id,
                device_id,
                status,
            )
            return DeviceStatusUpsertResult(device_status_id=status_id, created=True)

        status_id = int(row[0])
        prev_status = _safe_str(row[1])
        prev_offline_since = row[2]

        if status == "offline":
            offline_since = (
                prev_offline_since if prev_offline_since is not None else ts_datetime
            )
        else:
            offline_since = None

        self._update_status(
            conn,
            status_id=status_id,
            status=status,
            ts_epoch=ts_epoch,
            ts_datetime=ts_datetime,
            offline_since=offline_since,
        )

        if (prev_status or "").lower() != status:
            self._logger.info(
                "Device status changed device_id=%s %s -> %s",
                device_id,
                prev_status or "?",
                status,
            )
        else:
            self._logger.debug(
                "Device status updated device_id=%s status=%s",
                device_id,
                status,
            )

        return DeviceStatusUpsertResult(device_status_id=status_id, created=False)

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
            "SELECT id, status, offline_since FROM dbo.device_status WHERE device_id = ?",
            (int(device_id),),
        )
        return cur.fetchone()

    def _insert_status(
        self,
        conn: Any,
        *,
        device_id: int,
        status: str,
        ts_epoch: int,
        ts_datetime: datetime,
        offline_since: datetime | None,
    ) -> int:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dbo.device_status (
                device_id,
                status,
                ts_epoch,
                ts_datetime,
                offline_since,
                created_at,
                updated_at
            )
            OUTPUT Inserted.id
            VALUES (?, ?, ?, ?, ?, SYSUTCDATETIME(), SYSUTCDATETIME())
            """,
            (int(device_id), status, int(ts_epoch), ts_datetime, offline_since),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            raise RuntimeError("Insert succeeded but no id returned")
        return int(row[0])

    def _update_status(
        self,
        conn: Any,
        *,
        status_id: int,
        status: str,
        ts_epoch: int,
        ts_datetime: datetime,
        offline_since: datetime | None,
    ) -> None:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE dbo.device_status
            SET status = ?,
                ts_epoch = ?,
                ts_datetime = ?,
                offline_since = ?,
                updated_at = SYSUTCDATETIME()
            WHERE id = ?
            """,
            (status, int(ts_epoch), ts_datetime, offline_since, int(status_id)),
        )


def _normalize_status(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip().lower()
    else:
        try:
            v = str(value).strip().lower()
        except Exception:
            return None

    if v in {"online", "offline"}:
        return v
    return None


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
