"""ALL timezone conversion and time bucketing lives here (spec §2: one place)."""
import datetime

import config

_OFFSET = datetime.timedelta(hours=config.TZ_OFFSET_HOURS)


def local_expr(col: str = "s.ts_datetime") -> str:
    """SQL expression converting a UTC datetime column to display-local time."""
    return f"DATEADD(HOUR, {config.TZ_OFFSET_HOURS}, {col})"


def day_expr(col: str = "s.ts_datetime") -> str:
    """Daily bucket expression (local date)."""
    return f"CAST({local_expr(col)} AS date)"


def hour_of_day_expr(col: str = "s.ts_datetime") -> str:
    """Local hour-of-day (0-23)."""
    return f"DATEPART(HOUR, {local_expr(col)})"


def parse_range(date_from: str, date_to: str, max_days: int = 366):
    try:
        f = datetime.date.fromisoformat(date_from)
        t = datetime.date.fromisoformat(date_to)
    except (TypeError, ValueError):
        raise ValueError("dates must be YYYY-MM-DD")
    if f > t:
        raise ValueError("date_from must be <= date_to")
    if (t - f).days + 1 > max_days:
        raise ValueError(f"range limited to {max_days} days")
    return f, t


def utc_bounds(local_from: datetime.date, local_to: datetime.date):
    """Half-open UTC [start, end) covering the inclusive local-date range.

    WHERE clauses stay raw range scans on ts_datetime (index-friendly) instead
    of wrapping the indexed column in DATEADD.
    """
    start = datetime.datetime.combine(local_from, datetime.time.min) - _OFFSET
    end = (
        datetime.datetime.combine(local_to + datetime.timedelta(days=1), datetime.time.min)
        - _OFFSET
    )
    return start, end


def today_local() -> datetime.date:
    return (datetime.datetime.utcnow() + _OFFSET).date()
