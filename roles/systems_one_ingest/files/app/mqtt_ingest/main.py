from __future__ import annotations

import logging
import os
import time
from dataclasses import replace

from .mqtt_client import MQTTConnector, params_from_env


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def main() -> int:
    log_level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    log_level = getattr(logging, log_level_name, logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    if _is_truthy(os.getenv("DB_CONNECT_ON_START", "false")):
        from .db_client import MSSQLConnector, mssql_params_from_env

        required = _is_truthy(os.getenv("DB_CONNECT_REQUIRED", "false"))
        try:
            MSSQLConnector(mssql_params_from_env()).test_connection()
        except Exception:
            logging.getLogger("mqtt_ingest").exception("MSSQL connection test failed")
            if required:
                raise

    device_store = None
    device_status_store = None
    device_os_status_store = None
    device_storage_status_store = None
    device_statistics_store = None
    if _is_truthy(os.getenv("DB_ENABLE", "false")):
        from .db_client import MSSQLConnector, mssql_params_from_env
        from .device_store import DeviceStore
        from .device_status_store import DeviceStatusStore
        from .device_os_status_store import DeviceOsStatusStore
        from .device_storage_status_store import DeviceStorageStatusStore
        from .device_statistics_store import DeviceStatisticsStore

        db_connector = MSSQLConnector(mssql_params_from_env())
        device_store = DeviceStore(db_connector)
        device_status_store = DeviceStatusStore(db_connector)
        device_os_status_store = DeviceOsStatusStore(db_connector)
        device_storage_status_store = DeviceStorageStatusStore(db_connector)
        device_statistics_store = DeviceStatisticsStore(db_connector)

    params = params_from_env()
    connector = MQTTConnector(
        params,
        device_store=device_store,
        device_status_store=device_status_store,
        device_os_status_store=device_os_status_store,
        device_storage_status_store=device_storage_status_store,
        device_statistics_store=device_statistics_store,
    )
    try:
        connector.connect(timeout_seconds=10.0)
    except TimeoutError:
        transport = (params.transport or "").lower()
        if params.port == 443 and ("ws" in transport) and not params.tls:
            logging.getLogger("mqtt_ingest").warning(
                "MQTT connect timed out; retrying with TLS enabled (wss on port 443)"
            )
            connector.disconnect()
            params_tls = replace(params, tls=True)
            connector = MQTTConnector(
                params_tls,
                device_store=device_store,
                device_status_store=device_status_store,
                device_os_status_store=device_os_status_store,
                device_storage_status_store=device_storage_status_store,
                device_statistics_store=device_statistics_store,
            )
            connector.connect(timeout_seconds=10.0)
        else:
            raise

    if params.ingest_topics:
        connector.subscribe(params.ingest_topics, qos=params.ingest_qos)
    else:
        logging.getLogger("mqtt_ingest").warning(
            "No INGEST_TOPICS configured; connected but not subscribed to any topics"
        )

    try:
        run_seconds_raw = os.getenv("RUN_SECONDS", "").strip()
        run_seconds: float | None
        if run_seconds_raw:
            try:
                run_seconds = float(run_seconds_raw)
            except ValueError:
                run_seconds = None
        else:
            run_seconds = None

        if run_seconds is not None and run_seconds > 0:
            deadline = time.monotonic() + run_seconds
            while time.monotonic() < deadline:
                time.sleep(0.2)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        logging.getLogger("mqtt_ingest").info("Shutting down...")
    finally:
        connector.disconnect()
        if device_store is not None:
            device_store.close()
        if device_status_store is not None:
            device_status_store.close()
        if device_os_status_store is not None:
            device_os_status_store.close()
        if device_storage_status_store is not None:
            device_storage_status_store.close()
        if device_statistics_store is not None:
            device_statistics_store.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
