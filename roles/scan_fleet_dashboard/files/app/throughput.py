"""Tab 2 — throughput builders (spec §7).

Rates are per *observed* hour (hours that produced at least one packet), so a
machine idle half the day still shows its true running rate.
TODO(DECIDE): no capacity/target line yet; add via
thresholds.resolve_target(rows, customer, machine, location, ("throughput",)).
"""
import datetime

import config
import timeutil
from perf import customer_scope

_METRIC = config.THROUGHPUT_METRIC  # validated in config.py


def _display_name(r):
    return f"{r['location']} / {r['machine_name']}"


def _hourly_rows(q, start_utc, end_utc, scope_sql, scope_params):
    sql = f"""
        SELECT s.device_id, d.customer, d.location, d.machine_name,
               {timeutil.day_expr()} AS day,
               {timeutil.hour_of_day_expr()} AS hour_of_day,
               SUM(s.{_METRIC}) AS parcels,
               COUNT(*)         AS packet_count
        FROM dbo.device_statistics s
        JOIN dbo.devices d ON d.id = s.device_id
        WHERE s.ts_datetime >= ? AND s.ts_datetime < ?{scope_sql}
        GROUP BY s.device_id, d.customer, d.location, d.machine_name,
                 {timeutil.day_expr()}, {timeutil.hour_of_day_expr()}
        ORDER BY s.device_id, day, hour_of_day
    """
    return q(sql, tuple([start_utc, end_utc] + scope_params))


def _parcels_today(q, scope_sql, scope_params):
    start, end = timeutil.utc_bounds(timeutil.today_local(), timeutil.today_local())
    sql = f"""
        SELECT /* parcels_today */ COALESCE(SUM(s.{_METRIC}), 0) AS parcels
        FROM dbo.device_statistics s
        JOIN dbo.devices d ON d.id = s.device_id
        WHERE s.ts_datetime >= ? AND s.ts_datetime < ?{scope_sql}
    """
    rows = q(sql, tuple([start, end] + scope_params))
    return int(rows[0]["parcels"] or 0) if rows else 0


def _shift_of(hour):
    for name, lo, hi in config.SHIFTS:
        if lo < hi and lo <= hour < hi:
            return name
        if lo > hi and (hour >= lo or hour < hi):  # overnight wrap
            return name
    return config.SHIFTS[-1][0]


def build_kpis(q, date_from, date_to, customer=None, allowed=None):
    f, t = timeutil.parse_range(date_from, date_to)
    start, end = timeutil.utc_bounds(f, t)
    scope_sql, scope_params = customer_scope(customer, allowed)
    rows = _hourly_rows(q, start, end, scope_sql, scope_params)

    fleet_hours: dict[tuple, float] = {}      # (day, hour) -> fleet parcels
    machines: dict[int, dict] = {}            # device -> {name, parcels, hours}
    shift_parcels = {name: 0 for name, _, _ in config.SHIFTS}
    device_day_packets: dict[tuple, int] = {}  # (device, day) -> packets

    for r in rows:
        p = r["parcels"] or 0
        key = (r["day"], r["hour_of_day"])
        fleet_hours[key] = fleet_hours.get(key, 0) + p
        m = machines.setdefault(r["device_id"],
                                {"name": _display_name(r), "parcels": 0, "hours": 0})
        m["parcels"] += p
        m["hours"] += 1
        shift_parcels[_shift_of(r["hour_of_day"])] += p
        dd = (r["device_id"], r["day"])
        device_day_packets[dd] = device_day_packets.get(dd, 0) + (r["packet_count"] or 0)

    fleet_rate = (round(sum(fleet_hours.values()) / len(fleet_hours), 1)
                  if fleet_hours else 0)

    # Peak hour-of-day: average fleet parcels/hour per hour-of-day, take max.
    hod: dict[int, list] = {}
    for (day, hour), p in fleet_hours.items():
        hod.setdefault(hour, []).append(p)
    peak_hour, peak_value = None, None
    if hod:
        h = max(hod, key=lambda h: sum(hod[h]) / len(hod[h]))
        peak_hour = f"{h:02d}:00"
        peak_value = round(sum(hod[h]) / len(hod[h]), 1)

    rates = {dev: m["parcels"] / m["hours"]
             for dev, m in machines.items() if m["hours"]}
    busiest = machines[max(rates, key=rates.get)]["name"] if rates else None
    lowest = machines[min(rates, key=rates.get)]["name"] if rates else None

    received = sum(device_day_packets.values())
    missed = sum(max(0, config.EXPECTED_PACKETS_PER_DAY - c)
                 for c in device_day_packets.values())

    windows = {name: f"{lo:02d}:00–{hi:02d}:00" for name, lo, hi in config.SHIFTS}
    return {
        "fleet_parcels_per_hour": fleet_rate,
        "parcels_today": _parcels_today(q, scope_sql, scope_params),
        "peak_hour": peak_hour,
        "peak_value": peak_value,
        "busiest_machine": busiest,
        "lowest_machine": lowest,
        "shifts": {
            "shifts": [{"name": name, "window": windows[name],
                        "parcels": shift_parcels[name]}
                       for name, _, _ in config.SHIFTS],
            "packets_received": received,
            "packets_missed": missed,
            "expected_per_day": config.EXPECTED_PACKETS_PER_DAY,
            "interval_minutes": 5,
        },
    }


def _profile_for_date(q, day, device_id, scope_sql, scope_params):
    start, end = timeutil.utc_bounds(day, day)
    dev_sql = " AND s.device_id = ?" if device_id else ""
    params = [start, end] + scope_params + ([device_id] if device_id else [])
    rows = q(f"""
        SELECT {timeutil.hour_of_day_expr()} AS hour_of_day,
               SUM(s.{_METRIC}) AS parcels,
               COUNT(*)         AS packet_count
        FROM dbo.device_statistics s
        JOIN dbo.devices d ON d.id = s.device_id
        WHERE s.ts_datetime >= ? AND s.ts_datetime < ?{scope_sql}{dev_sql}
        GROUP BY {timeutil.hour_of_day_expr()}
        ORDER BY hour_of_day
    """, tuple(params))
    by_hour = {r["hour_of_day"]: int(r["parcels"] or 0) for r in rows}
    return [{"hour": h, "parcels": by_hour.get(h, 0)} for h in range(24)]


def build_intraday(q, device_id=None, date=None, customer=None, allowed=None):
    day = (datetime.date.fromisoformat(date) if date else timeutil.today_local())
    scope_sql, scope_params = customer_scope(customer, allowed)
    return {
        "date": day.isoformat(),
        "today": _profile_for_date(q, day, device_id, scope_sql, scope_params),
        "yesterday": _profile_for_date(
            q, day - datetime.timedelta(days=1), device_id, scope_sql, scope_params),
    }


def build_by_machine(q, date_from, date_to, customer=None, allowed=None):
    f, t = timeutil.parse_range(date_from, date_to)
    start, end = timeutil.utc_bounds(f, t)
    scope_sql, scope_params = customer_scope(customer, allowed)
    rows = _hourly_rows(q, start, end, scope_sql, scope_params)

    machines: dict[int, dict] = {}
    days: dict[tuple, list] = {}  # (device, day) -> [parcels, hours]
    for r in rows:
        machines.setdefault(r["device_id"], {
            "device_id": r["device_id"],
            "display_name": _display_name(r),
            "customer": r["customer"],
            "_parcels": 0, "_hours": 0,
        })
        m = machines[r["device_id"]]
        p = r["parcels"] or 0
        m["_parcels"] += p
        m["_hours"] += 1
        acc = days.setdefault((r["device_id"], r["day"]), [0, 0])
        acc[0] += p
        acc[1] += 1

    out = []
    for dev_id, m in machines.items():
        series = [
            {"date": day, "parcels_per_hour": round(v[0] / v[1], 1)}
            for (d_id, day), v in sorted(days.items()) if d_id == dev_id
        ]
        out.append({
            "device_id": m["device_id"],
            "display_name": m["display_name"],
            "customer": m["customer"],
            "avg_per_hour": round(m["_parcels"] / m["_hours"], 1) if m["_hours"] else 0,
            "series": series,
        })
    return sorted(out, key=lambda d: (d["customer"], d["display_name"]))
