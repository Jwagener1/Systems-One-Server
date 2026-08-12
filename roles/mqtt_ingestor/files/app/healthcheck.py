"""
Docker HEALTHCHECK probe.

Hits the /health endpoint and exits:
  0  — HTTP 200 (healthy)
  1  — any non-200 response or connection failure
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

HEALTH_URL = "http://localhost:8080/health"
TIMEOUT_SEC = 4


def main() -> None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=TIMEOUT_SEC) as resp:
            sys.exit(0 if resp.status == 200 else 1)
    except urllib.error.HTTPError as exc:
        # HTTPError is raised for 4xx/5xx; treat anything non-200 as unhealthy
        sys.exit(0 if exc.code == 200 else 1)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
