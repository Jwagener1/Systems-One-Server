"""
Systems One — Scan Fleet Monitoring Dashboard
=============================================
Fleet monitoring web app over S1_Remote_Monitoring (read-only).
Runs side-by-side with marketing_display until cutover.
"""
from fastapi import FastAPI

app = FastAPI(title="S1 Scan Fleet Dashboard")


@app.get("/health")
async def health():
    return {"status": "ok"}
