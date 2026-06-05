#!/usr/bin/env python3
"""
S1 Device Performance Reporter — v4
Daily and monthly HTML email reports with charts, per customer.
Usage:
    python3 report.py daily
    python3 report.py monthly
"""

import sys, os, io, base64, smtplib, pymssql, json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import numpy as np

# ── Config ─────────────────────────────────────────────────────────────────────
ENV_PATH = os.path.join(os.path.dirname(__file__), "report.env")

def load_config():
    # Prefer environment variables (Docker), fall back to report.env for local runs
    env_keys = ["DB_HOST","DB_PORT","DB_USER","DB_PASS","DB_NAME",
                "SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASS","REPORT_TO",
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

def diff_offline_state(current_offline):
    """
    Compare current offline devices against last known state.
    Returns:
        newly_offline  — devices that just went offline (not in previous state)
        recovered      — devices that were offline but are now reporting again
        still_offline  — devices that were already known to be offline
    """
    prev_state = load_offline_state()
    current_keys = {_device_key(d["machine_name"], d["location"]): d for d in current_offline}

    newly_offline = [d for k, d in current_keys.items() if k not in prev_state]
    recovered     = [prev_state[k] for k in prev_state if k not in current_keys]
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

def chart_nodim_heatmap(rows, title):
    with plt.rc_context(CHART_STYLE):
        devices = sorted(set((r["machine_name"], r["location"]) for r in rows))
        dates   = sorted(set(r["report_date"] for r in rows))
        matrix  = np.array([[
            float(r["daily_no_dim"])*100/float(r["daily_items"])
            if (r := next((x for x in rows if x["machine_name"]==mname and x["location"]==loc and x["report_date"]==d), None))
               and r["daily_items"] else 0.0
            for d in dates] for mname, loc in devices])
        cmap = LinearSegmentedColormap.from_list("rg", ["#f0fdf4","#fef9c3","#fee2e2"])
        fig, ax = plt.subplots(figsize=(13, max(3, len(devices)*0.7)))
        im = ax.imshow(matrix, cmap=cmap, aspect="auto", vmin=0, vmax=5)
        ax.set_xticks(range(len(dates)))
        ax.set_xticklabels([str(d) for d in dates], rotation=25, ha="right", fontsize=9)
        ax.set_yticks(range(len(devices)))
        ax.set_yticklabels([f"{m}@{l}" for m, l in devices], fontsize=9)
        plt.colorbar(im, ax=ax, label="No-Dim %")
        ax.set_title(title, fontsize=13, pad=12, fontweight="bold")
        for i in range(len(devices)):
            for j in range(len(dates)):
                if matrix[i,j] > 0:
                    ax.text(j, i, f"{matrix[i,j]:.1f}", ha="center", va="center", fontsize=8,
                            color="#1f2937", fontweight="bold")
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

# ── HTML helpers ───────────────────────────────────────────────────────────────
def pct_badge(pct):
    pct = float(pct) if pct else 0
    if pct >= 97:   return f'<span style="background:#dcfce7;color:#166534;padding:2px 9px;border-radius:12px;font-size:12px;font-weight:600">{pct:.1f}%</span>'
    elif pct >= 90: return f'<span style="background:#fef9c3;color:#854d0e;padding:2px 9px;border-radius:12px;font-size:12px;font-weight:600">{pct:.1f}%</span>'
    else:           return f'<span style="background:#fee2e2;color:#991b1b;padding:2px 9px;border-radius:12px;font-size:12px;font-weight:600">{pct:.1f}%</span>'

def disk_badge(pct):
    pct = float(pct) if pct else 0
    if pct >= 90:   return f'<span style="background:#fee2e2;color:#991b1b;padding:2px 9px;border-radius:12px;font-size:12px;font-weight:600">{pct:.0f}%</span>'
    elif pct >= 80: return f'<span style="background:#fef9c3;color:#854d0e;padding:2px 9px;border-radius:12px;font-size:12px;font-weight:600">{pct:.0f}%</span>'
    else:           return f'<span style="background:#dcfce7;color:#166534;padding:2px 9px;border-radius:12px;font-size:12px;font-weight:600">{pct:.0f}%</span>'

CSS = """<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
body,html{margin:0;padding:0;background:#f3f4f6;}
*{box-sizing:border-box;}
body{font-family:Inter,'Segoe UI',Arial,sans-serif;background:#f3f4f6;color:#1f2937;}
.outer{background:#f3f4f6;width:100%;padding:0;}
.wrap{max-width:860px;margin:0 auto;padding:28px 16px;}
.hdr{background:#ffffff;border:1px solid #e5e7eb;border-top:4px solid #2563eb;border-radius:10px;padding:24px 28px;margin-bottom:20px;}
.hdr-top{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;}
.hdr h1{margin:0;font-size:21px;font-weight:700;color:#111827;letter-spacing:.2px;}
.hdr .badge{background:#2563eb;color:#fff;padding:3px 12px;border-radius:20px;font-size:12px;font-weight:600;}
.hdr .meta{color:#6b7280;font-size:13px;margin-top:5px;}
.cust-hdr{background:#eff6ff;border:1px solid #bfdbfe;border-left:4px solid #2563eb;border-radius:8px;padding:12px 18px;margin:22px 0 10px;}
.cust-hdr h2{margin:0;font-size:16px;font-weight:700;color:#1d4ed8;}
.sec{background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:18px 20px;margin-bottom:14px;}
.sec h3{margin:0 0 14px;font-size:12px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.7px;border-bottom:1px solid #f3f4f6;padding-bottom:8px;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{background:#f9fafb;color:#6b7280;text-align:left;padding:9px 12px;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;border-bottom:2px solid #e5e7eb;}
td{padding:9px 12px;border-bottom:1px solid #f3f4f6;color:#1f2937;}
tr:last-child td{border-bottom:none;}
tr:hover td{background:#f9fafb;}
.alert-row{display:flex;align-items:flex-start;gap:10px;padding:10px 14px;border-radius:7px;margin-bottom:7px;font-size:13px;line-height:1.5;}
.alert-bad{background:#fff7f7;border:1px solid #fecaca;border-left:4px solid #dc2626;}
.alert-warn{background:#fffbeb;border:1px solid #fed7aa;border-left:4px solid #d97706;}
.alert-ok{background:#f0fdf4;border:1px solid #bbf7d0;border-left:4px solid #16a34a;}
.alert-icon{font-size:16px;margin-top:1px;flex-shrink:0;}
.kpi-row{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:4px;}
.kpi{flex:1;min-width:140px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:16px;text-align:center;}
.kpi-val{font-size:26px;font-weight:700;line-height:1.1;color:#111827;}
.kpi-lbl{color:#6b7280;font-size:12px;margin-top:4px;}
img{border-radius:8px;max-width:100%;display:block;margin:4px 0;}
.footer{text-align:center;color:#9ca3af;font-size:11px;padding-top:18px;border-top:1px solid #e5e7eb;margin-top:8px;}
</style>"""

def alerts_block(anomalies):
    if not anomalies:
        return '<div class="alert-row alert-ok"><span class="alert-icon">✅</span><span>No anomalies detected.</span></div>'
    html = ""
    for level, msg in anomalies:
        cls  = "alert-bad" if level == "bad" else "alert-warn"
        icon = "🔴" if level == "bad" else "⚠️"
        html += f'<div class="alert-row {cls}"><span class="alert-icon">{icon}</span><span>{msg}</span></div>'
    return html

def img_tag(cid):
    return f'<img src="cid:{cid}" alt="chart" style="max-width:100%;border-radius:8px;display:block;margin:4px 0;">'

def customer_section_daily(customer, today_label, images, days=7):
    caps     = get_caps(customer)
    trend    = get_daily_trend(days, customer)
    summary1 = get_device_summary(1, customer)
    summary7 = get_device_summary(days, customer)
    hourly   = get_hourly_pattern(days, customer)
    storage  = get_storage(customer)
    anomalies = detect_anomalies(trend, customer)

    c_vol_id = f"vol_{customer}"
    c_gr_id  = f"gr_{customer}"
    c_hr_id  = f"hr_{customer}"
    images[c_vol_id] = chart_daily_volume(trend,  f"Daily Volume — {customer} — Last {days} Days")
    images[c_gr_id]  = chart_goodread_trend(trend, f"Good Read % — {customer} — Last {days} Days")
    images[c_hr_id]  = chart_hourly_volume(hourly, f"Hourly Pattern — {customer}")

    # Today table — build headers and rows dynamically based on caps
    today_th = "<th>Device</th><th>Location</th><th style='text-align:right'>Items</th><th>Good Read</th><th style='text-align:right'>No Reads</th>"
    if caps["has_dimension"]:
        today_th += "<th style='text-align:right'>No Dims</th>"
    if caps["has_hand_scan"]:
        today_th += "<th style='text-align:right'>Hand Scanned</th>"
    if caps["has_weight"]:
        today_th += "<th style='text-align:right'>No Weight</th>"
    today_th += "<th style='text-align:right'>Not Sent</th>"

    today_colspan = 6 + int(caps["has_dimension"]) + int(caps["has_hand_scan"]) + int(caps["has_weight"])

    today_rows = ""
    for r in summary1:
        row = f"""<tr>
            <td><b>{r['machine_name']}</b></td><td>{r['location']}</td>
            <td style="text-align:right">{(r['total_items'] or 0):,}</td>
            <td>{pct_badge(r['good_read_pct'])}</td>
            <td style="text-align:right">{r['no_reads'] or 0:,}</td>"""
        if caps["has_dimension"]:
            row += f"<td style='text-align:right'>{r['no_dimensions'] or 0:,}</td>"
        if caps["has_hand_scan"]:
            row += f"<td style='text-align:right'>{r['hand_scanned'] or 0:,}</td>"
        if caps["has_weight"]:
            row += f"<td style='text-align:right'>{r['no_weight'] or 0:,}</td>"
        row += f"<td style='text-align:right'>{r['not_sent'] or 0:,}</td></tr>"
        today_rows += row

    # 7-day summary table — same cap logic
    week_th = "<th>Device</th><th>Location</th><th style='text-align:right'>Total Items</th><th>Good Read</th><th style='text-align:right'>No Reads</th>"
    if caps["has_dimension"]:
        week_th += "<th style='text-align:right'>No Dims</th>"
    if caps["has_hand_scan"]:
        week_th += "<th style='text-align:right'>Hand Scanned</th>"
    if caps["has_weight"]:
        week_th += "<th style='text-align:right'>No Weight</th>"

    week_rows = ""
    for r in summary7:
        row = f"""<tr>
            <td><b>{r['machine_name']}</b></td><td>{r['location']}</td>
            <td style="text-align:right">{(r['total_items'] or 0):,}</td>
            <td>{pct_badge(r['good_read_pct'])}</td>
            <td style="text-align:right">{r['no_reads'] or 0:,}</td>"""
        if caps["has_dimension"]:
            row += f"<td style='text-align:right'>{r['no_dimensions'] or 0:,}</td>"
        if caps["has_hand_scan"]:
            row += f"<td style='text-align:right'>{r['hand_scanned'] or 0:,}</td>"
        if caps["has_weight"]:
            row += f"<td style='text-align:right'>{r['no_weight'] or 0:,}</td>"
        row += "</tr>"
        week_rows += row

    stor_rows = ""
    for s in storage:
        stor_rows += f"""<tr>
            <td><b>{s['machine_name']}</b></td><td>{s['location']}</td>
            <td>{float(s['used_gb']):.1f} / {float(s['total_gb']):.1f} GB</td>
            <td>{disk_badge(s['usage_percent'])}</td>
        </tr>"""

    # No-dim heatmap only for customers with dimensioners
    nodim_section = ""
    if caps["has_dimension"]:
        c_heat_id = f"heat_{customer}"
        images[c_heat_id] = chart_nodim_heatmap(trend, f"No-Dimension Heatmap — {customer} — Last {days} Days")
        nodim_section = f"""
    <div class="sec">
      <h3>🟥 No-Dimension Rate Heatmap</h3>
      {img_tag(c_heat_id)}
    </div>"""

    return f"""
    <div class="cust-hdr"><h2>🏢 {customer}</h2></div>

    <div class="sec">
      <h3>🚨 Alerts</h3>
      {alerts_block(anomalies)}
    </div>

    <div class="sec">
      <h3>📦 Today's Scan Summary</h3>
      <table>
        <tr>{today_th}</tr>
        {today_rows or f'<tr><td colspan="{today_colspan}" style="color:#8b949e;text-align:center">No data today</td></tr>'}
      </table>
    </div>

    <div class="sec">
      <h3>📈 Daily Volume — Last {days} Days</h3>
      {img_tag(c_vol_id)}
    </div>

    <div class="sec">
      <h3>✅ Good Read % Trend — Last {days} Days</h3>
      {img_tag(c_gr_id)}
    </div>

    <div class="sec">
      <h3>🕐 Hourly Volume Pattern</h3>
      {img_tag(c_hr_id)}
    </div>

    <div class="sec">
      <h3>📊 {days}-Day Summary</h3>
      <table>
        <tr>{week_th}</tr>
        {week_rows}
      </table>
    </div>
    {nodim_section}

    <div class="sec">
      <h3>💾 Storage Health (C: Drive)</h3>
      <table>
        <tr><th>Device</th><th>Location</th><th>Usage</th><th>%</th></tr>
        {stor_rows}
      </table>
    </div>"""

def customer_section_monthly(customer, month_label, images):
    caps     = get_caps(customer)
    trend    = get_daily_trend(30, customer)
    summary  = get_device_summary(30, customer)
    hourly   = get_hourly_pattern(30, customer)
    storage  = get_storage(customer)
    anomalies = detect_anomalies(trend, customer)

    c_vol_id = f"mvol_{customer}"
    c_gr_id  = f"mgr_{customer}"
    c_hr_id  = f"mhr_{customer}"
    images[c_vol_id] = chart_daily_volume(trend,   f"Daily Volume — {customer} — {month_label}")
    images[c_gr_id]  = chart_goodread_trend(trend, f"Good Read % — {customer} — {month_label}")
    images[c_hr_id]  = chart_hourly_volume(hourly, f"Hourly Pattern — {customer}")

    total_items = sum(int(r["total_items"] or 0) for r in summary)
    avg_good    = sum(float(r["good_read_pct"] or 0) for r in summary) / max(len(summary), 1)

    # Monthly summary table — dynamic columns
    tbl_th = "<th>Device</th><th>Location</th><th style='text-align:right'>Items</th><th>Good Read</th><th style='text-align:right'>No Reads</th>"
    if caps["has_dimension"]:
        tbl_th += "<th style='text-align:right'>No Dims</th>"
    if caps["has_hand_scan"]:
        tbl_th += "<th style='text-align:right'>Hand Scanned</th>"
    if caps["has_weight"]:
        tbl_th += "<th style='text-align:right'>No Weight</th>"
    tbl_th += "<th style='text-align:right'>Not Sent</th><th>Trend</th>"

    tbl_rows = ""
    for r in summary:
        good = float(r["good_read_pct"] or 0)
        trend_lbl = '<span style="color:#4ade80;font-weight:600">▲ Strong</span>' if good >= 99 else '<span style="color:#fbbf24;font-weight:600">▼ Monitor</span>'
        row = f"""<tr>
            <td><b>{r['machine_name']}</b></td><td>{r['location']}</td>
            <td style="text-align:right">{int(r['total_items'] or 0):,}</td>
            <td>{pct_badge(r['good_read_pct'])}</td>
            <td style="text-align:right">{int(r['no_reads'] or 0):,}</td>"""
        if caps["has_dimension"]:
            row += f"<td style='text-align:right'>{int(r['no_dimensions'] or 0):,}</td>"
        if caps["has_hand_scan"]:
            row += f"<td style='text-align:right'>{int(r['hand_scanned'] or 0):,}</td>"
        if caps["has_weight"]:
            row += f"<td style='text-align:right'>{int(r['no_weight'] or 0):,}</td>"
        row += f"<td style='text-align:right'>{int(r['not_sent'] or 0):,}</td><td>{trend_lbl}</td></tr>"
        tbl_rows += row

    stor_rows = ""
    for s in storage:
        stor_rows += f"""<tr>
            <td><b>{s['machine_name']}</b></td><td>{s['location']}</td>
            <td>{float(s['used_gb']):.1f} / {float(s['total_gb']):.1f} GB</td>
            <td>{disk_badge(s['usage_percent'])}</td>
        </tr>"""

    # No-dim heatmap only for customers with dimensioners
    nodim_section = ""
    if caps["has_dimension"]:
        c_heat_id = f"mheat_{customer}"
        images[c_heat_id] = chart_nodim_heatmap(trend, f"No-Dimension Heatmap — {customer}")
        nodim_section = f"""
    <div class="sec">
      <h3>🟥 No-Dimension Rate Heatmap</h3>
      {img_tag(c_heat_id)}
    </div>"""

    return f"""
    <div class="cust-hdr"><h2>🏢 {customer}</h2></div>

    <div class="kpi-row">
      <div class="kpi"><div class="kpi-val" style="color:#2563eb">{total_items:,}</div><div class="kpi-lbl">Items Scanned</div></div>
      <div class="kpi"><div class="kpi-val" style="color:#16a34a">{avg_good:.1f}%</div><div class="kpi-lbl">Avg Good Read</div></div>
      <div class="kpi"><div class="kpi-val" style="color:#0891b2">{len(summary)}</div><div class="kpi-lbl">Active Devices</div></div>
    </div>

    <div class="sec">
      <h3>🚨 Alerts</h3>
      {alerts_block(anomalies)}
    </div>

    <div class="sec">
      <h3>📊 Monthly Device Summary</h3>
      <table>
        <tr>{tbl_th}</tr>
        {tbl_rows}
      </table>
    </div>

    <div class="sec">
      <h3>📈 Daily Volume</h3>
      {img_tag(c_vol_id)}
    </div>

    <div class="sec">
      <h3>✅ Good Read % Trend</h3>
      {img_tag(c_gr_id)}
    </div>
    {nodim_section}

    <div class="sec">
      <h3>🕐 Hourly Volume Pattern</h3>
      {img_tag(c_hr_id)}
    </div>

    <div class="sec">
      <h3>💾 Storage Health (C: Drive)</h3>
      <table>
        <tr><th>Device</th><th>Location</th><th>Usage</th><th>%</th></tr>
        {stor_rows}
      </table>
    </div>"""

# ── Report builders ────────────────────────────────────────────────────────────
def build_offline_alert_email(offline_devices):
    """Builds a compact HTML alert email for offline/late-reporting devices."""
    ts          = datetime.now().strftime("%Y-%m-%d %H:%M")
    today_label = datetime.now().strftime("%A, %d %B %Y")
    count       = len(offline_devices)

    rows_html = ""
    for d in offline_devices:
        last_seen = d["last_seen"]
        if isinstance(last_seen, str):
            last_seen = datetime.fromisoformat(last_seen)
        last_str = last_seen.strftime("%Y-%m-%d %H:%M") if last_seen else "Unknown"
        minutes  = d["minutes_ago"]
        if minutes >= 60:
            age = f"{minutes // 60}h {minutes % 60}m ago"
        else:
            age = f"{minutes}m ago"
        rows_html += f"""<tr>
            <td><b>{d['machine_name']}</b></td>
            <td>{d['location']}</td>
            <td>{d['customer']}</td>
            <td>{last_str} SAST</td>
            <td><span style="background:#fee2e2;color:#991b1b;padding:2px 9px;border-radius:12px;font-size:12px;font-weight:600">{age}</span></td>
        </tr>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">{CSS}</head>
<body>
<table width="100%" cellpadding="0" cellspacing="0" bgcolor="#f3f4f6" style="background:#f3f4f6">
<tr><td align="center" bgcolor="#f3f4f6" style="background:#f3f4f6">
<div class="wrap">
  <div class="hdr">
    <div class="hdr-top">
      <h1>⚙️ S1 — Device Offline Alert</h1>
      <span class="badge" style="background:#dc2626">ALERT</span>
    </div>
    <div class="meta">{today_label} &nbsp;·&nbsp; Generated by Systems-One &nbsp;·&nbsp; {ts} SAST</div>
  </div>
  <div class="sec">
    <h3>🔴 Devices Not Reporting ({count} device{"s" if count != 1 else ""})</h3>
    <div class="alert-row alert-bad" style="margin-bottom:14px;">
      <span class="alert-icon">🔴</span>
      <span>
        <b>{count} device{"s" if count != 1 else ""}</b> {"have" if count != 1 else "has"} not sent data within the expected
        interval ({OFFLINE_THRESHOLD_MIN} min threshold). Check connectivity or service status on the affected unit{"s" if count != 1 else ""}.
      </span>
    </div>
    <table>
      <tr><th>Device</th><th>Location</th><th>Customer</th><th>Last Seen</th><th>Overdue By</th></tr>
      {rows_html}
    </table>
  </div>
  <div class="footer">S1 Remote Monitoring &nbsp;·&nbsp; systems-one.com &nbsp;·&nbsp; {ts} SAST</div>
</div>
</td></tr></table>
</body></html>"""

    subject = f"🔴 S1 Alert — {count} Device{'s' if count != 1 else ''} Not Reporting — {datetime.now().strftime('%H:%M')} SAST"
    return html, subject

def build_recovery_email(recovered_devices):
    """Builds a green recovery email for devices that have come back online."""
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
            <td><span style="background:#dcfce7;color:#166534;padding:2px 9px;border-radius:12px;font-size:12px;font-weight:600">✅ Online</span></td>
        </tr>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">{CSS}</head>
<body>
<table width="100%" cellpadding="0" cellspacing="0" bgcolor="#f3f4f6" style="background:#f3f4f6">
<tr><td align="center" bgcolor="#f3f4f6" style="background:#f3f4f6">
<div class="wrap">
  <div class="hdr">
    <div class="hdr-top">
      <h1>⚙️ S1 — Device Recovery</h1>
      <span class="badge" style="background:#16a34a">RECOVERED</span>
    </div>
    <div class="meta">{today_label} &nbsp;·&nbsp; Generated by Systems-One &nbsp;·&nbsp; {ts} SAST</div>
  </div>
  <div class="sec">
    <h3>✅ Devices Back Online ({count} device{"s" if count != 1 else ""})</h3>
    <div class="alert-row alert-ok" style="margin-bottom:14px;">
      <span class="alert-icon">✅</span>
      <span>
        <b>{count} device{"s" if count != 1 else ""}</b> {"have" if count != 1 else "has"} resumed sending telemetry data normally.
      </span>
    </div>
    <table>
      <tr><th>Device</th><th>Location</th><th>Customer</th><th>Went Offline At</th><th>Status</th></tr>
      {rows_html}
    </table>
  </div>
  <div class="footer">S1 Remote Monitoring &nbsp;·&nbsp; systems-one.com &nbsp;·&nbsp; {ts} SAST</div>
</div>
</td></tr></table>
</body></html>"""

    subject = f"✅ S1 Recovery — {count} Device{'s' if count != 1 else ''} Back Online — {datetime.now().strftime('%H:%M')} SAST"
    return html, subject

def check_and_send_offline_alert():
    """
    Detects offline/recovered devices, sends appropriate emails, updates state.
    Returns (newly_offline_count, recovered_count).
    """
    current_offline = detect_offline_devices()
    newly_offline, recovered, still_offline = diff_offline_state(current_offline)

    if newly_offline:
        html, subject = build_offline_alert_email(newly_offline)
        send_email(subject, html)
        print(f"🔴 Offline alert sent for {len(newly_offline)} device(s): {[_device_key(d['machine_name'], d['location']) for d in newly_offline]}")

    if recovered:
        html, subject = build_recovery_email(recovered)
        send_email(subject, html)
        print(f"✅ Recovery alert sent for {len(recovered)} device(s): {[_device_key(d['machine_name'], d['location']) for d in recovered]}")

    if still_offline:
        print(f"⏳ Still offline (no re-alert): {[_device_key(d['machine_name'], d['location']) for d in still_offline]}")

    if not newly_offline and not recovered and not still_offline:
        print("✅ All devices reporting normally.")

    return len(newly_offline), len(recovered)


def build_daily_report():
    today_label = datetime.now().strftime("%A, %d %B %Y")
    customers   = get_customers()
    images      = {}
    body        = "".join(customer_section_daily(c, today_label, images) for c in customers)
    ts          = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">{CSS}</head>
<body>
<table width="100%" cellpadding="0" cellspacing="0" bgcolor="#f3f4f6" style="background:#f3f4f6">
<tr><td align="center" bgcolor="#f3f4f6" style="background:#f3f4f6">
<div class="wrap">
  <div class="hdr">
    <div class="hdr-top">
      <h1>⚙️ S1 — Daily Performance Report</h1>
      <span class="badge">DAILY</span>
    </div>
    <div class="meta">{today_label} &nbsp;·&nbsp; Generated by Systems-One &nbsp;·&nbsp; {ts} SAST</div>
  </div>
  {body}
  <div class="footer">S1 Remote Monitoring &nbsp;·&nbsp; systems-one.com &nbsp;·&nbsp; {ts} SAST</div>
</div>
</td></tr></table>
</body></html>"""
    return html, f"S1 Daily Report — {today_label}", images

def build_monthly_report():
    month_label = datetime.now().strftime("%B %Y")
    customers   = get_customers()
    images      = {}
    body        = "".join(customer_section_monthly(c, month_label, images) for c in customers)
    ts          = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">{CSS}</head>
<body>
<table width="100%" cellpadding="0" cellspacing="0" bgcolor="#f3f4f6" style="background:#f3f4f6">
<tr><td align="center" bgcolor="#f3f4f6" style="background:#f3f4f6">
<div class="wrap">
  <div class="hdr">
    <div class="hdr-top">
      <h1>⚙️ S1 — Monthly Deep-Dive Report</h1>
      <span class="badge" style="background:#1f6feb">MONTHLY</span>
    </div>
    <div class="meta">{month_label} &nbsp;·&nbsp; Generated by Systems-One &nbsp;·&nbsp; {ts} SAST</div>
  </div>
  {body}
  <div class="footer">S1 Remote Monitoring &nbsp;·&nbsp; systems-one.com &nbsp;·&nbsp; {ts} SAST</div>
</div>
</td></tr></table>
</body></html>"""
    return html, f"S1 Monthly Deep-Dive — {month_label}", images

# ── Email ──────────────────────────────────────────────────────────────────────
def send_email(subject, html_body, images=None):
    # Build multipart/related so inline images work in Outlook
    msg_root             = MIMEMultipart("mixed")
    msg_root["Subject"]  = subject
    msg_root["From"]     = f"S1 Reports <{CFG['SMTP_USER']}>"
    msg_root["To"]       = CFG["REPORT_TO"]

    msg_related = MIMEMultipart("related")
    msg_alt     = MIMEMultipart("alternative")
    msg_alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg_related.attach(msg_alt)

    for cid, png_bytes in (images or {}).items():
        img = MIMEImage(png_bytes, "png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        msg_related.attach(img)

    msg_root.attach(msg_related)

    with smtplib.SMTP(CFG["SMTP_HOST"], int(CFG["SMTP_PORT"])) as s:
        s.ehlo(); s.starttls()
        s.login(CFG["SMTP_USER"], CFG["SMTP_PASS"])
        s.sendmail(CFG["SMTP_USER"], CFG["REPORT_TO"].split(","), msg_root.as_string())
    print(f"✅ Sent: {subject}")

# ── Entry ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if mode == "daily":
        html, subject, images = build_daily_report()
        send_email(subject, html, images)
    elif mode == "monthly":
        html, subject, images = build_monthly_report()
        send_email(subject, html, images)
    elif mode == "offline":
        # Run this every ~20 min via cron to get near-realtime offline/recovery alerts.
        newly_offline, recovered = check_and_send_offline_alert()
        sys.exit(1 if (newly_offline or recovered) else 0)
    else:
        print("Usage: report.py [daily|monthly|offline]"); sys.exit(1)
