import datetime

import pytest

import timeutil


def test_parse_range_ok():
    f, t = timeutil.parse_range("2026-07-01", "2026-07-20")
    assert f == datetime.date(2026, 7, 1)
    assert t == datetime.date(2026, 7, 20)


def test_parse_range_rejects_bad_format():
    with pytest.raises(ValueError):
        timeutil.parse_range("07/01/2026", "2026-07-20")


def test_parse_range_rejects_reversed():
    with pytest.raises(ValueError):
        timeutil.parse_range("2026-07-20", "2026-07-01")


def test_parse_range_rejects_huge_span():
    with pytest.raises(ValueError):
        timeutil.parse_range("2020-01-01", "2026-07-20")


def test_utc_bounds_shifts_back_by_display_offset():
    start, end = timeutil.utc_bounds(datetime.date(2026, 7, 1), datetime.date(2026, 7, 1))
    # local midnight − 2h; half-open end at next local midnight − 2h
    assert start == datetime.datetime(2026, 6, 30, 22, 0)
    assert end == datetime.datetime(2026, 7, 1, 22, 0)


def test_sql_fragments_are_the_single_conversion_point():
    assert timeutil.local_expr("s.ts_datetime") == "DATEADD(HOUR, 2, s.ts_datetime)"
    assert timeutil.day_expr() == "CAST(DATEADD(HOUR, 2, s.ts_datetime) AS date)"
    assert timeutil.hour_of_day_expr() == "DATEPART(HOUR, DATEADD(HOUR, 2, s.ts_datetime))"
