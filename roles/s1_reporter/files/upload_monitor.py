#!/usr/bin/env python3
"""
S1 Upload Monitor
Detects devices where not_sent has been non-zero across consecutive packets,
indicating the upload service is failing to send data.

Run every 20 min alongside the offline checker.
Usage: python3 upload_monitor.py [check|test]
"""

import sys, os, json, smtplib, pymssql
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── Config (mirrors report.py env loading) ────────────────────────────────────
def load_config():
    keys = ["DB_HOST","DB_PORT","DB_USER","DB_PASS","DB_NAME",
            "SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASS","REPORT_TO"]
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

CSS = """<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
body,html{margin:0;padding:0;background:#f3f4f6;}
*{box-sizing:border-box;}
body{font-family:Inter,'Segoe UI',Arial,sans-serif;background:#f3f4f6;color:#1f2937;}
.wrap{max-width:860px;margin:0 auto;padding:28px 16px;}
.hdr{background:#fff;border:1px solid #e5e7eb;border-top:4px solid #d97706;border-radius:10px;padding:24px 28px;margin-bottom:20px;}
.hdr-top{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;}
.hdr h1{margin:0;font-size:21px;font-weight:700;color:#111827;}
.badge{background:#d97706;color:#fff;padding:3px 12px;border-radius:20px;font-size:12px;font-weight:600;}
.meta{color:#6b7280;font-size:13px;margin-top:5px;}
.sec{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:18px 20px;margin-bottom:14px;}
.sec h3{margin:0 0 14px;font-size:12px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.7px;border-bottom:1px solid #f3f4f6;padding-bottom:8px;}
.alert-row{display:flex;align-items:flex-start;gap:10px;padding:10px 14px;border-radius:7px;margin-bottom:7px;font-size:13px;line-height:1.5;}
.alert-warn{background:#fffbeb;border:1px solid #fed7aa;border-left:4px solid #d97706;}
.alert-ok{background:#f0fdf4;border:1px solid #bbf7d0;border-left:4px solid #16a34a;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{background:#f9fafb;color:#6b7280;text-align:left;padding:9px 12px;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;border-bottom:2px solid #e5e7eb;}
td{padding:9px 12px;border-bottom:1px solid #f3f4f6;color:#1f2937;}
tr:last-child td{border-bottom:none;}
tr:hover td{background:#f9fafb;}
.footer{text-align:center;color:#9ca3af;font-size:11px;padding-top:18px;border-top:1px solid #e5e7eb;margin-top:8px;}
</style>"""

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

# ── Email builders ────────────────────────────────────────────────────────────
def build_upload_alert_email(failing_devices):
    ts          = datetime.now().strftime("%Y-%m-%d %H:%M")
    today_label = datetime.now().strftime("%A, %d %B %Y")
    count       = len(failing_devices)

    rows_html = ""
    for d in failing_devices:
        latest = d["latest_ts"]
        if isinstance(latest, str):
            latest = datetime.fromisoformat(latest)
        # Build mini packet table
        packet_detail = ", ".join(
            f"[{p['ts_datetime'].strftime('%H:%M') if hasattr(p['ts_datetime'],'strftime') else str(p['ts_datetime'])[:16]}: {p['not_sent']} unsent]"
            for p in reversed(d["packets"])
        )
        rows_html += f"""<tr>
            <td><b>{d['machine_name']}</b></td>
            <td>{d['location']}</td>
            <td>{d['customer']}</td>
            <td style="text-align:right">
              <span style="background:#fef3c7;color:#92400e;padding:2px 9px;border-radius:12px;font-size:12px;font-weight:600">
                {d['total_not_sent']:,} items
              </span>
            </td>
            <td style="font-size:11px;color:#6b7280">{packet_detail}</td>
        </tr>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">{CSS}</head>
<body>
<table width="100%" cellpadding="0" cellspacing="0" bgcolor="#f3f4f6">
<tr><td align="center">
<div class="wrap">
  <div class="hdr">
    <div class="hdr-top">
      <h1>⚙️ S1 — Upload Failure Alert</h1>
      <span class="badge">UPLOAD ALERT</span>
    </div>
    <div class="meta">{today_label} &nbsp;·&nbsp; Generated by Systems-One &nbsp;·&nbsp; {ts} SAST</div>
  </div>
  <div class="sec">
    <h3>⚠️ Upload Backlog Detected ({count} device{"s" if count != 1 else ""})</h3>
    <div class="alert-row alert-warn" style="margin-bottom:14px;">
      <span style="font-size:16px;margin-top:1px">⚠️</span>
      <span>
        <b>{count} device{"s" if count != 1 else ""}</b> {"have" if count != 1 else "has"} reported
        <b>not_sent &gt; 0</b> across {CONSECUTIVE_THRESHOLD} or more consecutive packets.
        The upload service on these devices may not be functioning correctly.
        Items are being scanned but not transmitted to the server.
      </span>
    </div>
    <table>
      <tr>
        <th>Device</th><th>Location</th><th>Customer</th>
        <th style="text-align:right">Unsent Items</th>
        <th>Recent Packets</th>
      </tr>
      {rows_html}
    </table>
  </div>
  <div class="footer">S1 Remote Monitoring &nbsp;·&nbsp; systems-one.com &nbsp;·&nbsp; {ts} SAST</div>
</div>
</td></tr></table>
</body></html>"""

    subject = f"⚠️ S1 Upload Alert — {count} Device{'s' if count != 1 else ''} Not Uploading — {datetime.now().strftime('%H:%M')} SAST"
    return html, subject

def build_upload_recovery_email(recovered_devices):
    ts          = datetime.now().strftime("%Y-%m-%d %H:%M")
    today_label = datetime.now().strftime("%A, %d %B %Y")
    count       = len(recovered_devices)

    rows_html = ""
    for d in recovered_devices:
        alerted_at = d.get("alerted_at", "Unknown")
        if isinstance(alerted_at, str) and "T" in alerted_at:
            try:
                alerted_at = datetime.fromisoformat(alerted_at).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
        rows_html += f"""<tr>
            <td><b>{d['machine_name']}</b></td>
            <td>{d['location']}</td>
            <td>{d['customer']}</td>
            <td>{alerted_at} SAST</td>
            <td><span style="background:#dcfce7;color:#166534;padding:2px 9px;border-radius:12px;font-size:12px;font-weight:600">✅ Uploading</span></td>
        </tr>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">{CSS}</head>
<body>
<table width="100%" cellpadding="0" cellspacing="0" bgcolor="#f3f4f6">
<tr><td align="center">
<div class="wrap">
  <div class="hdr">
    <div class="hdr-top">
      <h1>⚙️ S1 — Upload Recovered</h1>
      <span class="badge" style="background:#16a34a">RECOVERED</span>
    </div>
    <div class="meta">{today_label} &nbsp;·&nbsp; Generated by Systems-One &nbsp;·&nbsp; {ts} SAST</div>
  </div>
  <div class="sec">
    <h3>✅ Upload Backlog Cleared ({count} device{"s" if count != 1 else ""})</h3>
    <div class="alert-row alert-ok" style="margin-bottom:14px;">
      <span style="font-size:16px;margin-top:1px">✅</span>
      <span>
        <b>{count} device{"s" if count != 1 else ""}</b> {"are" if count != 1 else "is"} now uploading data normally.
      </span>
    </div>
    <table>
      <tr><th>Device</th><th>Location</th><th>Customer</th><th>Alert Raised At</th><th>Status</th></tr>
      {rows_html}
    </table>
  </div>
  <div class="footer">S1 Remote Monitoring &nbsp;·&nbsp; systems-one.com &nbsp;·&nbsp; {ts} SAST</div>
</div>
</td></tr></table>
</body></html>"""

    subject = f"✅ S1 Upload Recovered — {count} Device{'s' if count != 1 else ''} Back Online — {datetime.now().strftime('%H:%M')} SAST"
    return html, subject

# ── Send email ────────────────────────────────────────────────────────────────
def send_email(subject, html_body):
    msg             = MIMEMultipart("alternative")
    msg["Subject"]  = subject
    msg["From"]     = f"S1 Reports <{CFG['SMTP_USER']}>"
    msg["To"]       = CFG["REPORT_TO"]
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(CFG["SMTP_HOST"], int(CFG["SMTP_PORT"])) as s:
        s.ehlo(); s.starttls()
        s.login(CFG["SMTP_USER"], CFG["SMTP_PASS"])
        s.sendmail(CFG["SMTP_USER"], CFG["REPORT_TO"].split(","), msg.as_string())
    print(f"✅ Sent: {subject}")

# ── Main ──────────────────────────────────────────────────────────────────────
def run_check(force=False):
    currently_failing = detect_upload_failures()
    newly_failing, recovered, still_failing = diff_upload_state(currently_failing)

    if force and currently_failing:
        html, subject = build_upload_alert_email(currently_failing)
        send_email(subject, html)
        print(f"⚠️ [FORCED] Upload alert sent for {len(currently_failing)} device(s)")
        return

    if newly_failing:
        html, subject = build_upload_alert_email(newly_failing)
        send_email(subject, html)
        keys = [d['machine_name']+'@'+d['location'] for d in newly_failing]
        print(f"⚠️ Upload alert sent for {len(newly_failing)} device(s): {keys}")

    if recovered:
        html, subject = build_upload_recovery_email(recovered)
        send_email(subject, html)
        print(f"✅ Upload recovery sent for {len(recovered)} device(s): {[d['machine_name']+'@'+d['location'] for d in recovered]}")

    if still_failing:
        keys = [d['machine_name']+'@'+d['location'] for d in still_failing]
        print(f"⏳ Still failing upload (no re-alert): {keys}")

    if not newly_failing and not recovered and not still_failing:
        print("✅ All devices uploading normally.")

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "test":
        # Force-send a test email using current data (even if already alerted)
        run_check(force=True)
    elif mode == "check":
        run_check()
    else:
        print("Usage: upload_monitor.py [check|test]")
        sys.exit(1)