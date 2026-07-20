from __future__ import annotations

import os
import time

from .db_client import MSSQLConnector, mssql_params_from_env
from .env import try_load_dotenv
from .main import main as ingest_main


def _try_load_dotenv() -> None:
    try_load_dotenv(override=False)


def _get_count_and_max_id() -> tuple[int, int | None]:
    conn = MSSQLConnector(mssql_params_from_env()).connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1), MAX(id) FROM dbo.device_statistics")
        row = cur.fetchone()
        if not row:
            return 0, None
        return int(row[0] or 0), (int(row[1]) if row[1] is not None else None)
    finally:
        conn.close()


def _get_new_rows_since_id(
    prev_max_id: int | None,
) -> list[tuple[int, int, int, object]]:
    """Return a sample of newly inserted rows.

    Tuple: (id, device_id, ts_epoch, created_at)
    """

    if prev_max_id is None:
        prev_max_id = 0

    conn = MSSQLConnector(mssql_params_from_env()).connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP 50 id, device_id, ts_epoch, created_at
            FROM dbo.device_statistics
            WHERE id > ?
            ORDER BY id ASC
            """,
            (int(prev_max_id),),
        )
        rows = cur.fetchall()
        # pyodbc returns Row objects; normalize to plain tuples for stable printing/typing.
        return [(int(r[0]), int(r[1]), int(r[2]), r[3]) for r in rows]
    finally:
        conn.close()


def main() -> int:
    _try_load_dotenv()

    # Default to 16 minutes unless overridden
    seconds = int(os.getenv("RUN_SECONDS", "960") or "960")

    before_cnt, before_max = _get_count_and_max_id()
    print(f"[diag] before: cnt={before_cnt} max_id={before_max}")

    start = time.time()
    os.environ["RUN_SECONDS"] = str(seconds)

    # Ensure DB writes are enabled while running diagnostics.
    os.environ.setdefault("DB_ENABLE", "true")

    rc = ingest_main()
    elapsed = int(time.time() - start)

    after_cnt, after_max = _get_count_and_max_id()
    print(f"[diag] after:  cnt={after_cnt} max_id={after_max} (elapsed={elapsed}s)")

    new_rows = _get_new_rows_since_id(before_max)
    print(f"[diag] inserted: {len(new_rows)} rows (id > {before_max})")
    if new_rows:
        print("[diag] sample new rows (id, device_id, ts_epoch, created_at):")
        for r in new_rows[:10]:
            print("  ", r)

    return int(rc)


if __name__ == "__main__":
    raise SystemExit(main())
