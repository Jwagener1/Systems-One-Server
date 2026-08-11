#!/usr/bin/env python3
"""
S1 Device Performance Reporter — v4
Daily and monthly Teams Adaptive Card reports with hosted chart images, per customer.
Usage:
    python3 report.py daily
    python3 report.py monthly
"""

import sys, os, io, re, pymssql, json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from teams_notifier import post_to_teams
from cards import build_offline_alert_card, build_recovery_card, build_customer_section_card
from chart_store import save_chart, cleanup_old_charts

# ── Config ─────────────────────────────────────────────────────────────────────
ENV_PATH = os.path.join(os.path.dirname(__file__), "report.env")

def load_config():
    # Prefer environment variables (Docker), fall back to report.env for local runs
    env_keys = ["DB_HOST","DB_PORT","DB_USER","DB_PASS","DB_NAME",
                "TEAMS_WEBHOOK_URL",
                "OFFLINE_THRESHOLD_MINUTES"]
    cfg = {k: os.environ[k] for k in env_keys if k in os.environ}
    if len(cfg) < len(env_keys) and os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k not in cfg:
                        cfg[k.strip()] = v.strip()
    return cfg

CFG = load_config()
TEAMS_WEBHOOK_URL = CFG.get("TEAMS_WEBHOOK_URL", "")
CHART_DIR = os.environ.get("CHART_DIR", "/data/charts")
CHART_PUBLIC_BASE_URL = os.environ.get("CHART_PUBLIC_BASE_URL", "")
CHART_RETENTION_DAYS = int(os.environ.get("CHART_RETENTION_DAYS", "14"))

# ── Customer capability flags ──────────────────────────────────────────────────
# Controls which columns/charts/alerts are shown per customer.
CUSTOMER_CAPS = {
    # customer_name: {has_dimension, has_weight, has_hand_scan, exclude}
    "PEPKOR":   {"has_dimension": True,  "has_weight": False, "has_hand_scan": False, "exclude": [("DIM2", "JBH")]},
    "MADIBANA": {"has_dimension": True,  "has_weight": True,  "has_hand_scan": True,  "exclude": []},
    "PEP":      {"has_dimension": False, "has_weight": False, "has_hand_scan": False, "exclude": []},
    "SNOWSOFT": {"has_dimension": False, "has_weight": False, "has_hand_scan": False, "exclude": []},
}
# Default caps for unknown future customers
DEFAULT_CAPS = {"has_dimension": True, "has_weight": False, "has_hand_scan": False, "exclude": []}

def get_caps(customer):
    return CUSTOMER_CAPS.get(customer, DEFAULT_CAPS)

# Offline alert threshold — how many minutes since last packet before we consider a device offline.
# Devices send every 15 min; default adds 10 min tolerance = 25 min.
OFFLINE_THRESHOLD_MIN = int(CFG.get("OFFLINE_THRESHOLD_MINUTES", 60))

# Path to persist offline state between runs (so we can detect recoveries)
OFFLINE_STATE_FILE = os.environ.get("OFFLINE_STATE_FILE",
    os.path.join(os.path.dirname(__file__), "offline_state.json"))

# ── DB ─────────────────────────────────────────────────────────────────────────
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

# ── SQL helpers ────────────────────────────────────────────────────────────────
def exclude_sql(customer=None):
    """Returns an AND clause excluding standby lines for the given customer."""
    caps = get_caps(customer) if customer else DEFAULT_CAPS
    lines = caps.get("exclude", [])
    if not lines:
        return ""
    return " AND NOT (" + " OR ".join(
        f"(d.machine_name='{m}' AND d.location='{l}')" for m, l in lines
    ) + ")"

def get_customers():
    rows = query("SELECT DISTINCT customer FROM devices ORDER BY customer")
    return [r["customer"] for r in rows]

def get_daily_trend(days=7, customer=None):
    cust  = f" AND d.customer='{customer}'" if customer else ""
    excl  = exclude_sql(customer)
    return query(f"""
        SELECT d.machine_name, d.location, d.customer,
            CAST(ds.ts_datetime AS DATE) AS report_date,
            SUM(ds.total_items) AS daily_items,
            SUM(ds.good_read) AS daily_good,
            SUM(ds.no_read) AS daily_no_read,
            SUM(ds.no_dimension) AS daily_no_dim,
            SUM(ds.hand_scanned) AS daily_hand_scanned,
            SUM(ds.no_weight) AS daily_no_weight,
            CAST(SUM(ds.good_read)*100.0/NULLIF(SUM(ds.total_items),0) AS DECIMAL(5,1)) AS good_read_pct
        FROM devices d JOIN device_statistics ds ON ds.device_id=d.id
        WHERE ds.ts_datetime >= DATEADD(day,-{days},GETDATE()){excl}{cust}
        GROUP BY d.machine_name, d.location, d.customer, CAST(ds.ts_datetime AS DATE)
        ORDER BY d.location, d.machine_name, report_date
    """)

def get_hourly_pattern(days=7, customer=None):
    cust = f" AND d.customer='{customer}'" if customer else ""
    excl = exclude_sql(customer)
    return query(f"""
        SELECT DATEPART(HOUR,ds.ts_datetime) AS hour_of_day,
            SUM(ds.total_items) AS total_items,
            CAST(AVG(CAST(ds.good_read AS FLOAT)*100.0/NULLIF(ds.total_items,0)) AS DECIMAL(5,1)) AS avg_good_read_pct
        FROM device_statistics ds JOIN devices d ON d.id=ds.device_id
        WHERE ds.ts_datetime >= DATEADD(day,-{days},GETDATE()) AND ds.total_items > 0{excl}{cust}
        GROUP BY DATEPART(HOUR,ds.ts_datetime)
        ORDER BY hour_of_day
    """)

def get_device_summary(days=7, customer=None):
    cust = f" AND d.customer='{customer}'" if customer else ""
    excl = exclude_sql(customer)
    return query(f"""
        SELECT d.machine_name, d.location, d.customer,
            SUM(ds.total_items) AS total_items,
            SUM(ds.good_read) AS good_reads,
            SUM(ds.no_read) AS no_reads,
            SUM(ds.no_dimension) AS no_dimensions,
            SUM(ds.not_sent) AS not_sent,
            SUM(ds.hand_scanned) AS hand_scanned,
            SUM(ds.no_weight) AS no_weight,
            CAST(SUM(ds.good_read)*100.0/NULLIF(SUM(ds.total_items),0) AS DECIMAL(5,2)) AS good_read_pct,
            MAX(ds.ts_datetime) AS latest_report
        FROM devices d JOIN device_statistics ds ON ds.device_id=d.id
        WHERE ds.ts_datetime >= DATEADD(day,-{days},GETDATE()){excl}{cust}
        GROUP BY d.machine_name, d.location, d.customer
        ORDER BY total_items DESC
    """)

def get_storage(customer=None):
    cust = f" AND d.customer='{customer}'" if customer else ""
    return query(f"""
        SELECT d.machine_name, d.location, dss.drive, dss.total_gb, dss.used_gb, dss.usage_percent
        FROM devices d JOIN device_storage_status dss ON dss.device_id=d.id
        WHERE dss.drive='C:'{cust}
        ORDER BY dss.usage_percent DESC
    """)

def get_device_last_seen(customer=None):
    """
    Returns the last telemetry packet time per device from device_statistics.
    Uses actual packet timestamps — does NOT touch device_status which is unreliable.
    Excludes standby lines defined per customer (or globally when customer=None).
    """
    cust = f" AND d.customer='{customer}'" if customer else ""
    excl = exclude_sql(customer)
    return query(f"""
        SELECT d.machine_name, d.location, d.customer,
               MAX(ds.ts_datetime) AS last_seen,
               DATEDIFF(MINUTE, MAX(ds.ts_datetime), GETDATE()) AS minutes_ago
        FROM devices d JOIN device_statistics ds ON ds.device_id=d.id
        WHERE 1=1{excl}{cust}
        GROUP BY d.machine_name, d.location, d.customer
    """)

def detect_offline_devices(threshold_min=None, customer=None):
    """
    Returns devices whose last telemetry packet is older than threshold_min.
    Based purely on device_statistics timestamps — does not use device_status.
    """
    if threshold_min is None:
        threshold_min = OFFLINE_THRESHOLD_MIN
    rows = get_device_last_seen(customer)
    offline = []
    for r in rows:
        if r["last_seen"] is None:
            continue
        minutes_ago = int(r["minutes_ago"])
        if minutes_ago >= threshold_min:
            last = r["last_seen"]
            if isinstance(last, str):
                last = datetime.fromisoformat(last)
            offline.append({
                "machine_name": r["machine_name"],
                "location":     r["location"],
                "customer":     r["customer"],
                "last_seen":    last,
                "minutes_ago":  minutes_ago,
            })
    return sorted(offline, key=lambda x: x["minutes_ago"], reverse=True)

def sync_device_status_to_db(threshold_min=None):
    """
    Writes the online/offline truth into dbo.device_status.

    Why this exists: device_status was previously driven only by what devices
    publish about themselves, which can never be correct. A dead device cannot
    publish "offline", and the MQTT status topics here are *retained*, so every
    broker/ingestor reconnect replays a stale "online" and resurrects devices
    that have been dark for weeks.

    Liveness is therefore derived from observed telemetry (device_statistics) --
    the same source detect_offline_devices() uses for alerts -- so the
    database and the alerts can never disagree.

    Returns (online_count, offline_count).
    """
    if threshold_min is None:
        threshold_min = OFFLINE_THRESHOLD_MIN

    rows = get_device_last_seen()
    if not rows:
        print("No devices with telemetry found; device_status left untouched.")
        return 0, 0

    online_n = offline_n = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for r in rows:
                if r["last_seen"] is None:
                    continue
                last_seen = r["last_seen"]
                if isinstance(last_seen, str):
                    last_seen = datetime.fromisoformat(last_seen)
                is_offline = int(r["minutes_ago"]) >= threshold_min
                status = "offline" if is_offline else "online"

                cur.execute(
                    "SELECT id FROM devices "
                    "WHERE machine_name=%s AND location=%s AND customer=%s",
                    (r["machine_name"], r["location"], r["customer"]))
                drow = cur.fetchone()
                if not drow:
                    continue
                device_id = drow[0]

                cur.execute(
                    "SELECT id, offline_since FROM device_status WHERE device_id=%s",
                    (device_id,))
                srow = cur.fetchone()

                if srow is None:
                    cur.execute("""
                        INSERT INTO device_status
                            (device_id, status, ts_epoch, ts_datetime,
                             offline_since, created_at, updated_at)
                        VALUES (%s, %s, DATEDIFF_BIG(second,'19700101',%s), %s,
                                %s, SYSUTCDATETIME(), SYSUTCDATETIME())
                    """, (device_id, status, last_seen, last_seen,
                          last_seen if is_offline else None))
                else:
                    # Preserve the original offline_since across repeated sweeps so
                    # "down since" reflects the first miss, not the latest sweep.
                    offline_since = (srow[1] or last_seen) if is_offline else None
                    cur.execute("""
                        UPDATE device_status
                        SET status=%s,
                            ts_epoch=DATEDIFF_BIG(second,'19700101',%s),
                            ts_datetime=%s,
                            offline_since=%s,
                            updated_at=SYSUTCDATETIME()
                        WHERE id=%s
                    """, (status, last_seen, last_seen, offline_since, srow[0]))

                if is_offline:
                    offline_n += 1
                else:
                    online_n += 1
        conn.commit()

    print("device_status synced: %d online, %d offline (threshold %dm)"
          % (online_n, offline_n, threshold_min))
    return online_n, offline_n

# ── Offline state persistence ──────────────────────────────────────────────────
def _device_key(machine_name, location):
    return f"{machine_name}@{location}"

def load_offline_state():
    """Load previously known offline devices from disk."""
    if os.path.exists(OFFLINE_STATE_FILE):
        try:
            with open(OFFLINE_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_offline_state(state):
    with open(OFFLINE_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)

def _downtime_minutes(alerted_at):
    """Minutes elapsed since the device was first alerted on. 0 if unknown/unparseable."""
    if not alerted_at:
        return 0
    try:
        started = datetime.fromisoformat(str(alerted_at))
    except (TypeError, ValueError):
        return 0
    return max(int((datetime.now() - started).total_seconds() // 60), 0)

def diff_offline_state(current_offline):
    """
    Compare current offline devices against last known state.
    Returns:
        newly_offline  — devices that just went offline (not in previous state)
        recovered      — devices that were offline but are now reporting again,
                         each annotated with downtime_minutes
        still_offline  — devices that were already known to be offline
    """
    prev_state = load_offline_state()
    current_keys = {_device_key(d["machine_name"], d["location"]): d for d in current_offline}

    newly_offline = [d for k, d in current_keys.items() if k not in prev_state]
    recovered     = []
    for k in prev_state:
        if k in current_keys:
            continue
        d = dict(prev_state[k])
        d["downtime_minutes"] = _downtime_minutes(d.get("alerted_at"))
        recovered.append(d)
    still_offline = [d for k, d in current_keys.items() if k in prev_state]

    # Update state — only keep currently offline devices
    new_state = {}
    for k, d in current_keys.items():
        new_state[k] = {
            "machine_name": d["machine_name"],
            "location":     d["location"],
            "customer":     d["customer"],
            "last_seen":    str(d["last_seen"]),
            "minutes_ago":  d["minutes_ago"],
            "alerted_at":   prev_state[k]["alerted_at"] if k in prev_state else datetime.now().isoformat(),
        }
    save_offline_state(new_state)

    return newly_offline, recovered, still_offline

# ── Anomaly detection ──────────────────────────────────────────────────────────

# Hardcoded fallback defaults (used when no DB threshold exists)
_DEFAULT_THRESHOLDS = {
    "good_read_pct": (95.0, 90.0),   # (warn, bad) — direction: low
    "no_dim_pct":    (5.0,  10.0),   # (warn, bad) — direction: high
}

def load_thresholds():
    """
    Load all rows from alert_thresholds and return a lookup dict.
    Keys: (customer, machine_name, location, metric)  — exact device match
    Also adds (customer, None, None, metric) keys for customer-wide rows
    (where machine_name IS NULL and location IS NULL in the DB).
    """
    thresholds = {}
    try:
        rows = query("SELECT customer, machine_name, location, metric, warn_value, bad_value FROM alert_thresholds")
        for r in rows:
            key = (r["customer"], r["machine_name"], r["location"], r["metric"])
            thresholds[key] = (
                float(r["warn_value"]) if r["warn_value"] is not None else None,
                float(r["bad_value"])  if r["bad_value"]  is not None else None,
            )
            # If this is a customer-wide row (no machine/location) also store under None keys
            if r["machine_name"] is None and r["location"] is None:
                thresholds[(r["customer"], None, None, r["metric"])] = thresholds[key]
    except Exception as e:
        print(f"⚠️  Could not load thresholds from DB (using hardcoded defaults): {e}")
    return thresholds

def get_threshold(thresholds, customer, machine_name, location, metric):
    """
    Return (warn_value, bad_value) for the given device + metric.
    Fallback order: device-level → customer-level → hardcoded default.
    """
    # 1. Exact device match
    val = thresholds.get((customer, machine_name, location, metric))
    if val and val[0] is not None:
        return val
    # 2. Customer-wide row
    val = thresholds.get((customer, None, None, metric))
    if val and val[0] is not None:
        return val
    # 3. Hardcoded default
    return _DEFAULT_THRESHOLDS.get(metric, (None, None))

def detect_anomalies(trend_rows, customer=None):
    caps      = get_caps(customer)
    alerts    = []
    today     = datetime.now().date()
    today_rows = [r for r in trend_rows if r["report_date"] == today and (r["daily_items"] or 0) > 100]

    # Load thresholds once
    thresholds = load_thresholds()

    for r in today_rows:
        if r["good_read_pct"]:
            pct = float(r["good_read_pct"])
            warn, bad = get_threshold(thresholds, r["customer"], r["machine_name"], r["location"], "good_read_pct")
            if bad is not None and pct < bad:
                alerts.append(("bad",  f"<b>{r['machine_name']} @ {r['location']}</b> — good read dropped to <b>{pct}%</b> today"))
            elif warn is not None and pct < warn:
                alerts.append(("warn", f"<b>{r['machine_name']} @ {r['location']}</b> — good read dropped to <b>{pct}%</b> today"))

    if caps["has_dimension"]:
        for r in trend_rows:
            if r["daily_items"] and r["daily_no_dim"]:
                pct = float(r["daily_no_dim"]) / float(r["daily_items"]) * 100
                warn, bad = get_threshold(thresholds, r["customer"], r["machine_name"], r["location"], "no_dim_pct")
                if bad is not None and pct > bad:
                    alerts.append(("bad",  f"<b>{r['machine_name']} @ {r['location']}</b> — no-dimension spike <b>{pct:.1f}%</b> on {r['report_date']}"))
                elif warn is not None and pct > warn:
                    alerts.append(("warn", f"<b>{r['machine_name']} @ {r['location']}</b> — no-dimension spike <b>{pct:.1f}%</b> on {r['report_date']}"))

    if caps["has_hand_scan"]:
        for r in today_rows:
            if r.get("daily_hand_scanned") and r["daily_items"]:
                pct = float(r["daily_hand_scanned"]) / float(r["daily_items"]) * 100
                if pct > 15.0:
                    alerts.append(("warn", f"<b>{r['machine_name']} @ {r['location']}</b> — hand-scanned spike <b>{pct:.1f}%</b> today"))

    if caps["has_weight"]:
        for r in today_rows:
            if r.get("daily_no_weight") and r["daily_items"]:
                pct = float(r["daily_no_weight"]) / float(r["daily_items"]) * 100
                if pct > 5.0:
                    alerts.append(("warn", f"<b>{r['machine_name']} @ {r['location']}</b> — no-weight spike <b>{pct:.1f}%</b> today"))

    storage = get_storage(customer)
    for s in storage:
        if s["usage_percent"] and float(s["usage_percent"]) > 90:
            alerts.append(("bad",  f"<b>{s['machine_name']} @ {s['location']}</b> — C: drive at <b>{float(s['usage_percent']):.0f}%</b> — action required"))
        elif s["usage_percent"] and float(s["usage_percent"]) > 80:
            alerts.append(("warn", f"<b>{s['machine_name']} @ {s['location']}</b> — C: drive at <b>{float(s['usage_percent']):.0f}%</b> — monitor"))

    # Offline / late-reporting devices for this customer
    offline = detect_offline_devices(customer=customer)
    for d in offline:
        if customer and d["customer"] != customer:
            continue
        age = d["minutes_ago"]
        age_str = f"{age // 60}h {age % 60}m" if age >= 60 else f"{age}m"
        alerts.append(("bad", f"<b>{d['machine_name']} @ {d['location']}</b> — no data received for <b>{age_str}</b> (last seen {d['last_seen'].strftime('%H:%M')})"))

    return alerts

# Severity icons for rendering (severity, message) anomaly tuples as card text.
_ANOMALY_ICON = {"bad": "🔴", "warn": "⚠️"}

def _anomaly_lines(anomalies):
    """Flatten detect_anomalies() (severity, message) tuples into plain card strings.

    Adaptive Card TextBlocks do not render HTML, so <b> markers are stripped and the
    severity is expressed with a leading icon instead.
    """
    return [
        f"{_ANOMALY_ICON.get(level, '•')} {re.sub(r'</?b>', '', msg)}"
        for level, msg in anomalies
    ]

# ── Charts ─────────────────────────────────────────────────────────────────────
CHART_STYLE = {
    "figure.facecolor": "none",
    "axes.facecolor":   "none",
    "axes.edgecolor":   "#d1d5db",
    "axes.labelcolor":  "#374151",
    "xtick.color":      "#6b7280",
    "ytick.color":      "#6b7280",
    "text.color":       "#1f2937",
    "grid.color":       "#e5e7eb",
    "grid.linestyle":   "--",
    "grid.alpha":       0.8,
}

# Distinct, high-contrast palette
PALETTE = ["#2563eb","#7c3aed","#059669","#d97706","#db2777",
           "#0891b2","#16a34a","#dc2626","#9333ea","#0284c7"]

def fig_to_png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=130, transparent=True)
    buf.seek(0)
    data = buf.read()
    plt.close(fig)
    return data

def chart_daily_volume(rows, title):
    with plt.rc_context(CHART_STYLE):
        devices = sorted(set((r["machine_name"], r["location"]) for r in rows))
        dates   = sorted(set(r["report_date"] for r in rows))
        fig, ax = plt.subplots(figsize=(12, 5))
        x = np.arange(len(dates))
        w = min(0.8 / max(len(devices), 1), 0.15)
        for i, (mname, loc) in enumerate(devices):
            vals = [next((r["daily_items"] for r in rows if r["machine_name"]==mname and r["location"]==loc and r["report_date"]==d), 0) for d in dates]
            ax.bar(x + i*w - (len(devices)*w/2) + w/2, vals, w*0.85,
                   label=f"{mname}@{loc}", color=PALETTE[i % len(PALETTE)], alpha=0.9)
        ax.set_xticks(x)
        ax.set_xticklabels([str(d) for d in dates], rotation=25, ha="right", fontsize=9)
        ax.set_ylabel("Items Scanned", fontsize=10)
        ax.set_title(title, fontsize=13, pad=12, fontweight="bold")
        ax.legend(fontsize=8, ncol=3, loc="upper left", framealpha=0.3)
        ax.grid(axis="y", alpha=0.4)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{int(x):,}"))
        fig.tight_layout()
        return fig_to_png(fig)

def chart_goodread_trend(rows, title):
    """Larger, clearer line chart with markers and value annotations on dips."""
    with plt.rc_context(CHART_STYLE):
        devices = sorted(set((r["machine_name"], r["location"]) for r in rows))
        dates   = sorted(set(r["report_date"] for r in rows))
        fig, ax = plt.subplots(figsize=(13, 6))
        for i, (mname, loc) in enumerate(devices):
            pts = [(d, float(r["good_read_pct"])) for d in dates
                   for r in rows if r["machine_name"]==mname and r["location"]==loc
                   and r["report_date"]==d and (r["daily_items"] or 0) > 0 and r["good_read_pct"]]
            if not pts: continue
            xs, ys = zip(*pts)
            ax.plot(xs, ys, marker="o", markersize=5, label=f"{mname}@{loc}",
                    color=PALETTE[i % len(PALETTE)], linewidth=2, zorder=3)
            # Annotate dips below 98%
            for x, y in pts:
                if y < 97.0:
                    ax.annotate(f"{y:.1f}%", (x, y), textcoords="offset points",
                                xytext=(0, -14), fontsize=7.5, ha="center",
                                color=PALETTE[i % len(PALETTE)], fontweight="bold")
        # Reference lines
        ax.axhline(97, color="#4ade80", linestyle=":", linewidth=1, alpha=0.5, label="97% target")
        ax.axhline(90, color="#f87171", linestyle=":", linewidth=1, alpha=0.5, label="90% threshold")
        ax.set_ylim(85, 101)
        ax.set_ylabel("Good Read %", fontsize=10)
        ax.set_title(title, fontsize=13, pad=12, fontweight="bold")
        ax.legend(fontsize=8, ncol=3, loc="lower left", framealpha=0.3)
        ax.grid(True, alpha=0.4)
        plt.xticks(rotation=25, ha="right", fontsize=9)
        fig.tight_layout()
        return fig_to_png(fig)

def chart_hourly_volume(rows, title):
    with plt.rc_context(CHART_STYLE):
        fig, ax = plt.subplots(figsize=(12, 4))
        hours = [r["hour_of_day"] for r in rows]
        items = [r["total_items"] for r in rows]
        bars  = ax.bar(hours, items, color="#38bdf8", alpha=0.85, width=0.7)
        # Highlight peak hours
        if items:
            peak = max(items)
            for bar, val in zip(bars, items):
                if val == peak:
                    bar.set_color("#a78bfa")
        ax.set_xlabel("Hour of Day (24h)", fontsize=10)
        ax.set_ylabel("Total Items", fontsize=10)
        ax.set_title(title, fontsize=13, pad=12, fontweight="bold")
        ax.set_xticks(range(0, 24))
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{int(x):,}"))
        ax.grid(axis="y", alpha=0.4)
        fig.tight_layout()
        return fig_to_png(fig)

def _try_save_chart(png_bytes, label):
    """Persist a chart PNG, degrading gracefully to None on failure.

    Returns None when the chart cannot be saved (disk full, permissions) or when
    CHART_PUBLIC_BASE_URL is unset — cards.py omits the Image element for a None url.
    """
    try:
        url = save_chart(png_bytes, CHART_DIR, CHART_PUBLIC_BASE_URL)
    except OSError as e:
        print(f"⚠️ Chart '{label}' could not be saved: {e}")
        return None
    if url is None:
        print(f"⚠️ Chart '{label}' saved but CHART_PUBLIC_BASE_URL is unset — omitting image")
    return url


# ── Table helpers ──────────────────────────────────────────────────────────────
def _caps_headers(caps, trailing=None):
    headers = ["Device", "Location", "Items", "Good Read %", "No Reads"]
    if caps["has_dimension"]:
        headers.append("No Dims")
    if caps["has_hand_scan"]:
        headers.append("Hand Scanned")
    if caps["has_weight"]:
        headers.append("No Weight")
    if trailing:
        headers.extend(trailing)
    return headers


def _caps_row(r, caps, trailing_values=None):
    row = [
        r["machine_name"], r["location"],
        f"{(r['total_items'] or 0):,}",
        f"{r['good_read_pct']:.1f}%" if r.get("good_read_pct") is not None else "—",
        f"{r['no_reads'] or 0:,}",
    ]
    if caps["has_dimension"]:
        row.append(f"{r['no_dimensions'] or 0:,}")
    if caps["has_hand_scan"]:
        row.append(f"{r['hand_scanned'] or 0:,}")
    if caps["has_weight"]:
        row.append(f"{r['no_weight'] or 0:,}")
    if trailing_values:
        row.extend(trailing_values(r))
    return row


def customer_section_daily_card(customer, days=7):
    caps      = get_caps(customer)
    trend     = get_daily_trend(days, customer)
    summary1  = get_device_summary(1, customer)
    summary7  = get_device_summary(days, customer)
    hourly    = get_hourly_pattern(days, customer)
    storage   = get_storage(customer)
    anomalies = detect_anomalies(trend, customer)

    chart_urls = {
        "volume":   _try_save_chart(chart_daily_volume(trend, f"Daily Volume — {customer} — Last {days} Days"), f"volume/{customer}"),
        "goodread": _try_save_chart(chart_goodread_trend(trend, f"Good Read % — {customer} — Last {days} Days"), f"goodread/{customer}"),
        "hourly":   _try_save_chart(chart_hourly_volume(hourly, f"Hourly Pattern — {customer}"), f"hourly/{customer}"),
    }

    today_headers = _caps_headers(caps, trailing=["Not Sent"])
    today_rows = [_caps_row(r, caps, trailing_values=lambda r: [f"{r['not_sent'] or 0:,}"]) for r in summary1]

    week_headers = _caps_headers(caps)
    week_rows = [_caps_row(r, caps) for r in summary7]

    storage_headers = ["Device", "Location", "Usage", "%"]
    storage_rows = [
        [s["machine_name"], s["location"], f"{float(s['used_gb']):.1f} / {float(s['total_gb']):.1f} GB", f"{float(s['usage_percent']):.0f}%"]
        for s in storage
    ]

    return build_customer_section_card(
        customer=customer, days=days, anomalies=_anomaly_lines(anomalies),
        today_table={"headers": today_headers, "rows": today_rows},
        week_table={"headers": week_headers, "rows": week_rows},
        storage_table={"headers": storage_headers, "rows": storage_rows},
        chart_urls=chart_urls,
    )


def customer_section_monthly_card(customer, month_label):
    caps     = get_caps(customer)
    trend    = get_daily_trend(30, customer)
    summary  = get_device_summary(30, customer)
    hourly   = get_hourly_pattern(30, customer)
    storage  = get_storage(customer)
    anomalies = detect_anomalies(trend, customer)

    chart_urls = {
        "volume":   _try_save_chart(chart_daily_volume(trend, f"Daily Volume — {customer} — {month_label}"), f"volume/{customer}"),
        "goodread": _try_save_chart(chart_goodread_trend(trend, f"Good Read % — {customer} — {month_label}"), f"goodread/{customer}"),
        "hourly":   _try_save_chart(chart_hourly_volume(hourly, f"Hourly Pattern — {customer}"), f"hourly/{customer}"),
    }

    total_items = sum(int(r["total_items"] or 0) for r in summary)
    avg_good    = sum(float(r["good_read_pct"] or 0) for r in summary) / max(len(summary), 1)
    kpis = {"total_items": total_items, "avg_good_read_pct": avg_good, "active_devices": len(summary)}

    def _trend_label(r):
        good = float(r["good_read_pct"] or 0)
        return "▲ Strong" if good >= 99 else "▼ Monitor"

    tbl_headers = _caps_headers(caps, trailing=["Not Sent", "Trend"])
    tbl_rows = [
        _caps_row(r, caps, trailing_values=lambda r: [f"{int(r['not_sent'] or 0):,}", _trend_label(r)])
        for r in summary
    ]

    storage_headers = ["Device", "Location", "Usage", "%"]
    storage_rows = [
        [s["machine_name"], s["location"], f"{float(s['used_gb']):.1f} / {float(s['total_gb']):.1f} GB", f"{float(s['usage_percent']):.0f}%"]
        for s in storage
    ]

    return build_customer_section_card(
        customer=customer, days=30, anomalies=_anomaly_lines(anomalies),
        today_table={"headers": [], "rows": []},
        week_table={"headers": tbl_headers, "rows": tbl_rows},
        storage_table={"headers": storage_headers, "rows": storage_rows},
        chart_urls=chart_urls,
        kpis=kpis,
    )

# ── Report builders ────────────────────────────────────────────────────────────
def check_and_send_offline_alert():
    """
    Detects offline/recovered devices, sends appropriate Teams alerts, updates state.
    Returns (newly_offline_count, recovered_count).
    """
    current_offline = detect_offline_devices()
    newly_offline, recovered, still_offline = diff_offline_state(current_offline)

    if newly_offline:
        card = build_offline_alert_card(newly_offline)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
        print(f"🔴 Offline alert sent for {len(newly_offline)} device(s): {[_device_key(d['machine_name'], d['location']) for d in newly_offline]}")

    if recovered:
        card = build_recovery_card(recovered)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
        print(f"✅ Recovery alert sent for {len(recovered)} device(s): {[_device_key(d['machine_name'], d['location']) for d in recovered]}")

    if still_offline:
        print(f"⏳ Still offline (no re-alert): {[_device_key(d['machine_name'], d['location']) for d in still_offline]}")

    if not newly_offline and not recovered and not still_offline:
        print("✅ All devices reporting normally.")

    return len(newly_offline), len(recovered)


def build_and_send_daily_report():
    cleanup_old_charts(CHART_DIR, CHART_RETENTION_DAYS)
    # Customer list is DB-driven; CUSTOMER_CAPS only customises display per customer.
    customers = get_customers()
    for customer in customers:
        card = customer_section_daily_card(customer)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
    print(f"✅ Daily report sent for {len(customers)} customer(s)")


def build_and_send_monthly_report():
    cleanup_old_charts(CHART_DIR, CHART_RETENTION_DAYS)
    month_label = datetime.now().strftime("%B %Y")
    customers = get_customers()
    for customer in customers:
        card = customer_section_monthly_card(customer, month_label)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
    print(f"✅ Monthly report sent for {len(customers)} customer(s)")

# ── Entry ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if mode == "daily":
        build_and_send_daily_report()
    elif mode == "monthly":
        build_and_send_monthly_report()
    elif mode == "offline":
        # Run this every ~20 min via cron to get near-realtime offline/recovery alerts.
        newly_offline, recovered = check_and_send_offline_alert()
        sys.exit(1 if (newly_offline or recovered) else 0)
    elif mode == "offline-sync":
        # DB-only sync of dbo.device_status. Posts no Teams card, so it is safe to run
        # 7 days a week and keeps dashboards truthful over weekends.
        sync_device_status_to_db()
    else:
        print("Usage: report.py [daily|monthly|offline|offline-sync]"); sys.exit(1)
