"""Central configuration. Env-driven; defaults match docker-compose defaults."""
import os

DB_HOST = os.getenv("DB_HOST", "mssql")
DB_PORT = int(os.getenv("DB_PORT", "1433"))
DB_NAME = os.getenv("DB_NAME", "S1_Remote_Monitoring")
DB_USER = os.getenv("DB_USER", "admin")
DB_PASS = os.getenv("DB_PASS", "")

# TODO(DECIDE): "parcels" = everything seen. Alternatives: "good_read", "complete".
THROUGHPUT_METRIC = os.getenv("THROUGHPUT_METRIC", "total_items")
_ALLOWED_THROUGHPUT_METRICS = ("total_items", "good_read", "complete")
if THROUGHPUT_METRIC not in _ALLOWED_THROUGHPUT_METRICS:
    raise ValueError(f"THROUGHPUT_METRIC must be one of {_ALLOWED_THROUGHPUT_METRICS}")

# TODO(DECIDE): DB stores UTC; display uses a fixed offset. Replace with
# per-customer IANA zones if the fleet ever spans timezones.
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "2"))

GOOD_READ_DEFAULT_TARGET = float(os.getenv("GOOD_READ_DEFAULT_TARGET", "93"))
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "45"))

EXPECTED_PACKETS_PER_DAY = 288  # 1440 min / 5-min packets
PACKETS_PER_HOUR = 12

# TODO(DECIDE): shift windows (name, start hour inclusive, end hour exclusive; local time)
SHIFTS = (("Shift 1", 6, 14), ("Shift 2", 14, 22), ("Overnight", 22, 6))

AUTH_ENABLED = os.getenv("AUTH_ENABLED", "0") == "1"
ADMIN_USERS = frozenset(
    u.strip() for u in os.getenv("ADMIN_USERS", "").split(",") if u.strip()
)
