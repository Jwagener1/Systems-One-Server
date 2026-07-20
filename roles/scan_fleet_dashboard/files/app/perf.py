"""Tab 1 — scan performance builders (spec §5)."""
import thresholds
import timeutil

PCT_METRICS = ("good_read", "no_read", "no_dimension", "no_weight", "item_out_of_spec")


def customer_scope(customer, allowed):
    """WHERE fragment + params for the customer filter and row-level scoping.

    customer: the UI filter value (None/'' = all customers).
    allowed:  None for unrestricted callers, else the caller's permitted list.
    """
    sql, params = "", []
    if allowed is not None:
        if not allowed:
            return " AND 1=0", []
        sql += " AND d.customer IN (" + ",".join("?" for _ in allowed) + ")"
        params += list(allowed)
    if customer:
        sql += " AND d.customer = ?"
        params.append(customer)
    return sql, params


def pct(numer, denom):
    if not denom:
        return None  # bucket with 0 items renders as a gap, not 0 (spec §2)
    return round(100.0 * numer / denom, 2)


def _daily_rows(q, start_utc, end_utc, scope_sql, scope_params):
    sql = f"""
        SELECT d.id AS device_id, d.customer, d.location, d.machine_name,
               {timeutil.day_expr()} AS day,
               SUM(s.total_items)      AS total_items,
               SUM(s.good_read)        AS good_read,
               SUM(s.no_read)          AS no_read,
               SUM(s.no_dimension)     AS no_dimension,
               SUM(s.no_weight)        AS no_weight,
               SUM(s.item_out_of_spec) AS item_out_of_spec
        FROM dbo.device_statistics s
        JOIN dbo.devices d ON d.id = s.device_id
        WHERE s.ts_datetime >= ? AND s.ts_datetime < ?{scope_sql}
        GROUP BY d.id, d.customer, d.location, d.machine_name, {timeutil.day_expr()}
        ORDER BY d.id, day
    """
    return q(sql, tuple([start_utc, end_utc] + scope_params))


def build_performance(q, date_from, date_to, customer=None, allowed=None):
    f, t = timeutil.parse_range(date_from, date_to)
    start, end = timeutil.utc_bounds(f, t)
    scope_sql, scope_params = customer_scope(customer, allowed)
    th = thresholds.load_thresholds(q)
    rows = _daily_rows(q, start, end, scope_sql, scope_params)

    devices: dict[int, dict] = {}
    sums: dict[int, list] = {}  # device_id -> [sum_good, sum_total]
    for r in rows:
        dev = devices.setdefault(r["device_id"], {
            "device_id": r["device_id"],
            "display_name": f"{r['location']} / {r['machine_name']}",
            "customer": r["customer"],
            "target_pct": thresholds.good_read_target(
                th, r["customer"], r["machine_name"], r["location"]),
            "series": [],
        })
        total = r["total_items"] or 0
        point = {"date": r["day"], "total_items": total}
        for m in PCT_METRICS:
            point[f"{m}_pct"] = pct(r[m] or 0, total)
        dev["series"].append(point)
        acc = sums.setdefault(r["device_id"], [0, 0])
        acc[0] += r["good_read"] or 0
        acc[1] += total

    for dev_id, dev in devices.items():
        good, total = sums[dev_id]
        current = pct(good, total)
        dev["current_good_read_pct"] = current
        dev["below_target"] = current is not None and current < dev["target_pct"]

    return sorted(devices.values(), key=lambda d: (d["customer"], d["display_name"]))


def build_machines(q, date_from, date_to, customer=None, allowed=None):
    return [
        {k: v for k, v in d.items() if k != "series"}
        for d in build_performance(q, date_from, date_to, customer, allowed)
    ]
