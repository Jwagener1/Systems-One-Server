"""
Systems One — Marketing Display
================================
Read-only API that surfaces anonymised aggregate metrics from S1_Remote_Monitoring.
No customer names, locations, or serial numbers are exposed.
"""

import os
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

import pyodbc
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

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
# Simple in-process cache
# ---------------------------------------------------------------------------
_cache: dict[str, Any] = {}
_cache_ts: float = 0.0
_cache_lock = asyncio.Lock()


def _query(sql: str) -> list[dict]:
    with pyodbc.connect(CONN_STR, timeout=8) as conn:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


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
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # pre-warm cache in background so first request is fast
    try:
        data = await asyncio.get_event_loop().run_in_executor(None, _build_stats)
        _cache.update(data)
        _cache_ts = time.monotonic()
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
# API
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


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Static (frontend)
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory="/app/static"), name="static")

@app.get("/")
async def index():
    return FileResponse("/app/static/index.html")
