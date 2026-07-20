"""
Systems One — Scan Fleet Monitoring Dashboard
=============================================
Fleet monitoring web app over S1_Remote_Monitoring (read-only).
Runs side-by-side with marketing_display until cutover.
"""
import asyncio
import datetime

from fastapi import FastAPI, HTTPException, Request

import auth
import db
import perf
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
