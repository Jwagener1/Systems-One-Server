from fastapi.testclient import TestClient

import perf
from conftest import FakeQuery


def _row(dev=1, day="2026-07-01", total=1000, good=950, **kw):
    base = {
        "device_id": dev, "customer": "ACME", "location": "DC-1",
        "machine_name": f"Line-0{dev}", "day": day, "total_items": total,
        "good_read": good, "no_read": 30, "no_dimension": 10,
        "no_weight": 5, "item_out_of_spec": 5,
    }
    base.update(kw)
    return base


def fake(rows, thresholds=()):
    return (FakeQuery()
            .add("dbo.alert_thresholds", list(thresholds))
            .add("dbo.device_statistics", rows))


def test_series_pct_headline_and_default_target():
    q = fake([_row(day="2026-07-01"), _row(day="2026-07-02", good=900)])
    out = perf.build_performance(q, "2026-07-01", "2026-07-02")
    assert len(out) == 1
    d = out[0]
    assert d["display_name"] == "DC-1 / Line-01"
    assert d["series"][0]["good_read_pct"] == 95.0
    assert d["series"][0]["no_read_pct"] == 3.0
    assert d["current_good_read_pct"] == 92.5   # (950+900)/2000
    assert d["target_pct"] == 93.0              # global default
    assert d["below_target"] is True


def test_threshold_row_overrides_default():
    th = [{"customer": "ACME", "machine_name": "Line-01", "location": "DC-1",
           "metric": "good_read_pct", "direction": "low",
           "warn_value": 95.0, "bad_value": 90.0}]
    d = perf.build_performance(fake([_row()], th), "2026-07-01", "2026-07-01")[0]
    assert d["target_pct"] == 90.0
    assert d["below_target"] is False           # 95.0 >= 90


def test_zero_total_bucket_is_gap_not_zero():
    q = fake([_row(total=0, good=0, no_read=0, no_dimension=0,
                   no_weight=0, item_out_of_spec=0)])
    d = perf.build_performance(q, "2026-07-01", "2026-07-01")[0]
    assert d["series"][0]["good_read_pct"] is None
    assert d["current_good_read_pct"] is None
    assert d["below_target"] is False


def test_customer_scope_fragments():
    assert perf.customer_scope(None, None) == ("", [])
    assert perf.customer_scope("ACME", None) == (" AND d.customer = ?", ["ACME"])
    sql, params = perf.customer_scope(None, ["A", "B"])
    assert sql == " AND d.customer IN (?,?)"
    assert params == ["A", "B"]
    sql, _ = perf.customer_scope(None, [])
    assert "1=0" in sql                          # mapped to no customers -> sees nothing


def test_api_performance_endpoint(monkeypatch):
    import db
    import main
    monkeypatch.setattr(db, "query", fake([_row()]))
    client = TestClient(main.app)
    r = client.get("/api/performance", params={
        "date_from": "2026-07-01", "date_to": "2026-07-01"})
    assert r.status_code == 200
    assert r.json()[0]["device_id"] == 1


def test_api_performance_bad_range_is_400(monkeypatch):
    import db
    import main
    monkeypatch.setattr(db, "query", fake([]))
    client = TestClient(main.app)
    r = client.get("/api/performance", params={
        "date_from": "2026-07-02", "date_to": "2026-07-01"})
    assert r.status_code == 400


def test_api_machines_has_no_series(monkeypatch):
    import db
    import main
    monkeypatch.setattr(db, "query", fake([_row()]))
    client = TestClient(main.app)
    r = client.get("/api/machines", params={
        "date_from": "2026-07-01", "date_to": "2026-07-01"})
    assert r.status_code == 200
    assert "series" not in r.json()[0]
    assert r.json()[0]["current_good_read_pct"] == 95.0
