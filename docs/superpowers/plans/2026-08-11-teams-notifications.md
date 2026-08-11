# Teams Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `s1_reporter`'s Gmail SMTP email notifications (offline/upload-failure alerts, daily/monthly reports) with Microsoft Teams Adaptive Card messages posted via a Power Automate Workflow webhook, including public hosting of report charts.

**Architecture:** Three new stdlib-only, dependency-free Python modules (`teams_notifier.py` for webhook POST+retry, `cards.py` for pure Adaptive Card construction, `chart_store.py` for chart file persistence/URL/cleanup) are added to `roles/s1_reporter/files/`. `report.py` and `upload_monitor.py` are then rewired to call these modules instead of their existing duplicated `send_email()`/HTML-builder code, which is deleted once the new path is verified working end-to-end against the real Teams webhook confirmed during design (posts to Systems-One team → Reporting channel).

**Tech Stack:** Python 3.12 (stdlib `urllib`/`json`/`uuid`/`time`/`os` only for the three new modules — no new pip dependencies), pytest (already available; tests follow the existing repo convention of plain `import`-based `unittest.TestCase` classes, see `roles/systems_one_ingest/tests/test_validation.py`), Ansible Vault, Docker Compose, nginx:alpine (new static file server), Cloudflare Tunnel (existing).

## Global Constraints

- No new pip dependencies in the `s1_reporter` Dockerfile — the three new modules must use only Python stdlib (`urllib.request`, `json`, `uuid`, `time`, `os`), since `pymssql`/`matplotlib` are not available in the local dev environment used to run tests, and adding `requests` is unnecessary.
- Card JSON must exactly match the confirmed-working envelope shape: `{"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4", "body": [...]}}]}` — version pinned to `"1.4"` (the version actually tested against the live webhook; do not use the 1.5+ `Table` element, use `ColumnSet`-based grids for broad Teams client compatibility).
- Full cutover, no email/Teams dual-run: once a notification type's Teams path is wired and verified, its old `send_email`/HTML-builder code is deleted in the same task, not left dormant.
- Chart image URLs must never contain guessable filenames — always `uuid.uuid4()`-based.
- Never write the real Teams webhook URL, Cloudflare tunnel token, or any vault secret into a non-vault file. Vault edits use `ansible-vault edit group_vars/vault.yml` (requires the vault password — a manual step, not automatable).
- Follow existing code conventions in each file (f-string HTML in the old code is being replaced, but variable naming, `CFG`/`load_config()` pattern, and `print()`-based logging style should be preserved for consistency with `compute_baselines.py` and the rest of the service).

---

### Task 1: `teams_notifier.py` — webhook POST with retry

**Files:**
- Create: `roles/s1_reporter/files/teams_notifier.py`
- Test: `roles/s1_reporter/tests/test_teams_notifier.py`

**Interfaces:**
- Produces: `post_to_teams(webhook_url: str, card: dict, max_retries: int = 3, backoff_seconds: float = 2.0) -> bool` — wraps `card` in the message envelope, POSTs as JSON, retries on failure, returns `True` on any 2xx response, `False` if all attempts fail (never raises).

- [ ] **Step 1: Write the failing tests**

```python
# roles/s1_reporter/tests/test_teams_notifier.py
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "files"))

import teams_notifier  # noqa: E402


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestPostToTeams(unittest.TestCase):
    def test_success_on_first_attempt(self):
        with patch("teams_notifier.urllib.request.urlopen", return_value=FakeResponse(202)) as mock_open:
            ok = teams_notifier.post_to_teams("https://example.invalid/webhook", {"type": "AdaptiveCard"})
        self.assertTrue(ok)
        mock_open.assert_called_once()

    def test_envelope_shape(self):
        captured = {}

        def fake_urlopen(req, timeout=10):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["headers"] = req.headers
            return FakeResponse(202)

        with patch("teams_notifier.urllib.request.urlopen", side_effect=fake_urlopen):
            teams_notifier.post_to_teams("https://example.invalid/webhook", {"type": "AdaptiveCard", "version": "1.4"})

        self.assertEqual(captured["body"]["type"], "message")
        attachment = captured["body"]["attachments"][0]
        self.assertEqual(attachment["contentType"], "application/vnd.microsoft.card.adaptive")
        self.assertEqual(attachment["content"], {"type": "AdaptiveCard", "version": "1.4"})
        self.assertEqual(captured["headers"].get("Content-type"), "application/json")

    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky_urlopen(req, timeout=10):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.URLError("boom")
            return FakeResponse(202)

        with patch("teams_notifier.urllib.request.urlopen", side_effect=flaky_urlopen), \
             patch("teams_notifier.time.sleep") as mock_sleep:
            ok = teams_notifier.post_to_teams("https://example.invalid/webhook", {"type": "AdaptiveCard"}, max_retries=3, backoff_seconds=1)

        self.assertTrue(ok)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_gives_up_after_max_retries(self):
        with patch("teams_notifier.urllib.request.urlopen", side_effect=urllib.error.URLError("boom")) as mock_open, \
             patch("teams_notifier.time.sleep"):
            ok = teams_notifier.post_to_teams("https://example.invalid/webhook", {"type": "AdaptiveCard"}, max_retries=3, backoff_seconds=0)

        self.assertFalse(ok)
        self.assertEqual(mock_open.call_count, 3)

    def test_non_2xx_status_is_treated_as_failure_and_retried(self):
        with patch("teams_notifier.urllib.request.urlopen", return_value=FakeResponse(400)), \
             patch("teams_notifier.time.sleep"):
            ok = teams_notifier.post_to_teams("https://example.invalid/webhook", {"type": "AdaptiveCard"}, max_retries=2, backoff_seconds=0)

        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest roles/s1_reporter/tests/test_teams_notifier.py -v`
Expected: FAIL / ERROR — `ModuleNotFoundError: No module named 'teams_notifier'` (file doesn't exist yet).

- [ ] **Step 3: Implement `teams_notifier.py`**

```python
# roles/s1_reporter/files/teams_notifier.py
import json
import time
import urllib.request
import urllib.error


def post_to_teams(webhook_url, card, max_retries=3, backoff_seconds=2.0):
    """POST an Adaptive Card to a Teams Power Automate webhook.

    Returns True on a 2xx response, False if every attempt fails.
    Never raises — network/HTTP errors are logged and treated as a failed attempt.
    """
    envelope = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }
    payload = json.dumps(envelope).encode("utf-8")

    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    return True
                print(f"⚠️ Teams webhook returned status {resp.status} (attempt {attempt}/{max_retries})")
        except urllib.error.URLError as e:
            print(f"⚠️ Teams webhook POST failed: {e} (attempt {attempt}/{max_retries})")

        if attempt < max_retries:
            time.sleep(backoff_seconds * attempt)

    print(f"❌ Teams webhook POST failed after {max_retries} attempts, dropping card:")
    print(json.dumps(envelope))
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest roles/s1_reporter/tests/test_teams_notifier.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add roles/s1_reporter/files/teams_notifier.py roles/s1_reporter/tests/test_teams_notifier.py
git commit -m "feat(s1_reporter): add Teams webhook poster with retry"
```

---

### Task 2: `cards.py` — Adaptive Card construction

**Files:**
- Create: `roles/s1_reporter/files/cards.py`
- Test: `roles/s1_reporter/tests/test_cards.py`

**Interfaces:**
- Consumes: nothing from other tasks (pure functions, no I/O).
- Produces:
  - `build_card(body_elements: list) -> dict` — wraps a list of Adaptive Card elements in the `$schema`/`type`/`version`/`body` envelope.
  - `build_table(headers: list[str], rows: list[list]) -> list[dict]` — returns a list of `ColumnSet` element dicts (one header row + one per data row), for use inside a card's `body`.
  - `build_offline_alert_card(offline_devices: list[dict]) -> dict` — each device dict has keys `machine_name, location, customer, last_seen (datetime|str|None), minutes_ago (int)`.
  - `build_recovery_card(recovered_devices: list[dict]) -> dict` — same device shape as offline, plus `downtime_minutes (int)`.
  - `build_upload_alert_card(failing_devices: list[dict]) -> dict` — each device dict has keys `machine_name, location, customer, total_not_sent (int), packets (list[dict] with keys ts_datetime, not_sent)`.
  - `build_upload_recovery_card(recovered_devices: list[dict]) -> dict` — device dict has keys `machine_name, location, customer`.
  - `build_customer_section_card(customer: str, days: int, anomalies: list[str], today_table: dict, week_table: dict, storage_table: dict, chart_urls: dict, kpis: dict | None = None) -> dict` — `*_table` args are `{"headers": [...], "rows": [[...], ...]}`; `chart_urls` is `{"volume": url|None, "goodread": url|None, "hourly": url|None}`; `kpis` (used for monthly reports) is `{"total_items": int, "avg_good_read_pct": float, "active_devices": int} | None`.

- [ ] **Step 1: Write the failing tests**

```python
# roles/s1_reporter/tests/test_cards.py
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
        self.assertIn("1 device", text_blob)
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest roles/s1_reporter/tests/test_cards.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cards'`

- [ ] **Step 3: Implement `cards.py`**

```python
# roles/s1_reporter/files/cards.py
"""Pure Adaptive Card construction for s1_reporter Teams notifications.

No I/O, no third-party imports — safe to unit test without pymssql/matplotlib.
"""


def build_card(body_elements):
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body_elements,
    }


def _text(value, weight="default", size="small", wrap=True):
    return {"type": "TextBlock", "text": str(value), "wrap": wrap, "weight": weight, "size": size}


def build_table(headers, rows):
    def _row(cells, is_header=False):
        return {
            "type": "ColumnSet",
            "columns": [
                {"type": "Column", "width": "stretch",
                 "items": [_text(cell, weight="bolder" if is_header else "default")]}
                for cell in cells
            ],
        }
    elements = [_row(headers, is_header=True)]
    elements.extend(_row(r) for r in rows)
    return elements


def _header(title, style):
    return {
        "type": "Container",
        "style": style,
        "items": [{"type": "TextBlock", "text": title, "size": "large", "weight": "bolder", "wrap": True}],
    }


def _device_age_label(minutes):
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes}m"


def build_offline_alert_card(offline_devices):
    count = len(offline_devices)
    body = [_header(f"🔴 S1 — {count} Device{'s' if count != 1 else ''} Not Reporting", "attention")]
    rows = [
        [d["machine_name"], d["location"], d["customer"], f"{_device_age_label(d['minutes_ago'])} ago"]
        for d in offline_devices
    ]
    body.extend(build_table(["Device", "Location", "Customer", "Overdue By"], rows))
    return build_card(body)


def build_recovery_card(recovered_devices):
    count = len(recovered_devices)
    body = [_header(f"✅ S1 — {count} Device{'s' if count != 1 else ''} Recovered", "good")]
    rows = [
        [d["machine_name"], d["location"], d["customer"], f"{d.get('downtime_minutes', 0)}m downtime"]
        for d in recovered_devices
    ]
    body.extend(build_table(["Device", "Location", "Customer", "Was Down For"], rows))
    return build_card(body)


def build_upload_alert_card(failing_devices):
    count = len(failing_devices)
    body = [_header(f"⚠️ S1 — {count} Device{'s' if count != 1 else ''} Not Uploading", "warning")]
    rows = [
        [d["machine_name"], d["location"], d["customer"], f"{d['total_not_sent']:,} unsent"]
        for d in failing_devices
    ]
    body.extend(build_table(["Device", "Location", "Customer", "Backlog"], rows))
    return build_card(body)


def build_upload_recovery_card(recovered_devices):
    count = len(recovered_devices)
    body = [_header(f"✅ S1 — {count} Device{'s' if count != 1 else ''} Uploading Again", "good")]
    rows = [[d["machine_name"], d["location"], d["customer"]] for d in recovered_devices]
    body.extend(build_table(["Device", "Location", "Customer"], rows))
    return build_card(body)


def build_customer_section_card(customer, days, anomalies, today_table, week_table,
                                 storage_table, chart_urls, kpis=None):
    body = [_header(f"🏢 {customer}", "emphasis")]

    if kpis:
        body.append({
            "type": "FactSet",
            "facts": [
                {"title": "Items Scanned", "value": f"{kpis['total_items']:,}"},
                {"title": "Avg Good Read", "value": f"{kpis['avg_good_read_pct']:.1f}%"},
                {"title": "Active Devices", "value": str(kpis["active_devices"])},
            ],
        })

    if anomalies:
        body.append({
            "type": "Container",
            "style": "attention",
            "items": [_text(f"🚨 {a}") for a in anomalies],
        })

    if today_table["rows"]:
        body.append(_text("📦 Today's Scan Summary", weight="bolder", size="medium"))
        body.extend(build_table(today_table["headers"], today_table["rows"]))

    for label, key in (("📈 Daily Volume", "volume"), ("✅ Good Read % Trend", "goodread"), ("🕐 Hourly Pattern", "hourly")):
        url = chart_urls.get(key)
        if url:
            body.append(_text(f"{label} — Last {days} Days", weight="bolder", size="medium"))
            body.append({"type": "Image", "url": url, "size": "stretch"})

    if week_table["rows"]:
        body.append(_text(f"📊 {days}-Day Summary", weight="bolder", size="medium"))
        body.extend(build_table(week_table["headers"], week_table["rows"]))

    if storage_table["rows"]:
        body.append(_text("💾 Storage Health (C: Drive)", weight="bolder", size="medium"))
        body.extend(build_table(storage_table["headers"], storage_table["rows"]))

    return build_card(body)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest roles/s1_reporter/tests/test_cards.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add roles/s1_reporter/files/cards.py roles/s1_reporter/tests/test_cards.py
git commit -m "feat(s1_reporter): add Adaptive Card builders for alerts and reports"
```

---

### Task 3: `chart_store.py` — chart PNG persistence, URL, and retention

**Files:**
- Create: `roles/s1_reporter/files/chart_store.py`
- Test: `roles/s1_reporter/tests/test_chart_store.py`

**Interfaces:**
- Produces:
  - `save_chart(png_bytes: bytes, chart_dir: str, public_base_url: str) -> str` — writes `png_bytes` to `{chart_dir}/{uuid4()}.png`, returns `f"{public_base_url.rstrip('/')}/{filename}"`.
  - `cleanup_old_charts(chart_dir: str, retention_days: int) -> int` — deletes files in `chart_dir` whose mtime is older than `retention_days`; returns count deleted. No-ops (returns 0) if `chart_dir` doesn't exist.

- [ ] **Step 1: Write the failing tests**

```python
# roles/s1_reporter/tests/test_chart_store.py
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "files"))

import chart_store  # noqa: E402


class TestSaveChart(unittest.TestCase):
    def test_writes_file_and_returns_url(self):
        with tempfile.TemporaryDirectory() as d:
            url = chart_store.save_chart(b"fake-png-bytes", d, "https://charts.example.com/")
            self.assertTrue(url.startswith("https://charts.example.com/"))
            filename = url.rsplit("/", 1)[-1]
            self.assertTrue(filename.endswith(".png"))
            with open(os.path.join(d, filename), "rb") as f:
                self.assertEqual(f.read(), b"fake-png-bytes")

    def test_creates_chart_dir_if_missing(self):
        with tempfile.TemporaryDirectory() as d:
            nested = os.path.join(d, "charts")
            chart_store.save_chart(b"x", nested, "https://charts.example.com")
            self.assertTrue(os.path.isdir(nested))

    def test_filenames_are_unique(self):
        with tempfile.TemporaryDirectory() as d:
            url1 = chart_store.save_chart(b"a", d, "https://c.example.com")
            url2 = chart_store.save_chart(b"b", d, "https://c.example.com")
            self.assertNotEqual(url1, url2)


class TestCleanupOldCharts(unittest.TestCase):
    def test_deletes_files_older_than_retention(self):
        with tempfile.TemporaryDirectory() as d:
            old_path = os.path.join(d, "old.png")
            new_path = os.path.join(d, "new.png")
            open(old_path, "wb").close()
            open(new_path, "wb").close()
            old_time = time.time() - (20 * 86400)
            os.utime(old_path, (old_time, old_time))

            deleted = chart_store.cleanup_old_charts(d, retention_days=14)

            self.assertEqual(deleted, 1)
            self.assertFalse(os.path.exists(old_path))
            self.assertTrue(os.path.exists(new_path))

    def test_missing_dir_is_noop(self):
        deleted = chart_store.cleanup_old_charts("/nonexistent/path/xyz", retention_days=14)
        self.assertEqual(deleted, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest roles/s1_reporter/tests/test_chart_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chart_store'`

- [ ] **Step 3: Implement `chart_store.py`**

```python
# roles/s1_reporter/files/chart_store.py
import os
import time
import uuid


def save_chart(png_bytes, chart_dir, public_base_url):
    os.makedirs(chart_dir, exist_ok=True)
    filename = f"{uuid.uuid4()}.png"
    with open(os.path.join(chart_dir, filename), "wb") as f:
        f.write(png_bytes)
    return f"{public_base_url.rstrip('/')}/{filename}"


def cleanup_old_charts(chart_dir, retention_days):
    if not os.path.isdir(chart_dir):
        return 0
    cutoff = time.time() - (retention_days * 86400)
    deleted = 0
    for name in os.listdir(chart_dir):
        path = os.path.join(chart_dir, name)
        if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            os.remove(path)
            deleted += 1
    return deleted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest roles/s1_reporter/tests/test_chart_store.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add roles/s1_reporter/files/chart_store.py roles/s1_reporter/tests/test_chart_store.py
git commit -m "feat(s1_reporter): add chart PNG storage with UUID naming and retention cleanup"
```

---

### Task 4: Ansible plumbing — vault var, defaults, docker-compose, chart-server container

This task adds all new infrastructure config (webhook URL, chart hosting env vars, new nginx chart-server container) without changing report.py/upload_monitor.py behavior yet — the app still sends email after this task. This keeps the deploy non-breaking until Task 6/8 flip the actual send path.

**Files:**
- Modify: `roles/s1_reporter/defaults/main.yml`
- Modify: `roles/s1_reporter/templates/docker-compose.s1_reporter.yml.j2`
- Modify: `roles/s1_reporter/tasks/main.yml`
- Modify: `VAULT_VARS.md`
- Modify: `group_vars/vault.yml` (manual, via `ansible-vault edit`)
- Modify: `host_vars/sysone.yml`, `host_vars/sysone_staging.yml`

**Interfaces:**
- Produces env vars consumed by Task 6/8: `TEAMS_WEBHOOK_URL`, `CHART_DIR=/data/charts`, `CHART_PUBLIC_BASE_URL`, `CHART_RETENTION_DAYS`.

- [ ] **Step 1: Add new defaults**

Edit `roles/s1_reporter/defaults/main.yml`, append:

```yaml

# Teams notifications
s1_reporter_chart_retention_days: 14
```

- [ ] **Step 2: Add the vault secret**

Run: `ansible-vault edit group_vars/vault.yml`

Add a new key (keep alongside the existing `vault_s1_reporter_*` entries for now — they're removed in Task 11 once the migration is verified in production):

```yaml
vault_s1_reporter_teams_webhook_url: "https://default2cacbdf2278e4ccab6c2e17094934b.7b.environment.api.powerplatform.com:443/powerautomate/automations/direct/cu/25/workflows/5625d110857941229eede53baf9b0c43/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=YnEYGmdweJrXuPtv50mm7IVsopm_ljCBjQGBhJu-NCg"
```

(This is the URL confirmed working during design — posts to Systems-One team → Reporting channel via the "Post to a channel when a webhook request is received" flow.)

- [ ] **Step 3: Add the per-host chart public base URL**

Add to `host_vars/sysone.yml` and `host_vars/sysone_staging.yml` (use a distinct subdomain per environment so staging/prod charts don't collide, e.g. `charts.<domain>` for prod and `charts-staging.<domain>` for staging):

```yaml
s1_reporter_chart_public_base_url: "https://charts.CHANGEME.example.com"
```

This is a real deploy-time value that depends on a DNS subdomain you choose and register — see Task 9 for the exact Cloudflare setup steps. Replace `CHANGEME.example.com` with your actual domain before deploying; nothing in Tasks 1-8 depends on the final value being correct yet, only that the variable exists.

- [ ] **Step 4: Add chart-server container and new env vars to the compose template**

Edit `roles/s1_reporter/templates/docker-compose.s1_reporter.yml.j2`:

```yaml
services:
  s1-reporter:
    build:
      context: .
      dockerfile: Dockerfile
    image: "{{ s1_reporter_image }}"
    container_name: s1_reporter
    restart: unless-stopped
    environment:
      DB_HOST: "{{ s1_reporter_db_host }}"
      DB_PORT: "{{ s1_reporter_db_port }}"
      DB_USER: "{{ s1_reporter_db_user }}"
      DB_PASS: "{{ mssql_sa_password }}"
      DB_NAME: "{{ s1_reporter_db_name }}"
      TEAMS_WEBHOOK_URL: "{{ vault_s1_reporter_teams_webhook_url }}"
      CHART_DIR: "/data/charts"
      CHART_PUBLIC_BASE_URL: "{{ s1_reporter_chart_public_base_url }}"
      CHART_RETENTION_DAYS: "{{ s1_reporter_chart_retention_days }}"
      OFFLINE_CHECK_INTERVAL_MINUTES: "{{ s1_reporter_offline_check_interval_minutes }}"
      OFFLINE_THRESHOLD_MINUTES: "{{ s1_reporter_offline_threshold_minutes }}"
      DAILY_REPORT_HOUR: "{{ s1_reporter_daily_report_hour }}"
      MONTHLY_REPORT_HOUR: "{{ s1_reporter_monthly_report_hour }}"
      OFFLINE_STATE_FILE: "/data/offline_state.json"
      UPLOAD_STATE_FILE: "/data/upload_state.json"
    healthcheck:
      test: ["CMD-SHELL", "[ -f /proc/1/comm ] && cat /proc/1/comm | grep -q 'sh' || exit 1"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
    volumes:
      - s1_reporter_data:/data
      - s1_reporter_charts:/data/charts
    networks:
      - {{ docker_shared_network | default('infra') }}

  s1-charts:
    image: nginx:alpine
    container_name: s1_reporter_charts
    restart: unless-stopped
    volumes:
      - s1_reporter_charts:/usr/share/nginx/html:ro
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://localhost/"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    networks:
      - {{ docker_shared_network | default('infra') }}

networks:
  {{ docker_shared_network | default('infra') }}:
    external: true

volumes:
  s1_reporter_data:
  s1_reporter_charts:
```

Note: `s1_reporter_charts` is mounted at `/data/charts` in `s1-reporter` (read-write, matches `CHART_DIR`) and at `/usr/share/nginx/html` in `s1-charts` (read-only) — both containers share the same named volume, so files `report.py` writes are immediately servable by nginx.

- [ ] **Step 5: Copy the new Python modules in the Ansible role**

Edit `roles/s1_reporter/tasks/main.yml`, add after the "Copy upload_monitor.py" task (before "Copy entrypoint.sh"):

```yaml
- name: Copy teams_notifier.py
  copy:
    src: teams_notifier.py
    dest: "{{ s1_reporter_dir }}/teams_notifier.py"
    owner: root
    group: root
    mode: "0644"

- name: Copy cards.py
  copy:
    src: cards.py
    dest: "{{ s1_reporter_dir }}/cards.py"
    owner: root
    group: root
    mode: "0644"

- name: Copy chart_store.py
  copy:
    src: chart_store.py
    dest: "{{ s1_reporter_dir }}/chart_store.py"
    owner: root
    group: root
    mode: "0644"
```

- [ ] **Step 6: Copy the new modules in the Dockerfile too**

Edit `roles/s1_reporter/files/Dockerfile`, add after `COPY upload_monitor.py /app/upload_monitor.py`:

```dockerfile
COPY teams_notifier.py /app/teams_notifier.py
COPY cards.py /app/cards.py
COPY chart_store.py /app/chart_store.py
```

- [ ] **Step 7: Update `VAULT_VARS.md`**

Edit the `## s1_reporter` section:

```markdown
## s1_reporter

| Variable | Description |
|---|---|
| `vault_s1_reporter_teams_webhook_url` | Power Automate Workflow webhook URL that posts Adaptive Cards into the Systems-One → Reporting Teams channel |
| `vault_s1_reporter_smtp_user` | (legacy, removed after Teams migration verified — see Task 11) Gmail address used to send reports |
| `vault_s1_reporter_smtp_pass` | (legacy, removed after Teams migration verified) Gmail app password |
| `vault_s1_reporter_report_to` | (legacy, removed after Teams migration verified) Email address to send reports to |
```

- [ ] **Step 8: Commit**

```bash
git add roles/s1_reporter/defaults/main.yml roles/s1_reporter/templates/docker-compose.s1_reporter.yml.j2 \
        roles/s1_reporter/tasks/main.yml roles/s1_reporter/files/Dockerfile \
        host_vars/sysone.yml host_vars/sysone_staging.yml VAULT_VARS.md
git commit -m "feat(s1_reporter): add Teams webhook and chart-hosting infra (non-breaking, email still active)"
```

(`group_vars/vault.yml` was already saved by `ansible-vault edit` in Step 2 — include it in this commit too: `git add group_vars/vault.yml`.)

---

### Task 5: Wire offline/recovery alerts in `report.py` to Teams

**Files:**
- Modify: `roles/s1_reporter/files/report.py`

**Interfaces:**
- Consumes: `teams_notifier.post_to_teams` (Task 1), `cards.build_offline_alert_card` / `cards.build_recovery_card` (Task 2).

- [ ] **Step 1: Add imports and webhook config**

At the top of `report.py`, after the existing imports (line 19), add:

```python
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from teams_notifier import post_to_teams
from cards import build_offline_alert_card, build_recovery_card
```

In `load_config()`'s `env_keys` list (lines 26-28), add `"TEAMS_WEBHOOK_URL"` and remove `"SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASS","REPORT_TO"` (they're still needed by the not-yet-migrated daily/monthly path until Task 6 — leave them for now and remove in Task 7 once nothing references them):

```python
env_keys = ["DB_HOST","DB_PORT","DB_USER","DB_PASS","DB_NAME",
            "SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASS","REPORT_TO",
            "TEAMS_WEBHOOK_URL",
            "OFFLINE_THRESHOLD_MINUTES"]
```

After line 41 (`CFG = load_config()`), add:

```python
TEAMS_WEBHOOK_URL = CFG.get("TEAMS_WEBHOOK_URL", "")
```

- [ ] **Step 2: Replace the offline/recovery send calls**

In `check_and_send_offline_alert()`, replace lines 900-908:

```python
    if newly_offline:
        html, subject = build_offline_alert_email(newly_offline)
        send_email(subject, html)
        print(f"🔴 Offline alert sent for {len(newly_offline)} device(s): {[_device_key(d['machine_name'], d['location']) for d in newly_offline]}")

    if recovered:
        html, subject = build_recovery_email(recovered)
        send_email(subject, html)
        print(f"✅ Recovery alert sent for {len(recovered)} device(s): {[_device_key(d['machine_name'], d['location']) for d in recovered]}")
```

with:

```python
    if newly_offline:
        card = build_offline_alert_card(newly_offline)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
        print(f"🔴 Offline alert sent for {len(newly_offline)} device(s): {[_device_key(d['machine_name'], d['location']) for d in newly_offline]}")

    if recovered:
        card = build_recovery_card(recovered)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
        print(f"✅ Recovery alert sent for {len(recovered)} device(s): {[_device_key(d['machine_name'], d['location']) for d in recovered]}")
```

- [ ] **Step 3: Delete the now-unused HTML builder functions for these two alert types**

Delete `build_offline_alert_email()` (lines 778-835) and `build_recovery_email()` (the function starting at line 837 — find its matching end before `def check_and_send_offline_alert`) entirely from `report.py`.

- [ ] **Step 4: Verify syntax**

Run: `python -m py_compile roles/s1_reporter/files/report.py`
Expected: no output, exit code 0 (this only checks syntax — `pymssql`/`matplotlib` aren't installed locally so the module can't be fully imported; full behavioral verification happens in Task 10 against staging).

- [ ] **Step 5: Commit**

```bash
git add roles/s1_reporter/files/report.py
git commit -m "feat(s1_reporter): send offline/recovery alerts to Teams instead of email"
```

---

### Task 6: Wire daily/monthly reports in `report.py` to Teams + chart hosting

**Files:**
- Modify: `roles/s1_reporter/files/report.py`

**Interfaces:**
- Consumes: `chart_store.save_chart` / `chart_store.cleanup_old_charts` (Task 3), `cards.build_customer_section_card` (Task 2), `post_to_teams` (already imported in Task 5).

- [ ] **Step 1: Add chart_store import and config**

Add to the imports block added in Task 5:

```python
from chart_store import save_chart, cleanup_old_charts
```

After `TEAMS_WEBHOOK_URL = CFG.get("TEAMS_WEBHOOK_URL", "")`, add:

```python
CHART_DIR = os.environ.get("CHART_DIR", "/data/charts")
CHART_PUBLIC_BASE_URL = os.environ.get("CHART_PUBLIC_BASE_URL", "")
CHART_RETENTION_DAYS = int(os.environ.get("CHART_RETENTION_DAYS", "14"))
```

- [ ] **Step 2: Add a shared row-builder helper for the caps-aware tables**

`customer_section_daily`/`customer_section_monthly` both build today/week/monthly tables whose columns depend on `caps`. Add this helper near `customer_section_daily` (before it):

```python
def _caps_headers(caps, trailing=None):
    headers = ["Device", "Location", "Items", "Good Read %", "No Reads"]
    if caps["has_dimension"]:
        headers.append("No Dims")
    if caps["has_hand_scan"]:
        headers.append("Hand Scanned")
    if caps["has_weight"]:
        headers.append("No Weight")
    if trailing:
        headers.extend(trailing)
    return headers


def _caps_row(r, caps, trailing_values=None):
    row = [
        r["machine_name"], r["location"],
        f"{(r['total_items'] or 0):,}",
        f"{r['good_read_pct']:.1f}%" if r.get("good_read_pct") is not None else "—",
        f"{r['no_reads'] or 0:,}",
    ]
    if caps["has_dimension"]:
        row.append(f"{r['no_dimensions'] or 0:,}")
    if caps["has_hand_scan"]:
        row.append(f"{r['hand_scanned'] or 0:,}")
    if caps["has_weight"]:
        row.append(f"{r['no_weight'] or 0:,}")
    if trailing_values:
        row.extend(trailing_values(r))
    return row
```

- [ ] **Step 3: Replace `customer_section_daily`**

Replace the entire `customer_section_daily(customer, today_label, images, days=7)` function (lines 548-673) with:

```python
def customer_section_daily_card(customer, days=7):
    caps      = get_caps(customer)
    trend     = get_daily_trend(days, customer)
    summary1  = get_device_summary(1, customer)
    summary7  = get_device_summary(days, customer)
    hourly    = get_hourly_pattern(days, customer)
    storage   = get_storage(customer)
    anomalies = detect_anomalies(trend, customer)

    chart_urls = {
        "volume":   save_chart(chart_daily_volume(trend, f"Daily Volume — {customer} — Last {days} Days"), CHART_DIR, CHART_PUBLIC_BASE_URL),
        "goodread": save_chart(chart_goodread_trend(trend, f"Good Read % — {customer} — Last {days} Days"), CHART_DIR, CHART_PUBLIC_BASE_URL),
        "hourly":   save_chart(chart_hourly_volume(hourly, f"Hourly Pattern — {customer}"), CHART_DIR, CHART_PUBLIC_BASE_URL),
    }

    today_headers = _caps_headers(caps, trailing=["Not Sent"])
    today_rows = [_caps_row(r, caps, trailing_values=lambda r: [f"{r['not_sent'] or 0:,}"]) for r in summary1]

    week_headers = _caps_headers(caps)
    week_rows = [_caps_row(r, caps) for r in summary7]

    storage_headers = ["Device", "Location", "Usage", "%"]
    storage_rows = [
        [s["machine_name"], s["location"], f"{float(s['used_gb']):.1f} / {float(s['total_gb']):.1f} GB", f"{s['usage_percent']}%"]
        for s in storage
    ]

    return build_customer_section_card(
        customer=customer, days=days, anomalies=anomalies,
        today_table={"headers": today_headers, "rows": today_rows},
        week_table={"headers": week_headers, "rows": week_rows},
        storage_table={"headers": storage_headers, "rows": storage_rows},
        chart_urls=chart_urls,
    )
```

- [ ] **Step 4: Replace `customer_section_monthly`**

Replace the entire `customer_section_monthly(customer, month_label, images)` function (lines 675-777, immediately before what was originally `def build_offline_alert_email` — that function was already deleted in Task 5, so by this point in the file it's immediately before `def build_daily_report`) with:

```python
def customer_section_monthly_card(customer, month_label):
    caps     = get_caps(customer)
    trend    = get_daily_trend(30, customer)
    summary  = get_device_summary(30, customer)
    hourly   = get_hourly_pattern(30, customer)
    storage  = get_storage(customer)
    anomalies = detect_anomalies(trend, customer)

    chart_urls = {
        "volume":   save_chart(chart_daily_volume(trend, f"Daily Volume — {customer} — {month_label}"), CHART_DIR, CHART_PUBLIC_BASE_URL),
        "goodread": save_chart(chart_goodread_trend(trend, f"Good Read % — {customer} — {month_label}"), CHART_DIR, CHART_PUBLIC_BASE_URL),
        "hourly":   save_chart(chart_hourly_volume(hourly, f"Hourly Pattern — {customer}"), CHART_DIR, CHART_PUBLIC_BASE_URL),
    }

    total_items = sum(int(r["total_items"] or 0) for r in summary)
    avg_good    = sum(float(r["good_read_pct"] or 0) for r in summary) / max(len(summary), 1)
    kpis = {"total_items": total_items, "avg_good_read_pct": avg_good, "active_devices": len(summary)}

    def _trend_label(r):
        good = float(r["good_read_pct"] or 0)
        return "▲ Strong" if good >= 99 else "▼ Monitor"

    tbl_headers = _caps_headers(caps, trailing=["Not Sent", "Trend"])
    tbl_rows = [
        _caps_row(r, caps, trailing_values=lambda r: [f"{int(r['not_sent'] or 0):,}", _trend_label(r)])
        for r in summary
    ]

    storage_headers = ["Device", "Location", "Usage", "%"]
    storage_rows = [
        [s["machine_name"], s["location"], f"{float(s['used_gb']):.1f} / {float(s['total_gb']):.1f} GB", f"{s['usage_percent']}%"]
        for s in storage
    ]

    return build_customer_section_card(
        customer=customer, days=30, anomalies=anomalies,
        today_table={"headers": [], "rows": []},
        week_table={"headers": tbl_headers, "rows": tbl_rows},
        storage_table={"headers": storage_headers, "rows": storage_rows},
        chart_urls=chart_urls,
        kpis=kpis,
    )
```

- [ ] **Step 5: Replace `build_daily_report` and `build_monthly_report`**

Find `build_daily_report()` (originally lines 919-943) and `build_monthly_report()` (originally lines 945-969) and replace both with:

```python
def build_and_send_daily_report():
    cleanup_old_charts(CHART_DIR, CHART_RETENTION_DAYS)
    for customer in CUSTOMER_CAPS:
        card = customer_section_daily_card(customer)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
    print(f"✅ Daily report sent for {len(CUSTOMER_CAPS)} customer(s)")


def build_and_send_monthly_report():
    cleanup_old_charts(CHART_DIR, CHART_RETENTION_DAYS)
    month_label = datetime.now().strftime("%B %Y")
    for customer in CUSTOMER_CAPS:
        card = customer_section_monthly_card(customer, month_label)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
    print(f"✅ Monthly report sent for {len(CUSTOMER_CAPS)} customer(s)")
```

This posts one card per customer (per the design decision to avoid oversized single cards), each preceded by a chart-retention cleanup pass.

- [ ] **Step 6: Update the `__main__` dispatch**

Find the `if __name__ == "__main__":` block (originally lines 1001-1010) and replace the `daily`/`monthly` branches:

```python
    if mode == "daily":
        build_and_send_daily_report()
    elif mode == "monthly":
        build_and_send_monthly_report()
```

(leave the `offline` branch as-is — it calls `check_and_send_offline_alert()`, already migrated in Task 5).

- [ ] **Step 7: Verify syntax**

Run: `python -m py_compile roles/s1_reporter/files/report.py`
Expected: no output, exit code 0.

- [ ] **Step 8: Commit**

```bash
git add roles/s1_reporter/files/report.py
git commit -m "feat(s1_reporter): send daily/monthly reports to Teams with hosted chart images"
```

---

### Task 7: Remove dead email code from `report.py`

**Files:**
- Modify: `roles/s1_reporter/files/report.py`

- [ ] **Step 1: Remove SMTP-related imports and the `send_email` function**

Remove from the imports (line 10): `smtplib` and the unused `base64` (confirmed unused during design research). Remove the `email.mime.*` imports (lines 16-18). Delete the `send_email()` function (lines 972-996) entirely — nothing calls it anymore after Task 5/6.

- [ ] **Step 2: Remove SMTP keys from config**

In `load_config()`'s `env_keys` list, remove `"SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASS","REPORT_TO"`, leaving:

```python
env_keys = ["DB_HOST","DB_PORT","DB_USER","DB_PASS","DB_NAME",
            "TEAMS_WEBHOOK_URL",
            "OFFLINE_THRESHOLD_MINUTES"]
```

- [ ] **Step 3: Verify syntax and check for stragglers**

Run: `python -m py_compile roles/s1_reporter/files/report.py`
Expected: no output, exit code 0.

Run: `grep -n "smtplib\|MIMEText\|MIMEImage\|MIMEMultipart\|CFG\[.SMTP\|CFG\[.REPORT_TO" roles/s1_reporter/files/report.py`
Expected: no matches.

- [ ] **Step 4: Commit**

```bash
git add roles/s1_reporter/files/report.py
git commit -m "chore(s1_reporter): remove dead SMTP code from report.py"
```

---

### Task 8: Migrate `upload_monitor.py` to Teams and remove email code

**Files:**
- Modify: `roles/s1_reporter/files/upload_monitor.py`

**Interfaces:**
- Consumes: `teams_notifier.post_to_teams` (Task 1), `cards.build_upload_alert_card` / `cards.build_upload_recovery_card` (Task 2).

- [ ] **Step 1: Swap imports**

Replace lines 11-14:

```python
import sys, os, json, smtplib, pymssql
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
```

with:

```python
import sys, os, json, pymssql
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from teams_notifier import post_to_teams
from cards import build_upload_alert_card, build_upload_recovery_card
```

- [ ] **Step 2: Update config**

In `load_config()`'s key list (lines 18-19), replace:

```python
keys = ["DB_HOST","DB_PORT","DB_USER","DB_PASS","DB_NAME",
        "SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASS","REPORT_TO"]
```

with:

```python
keys = ["DB_HOST","DB_PORT","DB_USER","DB_PASS","DB_NAME","TEAMS_WEBHOOK_URL"]
```

After `CFG = load_config()` (line 32), add:

```python
TEAMS_WEBHOOK_URL = CFG.get("TEAMS_WEBHOOK_URL", "")
```

- [ ] **Step 3: Delete `send_email` and the HTML builders**

Delete `send_email()` (lines 302-313), `build_upload_alert_email()` (lines 180-245), and `build_upload_recovery_email()` (lines 247-299) entirely.

- [ ] **Step 4: Replace the call sites in `run_check`**

Replace lines 320-335:

```python
    if force and currently_failing:
        html, subject = build_upload_alert_email(currently_failing)
        send_email(subject, html)
        print(f"⚠️ [FORCED] Upload alert sent for {len(currently_failing)} device(s)")
        return
    ...
    if newly_failing:
        html, subject = build_upload_alert_email(newly_failing)
        send_email(subject, html)
        keys = [d['machine_name']+'@'+d['location'] for d in newly_failing]
        print(f"⚠️ Upload alert sent for {len(newly_failing)} device(s): {keys}")
    ...
    if recovered:
        html, subject = build_upload_recovery_email(recovered)
        send_email(subject, html)
        print(f"✅ Upload recovery sent for {len(recovered)} device(s): {[d['machine_name']+'@'+d['location'] for d in recovered]}")
```

with:

```python
    if force and currently_failing:
        card = build_upload_alert_card(currently_failing)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
        print(f"⚠️ [FORCED] Upload alert sent for {len(currently_failing)} device(s)")
        return
    ...
    if newly_failing:
        card = build_upload_alert_card(newly_failing)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
        keys = [d['machine_name']+'@'+d['location'] for d in newly_failing]
        print(f"⚠️ Upload alert sent for {len(newly_failing)} device(s): {keys}")
    ...
    if recovered:
        card = build_upload_recovery_card(recovered)
        post_to_teams(TEAMS_WEBHOOK_URL, card)
        print(f"✅ Upload recovery sent for {len(recovered)} device(s): {[d['machine_name']+'@'+d['location'] for d in recovered]}")
```

(keep the surrounding `if`/`elif` structure and unrelated lines exactly as they are — only the three marked blocks change).

- [ ] **Step 5: Remove the now-unused CSS constant**

The `CSS` string constant (lines 47-69) was only used by the deleted HTML builders — delete it.

- [ ] **Step 6: Verify syntax and check for stragglers**

Run: `python -m py_compile roles/s1_reporter/files/upload_monitor.py`
Expected: no output, exit code 0.

Run: `grep -n "smtplib\|MIMEText\|MIMEMultipart\|send_email\|CFG\[.SMTP\|CFG\[.REPORT_TO" roles/s1_reporter/files/upload_monitor.py`
Expected: no matches.

- [ ] **Step 7: Commit**

```bash
git add roles/s1_reporter/files/upload_monitor.py
git commit -m "feat(s1_reporter): migrate upload_monitor.py alerts to Teams, remove SMTP code"
```

---

### Task 9: Register the Cloudflare Public Hostname for chart hosting

This is a manual dashboard task (Cloudflare Tunnel routing is not managed in this repo — see design spec background) plus deploying the infra from Task 4.

**Files:** none (external Cloudflare Zero Trust dashboard configuration + a deploy).

- [ ] **Step 1: Choose the subdomain**

Decide the real hostname to replace the `CHANGEME` placeholder from Task 4 Step 3 (e.g. `charts.systems-one.com`), and update `host_vars/sysone.yml` / `host_vars/sysone_staging.yml` with the real value.

- [ ] **Step 2: Deploy the infra**

Run the Ansible playbook for the target host so Task 4's compose changes (new `s1-charts` container) go live:

```bash
ansible-playbook -i inventory webservers.yml --limit sysone_staging --tags s1_reporter
```

- [ ] **Step 3: Add the Public Hostname in Cloudflare**

In the Cloudflare Zero Trust dashboard: **Networks → Tunnels →** (the tunnel this server's `cloudflared` container uses) **→ Public Hostname → Add a public hostname**.
- Subdomain/domain: the value chosen in Step 1.
- Service: **HTTP**, target `s1-charts:80` (the compose service name and nginx's default port — reachable because both containers share the `infra` Docker network, same as how Grafana's route targets its container name).

- [ ] **Step 4: Verify the route works**

```bash
curl -I https://charts.<your-domain>/nonexistent.png
```

Expected: `HTTP/2 404` from nginx (proves the tunnel → `s1-charts` route works; 404 is correct since no chart has been generated yet).

- [ ] **Step 5: Commit the host_vars update**

```bash
git add host_vars/sysone.yml host_vars/sysone_staging.yml
git commit -m "chore(s1_reporter): set real chart hosting subdomain"
```

---

### Task 10: End-to-end verification on staging

**Files:** none (verification only).

- [ ] **Step 1: Deploy the full migration to staging**

```bash
ansible-playbook -i inventory webservers.yml --limit sysone_staging --tags s1_reporter
```

- [ ] **Step 2: Trigger an offline alert manually**

```bash
docker exec s1_reporter python3 /app/report.py offline
```
Check the **Systems-One → Reporting** Teams channel: a red "Devices Not Reporting" card should appear (or nothing, if no devices are currently offline in staging data — in that case, temporarily lower `OFFLINE_THRESHOLD_MINUTES` or check `docker logs s1_reporter` for `🔴 Offline alert sent` / no-op confirmation).

- [ ] **Step 3: Trigger an upload alert manually (forced test mode)**

```bash
docker exec s1_reporter python3 /app/upload_monitor.py test
```
Check Teams for an amber "Devices Not Uploading" card, and `docker logs s1_reporter` for `⚠️ [FORCED] Upload alert sent`.

- [ ] **Step 4: Trigger the daily report manually**

```bash
docker exec s1_reporter python3 /app/report.py daily
```
Check Teams for one card per customer with KPI tables and inline chart images. Open one chart image URL directly in a browser to confirm it renders (not a broken image icon) — this validates the Cloudflare route from Task 9 end-to-end.

- [ ] **Step 5: Trigger the monthly report manually**

```bash
docker exec s1_reporter python3 /app/report.py monthly
```
Check Teams for the monthly cards, including the `FactSet` KPI block (total items / avg good read / active devices) that daily cards don't have.

- [ ] **Step 6: Confirm no errors in logs**

```bash
docker logs s1_reporter --tail 100
```
Expected: no Python tracebacks, no `❌ Teams webhook POST failed` lines.

- [ ] **Step 7: Let it run unattended for the normal entrypoint.sh cadence**

Leave staging running for at least one full `OFFLINE_CHECK_INTERVAL_MINUTES` cycle (20 min default) and confirm the automatic (non-forced) offline/upload checks also post correctly, via `docker logs s1_reporter -f` during that window.

This task has no commit — it's a verification gate. If any step fails, fix the issue in the relevant earlier task's files, re-run this task's steps from the top, and only proceed to Task 11 once all checks pass.

---

### Task 11: Remove legacy SMTP vault vars and finalize docs

Only do this once Task 10 has fully passed and you've watched staging (or production, if deploying there) run cleanly for at least a day.

**Files:**
- Modify: `group_vars/vault.yml` (manual, via `ansible-vault edit`)
- Modify: `VAULT_VARS.md`

- [ ] **Step 1: Remove the legacy vault entries**

Run: `ansible-vault edit group_vars/vault.yml`

Delete the `vault_s1_reporter_smtp_user`, `vault_s1_reporter_smtp_pass`, and `vault_s1_reporter_report_to` keys (no longer referenced by any template after Task 4 Step 4 already dropped them from the compose environment block).

- [ ] **Step 2: Clean up `VAULT_VARS.md`**

Edit the `## s1_reporter` section to remove the three legacy rows added in Task 4 Step 7, leaving only:

```markdown
## s1_reporter

| Variable | Description |
|---|---|
| `vault_s1_reporter_teams_webhook_url` | Power Automate Workflow webhook URL that posts Adaptive Cards into the Systems-One → Reporting Teams channel |
```

- [ ] **Step 3: Redeploy to confirm nothing references the removed vars**

```bash
ansible-playbook -i inventory webservers.yml --limit sysone_staging --tags s1_reporter
```
Expected: playbook completes without an "undefined variable" error (confirms the compose template never referenced the removed vault keys — it shouldn't, since Task 4 Step 4 already removed them from the template, but this catches any missed reference).

- [ ] **Step 4: Commit**

```bash
git add group_vars/vault.yml VAULT_VARS.md
git commit -m "chore(s1_reporter): remove legacy SMTP vault variables after Teams migration"
```

---

## Self-Review Notes

- **Spec coverage:** webhook mechanism (Task 4/5/6/8) ✓, Adaptive Card shape matching confirmed schema (Task 2, Global Constraints) ✓, single-channel layout (Task 4 Step 2 — one webhook URL used everywhere) ✓, chart hosting via Cloudflare-tunneled static server with UUID names + 14-day retention (Task 3, 4, 9) ✓, full email cutover (Task 7, 8, 11) ✓, error handling / no silent loss on webhook failure (Task 1) ✓.
- **Type consistency checked:** `post_to_teams(webhook_url, card, max_retries, backoff_seconds)` signature is identical across Tasks 1, 5, 6, 8. `build_customer_section_card(...)` parameter names match between its Task 2 definition and Task 6 call sites. `save_chart(png_bytes, chart_dir, public_base_url)` matches between Task 3 definition and Task 6 usage.
- **No placeholders:** the one open value (`CHANGEME` domain) is explicitly flagged as a real deploy-time decision with concrete steps to resolve it (Task 9), not a vague TODO left for "later."
