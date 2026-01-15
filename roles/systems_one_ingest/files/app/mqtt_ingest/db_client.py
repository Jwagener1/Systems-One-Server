from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MSSQLConnectionParams:
    host: str
    port: int = 1433
    database: str = "master"
    user: Optional[str] = None
    password: Optional[str] = None

    # ODBC driver name installed on the machine.
    # Common values on Windows: "ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server".
    driver: str = "ODBC Driver 18 for SQL Server"

    # TLS/encryption behavior.
    # - "required" / true-ish: Encrypt=yes
    # - "optional": Encrypt=yes + TrustServerCertificate=yes (works in many dev setups)
    # - "false" / "no": Encrypt=no
    encrypt: str = "optional"
    trust_server_certificate: Optional[bool] = None

    connect_timeout_seconds: int = 5


class MSSQLConnector:
    def __init__(self, params: MSSQLConnectionParams) -> None:
        self._logger = logging.getLogger("mqtt_ingest.mssql")
        self._params = params

    def connect(self):
        """Create and return a live pyodbc connection.

        Raises a helpful error if pyodbc isn't installed.
        """

        try:
            import pyodbc  # type: ignore
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "pyodbc is not installed. Add it to requirements and install dependencies."
            ) from exc

        conn_str = self._build_connection_string()

        self._logger.info(
            "Connecting to MSSQL %s:%s db=%s driver=%s",
            self._params.host,
            self._params.port,
            self._params.database,
            self._params.driver,
        )

        # autocommit=True is convenient for simple health checks.
        return pyodbc.connect(
            conn_str, timeout=self._params.connect_timeout_seconds, autocommit=True
        )

    def test_connection(self) -> None:
        """Connect and run a simple query (SELECT 1)."""

        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        self._logger.info("MSSQL connection test succeeded")

    def _build_connection_string(self) -> str:
        p = self._params

        server = f"{p.host},{int(p.port)}" if p.port else p.host

        encrypt_mode = (p.encrypt or "optional").strip().lower()
        if encrypt_mode in {"1", "true", "yes", "y", "on", "required"}:
            encrypt = "yes"
            trust = (
                "no"
                if p.trust_server_certificate is None
                else ("yes" if p.trust_server_certificate else "no")
            )
        elif encrypt_mode in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            encrypt = "no"
            trust = (
                "no"
                if p.trust_server_certificate is None
                else ("yes" if p.trust_server_certificate else "no")
            )
        else:
            # "optional" (best-effort): use encryption but allow self-signed certs.
            encrypt = "yes"
            if p.trust_server_certificate is None:
                trust = "yes"
            else:
                trust = "yes" if p.trust_server_certificate else "no"

        parts: list[str] = [
            f"DRIVER={{{p.driver}}}",
            f"SERVER={server}",
            f"DATABASE={p.database}",
            f"Encrypt={encrypt}",
            f"TrustServerCertificate={trust}",
        ]

        if p.user:
            parts.append(f"UID={p.user}")
            parts.append(f"PWD={p.password or ''}")
        else:
            # Windows integrated auth
            parts.append("Trusted_Connection=yes")

        return ";".join(parts) + ";"


def mssql_params_from_env() -> MSSQLConnectionParams:
    def _get(name: str, default: str = "") -> str:
        return os.getenv(name, default).strip()

    host = _get("DB_HOST", "")
    if not host:
        # Keep it explicit: caller can decide how to behave.
        host = "localhost"

    port_raw = _get("DB_PORT", "1433")
    try:
        port = int(port_raw)
    except ValueError:
        port = 1433

    database = _get("DB_NAME", "master") or "master"
    user = _get("DB_USER") or None
    password = _get("DB_PASSWORD") or None

    encrypt = _get("DB_ENCRYPT", "optional") or "optional"

    trust_raw = _get("DB_TRUST_SERVER_CERT", "")
    trust_server_certificate: Optional[bool]
    if trust_raw:
        trust_server_certificate = trust_raw.lower() in {"1", "true", "yes", "y", "on"}
    else:
        trust_server_certificate = None

    driver = (
        _get("DB_DRIVER", "ODBC Driver 18 for SQL Server")
        or "ODBC Driver 18 for SQL Server"
    )

    timeout_raw = _get("DB_CONNECT_TIMEOUT", "5")
    try:
        connect_timeout_seconds = int(timeout_raw)
    except ValueError:
        connect_timeout_seconds = 5

    return MSSQLConnectionParams(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        driver=driver,
        encrypt=encrypt,
        trust_server_certificate=trust_server_certificate,
        connect_timeout_seconds=connect_timeout_seconds,
    )
