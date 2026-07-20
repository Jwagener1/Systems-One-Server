import throughput
from conftest import FakeQuery


def _h(dev=1, day="2026-07-01", hour=8, parcels=600, packets=12, name="Line-01"):
    return {"device_id": dev, "customer": "ACME", "location": "DC-1",
            "machine_name": name, "day": day, "hour_of_day": hour,
            "parcels": parcels, "packet_count": packets}


def fake(hourly, today_parcels=0):
    return (FakeQuery()
            .add("/* parcels_today */", [{"parcels": today_parcels}])
            .add("AS hour_of_day", hourly))


def test_kpis_rates_peak_and_extremes():
    rows = [
        _h(hour=8, parcels=600),
        _h(hour=9, parcels=1200),
        _h(dev=2, hour=9, parcels=300, name="Line-02"),
    ]
    k = throughput.build_kpis(fake(rows, today_parcels=4500),
                              "2026-07-01", "2026-07-01")
    # fleet per (day,hour): h8=600, h9=1500 -> avg 1050
    assert k["fleet_parcels_per_hour"] == 1050.0
    assert k["parcels_today"] == 4500
    assert k["peak_hour"] == "09:00"
    assert k["peak_value"] == 1500.0
    # dev1 avg/hr = (600+1200)/2 = 900; dev2 = 300/1
    assert k["busiest_machine"] == "DC-1 / Line-01"
    assert k["lowest_machine"] == "DC-1 / Line-02"


def test_kpis_shift_summary_and_missed_packets():
    rows = [
        _h(hour=8, parcels=600),                # Shift 1
        _h(hour=15, parcels=400),               # Shift 2
        _h(hour=23, parcels=100),               # Overnight
        _h(dev=2, hour=2, parcels=50, name="Line-02"),  # Overnight
    ]
    s = throughput.build_kpis(fake(rows), "2026-07-01", "2026-07-01")["shifts"]
    by_name = {x["name"]: x["parcels"] for x in s["shifts"]}
    assert by_name == {"Shift 1": 600, "Shift 2": 400, "Overnight": 150}
    # dev1: 36 packets -> missed 252; dev2: 12 -> missed 276
    assert s["packets_received"] == 48
    assert s["packets_missed"] == 252 + 276
    assert s["expected_per_day"] == 288
    assert s["interval_minutes"] == 5


def test_kpis_empty_range():
    k = throughput.build_kpis(fake([]), "2026-07-01", "2026-07-01")
    assert k["fleet_parcels_per_hour"] == 0
    assert k["peak_hour"] is None
    assert k["busiest_machine"] is None


def test_intraday_zero_fills_24_hours():
    out = throughput.build_intraday(fake([_h(hour=8, parcels=600)]),
                                    date="2026-07-01")
    assert len(out["today"]) == 24
    assert out["today"][8] == {"hour": 8, "parcels": 600}
    assert out["today"][0] == {"hour": 0, "parcels": 0}
    assert len(out["yesterday"]) == 24


def test_by_machine_daily_rate():
    rows = [
        _h(day="2026-07-01", hour=8, parcels=600),
        _h(day="2026-07-01", hour=9, parcels=1200),
        _h(day="2026-07-02", hour=8, parcels=500),
    ]
    out = throughput.build_by_machine(fake(rows), "2026-07-01", "2026-07-02")
    assert len(out) == 1
    d = out[0]
    # 2026-07-01: 1800 parcels over 2 observed hours -> 900/hr; 07-02: 500/1
    assert d["series"] == [
        {"date": "2026-07-01", "parcels_per_hour": 900.0},
        {"date": "2026-07-02", "parcels_per_hour": 500.0},
    ]
    assert d["avg_per_hour"] == 766.7           # 2300 parcels / 3 observed hours
