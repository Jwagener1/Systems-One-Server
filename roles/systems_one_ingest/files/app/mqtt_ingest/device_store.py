from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from .db_client import MSSQLConnector
from .ingest_models import IngestEvent


@dataclass(frozen=True)
class DeviceUpsertResult:
    device_id: int
    created: bool


class DeviceStore:
    """Upserts devices into dbo.devices.

    Table shape (expected):
      - id (identity)
      - serial_number
      - customer
      - location
      - machine_name
      - created_at
      - updated_at

    Behavior:
      - If serial_number is missing, no-op.
      - If serial_number not found, insert row.
      - If found, update customer/location/machine_name and updated_at.
    """

    def __init__(self, connector: MSSQLConnector) -> None:
        self._logger = logging.getLogger("mqtt_ingest.devices")
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

    def ensure_from_event(self, event: IngestEvent) -> Optional[DeviceUpsertResult]:
        serial = (event.serial_number or "").strip()
        if not serial:
            self._logger.debug(
                "No serial_number in payload; skipping device upsert (topic=%s)",
                f"{event.prefix}/{event.customer}/{event.location}/{event.machine}/{event.subtype}",
            )
            return None

        customer = (event.customer or "").strip()
        location = (event.location or "").strip()
        machine_name = (event.machine or "").strip()

        return self.upsert_device(
            serial_number=serial,
            customer=customer,
            location=location,
            machine_name=machine_name,
        )

    def upsert_device(
        self,
        *,
        serial_number: str,
        customer: str,
        location: str,
        machine_name: str,
    ) -> DeviceUpsertResult:
        conn = self._get_conn()

        # 1) Look up by serial
        row = self._select_by_serial(conn, serial_number)
        if row is None:
            # 2) Insert if missing
            try:
                device_id = self._insert_device(
                    conn,
                    serial_number=serial_number,
                    customer=customer,
                    location=location,
                    machine_name=machine_name,
                )
                self._logger.info(
                    "Device inserted id=%s serial=%s customer=%s location=%s machine=%s",
                    device_id,
                    serial_number,
                    customer,
                    location,
                    machine_name,
                )
                return DeviceUpsertResult(device_id=device_id, created=True)
            except Exception:
                # If another process inserted concurrently (or unique constraint), re-select.
                self._logger.exception(
                    "Device insert failed; retrying select/update (serial=%s)",
                    serial_number,
                )

            row = self._select_by_serial(conn, serial_number)
            if row is None:
                raise

        device_id = int(row[0])

        # 3) Update to reflect latest metadata and bump updated_at.
        self._update_device(
            conn,
            device_id=device_id,
            customer=customer,
            location=location,
            machine_name=machine_name,
        )

        self._logger.debug(
            "Device updated id=%s serial=%s customer=%s location=%s machine=%s",
            device_id,
            serial_number,
            customer,
            location,
            machine_name,
        )

        return DeviceUpsertResult(device_id=device_id, created=False)

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

    def _select_by_serial(self, conn: Any, serial_number: str) -> Any:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, serial_number FROM dbo.devices WHERE serial_number = ?",
            (serial_number,),
        )
        return cur.fetchone()

    def _insert_device(
        self,
        conn: Any,
        *,
        serial_number: str,
        customer: str,
        location: str,
        machine_name: str,
    ) -> int:
        cur = conn.cursor()

        # OUTPUT gives us the inserted identity value reliably.
        cur.execute(
            """
            INSERT INTO dbo.devices (
                serial_number,
                customer,
                location,
                machine_name,
                created_at,
                updated_at
            )
            OUTPUT Inserted.id
            VALUES (?, ?, ?, ?, SYSUTCDATETIME(), SYSUTCDATETIME())
            """,
            (serial_number, customer, location, machine_name),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            raise RuntimeError("Insert succeeded but no id returned")
        return int(row[0])

    def _update_device(
        self,
        conn: Any,
        *,
        device_id: int,
        customer: str,
        location: str,
        machine_name: str,
    ) -> None:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE dbo.devices
            SET customer = ?,
                location = ?,
                machine_name = ?,
                updated_at = SYSUTCDATETIME()
            WHERE id = ?
            """,
            (customer, location, machine_name, int(device_id)),
        )
