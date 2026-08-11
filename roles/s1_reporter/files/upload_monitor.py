#!/usr/bin/env python3
"""
S1 Upload Monitor
Detects devices where not_sent has been non-zero across consecutive packets,
indicating the upload service is failing to send data.

Run every 20 min alongside the offline checker.
Usage: python3 upload_monitor.py [check|test]
"""

import sys, os, json, pymssql
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from teams_notifier import post_to_teams
from cards import build_upload_alert_card, build_upload_recovery_card

# ── Config (mirrors report.py env loading) ────────────────────────────────────
def load_config():
    keys = ["DB_HOST","DB_PORT","DB_USER","DB_PASS","DB_NAME","TEAMS_WEBHOOK_URL"]
    cfg = {k: os.environ[k] for k in keys if k in os.environ}
    env_path = os.path.join(os.path.dirname(__file__), "report.env")
    if len(cfg) < len(keys) and os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() not in cfg:
                        cfg[k.strip()] = v.strip()
    return cfg

CFG = load_config()
TEAMS_WEBHOOK_URL = CFG.get("TEAMS_WEBHOOK_URL", "")

# How many consecutive packets with not_sent > 0 before alerting
CONSECUTIVE_THRESHOLD = int(os.environ.get("UPLOAD_ALERT_CONSECUTIVE", "3"))
# Packets to inspect per device
LOOKBACK_PACKETS = int(os.environ.get("UPLOAD_LOOKBACK_PACKETS", "6"))
# State file path
UPLOAD_STATE_FILE = os.environ.get("UPLOAD_STATE_FILE",
    "/data/upload_state.json")
# Standby lines to exclude
EXCLUDE = [("DIM2", "JBH")]
EXCLUDE_SQL = " AND NOT (" + " OR ".join(
    f"(d.machine_name='{m}' AND d.location='{l}')" for m, l in EXCLUDE
) + ")"

# ── DB ────────────────────────────────────────────────────────────────────────
def get_conn():
    return pymssql.connect(
        server=CFG["DB_HOST"], port=int(CFG["DB_PORT"]),
        user=CFG["DB_USER"], password=CFG["DB_PASS"],
        database=CFG["DB_NAME"], timeout=30
    )

def query(sql):
    with get_conn() as conn:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(sql)
            return cur.fetchall()

# ── Detection ─────────────────────────────────────────────────────────────────
def get_recent_not_sent(lookback=LOOKBACK_PACKETS):
    """
    Returns last `lookback` packets per device with not_sent values.
    """
    return query(f"""
        SELECT machine_name, location, customer, ts_datetime,
               total_items, not_sent, data_sent, rn
        FROM (
            SELECT d.machine_name, d.location, d.customer,
                   ds.ts_datetime, ds.total_items, ds.not_sent, ds.data_sent,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.id ORDER BY ds.ts_datetime DESC
                   ) AS rn
            FROM dbo.devices d
            JOIN dbo.device_statistics ds ON ds.device_id = d.id
            WHERE 1=1 {EXCLUDE_SQL}
        ) x
        WHERE rn <= {lookback}
        ORDER BY machine_name, location, rn
    """)

def detect_upload_failures():
    """
    Returns list of devices where the last CONSECUTIVE_THRESHOLD packets
    all have not_sent > 0 AND total_items > 0 (only flag when actually scanning).
    """
    rows = get_recent_not_sent(LOOKBACK_PACKETS)

    # Group by device
    devices = {}
    for r in rows:
        key = f"{r['machine_name']}@{r['location']}"
        devices.setdefault(key, []).append(r)

    failing = []
    for key, packets in devices.items():
        # Sort oldest→newest (rn desc = newest first, so reverse)
        packets_sorted = sorted(packets, key=lambda x: x["rn"], reverse=True)
        # Take the most recent CONSECUTIVE_THRESHOLD packets
        recent = packets_sorted[:CONSECUTIVE_THRESHOLD]
        if len(recent) < CONSECUTIVE_THRESHOLD:
            continue
        # All must have total_items > 0 AND not_sent > 0
        if all((p["total_items"] or 0) > 0 and (p["not_sent"] or 0) > 0 for p in recent):
            total_not_sent = sum(int(p["not_sent"] or 0) for p in recent)
            latest_ts = max(p["ts_datetime"] for p in recent)
            failing.append({
                "machine_name":  packets[0]["machine_name"],
                "location":      packets[0]["location"],
                "customer":      packets[0]["customer"],
                "total_not_sent": total_not_sent,
                "consecutive":   CONSECUTIVE_THRESHOLD,
                "latest_ts":     latest_ts,
                "packets":       recent,
            })

    return sorted(failing, key=lambda x: x["total_not_sent"], reverse=True)

# ── State persistence ─────────────────────────────────────────────────────────
def load_upload_state():
    if os.path.exists(UPLOAD_STATE_FILE):
        try:
            with open(UPLOAD_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_upload_state(state):
    with open(UPLOAD_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)

def diff_upload_state(currently_failing):
    prev = load_upload_state()
    current_keys = {f"{d['machine_name']}@{d['location']}": d for d in currently_failing}

    newly_failing = [d for k, d in current_keys.items() if k not in prev]
    recovered     = [prev[k] for k in prev if k not in current_keys]
    still_failing = [d for k, d in current_keys.items() if k in prev]

    new_state = {}
    for k, d in current_keys.items():
        new_state[k] = {
            "machine_name":   d["machine_name"],
            "location":       d["location"],
            "customer":       d["customer"],
            "total_not_sent": d["total_not_sent"],
            "alerted_at":     prev[k]["alerted_at"] if k in prev else datetime.now().isoformat(),
            "latest_ts":      str(d["latest_ts"]),
        }
    save_upload_state(new_state)
    return newly_failing, recovered, still_failing

# ── Main ──────────────────────────────────────────────────────────────────────
def run_check(force=False):
    currently_failing = detect_upload_failures()
    newly_failing, recovered, still_failing = diff_upload_state(currently_failing)

    if force and currently_failing:
        card = build_upload_alert_card(currently_failing)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
        print(f"⚠️ [FORCED] Upload alert sent for {len(currently_failing)} device(s)")
        return

    if newly_failing:
        card = build_upload_alert_card(newly_failing)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
        keys = [d['machine_name']+'@'+d['location'] for d in newly_failing]
        print(f"⚠️ Upload alert sent for {len(newly_failing)} device(s): {keys}")

    if recovered:
        card = build_upload_recovery_card(recovered)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
        print(f"✅ Upload recovery sent for {len(recovered)} device(s): {[d['machine_name']+'@'+d['location'] for d in recovered]}")

    if still_failing:
        keys = [d['machine_name']+'@'+d['location'] for d in still_failing]
        print(f"⏳ Still failing upload (no re-alert): {keys}")

    if not newly_failing and not recovered and not still_failing:
        print("✅ All devices uploading normally.")

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "test":
        # Force-send a test Teams alert using current data (even if already alerted)
        run_check(force=True)
    elif mode == "check":
        run_check()
    else:
        print("Usage: upload_monitor.py [check|test]")
        sys.exit(1)