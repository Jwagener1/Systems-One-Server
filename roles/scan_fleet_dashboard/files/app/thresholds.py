"""Target/threshold resolution (spec §6).

Resolution order: machine-specific row -> customer-wide row (machine_name NULL)
-> caller-supplied default. The dashed line uses bad_value (the hard limit).
TODO(DECIDE): switch to warn_value if the softer line is preferred.

Live-data note: the deployed alert_thresholds table stores the good-read metric
as 'good_read_pct' (spec says 'good_read') — we accept both spellings.
"""
import config

GOOD_READ_METRICS = ("good_read", "good_read_pct")

_SQL = """
    SELECT customer, machine_name, location, metric, direction, warn_value, bad_value
    FROM dbo.alert_thresholds
"""


def load_thresholds(q) -> list[dict]:
    return q(_SQL)


def _best_row(rows, customer, machine_name, location, metrics):
    metrics = (metrics,) if isinstance(metrics, str) else tuple(metrics)
    machine_any = None   # machine row not pinned to a location
    fallback = None      # customer-wide row
    for r in rows:
        if r["metric"] not in metrics or r["customer"] != customer:
            continue
        if r["machine_name"] == machine_name:
            loc = r.get("location")
            if loc == location:
                return r                 # most specific: machine + exact location
            if loc is None and machine_any is None:
                machine_any = r
        elif r["machine_name"] is None and fallback is None:
            fallback = r                 # customer-wide rows are not location-filtered by design
    return machine_any or fallback


def resolve_target(rows, customer, machine_name, location, metrics, default=None):
    row = _best_row(rows, customer, machine_name, location, metrics)
    if row is not None and row["bad_value"] is not None:
        return float(row["bad_value"])
    return default


def warn_bad(rows, customer, machine_name, location, metrics):
    row = _best_row(rows, customer, machine_name, location, metrics)
    if row is None:
        return None, None
    return (
        float(row["warn_value"]) if row["warn_value"] is not None else None,
        float(row["bad_value"]) if row["bad_value"] is not None else None,
    )


def good_read_target(rows, customer, machine_name, location) -> float:
    return resolve_target(
        rows, customer, machine_name, location,
        GOOD_READ_METRICS, config.GOOD_READ_DEFAULT_TARGET,
    )
