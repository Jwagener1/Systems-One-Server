"""
Lightweight HTTP health endpoint served from a daemon thread.
GET /health or /healthz → JSON body, 200 OK or 503 Service Unavailable.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

logger = logging.getLogger(__name__)


class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal request handler — no routing framework needed."""

    # Injected by HealthServer
    get_status: Callable[[], dict]

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in ("/health", "/healthz"):
            self.send_response(404)
            self.end_headers()
            return

        status = self.get_status()
        is_healthy: bool = status.get("healthy", False)
        code = 200 if is_healthy else 503
        body = json.dumps(status, default=str).encode("utf-8")

        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802
        # Suppress default stderr output from BaseHTTPRequestHandler
        pass


class HealthServer:
    def __init__(self, port: int, get_status: Callable[[], dict]) -> None:
        self._port = port
        self._get_status = get_status
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler_cls = type(
            "_BoundHealthHandler",
            (_HealthHandler,),
            {"get_status": staticmethod(self._get_status)},
        )
        self._server = HTTPServer(("", self._port), handler_cls)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="health-server",
        )
        self._thread.start()
        logger.info("Health server started", extra={"port": self._port})

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
        logger.info("Health server stopped")
