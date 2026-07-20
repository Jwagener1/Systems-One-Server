import glob
import os

import yaml

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _load(relpath):
    with open(os.path.join(ROLE, relpath), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_role_yaml_parses():
    files = []
    for sub in ("defaults", "tasks", "handlers"):
        files += glob.glob(os.path.join(ROLE, sub, "*.yml"))
    assert len(files) >= 3
    for f in files:
        with open(f, encoding="utf-8") as fh:
            assert yaml.safe_load(fh) is not None, f


def test_defaults_pin_side_by_side_port_and_loopback():
    d = _load("defaults/main.yml")
    assert d["scan_fleet_dashboard_port"] == 8091          # NOT 8090 (marketing_display)
    assert d["scan_fleet_dashboard_bind_address"] == "127.0.0.1"
    assert d["scan_fleet_dashboard_dir"] == "/opt/scan-fleet-dashboard"


def test_webservers_playbook_includes_tagged_role():
    with open(os.path.join(ROLE, "..", "..", "webservers.yml"), encoding="utf-8") as fh:
        play = yaml.safe_load(fh)[0]
    entry = [r for r in play["roles"] if isinstance(r, dict) and r.get("role") == "scan_fleet_dashboard"]
    assert entry and "scan_fleet_dashboard" in entry[0]["tags"]
