#!/usr/bin/env python3
"""
sync_nodered_flows.py — Pull Node-RED flows.json and Grafana dashboards from
the live server back into this repo so Ansible picks them up on the next run.

Usage:
    python3 tools/sync_nodered_flows.py [--host HOST] [--user USER] [--key KEY]
                                        [--grafana-url URL] [--grafana-password PW]
                                        [--commit]

Examples:
    # Pull flows only (SSH)
    python3 tools/sync_nodered_flows.py --host 192.168.1.110 --user s1

    # Pull flows + Grafana dashboards and commit
    python3 tools/sync_nodered_flows.py \
        --host 192.168.1.110 --user s1 \
        --grafana-url http://127.0.0.1:3000 --grafana-password changeme \
        --commit
"""

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
FLOWS_DEST = REPO_ROOT / "roles" / "nodered" / "files" / "flows.json"
DASHBOARDS_DEST = REPO_ROOT / "roles" / "grafana" / "files" / "dashboards"
REMOTE_FLOWS_PATH = "/opt/nodered/data/flows.json"


def run(cmd, **kwargs):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, check=True, **kwargs)
    return result


def pull_nodered_flows(host: str, user: str, key: str | None):
    print("\n[Node-RED] Pulling flows.json from server...")
    scp_src = f"{user}@{host}:{REMOTE_FLOWS_PATH}"
    cmd = ["scp"]
    if key:
        cmd += ["-i", key]
    cmd += [scp_src, str(FLOWS_DEST)]
    run(cmd)
    print(f"  Done: {FLOWS_DEST.relative_to(REPO_ROOT)}")


def pull_grafana_dashboards(grafana_url: str, grafana_user: str, grafana_password: str):
    print("\n[Grafana] Pulling dashboards via export script...")
    export_script = REPO_ROOT / "tools" / "grafana_export_dashboards.py"
    DASHBOARDS_DEST.mkdir(parents=True, exist_ok=True)
    run([
        sys.executable, str(export_script),
        "--url", grafana_url,
        "--username", grafana_user,
        "--password", grafana_password,
        "--out-dir", str(DASHBOARDS_DEST),
        "--overwrite",
    ])
    print(f"  Done: {DASHBOARDS_DEST.relative_to(REPO_ROOT)}/")


def git_commit():
    print("\n[Git] Committing changes...")
    try:
        run(["git", "-C", str(REPO_ROOT), "add",
             str(FLOWS_DEST), str(DASHBOARDS_DEST)],
            capture_output=True)
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "diff", "--cached", "--quiet"],
            capture_output=True
        )
        if result.returncode == 0:
            print("  Nothing to commit - files unchanged.")
            return
        run(["git", "-C", str(REPO_ROOT), "commit",
             "-m", "chore: sync Node-RED flows and Grafana dashboards from live server"])
        print("  Committed.")
    except subprocess.CalledProcessError as e:
        print(f"  Git commit failed: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="192.168.1.110",
                        help="Target server IP/hostname (default: 192.168.1.110)")
    parser.add_argument("--user", default="s1",
                        help="SSH user (default: s1)")
    parser.add_argument("--key", default=None,
                        help="Path to SSH private key (uses ssh-agent/default if omitted)")
    parser.add_argument("--grafana-url", default=None,
                        help="Grafana URL e.g. http://127.0.0.1:3000 (omit to skip dashboards)")
    parser.add_argument("--grafana-user", default="admin",
                        help="Grafana admin user (default: admin)")
    parser.add_argument("--grafana-password", default=None,
                        help="Grafana admin password (required if --grafana-url set)")
    parser.add_argument("--commit", action="store_true",
                        help="Auto-commit pulled changes to git")

    args = parser.parse_args()

    print("=" * 60)
    print("  Systems-One-Server - Sync Live State to Repo")
    print("=" * 60)

    pull_nodered_flows(args.host, args.user, args.key)

    if args.grafana_url:
        if not args.grafana_password:
            print("Error: --grafana-password required when --grafana-url is set")
            sys.exit(1)
        pull_grafana_dashboards(args.grafana_url, args.grafana_user, args.grafana_password)

    if args.commit:
        git_commit()

    print("\nDone. Run 'ansible-playbook site.yml' to push changes back to the server.")


if __name__ == "__main__":
    main()
