#!/usr/bin/env python3
"""
compute_baselines.py — Compute per-device alert thresholds from historical data
and upsert into the alert_thresholds table.

Usage:
    python3 compute_baselines.py [--dry-run] [--lookback 60]
"""

import sys, os, argparse, pymssql
from datetime import datetime
from collections import defaultdict
import statistics

# ── Config ─────────────────────────────────────────────────────────────────────
ENV_PATH = os.path.join(os.path.dirname(__file__), "report.env")

def load_config():
    env_keys = ["DB_HOST", "DB_PORT", "DB_USER", "DB_PASS", "DB_NAME"]
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

def get_conn():
    return pymssql.connect(
        server=CFG["DB_HOST"], port=int(CFG["DB_PORT"]),
        user=CFG["DB_USER"], password=CFG["DB_PASS"],
        database=CFG["DB_NAME"], timeout=30
    )

# ── Stats helpers ──────────────────────────────────────────────────────────────
def percentile(data, p):
    """Compute p-th percentile (0-100) of a sorted list."""
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)

def compute_stats(values):
    """Return (mean, stddev, p05, p10, p90, p95, n) for a list of floats."""
    n = len(values)
    if n == 0:
        return None
    mean = statistics.mean(values)
    stddev = statistics.stdev(values) if n > 1 else 0.0
    p05 = percentile(values, 5)
    p10 = percentile(values, 10)
    p90 = percentile(values, 90)
    p95 = percentile(values, 95)
    return mean, stddev, p05, p10, p90, p95, n

# ── Threshold derivation ───────────────────────────────────────────────────────
def derive_thresholds(metric, mean, stddev, p05, p10, p90, p95):
    if metric == "good_read_pct":
        warn = max(p10, mean - 2 * stddev)
        bad  = max(p05, mean - 3 * stddev)
        warn = max(warn, 50.0)
        bad  = max(bad,  40.0)
        # Cap warn below 100 so near-perfect devices don't get absurd thresholds
        if warn >= 100.0:
            warn = round(mean - 1.0, 4)
        if bad >= 100.0 or bad >= warn:
            bad = round(warn - 1.0, 4)
        return round(warn, 4), round(bad, 4)
    elif metric == "no_dim_pct":
        warn = (p90 * 1.5) if p90 > 0 else 3.0
        bad  = (p95 * 2.0) if p95 > 0 else 5.0
        return round(warn, 4), round(bad, 4)
    return None, None

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Compute per-device alert thresholds")
    parser.add_argument("--dry-run", action="store_true", help="Print thresholds but don't write to DB")
    parser.add_argument("--lookback", type=int, default=60, help="Days of history to use (default: 60)")
    args = parser.parse_args()

    lookback = args.lookback
    dry_run  = args.dry_run

    print(f"{'[DRY RUN] ' if dry_run else ''}Computing baselines from last {lookback} days of device_statistics...")

    with get_conn() as conn:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(f"""
                SELECT d.customer, d.machine_name, d.location,
                       ds.total_items, ds.good_read, ds.no_dimension
                FROM devices d
                JOIN device_statistics ds ON ds.device_id = d.id
                WHERE ds.ts_datetime >= DATEADD(day, -{lookback}, GETDATE())
                  AND ds.total_items >= 50
                ORDER BY d.customer, d.machine_name, d.location
            """)
            rows = cur.fetchall()

        print(f"  Fetched {len(rows)} qualifying rows.")

        # Group rows by device
        device_data = defaultdict(lambda: {"good_read_pcts": [], "no_dim_pcts": []})
        for r in rows:
            key = (r["customer"], r["machine_name"], r["location"])
            total = float(r["total_items"] or 0)
            if total <= 0:
                continue
            gr_pct = float(r["good_read"] or 0) / total * 100
            nd_pct = float(r["no_dimension"] or 0) / total * 100
            device_data[key]["good_read_pcts"].append(gr_pct)
            device_data[key]["no_dim_pcts"].append(nd_pct)

        # Check which devices have is_override set — skip those
        with conn.cursor(as_dict=True) as cur:
            cur.execute("""
                SELECT customer, machine_name, location, metric
                FROM alert_thresholds
                WHERE is_override = 1
            """)
            override_rows = cur.fetchall()
        override_set = {
            (r["customer"], r["machine_name"], r["location"], r["metric"])
            for r in override_rows
        }

        results = []
        skipped_override = 0

        for (customer, machine_name, location), data in sorted(device_data.items()):
            for metric, field in [("good_read_pct", "good_read_pcts"), ("no_dim_pct", "no_dim_pcts")]:
                if (customer, machine_name, location, metric) in override_set:
                    skipped_override += 1
                    print(f"  SKIP (override) {customer} / {machine_name} / {location} / {metric}")
                    continue

                values = data[field]
                stats = compute_stats(values)
                if stats is None:
                    continue

                mean, stddev, p05, p10, p90, p95, n = stats
                warn, bad = derive_thresholds(metric, mean, stddev, p05, p10, p90, p95)

                direction = "low" if metric == "good_read_pct" else "high"
                results.append({
                    "customer": customer,
                    "machine_name": machine_name,
                    "location": location,
                    "metric": metric,
                    "direction": direction,
                    "warn_value": warn,
                    "bad_value": bad,
                    "baseline_mean": round(mean, 4),
                    "baseline_stddev": round(stddev, 4),
                    "baseline_p05": round(p05, 4),
                    "baseline_p10": round(p10, 4),
                    "baseline_p90": round(p90, 4),
                    "baseline_p95": round(p95, 4),
                    "baseline_samples": n,
                })

        # Print summary
        print(f"\n{'─'*90}")
        print(f"{'CUSTOMER':<12} {'MACHINE':<8} {'LOC':<6} {'METRIC':<16} {'WARN':>8} {'BAD':>8} {'MEAN':>8} {'STDDEV':>8} {'N':>6}")
        print(f"{'─'*90}")
        for r in results:
            print(f"{r['customer']:<12} {r['machine_name']:<8} {r['location']:<6} {r['metric']:<16} "
                  f"{r['warn_value']:>8.4f} {r['bad_value']:>8.4f} {r['baseline_mean']:>8.4f} "
                  f"{r['baseline_stddev']:>8.4f} {r['baseline_samples']:>6}")
        print(f"{'─'*90}")
        print(f"\nTotal: {len(results)} threshold rows to upsert, {skipped_override} skipped (is_override=1)")

        if dry_run:
            print("\n[DRY RUN] No changes written to database.")
            return

        # Upsert
        now = datetime.now()
        inserted = updated = 0
        with conn.cursor(as_dict=True) as cur:
            for r in results:
                # Check if exists
                cur.execute("""
                    SELECT id FROM alert_thresholds
                    WHERE customer=%s AND machine_name=%s AND location=%s AND metric=%s
                """, (r["customer"], r["machine_name"], r["location"], r["metric"]))
                existing = cur.fetchone()

                if existing:
                    cur.execute("""
                        UPDATE alert_thresholds SET
                            direction=%s, warn_value=%s, bad_value=%s,
                            baseline_mean=%s, baseline_stddev=%s,
                            baseline_p05=%s, baseline_p10=%s, baseline_p90=%s, baseline_p95=%s,
                            baseline_samples=%s, lookback_days=%s,
                            last_computed=%s, updated_at=%s
                        WHERE customer=%s AND machine_name=%s AND location=%s AND metric=%s
                    """, (
                        r["direction"], r["warn_value"], r["bad_value"],
                        r["baseline_mean"], r["baseline_stddev"],
                        r["baseline_p05"], r["baseline_p10"], r["baseline_p90"], r["baseline_p95"],
                        r["baseline_samples"], lookback,
                        now, now,
                        r["customer"], r["machine_name"], r["location"], r["metric"]
                    ))
                    updated += 1
                else:
                    cur.execute("""
                        INSERT INTO alert_thresholds
                            (customer, machine_name, location, metric, direction,
                             warn_value, bad_value,
                             baseline_mean, baseline_stddev,
                             baseline_p05, baseline_p10, baseline_p90, baseline_p95,
                             baseline_samples, lookback_days, last_computed, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        r["customer"], r["machine_name"], r["location"], r["metric"], r["direction"],
                        r["warn_value"], r["bad_value"],
                        r["baseline_mean"], r["baseline_stddev"],
                        r["baseline_p05"], r["baseline_p10"], r["baseline_p90"], r["baseline_p95"],
                        r["baseline_samples"], lookback, now, now
                    ))
                    inserted += 1
        conn.commit()
        print(f"\n✅ Done — {inserted} inserted, {updated} updated.")

if __name__ == "__main__":
    main()
