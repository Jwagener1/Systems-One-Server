import thresholds

ROWS = [
    {"customer": "ACME", "machine_name": "Line-01", "location": "DC-1",
     "metric": "good_read_pct", "direction": "low", "warn_value": 95.0, "bad_value": 91.0},
    {"customer": "ACME", "machine_name": None, "location": None,
     "metric": "good_read_pct", "direction": "low", "warn_value": 96.0, "bad_value": 92.0},
]


def test_machine_specific_row_wins():
    assert thresholds.good_read_target(ROWS, "ACME", "Line-01", "DC-1") == 91.0


def test_customer_wide_fallback():
    assert thresholds.good_read_target(ROWS, "ACME", "Line-02", "DC-1") == 92.0


def test_global_default_when_no_rows_match():
    assert thresholds.good_read_target(ROWS, "Other", "X", "Y") == 93.0


def test_accepts_both_metric_spellings():
    rows = [dict(ROWS[0], metric="good_read")]
    assert thresholds.good_read_target(rows, "ACME", "Line-01", "DC-1") == 91.0


def test_location_mismatch_does_not_match_machine_row():
    # Same machine name at a different location must not inherit DC-1's row.
    assert thresholds.good_read_target(ROWS, "ACME", "Line-01", "DC-9") == 92.0


def test_warn_bad_pair():
    assert thresholds.warn_bad(ROWS, "ACME", "Line-01", "DC-1", ("good_read_pct",)) == (95.0, 91.0)
    assert thresholds.warn_bad(ROWS, "Other", "X", "Y", ("good_read_pct",)) == (None, None)
