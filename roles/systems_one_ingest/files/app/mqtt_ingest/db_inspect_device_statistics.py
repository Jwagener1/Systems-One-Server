from __future__ import annotations

import logging

from .db_client import MSSQLConnector, mssql_params_from_env
from .env import try_load_dotenv


def _try_load_dotenv() -> None:
    try_load_dotenv(override=False)


def main() -> int:
    _try_load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s - %(message)s"
    )

    conn = MSSQLConnector(mssql_params_from_env()).connect()
    try:
        cur = conn.cursor()

        print("--- triggers on dbo.device_statistics ---")
        cur.execute(
            """
            SELECT name, is_instead_of_trigger, is_disabled
            FROM sys.triggers
            WHERE parent_id = OBJECT_ID('dbo.device_statistics')
            """
        )
        print(cur.fetchall())

        print("--- unique indexes on dbo.device_statistics ---")
        cur.execute(
            """
            SELECT name, is_unique, is_primary_key
            FROM sys.indexes
            WHERE object_id = OBJECT_ID('dbo.device_statistics') AND index_id > 0
            """
        )
        print(cur.fetchall())

        print("--- unique index columns ---")
        cur.execute(
            """
            SELECT i.name, c.name
            FROM sys.indexes i
            JOIN sys.index_columns ic
              ON ic.object_id=i.object_id AND ic.index_id=i.index_id
            JOIN sys.columns c
              ON c.object_id=ic.object_id AND c.column_id=ic.column_id
            WHERE i.object_id=OBJECT_ID('dbo.device_statistics') AND i.is_unique=1
            ORDER BY i.name, ic.key_ordinal
            """
        )
        print(cur.fetchall())

        print("--- count/max ---")
        cur.execute(
            "SELECT COUNT(1), MAX(id), MAX(created_at) FROM dbo.device_statistics"
        )
        print(cur.fetchone())

        print("--- latest 10 ---")
        cur.execute(
            "SELECT TOP 10 id, device_id, ts_epoch, created_at FROM dbo.device_statistics ORDER BY id DESC"
        )
        print(cur.fetchall())

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
