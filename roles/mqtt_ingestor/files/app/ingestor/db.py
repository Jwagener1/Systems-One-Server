"""
MS SQL Server writer using pyodbc.

Responsibilities:
  - Route each SpoolEntry to the correct legacy dbo.* table based on topic.
  - Maintain a single lazily-created pyodbc connection (auto-reconnect on error).
  - Auto-register unknown devices in dbo.devices with an in-memory cache.
  - write_batch()           -> dispatch per-topic; retry with back-off.
  - write_deadletter()      -> INSERT into ingest.telemetry_deadletter.
  - update_pipeline_state() -> MERGE into ingest.pipeline_state KV table.
  - write_broker_snapshot() -> INSERT into broker.broker_stats; retry with back-off.
  - run_migrations()        -> execute a T-SQL file split on GO boundaries.

Security:
  - All SQL uses parameterised queries -- no f-string injection.
  - Connection string built from discrete typed fields only.

Topic routing (type_path after prefix/customer/location/machine_name):
  status          -> dbo.device_status                MERGE
  App/running     -> dbo.device_application_status    MERGE
  OS/version      -> dbo.device_os_status              MERGE
  OS/uptime       -> dbo.device_uptime_status          MERGE
  OS/drives       -> dbo.device_storage_status         MERGE per drive
  DB/itemlog      -> dbo.device_statistics             INSERT
  OS/cpu          -> dbo.device_os_metrics             MERGE (cpu columns only)
  OS/memory       -> dbo.device_os_metrics             MERGE (memory columns only)
  OS/temperature  -> dbo.device_os_metrics             MERGE (temp columns only)
  everything else -> discard (log DEBUG)
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pyodbc

from .config import DbConfig
from .models import BrokerSnapshot, SpoolEntry
from .validation import MACHINE_NAME_ONLY_RE

if TYPE_CHECKING:
    from .metrics import Metrics

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL templates
# ---------------------------------------------------------------------------

_INSERT_DEADLETTER = """
INSERT INTO [ingest].[telemetry_deadletter] (
    first_seen_utc, mqtt_topic, payload_hash_sha256,
    payload_text, payload_json, failure_reason,
    retry_count, source_timestamp_utc, source_id
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPSERT_PIPELINE_STATE = """
MERGE [ingest].[pipeline_state] AS target
USING (VALUES (?, ?, ?)) AS src(state_key, state_value, updated_utc)
ON target.state_key = src.state_key
WHEN MATCHED THEN
    UPDATE SET state_value = src.state_value,
               updated_utc  = src.updated_utc
WHEN NOT MATCHED THEN
    INSERT (state_key, state_value, updated_utc)
    VALUES (src.state_key, src.state_value, src.updated_utc);
"""

_UPDATE_DEVICE_SERIAL = """
UPDATE [dbo].[devices]
SET serial_number = ?,
    updated_at    = SYSUTCDATETIME()
WHERE id = ?
"""

_INSERT_DEVICE = """
INSERT INTO [dbo].[devices] (serial_number, customer, location, machine_name)
OUTPUT INSERTED.id
VALUES (?, ?, ?, ?)
"""

_SELECT_DEVICE = """
SELECT TOP 1 id, serial_number FROM [dbo].[devices]
WHERE customer = ? AND location = ? AND machine_name = ?
ORDER BY id ASC
"""

_MERGE_DEVICE_STATUS = """
MERGE [dbo].[device_status] AS t
USING (VALUES (?, ?, ?, ?)) AS s(device_id, status, ts_epoch, ts_datetime)
ON t.device_id = s.device_id
WHEN MATCHED THEN UPDATE SET
    status        = s.status,
    ts_epoch      = s.ts_epoch,
    ts_datetime   = s.ts_datetime,
    offline_since = CASE
        WHEN s.status = 'offline' AND t.offline_since IS NULL THEN SYSUTCDATETIME()
        WHEN s.status = 'online'  THEN NULL
        ELSE t.offline_since
    END,
    updated_at = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (device_id, status, ts_epoch, ts_datetime, offline_since)
    VALUES (
        s.device_id, s.status, s.ts_epoch, s.ts_datetime,
        CASE WHEN s.status = 'offline' THEN SYSUTCDATETIME() ELSE NULL END
    );
"""

_MERGE_APP_RUNNING = """
MERGE [dbo].[device_application_status] AS t
USING (VALUES (?, ?, ?, ?)) AS s(device_id, app_running, ts_epoch, ts_datetime)
ON t.device_id = s.device_id
WHEN MATCHED THEN UPDATE SET
    application_running = s.app_running,
    ts_epoch    = s.ts_epoch,
    ts_datetime = s.ts_datetime,
    stopped_since = CASE
        WHEN s.app_running = 0 AND t.stopped_since IS NULL THEN SYSUTCDATETIME()
        WHEN s.app_running = 1 THEN NULL
        ELSE t.stopped_since
    END,
    updated_at = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (device_id, application_running, ts_epoch, ts_datetime, stopped_since)
    VALUES (
        s.device_id, s.app_running, s.ts_epoch, s.ts_datetime,
        CASE WHEN s.app_running = 0 THEN SYSUTCDATETIME() ELSE NULL END
    );
"""

_MERGE_OS_VERSION = """
MERGE [dbo].[device_os_status] AS t
USING (VALUES (?, ?, ?, ?)) AS s(device_id, os_version, ts_epoch, ts_datetime)
ON t.device_id = s.device_id
WHEN MATCHED THEN UPDATE SET
    os_version  = s.os_version,
    ts_epoch    = s.ts_epoch,
    ts_datetime = s.ts_datetime,
    updated_at  = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (device_id, os_version, ts_epoch, ts_datetime)
    VALUES (s.device_id, s.os_version, s.ts_epoch, s.ts_datetime);
"""

_MERGE_UPTIME = """
MERGE [dbo].[device_uptime_status] AS t
USING (VALUES (?, ?, ?, ?)) AS s(device_id, uptime_seconds, ts_epoch, ts_datetime)
ON t.device_id = s.device_id
WHEN MATCHED THEN UPDATE SET
    uptime_seconds = s.uptime_seconds,
    ts_epoch       = s.ts_epoch,
    ts_datetime    = s.ts_datetime,
    updated_at     = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (device_id, uptime_seconds, ts_epoch, ts_datetime)
    VALUES (s.device_id, s.uptime_seconds, s.ts_epoch, s.ts_datetime);
"""

_MERGE_STORAGE = """
MERGE [dbo].[device_storage_status] AS t
USING (VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)) AS s(
    device_id, drive, drive_type, format,
    total_gb, free_gb, used_gb, usage_percent,
    ts_epoch, ts_datetime
)
ON t.device_id = s.device_id AND t.drive = s.drive
WHEN MATCHED THEN UPDATE SET
    drive_type    = s.drive_type,
    format        = s.format,
    total_gb      = s.total_gb,
    free_gb       = s.free_gb,
    used_gb       = s.used_gb,
    usage_percent = s.usage_percent,
    ts_epoch      = s.ts_epoch,
    ts_datetime   = s.ts_datetime,
    updated_at    = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (
    device_id, drive, drive_type, format,
    total_gb, free_gb, used_gb, usage_percent,
    ts_epoch, ts_datetime
) VALUES (
    s.device_id, s.drive, s.drive_type, s.format,
    s.total_gb, s.free_gb, s.used_gb, s.usage_percent,
    s.ts_epoch, s.ts_datetime
);
"""

_INSERT_STATISTICS = """
INSERT INTO [dbo].[device_statistics] (
    device_id, ts_epoch, ts_datetime,
    total_items, no_read, good_read, no_dimension,
    no_weight, data_sent, not_sent, image_sent,
    image_not_sent, item_out_of_spec, more_than_1_item,
    hand_scanned, complete
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_MERGE_OS_CPU = """
MERGE [dbo].[device_os_metrics] AS t
USING (VALUES (?, ?, ?)) AS s(device_id, cpu_percent, ts_epoch)
ON t.device_id = s.device_id
WHEN MATCHED THEN UPDATE SET
    cpu_percent  = s.cpu_percent,
    cpu_ts_epoch = s.ts_epoch,
    ts_datetime  = SYSUTCDATETIME(),
    updated_at   = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (device_id, cpu_percent, cpu_ts_epoch, ts_datetime)
    VALUES (s.device_id, s.cpu_percent, s.ts_epoch, SYSUTCDATETIME());
"""

_MERGE_OS_MEMORY = """
MERGE [dbo].[device_os_metrics] AS t
USING (VALUES (?, ?, ?, ?, ?, ?)) AS s(
    device_id, mem_total_gb, mem_used_gb, mem_free_gb, mem_usage_pct, ts_epoch
)
ON t.device_id = s.device_id
WHEN MATCHED THEN UPDATE SET
    mem_total_gb  = s.mem_total_gb,
    mem_used_gb   = s.mem_used_gb,
    mem_free_gb   = s.mem_free_gb,
    mem_usage_pct = s.mem_usage_pct,
    mem_ts_epoch  = s.ts_epoch,
    ts_datetime   = SYSUTCDATETIME(),
    updated_at    = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (
    device_id, mem_total_gb, mem_used_gb, mem_free_gb,
    mem_usage_pct, mem_ts_epoch, ts_datetime
) VALUES (
    s.device_id, s.mem_total_gb, s.mem_used_gb, s.mem_free_gb,
    s.mem_usage_pct, s.ts_epoch, SYSUTCDATETIME()
);
"""

_MERGE_OS_TEMP = """
MERGE [dbo].[device_os_metrics] AS t
USING (VALUES (?, ?, ?, ?)) AS s(device_id, temp_celsius, temp_status, ts_epoch)
ON t.device_id = s.device_id
WHEN MATCHED THEN UPDATE SET
    temp_celsius  = s.temp_celsius,
    temp_status   = s.temp_status,
    temp_ts_epoch = s.ts_epoch,
    ts_datetime   = SYSUTCDATETIME(),
    updated_at    = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (device_id, temp_celsius, temp_status, temp_ts_epoch, ts_datetime)
    VALUES (s.device_id, s.temp_celsius, s.temp_status, s.ts_epoch, SYSUTCDATETIME());
"""

_INSERT_BROKER_STATS = """
INSERT INTO [broker].[broker_stats] (
    collected_utc, clients_connected, clients_total, clients_inactive,
    clients_max, msgs_received, msgs_sent, msgs_stored, bytes_received,
    bytes_sent, subscriptions, uptime_seconds, version,
    load_msgs_recv_1min, load_msgs_sent_1min
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_topic(topic: str, prefix_depth: int) -> tuple[str, str, str, str] | None:
    """
    Split topic into (customer, location, machine_name, type_path).
    Returns None if the topic has fewer segments than expected.
    prefix_depth: number of leading segments to skip (e.g. 1 for 'systems-one').
    """
    parts = topic.split("/")
    needed = prefix_depth + 3  # +customer +location +machine_name
    if len(parts) < needed + 1:
        return None
    customer = parts[prefix_depth]
    location = parts[prefix_depth + 1]
    machine_name = parts[prefix_depth + 2]
    type_path = "/".join(parts[prefix_depth + 3 :])
    return customer, location, machine_name, type_path


def _ts_pair(entry: SpoolEntry) -> tuple[int | None, str | None]:
    """Return (ts_epoch_int, ts_iso_str) from spool entry source or enqueue time."""
    if entry.source_timestamp_utc:
        try:
            dt = datetime.fromisoformat(entry.source_timestamp_utc)
            return int(dt.timestamp()), entry.source_timestamp_utc
        except (ValueError, OSError):
            pass
    try:
        dt = datetime.fromisoformat(entry.enqueued_utc)
        return int(dt.timestamp()), entry.enqueued_utc
    except (ValueError, OSError):
        return None, None


def _parse_json(entry: SpoolEntry) -> dict | None:
    """Parse the spool entry payload_json string. Returns None on failure."""
    if not entry.payload_json:
        return None
    try:
        obj = json.loads(entry.payload_json)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Writer class
# ---------------------------------------------------------------------------

class DbWriter:
    def __init__(
        self,
        cfg: DbConfig,
        prefix_depth: int,
        max_retries: int,
        retry_base_ms: int,
        retry_max_ms: int,
        metrics: "Metrics | None" = None,
    ) -> None:
        self._cfg = cfg
        self._prefix_depth = prefix_depth
        self._max_retries = max_retries
        self._retry_base_ms = retry_base_ms
        self._retry_max_ms = retry_max_ms
        self._metrics = metrics
        self._conn: pyodbc.Connection | None = None
        # device cache: (customer, location, machine_name) -> device_id
        self._device_cache: dict[tuple[str, str, str], int] = {}

    # -- connection management ------------------------------------------------

    def _connection_string(self) -> str:
        trust = "Yes" if self._cfg.trust_server_certificate else "No"
        return (
            f"DRIVER={{{self._cfg.driver}}};"
            f"SERVER={self._cfg.host},{self._cfg.port};"
            f"DATABASE={self._cfg.name};"
            f"UID={self._cfg.user};"
            f"PWD={self._cfg.password};"
            f"TrustServerCertificate={trust};"
            f"Connection Timeout={self._cfg.connect_timeout_sec};"
        )

    def _get_conn(self) -> pyodbc.Connection:
        if self._conn is None:
            logger.info(
                "DbWriter opening MSSQL connection",
                extra={"host": self._cfg.host, "db": self._cfg.name},
            )
            conn = pyodbc.connect(self._connection_string(), autocommit=False)
            conn.timeout = self._cfg.command_timeout_sec
            self._conn = conn
        return self._conn

    def _close_conn(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        # Clear device cache so stale IDs are not used after reconnect.
        self._device_cache.clear()

    # -- retry helper ---------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        base = self._retry_base_ms / 1000.0
        cap = self._retry_max_ms / 1000.0
        delay = min(base * (2 ** attempt), cap)
        jitter = delay * 0.2 * (2 * random.random() - 1)
        return max(0.05, delay + jitter)

    # -- validation -----------------------------------------------------------

    # A real serial must NOT look like a bare machine name (e.g. "DIM5", "STATIC1").
    # _resolve_device (below) uses this independently of the allowlist-gate
    # rejection check in validation.py: it decides whether an incoming serial
    # looks like a machine-name placeholder before deciding whether to treat
    # it as a real serial worth upgrading a stored "UNKNOWN-" placeholder to.
    _MACHINE_NAME_RE = __import__("re").compile(r"^(DIM|STATIC)\d*$", __import__("re").IGNORECASE)

    @staticmethod
    def _validate_customer_location(customer: str, location: str) -> bool:
        import os as _os

        def _allowlist(name: str) -> set[str]:
            raw = _os.environ.get(name, "")
            return {v.strip().upper() for v in raw.split(",") if v.strip()}

        allowed_customers = _allowlist("INGEST_ALLOWED_CUSTOMERS")
        if allowed_customers and customer.strip().upper() not in allowed_customers:
            return False
        allowed_locations = _allowlist("INGEST_ALLOWED_LOCATIONS")
        if allowed_locations and location.strip().upper() not in allowed_locations:
            return False
        return True

    # -- device resolution ----------------------------------------------------

    def _resolve_device(
        self,
        conn: pyodbc.Connection,
        customer: str,
        location: str,
        machine_name: str,
        serial_number: str | None,
    ) -> int:
        cache_key = (customer, location, machine_name)

        # If a real serial has arrived, bypass cache so we can check/upgrade the placeholder
        real_serial = (
            serial_number
            and not serial_number.startswith("UNKNOWN-")
            and not self._MACHINE_NAME_RE.match(serial_number)
        )
        if cache_key in self._device_cache and not real_serial:
            return self._device_cache[cache_key]

        row = conn.execute(_SELECT_DEVICE, (customer, location, machine_name)).fetchone()
        if row:
            device_id = int(row[0])
            db_serial = row[1] or ""
            # If the DB has a placeholder and a real serial has now arrived, upgrade it
            if (
                serial_number
                and not self._MACHINE_NAME_RE.match(serial_number)
                and db_serial.startswith("UNKNOWN-")
            ):
                conn.execute(_UPDATE_DEVICE_SERIAL, (serial_number, device_id))
                conn.commit()
                logger.info(
                    "Device serial upgraded from placeholder to real serial",
                    extra={
                        "customer": customer,
                        "location": location,
                        "machine_name": machine_name,
                        "old_serial": db_serial,
                        "new_serial": serial_number,
                        "device_id": device_id,
                    },
                )
                # Evict cache so next lookup reflects updated serial
                self._device_cache.pop(cache_key, None)
            elif serial_number and db_serial and db_serial != serial_number and not db_serial.startswith("UNKNOWN-"):
                logger.warning(
                    "Serial number mismatch for device — using existing DB record",
                    extra={
                        "customer": customer,
                        "location": location,
                        "machine_name": machine_name,
                        "mqtt_serial": serial_number,
                        "db_serial": db_serial,
                        "device_id": device_id,
                    },
                )
        else:
            sn = serial_number if serial_number else f"UNKNOWN-{machine_name}-{location}"
            row = conn.execute(_INSERT_DEVICE, (sn, customer, location, machine_name)).fetchone()
            conn.commit()
            device_id = int(row[0])
            logger.info(
                "Auto-registered new device",
                extra={
                    "customer": customer,
                    "location": location,
                    "machine_name": machine_name,
                    "serial_number": sn,
                    "device_id": device_id,
                },
            )

        self._device_cache[cache_key] = device_id
        return device_id

    # -- per-topic handlers ---------------------------------------------------

    def _handle_status(
        self,
        conn: pyodbc.Connection,
        entry: SpoolEntry,
        device_id: int,
        ts_epoch: int | None,
        ts_dt: str | None,
    ) -> None:
        # A retained delivery means the broker replayed this from its retained
        # store when we (re)subscribed -- it is NOT evidence the device is alive
        # now. Honouring it resurrects devices that have been dark for weeks on
        # every reconnect. Live publishes are delivered with retain=0, so real
        # devices still update normally. Liveness is owned by the offline sweep.
        if entry.retain:
            return

        # New-generation devices publish a JSON object on the status topic.
        # Extract device_status from it; also honour the embedded ts (ms epoch)
        # and opportunistically write os_version if present.
        payload = _parse_json(entry)
        if payload is not None:
            raw_status = str(payload.get("device_status", "unknown")).lower()
            status = raw_status if raw_status in ("online", "offline") else "unknown"
            # Prefer the embedded ms-epoch timestamp over the spool enqueue time.
            raw_ts = payload.get("ts")
            if isinstance(raw_ts, (int, float)):
                try:
                    ts_sec = raw_ts / 1000.0 if raw_ts > 1e10 else raw_ts
                    from datetime import UTC, datetime
                    ts_dt_obj = datetime.fromtimestamp(ts_sec, tz=UTC)
                    ts_epoch = int(ts_dt_obj.timestamp())
                    ts_dt = ts_dt_obj.isoformat()
                except (ValueError, OSError, OverflowError):
                    pass
            conn.execute(_MERGE_DEVICE_STATUS, (device_id, status, ts_epoch, ts_dt))
            # Bonus: write os_version if the payload includes it.
            os_ver = payload.get("device_os_version")
            if os_ver:
                conn.execute(_MERGE_OS_VERSION, (device_id, str(os_ver), ts_epoch, ts_dt))
        else:
            # Legacy plain-text format: "online" / "offline"
            status = (entry.payload_text or "").strip().lower()
            if status not in ("online", "offline"):
                status = "unknown"
            conn.execute(_MERGE_DEVICE_STATUS, (device_id, status, ts_epoch, ts_dt))

    def _handle_app_running(
        self,
        conn: pyodbc.Connection,
        entry: SpoolEntry,
        device_id: int,
        payload: dict,
        ts_epoch: int | None,
        ts_dt: str | None,
    ) -> None:
        running = payload.get("Value", False)
        app_running = 1 if running else 0
        conn.execute(_MERGE_APP_RUNNING, (device_id, app_running, ts_epoch, ts_dt))

    def _handle_os_version(
        self,
        conn: pyodbc.Connection,
        entry: SpoolEntry,
        device_id: int,
        payload: dict,
        ts_epoch: int | None,
        ts_dt: str | None,
    ) -> None:
        version = str(payload.get("Value", ""))
        conn.execute(_MERGE_OS_VERSION, (device_id, version, ts_epoch, ts_dt))

    def _handle_os_uptime(
        self,
        conn: pyodbc.Connection,
        entry: SpoolEntry,
        device_id: int,
        payload: dict,
        ts_epoch: int | None,
        ts_dt: str | None,
    ) -> None:
        uptime = payload.get("Value", 0)
        conn.execute(_MERGE_UPTIME, (device_id, uptime, ts_epoch, ts_dt))

    def _handle_os_drives(
        self,
        conn: pyodbc.Connection,
        entry: SpoolEntry,
        device_id: int,
        payload: dict,
        ts_epoch: int | None,
        ts_dt: str | None,
    ) -> None:
        drives = payload.get("Value", [])
        if not isinstance(drives, list):
            return
        for d in drives:
            if not isinstance(d, dict):
                continue
            raw_drive = d.get("drive", "")
            drive = raw_drive if raw_drive.endswith(":") else raw_drive.rstrip() + ":"
            conn.execute(
                _MERGE_STORAGE,
                (
                    device_id,
                    drive,
                    d.get("driveType"),
                    d.get("format"),
                    d.get("totalGB"),
                    d.get("freeGB"),
                    d.get("usedGB"),
                    d.get("usagePercent"),
                    ts_epoch,
                    ts_dt,
                ),
            )

    def _handle_db_itemlog(
        self,
        conn: pyodbc.Connection,
        entry: SpoolEntry,
        device_id: int,
        payload: dict,
        ts_epoch: int | None,
        ts_dt: str | None,
    ) -> None:
        v = payload.get("Value", {})
        if not isinstance(v, dict):
            return
        conn.execute(
            _INSERT_STATISTICS,
            (
                device_id,
                ts_epoch,
                ts_dt,
                v.get("Total_Items", 0),
                v.get("No_Read", 0),
                v.get("Good_Read", 0),
                v.get("No_Dimension", 0),
                v.get("No_Weight", 0),
                v.get("Data_Sent", 0),
                v.get("Not_Sent", 0),
                v.get("Image_Sent", 0),
                v.get("Image_Not_Sent", 0),
                v.get("Item_Out_Of_Spec", 0),
                v.get("More_Than_1_Item", 0),
                v.get("Hand_Scanned", 0),
                v.get("Complete", 0),
            ),
        )

    def _handle_os_cpu(
        self,
        conn: pyodbc.Connection,
        device_id: int,
        payload: dict,
        ts_epoch: int | None,
    ) -> None:
        cpu = payload.get("Value")
        conn.execute(_MERGE_OS_CPU, (device_id, cpu, ts_epoch))

    def _handle_os_memory(
        self,
        conn: pyodbc.Connection,
        device_id: int,
        payload: dict,
        ts_epoch: int | None,
    ) -> None:
        v = payload.get("Value", {})
        if not isinstance(v, dict):
            return
        conn.execute(
            _MERGE_OS_MEMORY,
            (
                device_id,
                v.get("totalGB"),
                v.get("usedGB"),
                v.get("freeGB"),
                v.get("usagePercent"),
                ts_epoch,
            ),
        )

    def _handle_os_temperature(
        self,
        conn: pyodbc.Connection,
        device_id: int,
        payload: dict,
        ts_epoch: int | None,
    ) -> None:
        v = payload.get("Value", {})
        if not isinstance(v, dict):
            return
        conn.execute(
            _MERGE_OS_TEMP,
            (
                device_id,
                v.get("celsius"),
                v.get("status"),
                ts_epoch,
            ),
        )

    # -- routing --------------------------------------------------------------

    def _dispatch_entry(self, conn: pyodbc.Connection, entry: SpoolEntry) -> None:
        """Route one spool entry to the correct dbo table. Raises on DB error."""
        parsed = _parse_topic(entry.topic, self._prefix_depth)
        if parsed is None:
            logger.debug(
                "Topic has too few segments -- discarding",
                extra={"topic": entry.topic},
            )
            return

        customer, location, machine_name, type_path = parsed

        # Settings entries are handled by SettingsWriter -- skip here
        if type_path.startswith("App/settings"):
            return

        payload: dict | None = None
        serial_number: str | None = None
        if entry.payload_json:
            payload = _parse_json(entry)
            if payload:
                # Try both casing variants used across device firmware versions
                sn = payload.get("serial_number") or payload.get("SerialNumber")
                if sn is not None:
                    serial_number = str(sn).strip() or None

        ts_epoch, ts_dt = _ts_pair(entry)

        if serial_number and MACHINE_NAME_ONLY_RE.match(serial_number):
            logger.warning(
                "Rejecting device: serial_number looks like a machine name (serial=%s customer=%s location=%s machine=%s)",
                serial_number, customer, location, machine_name,
            )
            return
        if not self._validate_customer_location(customer, location):
            return

        device_id = self._resolve_device(conn, customer, location, machine_name, serial_number)

        if type_path == "status":
            self._handle_status(conn, entry, device_id, ts_epoch, ts_dt)

        elif type_path == "App/running":
            if payload is None:
                return
            self._handle_app_running(conn, entry, device_id, payload, ts_epoch, ts_dt)

        elif type_path == "OS/version":
            if payload is None:
                return
            self._handle_os_version(conn, entry, device_id, payload, ts_epoch, ts_dt)

        elif type_path == "OS/uptime":
            if payload is None:
                return
            self._handle_os_uptime(conn, entry, device_id, payload, ts_epoch, ts_dt)

        elif type_path == "OS/drives":
            if payload is None:
                return
            self._handle_os_drives(conn, entry, device_id, payload, ts_epoch, ts_dt)

        elif type_path in ("DB/itemlog", "DB/itemlog.summary"):
            if payload is None:
                return
            # Extract serial from itemlog payload and upgrade placeholder if needed.
            # New-generation devices embed serial_number (lowercase) directly in the payload.
            sn = payload.get("serial_number") or payload.get("SerialNumber")
            if sn:
                serial_number = str(sn)
                self._resolve_device(conn, customer, location, machine_name, serial_number)
            self._handle_db_itemlog(conn, entry, device_id, payload, ts_epoch, ts_dt)

        elif type_path == "OS/cpu":
            if payload is None:
                return
            self._handle_os_cpu(conn, device_id, payload, ts_epoch)

        elif type_path == "OS/memory":
            if payload is None:
                return
            self._handle_os_memory(conn, device_id, payload, ts_epoch)

        elif type_path == "OS/temperature":
            if payload is None:
                return
            self._handle_os_temperature(conn, device_id, payload, ts_epoch)

        else:
            logger.debug(
                "No table mapping for topic -- discarding",
                extra={"topic": entry.topic, "type_path": type_path},
            )

    # -- public API -----------------------------------------------------------

    def write_batch(
        self, entries: list[SpoolEntry]
    ) -> tuple[list[int], list[SpoolEntry]]:
        """
        Route and write a batch of SpoolEntry rows to the appropriate dbo tables.
        Returns (committed_ids, failed_entries).
        Settings entries must already have been removed by the caller.
        """
        if not entries:
            return [], []

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                conn = self._get_conn()
                for entry in entries:
                    self._dispatch_entry(conn, entry)
                conn.commit()

                committed_ids = [e.id for e in entries]
                if self._metrics:
                    self._metrics.db_write_success_total.inc()
                logger.debug("DB batch written", extra={"count": len(entries)})
                return committed_ids, []

            except pyodbc.Error as exc:
                last_exc = exc
                if self._metrics:
                    self._metrics.db_write_failure_total.inc()
                try:
                    self._get_conn().rollback()
                except Exception:
                    pass
                self._close_conn()
                logger.warning(
                    "DB write failed -- retrying",
                    extra={
                        "attempt": attempt + 1,
                        "max_retries": self._max_retries,
                        "error": str(exc),
                    },
                )
                if attempt < self._max_retries:
                    time.sleep(self._backoff(attempt))

        logger.error(
            "DB batch write exhausted retries -- sending to dead-letter",
            extra={"count": len(entries), "error": str(last_exc)},
        )
        return [], entries

    def write_deadletter(
        self,
        entries: list[SpoolEntry],
        reason: str,
        retry_count: int,
    ) -> None:
        if not entries:
            return

        params = [
            (
                entry.enqueued_utc,
                entry.topic,
                entry.payload_hash_sha256,
                entry.payload_text,
                entry.payload_json,
                reason,
                retry_count,
                entry.source_timestamp_utc,
                entry.source_id,
            )
            for entry in entries
        ]

        for attempt in range(3):
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.fast_executemany = True
                cursor.executemany(_INSERT_DEADLETTER, params)
                conn.commit()
                if self._metrics:
                    self._metrics.db_deadletter_total.inc(len(entries))
                return
            except pyodbc.Error as exc:
                self._close_conn()
                logger.error(
                    "Dead-letter write failed",
                    extra={"attempt": attempt + 1, "error": str(exc)},
                )
                if attempt < 2:
                    time.sleep(self._backoff(attempt))

        logger.critical(
            "Dead-letter write exhausted retries -- messages permanently lost",
            extra={"count": len(entries)},
        )

    def update_pipeline_state(self, key: str, value: str) -> None:
        now_utc = datetime.now(UTC).isoformat()
        for attempt in range(3):
            try:
                conn = self._get_conn()
                conn.execute(_UPSERT_PIPELINE_STATE, (key, value, now_utc))
                conn.commit()
                return
            except pyodbc.Error as exc:
                self._close_conn()
                logger.warning(
                    "pipeline_state update failed",
                    extra={"key": key, "error": str(exc)},
                )
                if attempt < 2:
                    time.sleep(self._backoff(attempt))

    def write_broker_snapshot(self, snapshot: BrokerSnapshot) -> bool:
        params = (
            snapshot.collected_utc, snapshot.clients_connected, snapshot.clients_total,
            snapshot.clients_inactive, snapshot.clients_max, snapshot.msgs_received,
            snapshot.msgs_sent, snapshot.msgs_stored, snapshot.bytes_received,
            snapshot.bytes_sent, snapshot.subscriptions, snapshot.uptime_seconds,
            snapshot.version, snapshot.load_msgs_recv_1min, snapshot.load_msgs_sent_1min,
        )
        for attempt in range(self._max_retries + 1):
            try:
                conn = self._get_conn()
                conn.execute(_INSERT_BROKER_STATS, params)
                conn.commit()
                if self._metrics:
                    self._metrics.db_write_success_total.inc()
                return True
            except pyodbc.Error as exc:
                if self._metrics:
                    self._metrics.db_write_failure_total.inc()
                try:
                    self._get_conn().rollback()
                except Exception:
                    pass
                self._close_conn()
                logger.warning(
                    "Broker stats write failed -- retrying",
                    extra={"attempt": attempt + 1, "error": str(exc)},
                )
                if attempt < self._max_retries:
                    time.sleep(self._backoff(attempt))

        logger.error("Broker stats write exhausted retries -- snapshot dropped")
        return False

    def run_migrations(self, sql_path: str) -> None:
        with open(sql_path, encoding="utf-8") as fh:
            raw = fh.read()

        batches = [b.strip() for b in raw.split("\nGO") if b.strip()]

        conn = self._get_conn()
        conn.autocommit = True
        try:
            for batch in batches:
                if batch:
                    conn.execute(batch)
            logger.info(
                "Migrations applied",
                extra={"file": sql_path, "batches": len(batches)},
            )
        finally:
            conn.autocommit = False

    def close(self) -> None:
        self._close_conn()
