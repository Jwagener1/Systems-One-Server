"""
Systems One — Marketing Display
================================
Read-only API that surfaces anonymised aggregate metrics from S1_Remote_Monitoring.
No customer names, locations, or serial numbers are exposed.
"""

import os
import asyncio
import time
import decimal
import datetime
from contextlib import asynccontextmanager
from typing import Any

import pyodbc
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config (env vars, all have sensible defaults for docker-compose)
# ---------------------------------------------------------------------------
DB_HOST   = os.getenv("DB_HOST", "mssql")
DB_PORT   = int(os.getenv("DB_PORT", "1433"))
DB_NAME   = os.getenv("DB_NAME", "S1_Remote_Monitoring")
DB_USER   = os.getenv("DB_USER", "admin")
DB_PASS   = os.getenv("DB_PASS", "")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "30"))

CONN_STR = (
    f"DRIVER={{ODBC Driver 18 for SQL Server}};"
    f"SERVER={DB_HOST},{DB_PORT};"
    f"DATABASE={DB_NAME};"
    f"UID={DB_USER};"
    f"PWD={DB_PASS};"
    "Encrypt=optional;"
    "TrustServerCertificate=yes;"
    "Connection Timeout=5;"
)

# ---------------------------------------------------------------------------
# Simple in-process cache — separate caches for /api/stats and /api/dashboard
# ---------------------------------------------------------------------------
_cache: dict[str, Any] = {}
_cache_ts: float = 0.0
_cache_lock = asyncio.Lock()

_dash_cache: dict[str, Any] = {}
_dash_cache_ts: float = 0.0
_dash_cache_lock = asyncio.Lock()


def _coerce(v):
    """Recursively convert pyodbc/decimal/datetime types to JSON-serialisable types."""
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _coerce(v2) for k, v2 in v.items()}
    if isinstance(v, list):
        return [_coerce(i) for i in v]
    return v


def _query(sql: str) -> list[dict]:
    with pyodbc.connect(CONN_STR, timeout=8) as conn:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        return [_coerce(dict(zip(cols, row))) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Existing /api/stats builder (anonymised marketing display)
# ---------------------------------------------------------------------------
def _build_stats() -> dict:
    # ---- Fleet health -------------------------------------------------------
    fleet = _query("""
        SELECT
            COUNT(DISTINCT d.id)                                       AS total_machines,
            SUM(CASE WHEN s.status = 'online'  THEN 1 ELSE 0 END)     AS online,
            SUM(CASE WHEN s.status = 'offline' THEN 1 ELSE 0 END)     AS offline
        FROM dbo.devices d
        LEFT JOIN (
            SELECT device_id, status,
                   ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY ts_datetime DESC) AS rn
            FROM dbo.device_status
        ) s ON s.device_id = d.id AND s.rn = 1
    """)[0]

    # ---- Today aggregate ----------------------------------------------------
    today = _query("""
        SELECT
            COALESCE(SUM(total_items), 0)  AS items_today,
            COALESCE(SUM(good_read),   0)  AS good_reads_today,
            COALESCE(SUM(no_read),     0)  AS no_reads_today,
            COALESCE(SUM(not_sent),    0)  AS not_sent_today,
            CAST(
                COALESCE(100.0 * SUM(good_read) / NULLIF(SUM(total_items), 0), 0)
            AS DECIMAL(5,1))               AS good_read_pct_today
        FROM dbo.device_statistics
        WHERE CAST(DATEADD(HOUR, 2, ts_datetime) AS date)
            = CAST(DATEADD(HOUR, 2, GETDATE()) AS date)
    """)[0]

    # ---- Month aggregate ----------------------------------------------------
    month = _query("""
        SELECT
            COALESCE(SUM(total_items), 0)  AS items_month,
            CAST(
                COALESCE(100.0 * SUM(good_read) / NULLIF(SUM(total_items), 0), 0)
            AS DECIMAL(5,1))               AS good_read_pct_month
        FROM dbo.device_statistics
        WHERE DATEADD(HOUR, 2, ts_datetime)
            >= DATEFROMPARTS(YEAR(DATEADD(HOUR,2,GETDATE())), MONTH(DATEADD(HOUR,2,GETDATE())), 1)
    """)[0]

    # ---- Year aggregate -----------------------------------------------------
    year = _query("""
        SELECT
            COALESCE(SUM(total_items), 0)  AS items_year,
            CAST(
                COALESCE(100.0 * SUM(good_read) / NULLIF(SUM(total_items), 0), 0)
            AS DECIMAL(5,1))               AS good_read_pct_year
        FROM dbo.device_statistics
        WHERE DATEADD(HOUR, 2, ts_datetime)
            >= DATEFROMPARTS(YEAR(DATEADD(HOUR, 2, GETDATE())), 1, 1)
    """)[0]

    # ---- Hourly sparkline (last 24 h, no customer split) --------------------
    hourly = _query("""
        SELECT
            DATEPART(HOUR, DATEADD(HOUR, 2, ts_datetime)) AS hour_of_day,
            CAST(DATEADD(HOUR, 2, ts_datetime) AS date)   AS day,
            SUM(total_items)                               AS items
        FROM dbo.device_statistics
        WHERE ts_datetime >= DATEADD(HOUR, -24, GETDATE())
        GROUP BY
            CAST(DATEADD(HOUR, 2, ts_datetime) AS date),
            DATEPART(HOUR, DATEADD(HOUR, 2, ts_datetime))
        ORDER BY day, hour_of_day
    """)

    sparkline = [
        {"label": f"{int(r['hour_of_day']):02d}:00", "value": int(r["items"] or 0)}
        for r in hourly
    ]

    # ---- App health ---------------------------------------------------------
    app_health = _query("""
        SELECT
            SUM(CASE WHEN a.application_running = 1 THEN 1 ELSE 0 END) AS apps_running,
            SUM(CASE WHEN a.application_running = 0 THEN 1 ELSE 0 END) AS apps_stopped
        FROM (
            SELECT device_id, application_running,
                   ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY ts_datetime DESC) AS rn
            FROM dbo.device_application_status
        ) a WHERE a.rn = 1
    """)[0]

    return {
        "generated_at": int(time.time()),
        "fleet": {
            "total_machines": int(fleet["total_machines"] or 0),
            "online":         int(fleet["online"]  or 0),
            "offline":        int(fleet["offline"] or 0),
            "apps_running":   int(app_health["apps_running"]  or 0),
            "apps_stopped":   int(app_health["apps_stopped"]  or 0),
        },
        "today": {
            "items":          int(today["items_today"]),
            "good_reads":     int(today["good_reads_today"]),
            "no_reads":       int(today["no_reads_today"]),
            "not_sent":       int(today["not_sent_today"]),
            "good_read_pct":  float(today["good_read_pct_today"]),
        },
        "month": {
            "items":         int(month["items_month"]),
            "good_read_pct": float(month["good_read_pct_month"]),
        },
        "year": {
            "items":         int(year["items_year"]),
            "good_read_pct": float(year["good_read_pct_year"]),
        },
        "sparkline": sparkline,
    }


# ---------------------------------------------------------------------------
# New /api/dashboard builder (full operational dashboard)
# ---------------------------------------------------------------------------
def _build_dashboard() -> dict:
    # 0. Year / Month aggregates (for KPI stat panels)
    year_month = _query("""
        SELECT
            COALESCE(SUM(CASE WHEN DATEADD(HOUR,2,ts_datetime) >= DATEFROMPARTS(YEAR(DATEADD(HOUR,2,GETDATE())),1,1) THEN total_items ELSE 0 END), 0) AS items_year,
            CAST(COALESCE(100.0*SUM(CASE WHEN DATEADD(HOUR,2,ts_datetime) >= DATEFROMPARTS(YEAR(DATEADD(HOUR,2,GETDATE())),1,1) THEN good_read ELSE 0 END)
                /NULLIF(SUM(CASE WHEN DATEADD(HOUR,2,ts_datetime) >= DATEFROMPARTS(YEAR(DATEADD(HOUR,2,GETDATE())),1,1) THEN total_items ELSE 0 END),0),0) AS DECIMAL(5,1)) AS good_read_pct_year,
            COALESCE(SUM(CASE WHEN DATEADD(HOUR,2,ts_datetime) >= DATEFROMPARTS(YEAR(DATEADD(HOUR,2,GETDATE())),MONTH(DATEADD(HOUR,2,GETDATE())),1) THEN total_items ELSE 0 END),0) AS items_month,
            CAST(COALESCE(100.0*SUM(CASE WHEN DATEADD(HOUR,2,ts_datetime) >= DATEFROMPARTS(YEAR(DATEADD(HOUR,2,GETDATE())),MONTH(DATEADD(HOUR,2,GETDATE())),1) THEN good_read ELSE 0 END)
                /NULLIF(SUM(CASE WHEN DATEADD(HOUR,2,ts_datetime) >= DATEFROMPARTS(YEAR(DATEADD(HOUR,2,GETDATE())),MONTH(DATEADD(HOUR,2,GETDATE())),1) THEN total_items ELSE 0 END),0),0) AS DECIMAL(5,1)) AS good_read_pct_month
        FROM dbo.device_statistics
        WHERE DATEADD(HOUR,2,ts_datetime) >= DATEFROMPARTS(YEAR(DATEADD(HOUR,2,GETDATE())),1,1)
    """)[0]

    # 1. Today's totals
    totals = _query("""
        SELECT
            COALESCE(SUM(total_items),       0) AS total_items,
            COALESCE(SUM(good_read),         0) AS good_read,
            COALESCE(SUM(no_read),           0) AS no_read,
            COALESCE(SUM(no_dimension),      0) AS no_dimension,
            COALESCE(SUM(no_weight),         0) AS no_weight,
            COALESCE(SUM(data_sent),         0) AS data_sent,
            COALESCE(SUM(not_sent),          0) AS not_sent,
            COALESCE(SUM(item_out_of_spec),  0) AS out_of_spec,
            COALESCE(SUM(more_than_1_item),  0) AS multi_item,
            COALESCE(SUM(hand_scanned),      0) AS hand_scanned
        FROM dbo.device_statistics
        WHERE ts_datetime >= CAST(GETDATE() AS DATE)
    """)[0]

    # 2. Hourly breakdown today
    hourly = _query("""
        SELECT
            DATEPART(HOUR, ts_datetime)  AS hr,
            SUM(total_items)             AS items,
            SUM(good_read)               AS good,
            SUM(no_read)                 AS no_read,
            SUM(hand_scanned)            AS hand_scanned
        FROM dbo.device_statistics
        WHERE ts_datetime >= CAST(GETDATE() AS DATE)
        GROUP BY DATEPART(HOUR, ts_datetime)
        ORDER BY hr
    """)

    # 3. Per-device details
    devices = _query("""
        SELECT
            d.id, d.serial_number, d.machine_name, d.customer, d.location,
            ds.status, ds.offline_since,
            das.application_running, das.stopped_since,
            m.cpu_percent, m.mem_usage_pct, m.temp_celsius, m.temp_status,
            u.uptime_seconds,
            s_today.total_items, s_today.good_read, s_today.no_read,
            s_today.hand_scanned, s_today.data_sent, s_today.not_sent,
            (SELECT MAX(usage_percent) FROM dbo.device_storage_status
             WHERE device_id = d.id AND drive = 'C:') AS c_disk_pct,
            (SELECT MAX(usage_percent) FROM dbo.device_storage_status
             WHERE device_id = d.id) AS max_disk_pct
        FROM dbo.devices d
        LEFT JOIN dbo.device_status ds ON ds.device_id = d.id
        LEFT JOIN dbo.device_application_status das ON das.device_id = d.id
        LEFT JOIN dbo.device_os_metrics m ON m.device_id = d.id
        LEFT JOIN dbo.device_uptime_status u ON u.device_id = d.id
        LEFT JOIN (
            SELECT device_id,
                SUM(total_items)  AS total_items,
                SUM(good_read)    AS good_read,
                SUM(no_read)      AS no_read,
                SUM(hand_scanned) AS hand_scanned,
                SUM(data_sent)    AS data_sent,
                SUM(not_sent)     AS not_sent
            FROM dbo.device_statistics
            WHERE ts_datetime >= CAST(GETDATE() AS DATE)
            GROUP BY device_id
        ) s_today ON s_today.device_id = d.id
        ORDER BY d.customer, d.location, d.machine_name
    """)

    # 4. MQTT broker stats (latest row)
    broker_rows = _query("""
        SELECT TOP 1
            collected_utc, clients_connected, clients_total, clients_inactive,
            msgs_received, msgs_sent, msgs_stored, bytes_received, bytes_sent,
            subscriptions, uptime_seconds,
            load_msgs_recv_1min, load_msgs_sent_1min, version
        FROM broker.broker_stats ORDER BY id DESC
    """)
    broker = broker_rows[0] if broker_rows else None

    # 5. Ingest pipeline state
    pipeline_rows = _query("""
        SELECT state_key, state_value, updated_utc
        FROM ingest.pipeline_state ORDER BY state_key
    """)
    pipeline = {
        r["state_key"]: {"value": r["state_value"], "updated": r["updated_utc"]}
        for r in pipeline_rows
    }

    # 6. Dead letter count
    dl_rows = _query("SELECT COUNT(*) AS total FROM ingest.telemetry_deadletter")
    deadletter = int(dl_rows[0]["total"]) if dl_rows else 0

    # 7. Storage warnings (>70%)
    storage_warn = _query("""
        SELECT s.device_id, d.machine_name, d.customer, d.location,
               s.drive, s.usage_percent, s.total_gb, s.free_gb
        FROM dbo.device_storage_status s
        JOIN dbo.devices d ON d.id = s.device_id
        WHERE s.usage_percent > 70
        ORDER BY s.usage_percent DESC
    """)

    # 8. 14-day daily trend
    daily_trend = _query("""
        SELECT
            CAST(ts_datetime AS DATE) AS day,
            SUM(total_items)          AS items,
            SUM(good_read)            AS good_read,
            SUM(no_read)              AS no_read
        FROM dbo.device_statistics
        WHERE ts_datetime >= DATEADD(DAY, -14, CAST(GETDATE() AS DATE))
        GROUP BY CAST(ts_datetime AS DATE)
        ORDER BY day
    """)

    # 9a. OS version per device
    os_versions = _query("""
        SELECT device_id, os_version FROM dbo.device_os_status
    """)
    os_map = {r["device_id"]: r["os_version"] for r in os_versions}
    for dev in devices:
        dev["os_version"] = os_map.get(dev["id"])

    # 9b. All storage drives per device (not just C:)
    all_storage = _query("""
        SELECT s.device_id, d.machine_name, d.customer, d.location,
               s.drive, s.usage_percent, s.total_gb, s.free_gb
        FROM dbo.device_storage_status s
        JOIN dbo.devices d ON d.id = s.device_id
        ORDER BY s.usage_percent DESC
    """)

    # 9. Fleet online/offline counts
    fleet = _query("""
        SELECT
            COUNT(DISTINCT d.id) AS total_machines,
            SUM(CASE WHEN ds.status='online'  THEN 1 ELSE 0 END) AS online,
            SUM(CASE WHEN ds.status='offline' THEN 1 ELSE 0 END) AS offline,
            SUM(CASE WHEN das.application_running=0 THEN 1 ELSE 0 END) AS apps_stopped
        FROM dbo.devices d
        LEFT JOIN dbo.device_status ds ON ds.device_id = d.id
        LEFT JOIN dbo.device_application_status das ON das.device_id = d.id
    """)[0]

    # 10. Per-customer summary today
    customer_summary = _query("""
        SELECT
            d.customer,
            COUNT(DISTINCT d.id)          AS machines,
            COALESCE(SUM(s.total_items),0) AS total_items,
            COALESCE(SUM(s.good_read),  0) AS good_read,
            COALESCE(SUM(s.no_read),    0) AS no_read,
            COALESCE(SUM(s.hand_scanned),0) AS hand_scanned,
            COALESCE(SUM(s.not_sent),   0) AS not_sent,
            CAST(COALESCE(100.0*SUM(s.good_read)/NULLIF(SUM(s.total_items),0),0) AS DECIMAL(5,1)) AS good_read_pct
        FROM dbo.devices d
        LEFT JOIN dbo.device_statistics s
            ON s.device_id = d.id AND s.ts_datetime >= CAST(GETDATE() AS DATE)
        GROUP BY d.customer
        ORDER BY d.customer
    """)

    # 11b. Alert thresholds / baselines
    thresholds = _query("""
        SELECT customer, machine_name, location, metric, direction,
               warn_value, bad_value, baseline_mean, baseline_stddev,
               baseline_p10, baseline_p90, baseline_samples, last_computed
        FROM dbo.alert_thresholds
        ORDER BY customer, machine_name, metric
    """)

    # 11c. 7-day per-customer trend
    customer_trend = _query("""
        SELECT
            CAST(ts_datetime AS DATE) AS day,
            d.customer,
            SUM(s.total_items) AS items,
            CAST(COALESCE(100.0*SUM(s.good_read)/NULLIF(SUM(s.total_items),0),0) AS DECIMAL(5,1)) AS good_read_pct
        FROM dbo.device_statistics s
        JOIN dbo.devices d ON d.id = s.device_id
        WHERE s.ts_datetime >= DATEADD(DAY, -7, CAST(GETDATE() AS DATE))
        GROUP BY CAST(ts_datetime AS DATE), d.customer
        ORDER BY day, d.customer
    """)

    # 11d. Threshold breach summary (machines currently below warn threshold)
    threshold_breaches = _query("""
        SELECT
            at.customer, at.machine_name, at.location, at.metric,
            at.warn_value, at.bad_value, at.baseline_mean,
            CAST(COALESCE(100.0*SUM(s.good_read)/NULLIF(SUM(s.total_items),0),0) AS DECIMAL(5,1)) AS current_pct
        FROM dbo.alert_thresholds at
        JOIN dbo.devices d ON d.customer=at.customer AND d.machine_name=at.machine_name AND d.location=at.location
        LEFT JOIN dbo.device_statistics s ON s.device_id=d.id AND s.ts_datetime >= CAST(GETDATE() AS DATE)
        WHERE at.metric = 'good_read_pct'
        GROUP BY at.customer, at.machine_name, at.location, at.metric, at.warn_value, at.bad_value, at.baseline_mean
        HAVING CAST(COALESCE(100.0*SUM(s.good_read)/NULLIF(SUM(s.total_items),0),0) AS DECIMAL(5,1)) < at.warn_value
           AND SUM(s.total_items) > 30
        ORDER BY current_pct ASC
    """)

    # 11. Per-machine good read % today (for bar chart)
    machine_goodread = _query("""
        SELECT
            d.machine_name, d.customer, d.location,
            COALESCE(SUM(s.total_items),0) AS total_items,
            CAST(COALESCE(100.0*SUM(s.good_read)/NULLIF(SUM(s.total_items),0),0) AS DECIMAL(5,1)) AS good_read_pct
        FROM dbo.devices d
        LEFT JOIN dbo.device_statistics s
            ON s.device_id = d.id AND s.ts_datetime >= CAST(GETDATE() AS DATE)
        GROUP BY d.machine_name, d.customer, d.location
        ORDER BY good_read_pct ASC
    """)

    return {
        "year":               year_month,
        "totals":             totals,
        "fleet":              fleet,
        "hourly":             hourly,
        "devices":            devices,
        "customerSummary":    customer_summary,
        "customerTrend":      customer_trend,
        "machineGoodRead":    machine_goodread,
        "thresholds":         thresholds,
        "thresholdBreaches":  threshold_breaches,
        "broker":             broker,
        "pipeline":           pipeline,
        "deadletter":         deadletter,
        "storageWarn":        storage_warn,
        "allStorage":         all_storage,
        "dailyTrend":         daily_trend,
    }


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        data = await asyncio.get_event_loop().run_in_executor(None, _build_stats)
        _cache.update(data)
    except Exception:
        pass
    yield


app = FastAPI(title="S1 Marketing Display", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# API — original anonymised stats
# ---------------------------------------------------------------------------
@app.get("/api/stats")
async def get_stats():
    global _cache, _cache_ts
    async with _cache_lock:
        age = time.monotonic() - _cache_ts
        if age > CACHE_TTL or not _cache:
            try:
                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(None, _build_stats)
                _cache = data
                _cache_ts = time.monotonic()
            except Exception as exc:
                if _cache:
                    return {**_cache, "stale": True}
                raise HTTPException(status_code=503, detail=str(exc))
    return _cache


# ---------------------------------------------------------------------------
# API — full operational dashboard
# ---------------------------------------------------------------------------
@app.get("/api/dashboard")
async def get_dashboard():
    global _dash_cache, _dash_cache_ts
    async with _dash_cache_lock:
        age = time.monotonic() - _dash_cache_ts
        if age > CACHE_TTL or not _dash_cache:
            try:
                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(None, _build_dashboard)
                _dash_cache = data
                _dash_cache_ts = time.monotonic()
            except Exception as exc:
                if _dash_cache:
                    return JSONResponse(content={**_dash_cache, "stale": True})
                raise HTTPException(status_code=503, detail=str(exc))
    return JSONResponse(content=_dash_cache)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# API — historical performance over a date range
# ---------------------------------------------------------------------------
@app.get("/api/history")
async def get_history(date_from: str, date_to: str, customer: str = "", machine: str = ""):
    """
    Returns per-day performance data for all (or filtered) machines between
    date_from and date_to (inclusive, YYYY-MM-DD format).
    """
    try:
        import datetime
        datetime.date.fromisoformat(date_from)
        datetime.date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format; use YYYY-MM-DD")

    cust_filter = f"AND d.customer = '{customer}'" if customer else ""
    mach_filter = f"AND d.machine_name = '{machine}'" if machine else ""

    def _run():
        # Daily per-machine stats
        daily = _query(f"""
            SELECT
                CAST(s.ts_datetime AS DATE)   AS day,
                d.machine_name,
                d.customer,
                d.location,
                SUM(s.total_items)            AS total_items,
                SUM(s.good_read)              AS good_read,
                SUM(s.no_read)                AS no_read,
                SUM(s.hand_scanned)           AS hand_scanned,
                SUM(s.not_sent)               AS not_sent,
                CAST(COALESCE(100.0*SUM(s.good_read)/NULLIF(SUM(s.total_items),0),0)
                    AS DECIMAL(5,1))          AS good_read_pct
            FROM dbo.device_statistics s
            JOIN dbo.devices d ON d.id = s.device_id
            WHERE CAST(s.ts_datetime AS DATE) BETWEEN '{date_from}' AND '{date_to}'
              {cust_filter} {mach_filter}
            GROUP BY CAST(s.ts_datetime AS DATE), d.machine_name, d.customer, d.location
            ORDER BY day, d.customer, d.machine_name
        """)

        # Daily fleet totals
        fleet_daily = _query(f"""
            SELECT
                CAST(s.ts_datetime AS DATE) AS day,
                SUM(s.total_items)          AS total_items,
                SUM(s.good_read)            AS good_read,
                SUM(s.no_read)              AS no_read,
                SUM(s.hand_scanned)         AS hand_scanned,
                CAST(COALESCE(100.0*SUM(s.good_read)/NULLIF(SUM(s.total_items),0),0)
                    AS DECIMAL(5,1))        AS good_read_pct
            FROM dbo.device_statistics s
            JOIN dbo.devices d ON d.id = s.device_id
            WHERE CAST(s.ts_datetime AS DATE) BETWEEN '{date_from}' AND '{date_to}'
              {cust_filter} {mach_filter}
            GROUP BY CAST(s.ts_datetime AS DATE)
            ORDER BY day
        """)

        # Period summary per machine
        machine_summary = _query(f"""
            SELECT
                d.machine_name,
                d.customer,
                d.location,
                SUM(s.total_items)  AS total_items,
                SUM(s.good_read)    AS good_read,
                SUM(s.no_read)      AS no_read,
                SUM(s.hand_scanned) AS hand_scanned,
                SUM(s.not_sent)     AS not_sent,
                CAST(COALESCE(100.0*SUM(s.good_read)/NULLIF(SUM(s.total_items),0),0)
                    AS DECIMAL(5,1)) AS good_read_pct
            FROM dbo.device_statistics s
            JOIN dbo.devices d ON d.id = s.device_id
            WHERE CAST(s.ts_datetime AS DATE) BETWEEN '{date_from}' AND '{date_to}'
              {cust_filter} {mach_filter}
            GROUP BY d.machine_name, d.customer, d.location
            ORDER BY good_read_pct ASC
        """)

        # Customer daily rollup
        customer_daily = _query(f"""
            SELECT
                CAST(s.ts_datetime AS DATE) AS day,
                d.customer,
                SUM(s.total_items)          AS total_items,
                SUM(s.good_read)            AS good_read,
                CAST(COALESCE(100.0*SUM(s.good_read)/NULLIF(SUM(s.total_items),0),0)
                    AS DECIMAL(5,1))        AS good_read_pct
            FROM dbo.device_statistics s
            JOIN dbo.devices d ON d.id = s.device_id
            WHERE CAST(s.ts_datetime AS DATE) BETWEEN '{date_from}' AND '{date_to}'
              {cust_filter} {mach_filter}
            GROUP BY CAST(s.ts_datetime AS DATE), d.customer
            ORDER BY day, d.customer
        """)

        # Available filter options
        customers_list = _query("""
            SELECT DISTINCT customer FROM dbo.devices ORDER BY customer
        """)
        machines_list = _query("""
            SELECT DISTINCT machine_name, customer, location FROM dbo.devices ORDER BY customer, machine_name
        """)

        return {
            "date_from": date_from,
            "date_to": date_to,
            "daily": daily,
            "fleetDaily": fleet_daily,
            "machineSummary": machine_summary,
            "customerDaily": customer_daily,
            "customers": [r["customer"] for r in customers_list],
            "machines": machines_list,
        }

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _run)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return JSONResponse(content=data)


# ---------------------------------------------------------------------------
# Static (frontend)
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory="/app/static"), name="static")

@app.get("/")
async def index():
    return FileResponse(
        "/app/static/index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
