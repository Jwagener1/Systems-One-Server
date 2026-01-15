#!/usr/bin/env python3

import argparse
import json
import os
import re
import sys
from urllib.parse import urljoin

import requests


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_") or "dashboard"


def _session(url: str, token: str | None, username: str | None, password: str | None, verify: bool) -> requests.Session:
    sess = requests.Session()
    sess.verify = verify
    sess.headers.update({"Accept": "application/json"})

    if token:
        sess.headers.update({"Authorization": f"Bearer {token}"})
    elif username and password:
        sess.auth = (username, password)
    else:
        raise SystemExit("Provide either --token or --username/--password")

    # Normalize trailing slash
    sess.base_url = url.rstrip("/") + "/"  # type: ignore[attr-defined]
    return sess


def api_get(sess: requests.Session, path: str, params: dict | None = None) -> dict:
    url = urljoin(sess.base_url, path.lstrip("/"))  # type: ignore[attr-defined]
    resp = sess.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Export Grafana dashboards (current org) to JSON files suitable for file provisioning."
    )
    ap.add_argument("--url", required=True, help="Grafana base URL, e.g. http://127.0.0.1:3000")
    ap.add_argument("--token", help="Grafana API token (recommended)")
    ap.add_argument("--username", help="Grafana username (basic auth)")
    ap.add_argument("--password", help="Grafana password (basic auth)")
    ap.add_argument("--insecure", action="store_true", help="Disable TLS verification")
    ap.add_argument(
        "--out-dir",
        default="roles/grafana/files/dashboards",
        help="Output directory for dashboard JSON files",
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files",
    )

    args = ap.parse_args()

    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)

    sess = _session(args.url, args.token, args.username, args.password, verify=not args.insecure)

    # Search dashboards in current org
    search = api_get(sess, "/api/search", params={"type": "dash-db", "limit": 5000})

    count = 0
    for item in search:
        uid = item.get("uid")
        title = item.get("title") or uid or "dashboard"
        if not uid:
            continue

        payload = api_get(sess, f"/api/dashboards/uid/{uid}")
        dashboard = payload.get("dashboard")
        if not isinstance(dashboard, dict):
            continue

        # Make it safer for provisioning/import by removing instance-specific fields
        dashboard.pop("id", None)
        dashboard.pop("version", None)

        filename = f"{_slugify(title)}__{uid}.json"
        path = os.path.join(out_dir, filename)

        if os.path.exists(path) and not args.overwrite:
            continue

        with open(path, "w", encoding="utf-8") as f:
            json.dump(dashboard, f, indent=2, sort_keys=True)
            f.write("\n")

        count += 1

    print(f"Exported {count} dashboards to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
