import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "files"))

import cards  # noqa: E402


class TestBuildCard(unittest.TestCase):
    def test_envelope_fields(self):
        card = cards.build_card([{"type": "TextBlock", "text": "hi"}])
        self.assertEqual(card["type"], "AdaptiveCard")
        self.assertEqual(card["version"], "1.4")
        self.assertEqual(card["$schema"], "http://adaptivecards.io/schemas/adaptive-card.json")
        self.assertEqual(card["body"], [{"type": "TextBlock", "text": "hi"}])


class TestBuildTable(unittest.TestCase):
    def test_header_plus_rows(self):
        elements = cards.build_table(["Device", "Items"], [["scanner-1", "42"], ["scanner-2", "7"]])
        self.assertEqual(len(elements), 3)  # header + 2 rows
        for el in elements:
            self.assertEqual(el["type"], "ColumnSet")
            self.assertEqual(len(el["columns"]), 2)
        header_texts = [c["items"][0]["text"] for c in elements[0]["columns"]]
        self.assertEqual(header_texts, ["Device", "Items"])
        self.assertTrue(elements[0]["columns"][0]["items"][0]["weight"] == "bolder")
        row1_texts = [c["items"][0]["text"] for c in elements[1]["columns"]]
        self.assertEqual(row1_texts, ["scanner-1", "42"])

    def test_empty_rows_returns_header_only(self):
        elements = cards.build_table(["Device"], [])
        self.assertEqual(len(elements), 1)


class TestOfflineAlertCard(unittest.TestCase):
    def test_contains_device_and_count(self):
        devices = [
            {"machine_name": "scanner-1", "location": "DC1", "customer": "PEPKOR",
             "last_seen": "2026-08-11T09:00:00", "minutes_ago": 45},
        ]
        card = cards.build_offline_alert_card(devices)
        text_blob = str(card)
        self.assertIn("scanner-1", text_blob)
        self.assertIn("PEPKOR", text_blob)
        self.assertIn("1 Device", text_blob)
        self.assertEqual(card["body"][0]["style"] if "style" in card["body"][0] else None,
                          card["body"][0].get("style"))  # first block exists


class TestRecoveryCard(unittest.TestCase):
    def test_contains_device(self):
        devices = [{"machine_name": "scanner-1", "location": "DC1", "customer": "PEPKOR",
                    "last_seen": "2026-08-11T09:00:00", "minutes_ago": 0, "downtime_minutes": 45}]
        card = cards.build_recovery_card(devices)
        self.assertIn("scanner-1", str(card))
        self.assertIn("good", str(card))  # attention/good container style present


class TestUploadAlertCard(unittest.TestCase):
    def test_contains_unsent_count(self):
        devices = [{
            "machine_name": "scanner-2", "location": "DC1", "customer": "MADIBANA",
            "total_not_sent": 12,
            "packets": [{"ts_datetime": "2026-08-11T09:00:00", "not_sent": 4}],
        }]
        card = cards.build_upload_alert_card(devices)
        self.assertIn("scanner-2", str(card))
        self.assertIn("12", str(card))


class TestUploadRecoveryCard(unittest.TestCase):
    def test_contains_device(self):
        devices = [{"machine_name": "scanner-2", "location": "DC1", "customer": "MADIBANA"}]
        card = cards.build_upload_recovery_card(devices)
        self.assertIn("scanner-2", str(card))


class TestCustomerSectionCard(unittest.TestCase):
    def test_includes_charts_and_tables(self):
        card = cards.build_customer_section_card(
            customer="PEPKOR",
            days=7,
            anomalies=["Device scanner-1 good-read 82% (below 90% threshold)"],
            today_table={"headers": ["Device", "Items"], "rows": [["scanner-1", "100"]]},
            week_table={"headers": ["Device", "Items"], "rows": [["scanner-1", "700"]]},
            storage_table={"headers": ["Device", "Usage"], "rows": [["scanner-1", "45%"]]},
            chart_urls={"volume": "https://charts.example.com/a.png", "goodread": None, "hourly": None},
        )
        blob = str(card)
        self.assertIn("PEPKOR", blob)
        self.assertIn("https://charts.example.com/a.png", blob)
        self.assertIn("below 90% threshold", blob)

    def test_kpis_included_when_provided(self):
        card = cards.build_customer_section_card(
            customer="MADIBANA", days=30, anomalies=[],
            today_table={"headers": [], "rows": []},
            week_table={"headers": [], "rows": []},
            storage_table={"headers": [], "rows": []},
            chart_urls={"volume": None, "goodread": None, "hourly": None},
            kpis={"total_items": 5000, "avg_good_read_pct": 96.5, "active_devices": 3},
        )
        blob = str(card)
        self.assertIn("5,000", blob)
        self.assertIn("96.5", blob)


if __name__ == "__main__":
    unittest.main()
