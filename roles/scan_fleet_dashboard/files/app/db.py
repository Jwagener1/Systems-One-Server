"""Single DB access point. pyodbc is imported lazily so unit tests (and a
static-only dev server) run on machines without the ODBC stack."""
import datetime
import decimal

import config

CONN_STR = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER={config.DB_HOST},{config.DB_PORT};"
    f"DATABASE={config.DB_NAME};"
    f"UID={config.DB_USER};"
    f"PWD={config.DB_PASS};"
    "Encrypt=optional;"
    "TrustServerCertificate=yes;"
    "Connection Timeout=5;"
)


def _coerce(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    return v


def query(sql: str, params: tuple = ()) -> list[dict]:
    import pyodbc

    with pyodbc.connect(CONN_STR, timeout=8) as conn:
        cur = conn.cursor()
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        cols = [c[0] for c in cur.description]
        return [{k: _coerce(v) for k, v in zip(cols, row)} for row in cur.fetchall()]
