"""
Systems One — Scan Fleet Monitoring Dashboard
=============================================
Fleet monitoring web app over S1_Remote_Monitoring (read-only).
Runs side-by-side with marketing_display until cutover.
"""
import asyncio
import datetime
import time

from fastapi import FastAPI, HTTPException, Request

import auth
import config
import db
import perf
import throughput
import timeutil

app = FastAPI(title="S1 Scan Fleet Dashboard")


async def _exec(fn):
    """Run a builder in a thread; map errors to HTTP codes."""
    try:
        return await asyncio.get_event_loop().run_in_executor(None, fn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


def _allowed(request: Request):
    return auth.allowed_customers(db.query, auth.resolve_user(request))


def _default_range(days: int):
    t = timeutil.today_local()
    return (t - datetime.timedelta(days=days - 1)).isoformat(), t.isoformat()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/customers")
async def api_customers(request: Request):
    allowed = _allowed(request)

    def run():
        rows = db.query("SELECT DISTINCT customer FROM dbo.devices ORDER BY customer")
        return [r["customer"] for r in rows
                if allowed is None or r["customer"] in allowed]

    return await _exec(run)


@app.get("/api/machines")
async def api_machines(request: Request, customer: str = "",
                       date_from: str = "", date_to: str = ""):
    allowed = _allowed(request)
    if not (date_from and date_to):
        date_from, date_to = _default_range(30)
    return await _exec(lambda: perf.build_machines(
        db.query, date_from, date_to, customer or None, allowed))


@app.get("/api/performance")
async def api_performance(request: Request, customer: str = "",
                          date_from: str = "", date_to: str = ""):
    allowed = _allowed(request)
    if not (date_from and date_to):
        date_from, date_to = _default_range(30)
    return await _exec(lambda: perf.build_performance(
        db.query, date_from, date_to, customer or None, allowed))


# ---------------------------------------------------------------------------
# Throughput (spec §7). KPI responses cached CACHE_TTL seconds (spec §9).
# ---------------------------------------------------------------------------
_kpi_cache: dict = {}


@app.get("/api/throughput/kpis")
async def api_throughput_kpis(request: Request, customer: str = "",
                              date_from: str = "", date_to: str = ""):
    allowed = _allowed(request)
    if not (date_from and date_to):
        date_from, date_to = _default_range(14)
    key = (customer, date_from, date_to,
           tuple(sorted(allowed)) if allowed is not None else None)
    hit = _kpi_cache.get(key)
    if hit and time.monotonic() - hit[0] < config.CACHE_TTL:
        return hit[1]
    data = await _exec(lambda: throughput.build_kpis(
        db.query, date_from, date_to, customer or None, allowed))
    _kpi_cache[key] = (time.monotonic(), data)
    return data


@app.get("/api/throughput/intraday")
async def api_throughput_intraday(request: Request, device_id: int = 0,
                                  date: str = "", customer: str = ""):
    allowed = _allowed(request)
    return await _exec(lambda: throughput.build_intraday(
        db.query, device_id or None, date or None, customer or None, allowed))


@app.get("/api/throughput/by-machine")
async def api_throughput_by_machine(request: Request, customer: str = "",
                                    date_from: str = "", date_to: str = ""):
    allowed = _allowed(request)
    if not (date_from and date_to):
        date_from, date_to = _default_range(14)
    return await _exec(lambda: throughput.build_by_machine(
        db.query, date_from, date_to, customer or None, allowed))
