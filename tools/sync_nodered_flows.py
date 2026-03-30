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
# Grafana dashboards live in a separate repo: https://github.com/Jwagener1/grafana
# The export script writes to this path; commit and push that repo separately.
DASHBOARDS_DEST = REPO_ROOT / "roles" / "grafana" / "files" / "dashboards"
REMOTE_FLOWS_PATH = "/opt/nodered/data/flows.json"
GRAFANA_REPO_URL = "https://github.com/Jwagener1/grafana"
# Default GitHub token — store in env var GITHUB_TOKEN or pass via --github-token
DEFAULT_GITHUB_TOKEN = "{{ vault_grafana_git_sync_token }}"


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


def pull_grafana_dashboards(grafana_url: str, grafana_user: str, grafana_password: str,
                            github_token: str | None = None):
    """Pull dashboards from Grafana and push them to the dedicated grafana repo."""
    import tempfile, os, shutil

    print("\n[Grafana] Pulling dashboards via Grafana API...")
    export_script = REPO_ROOT / "tools" / "grafana_export_dashboards.py"

    # Clone the grafana repo into a temp dir
    token = github_token or os.environ.get("GITHUB_TOKEN") or DEFAULT_GITHUB_TOKEN
    auth_url = GRAFANA_REPO_URL.replace("https://", f"https://{token}@")

    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"  Cloning {GRAFANA_REPO_URL} ...")
        run(["git", "clone", "--depth=1", auth_url, tmpdir], capture_output=True)

        out_dir = Path(tmpdir) / "grafana"
        out_dir.mkdir(parents=True, exist_ok=True)

        run([
            sys.executable, str(export_script),
            "--url", grafana_url,
            "--username", grafana_user,
            "--password", grafana_password,
            "--out-dir", str(out_dir),
            "--overwrite",
        ])

        # Commit and push back to grafana repo
        run(["git", "-C", tmpdir, "config", "user.name", "Systems-One S1"], capture_output=True)
        run(["git", "-C", tmpdir, "config", "user.email", "s1@systems-one"], capture_output=True)
        run(["git", "-C", tmpdir, "add", "grafana/"], capture_output=True)

        diff = subprocess.run(["git", "-C", tmpdir, "diff", "--cached", "--quiet"],
                              capture_output=True)
        if diff.returncode == 0:
            print("  Nothing changed in dashboards — skipping push.")
        else:
            run(["git", "-C", tmpdir, "commit",
                 "-m", "chore: sync dashboards from live Grafana"], capture_output=True)
            run(["git", "-C", tmpdir, "push"], capture_output=True)
            print(f"  Done: dashboards pushed to {GRAFANA_REPO_URL}")


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
    parser.add_argument("--github-token", default=None,
                        help="GitHub PAT for pushing to Jwagener1/grafana (falls back to GITHUB_TOKEN env var)")
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
        pull_grafana_dashboards(args.grafana_url, args.grafana_user, args.grafana_password,
                                github_token=args.github_token)

    if args.commit:
        git_commit()

    print("\nDone. Run 'ansible-playbook site.yml' to push changes back to the server.")


if __name__ == "__main__":
    main()
