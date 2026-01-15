from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, cast

from .db_client import MSSQLConnector
from .ingest_models import IngestEvent


@dataclass(frozen=True)
class DeviceStatisticsInsertResult:
    device_statistics_id: int


class DeviceStatisticsStore:
    """Inserts per-message device statistics into dbo.device_statistics.

    Expected table columns:
      - id (identity)
      - device_id
      - ts_epoch (epoch seconds)
      - ts_datetime (UTC datetime)
      - total_items
      - no_read
      - good_read
      - no_dimension
      - no_weight
      - data_sent
      - not_sent
      - image_sent
      - image_not_sent
      - item_out_of_spec
      - more_than_1_item
      - created_at

    Behavior:
      - Only processes events with subtype == "statistics" and payload.statistics.
      - Every new message inserts a new row (no upsert).
    """

    def __init__(self, connector: MSSQLConnector) -> None:
        self._logger = logging.getLogger("mqtt_ingest.device_statistics")
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

    def insert_from_event(
        self, event: IngestEvent, *, device_id: int
    ) -> Optional[DeviceStatisticsInsertResult]:
        if (event.subtype or "") != "statistics":
            return None

        payload = event.payload
        if not isinstance(payload, Mapping):
            return None

        stats = payload.get("statistics")
        if not isinstance(stats, Mapping):
            return None

        stats_map = cast(Mapping[str, Any], stats)

        ts_epoch, ts_dt = _event_timestamp(event)

        # Map payload keys -> DB columns
        total_items = _safe_int_or_zero(stats_map.get("total_items"))
        no_read = _safe_int_or_zero(stats_map.get("no_reads"))
        good_read = _safe_int_or_zero(stats_map.get("good_reads"))
        no_dimension = _safe_int_or_zero(stats_map.get("no_dimensions"))
        no_weight = _safe_int_or_zero(stats_map.get("no_weight"))

        data_sent = _safe_int_or_zero(stats_map.get("sent"))
        not_sent = _safe_int_or_zero(stats_map.get("not_sent"))

        item_out_of_spec = _safe_int_or_zero(stats_map.get("out_of_spec"))
        more_than_1_item = _safe_int_or_zero(stats_map.get("more_than_one_item"))

        # Some DB schemas declare these as NOT NULL; default to 0.
        image_sent = _safe_int_or_zero(stats_map.get("image_sent"))
        image_not_sent = _safe_int_or_zero(stats_map.get("image_not_sent"))

        stats_id = self._insert_statistics(
            device_id=int(device_id),
            ts_epoch=ts_epoch,
            ts_datetime=ts_dt,
            total_items=total_items,
            no_read=no_read,
            good_read=good_read,
            no_dimension=no_dimension,
            no_weight=no_weight,
            data_sent=data_sent,
            not_sent=not_sent,
            image_sent=image_sent,
            image_not_sent=image_not_sent,
            item_out_of_spec=item_out_of_spec,
            more_than_1_item=more_than_1_item,
        )

        self._logger.info(
            "Statistics inserted id=%s device_id=%s ts=%s",
            stats_id,
            device_id,
            ts_epoch,
        )

        return DeviceStatisticsInsertResult(device_statistics_id=stats_id)

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

    def _insert_statistics(
        self,
        *,
        device_id: int,
        ts_epoch: int,
        ts_datetime: datetime,
        total_items: int | None,
        no_read: int | None,
        good_read: int | None,
        no_dimension: int | None,
        no_weight: int | None,
        data_sent: int | None,
        not_sent: int | None,
        image_sent: int | None,
        image_not_sent: int | None,
        item_out_of_spec: int | None,
        more_than_1_item: int | None,
    ) -> int:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dbo.device_statistics (
                device_id,
                ts_epoch,
                ts_datetime,
                total_items,
                no_read,
                good_read,
                no_dimension,
                no_weight,
                data_sent,
                not_sent,
                image_sent,
                image_not_sent,
                item_out_of_spec,
                more_than_1_item,
                created_at
            )
            OUTPUT Inserted.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME())
            """,
            (
                int(device_id),
                int(ts_epoch),
                ts_datetime,
                total_items,
                no_read,
                good_read,
                no_dimension,
                no_weight,
                data_sent,
                not_sent,
                image_sent,
                image_not_sent,
                item_out_of_spec,
                more_than_1_item,
            ),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            raise RuntimeError("Insert succeeded but no id returned")
        return int(row[0])


def _event_timestamp(event: IngestEvent) -> tuple[int, datetime]:
    if event.ts_ms is not None:
        ts_dt = datetime.fromtimestamp(event.ts_ms / 1000.0, tz=timezone.utc)
        ts_epoch = int(event.ts_ms // 1000)
        return ts_epoch, ts_dt

    now = datetime.now(timezone.utc)
    return int(now.timestamp()), now


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except Exception:
        return None


def _safe_int_or_zero(value: Any) -> int:
    parsed = _safe_int(value)
    return 0 if parsed is None else parsed
