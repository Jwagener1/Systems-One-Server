# MQTT Ingestor Consolidation (Build + Stage) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one new Ansible role, `roles/mqtt_ingestor`, that merges `systems_one_ingest`'s allowlist validation fix, `mqtt-ingestor`'s resilient pipeline (spool/retry/dead-letter/metrics/health), and `broker-ingestor`'s `$SYS/#` broker-health collection into a single service — then deploy it to `sysone_staging` and verify it end-to-end. Production cutover (removing the three old services) is a separate follow-up plan, written after this one's staging results exist.

**Architecture:** One process, one MQTT connection subscribing to `systems-one/#`, `systemsone/#`, and `$SYS/#`. Device-telemetry messages flow through `mqtt-ingestor`'s existing spool → batched-writer → retry → dead-letter pipeline (vendored near-unchanged), gated by a ported allowlist check before device resolution. Broker-health messages are aggregated in-memory and flushed periodically to `broker.broker_stats`, sharing the same DB connection, health endpoint, and metrics registry as the device pipeline, but not the discrete per-message spool (broker stats are periodic aggregates, not discrete historical events — see design note in Task 4).

**Tech Stack:** Python 3.12, `paho-mqtt>=2.0.0,<3.0.0`, `pyodbc>=5.0.0,<6.0.0`, `prometheus-client`, SQLite (spool), MSSQL (`ODBC Driver 18 for SQL Server`), Docker multi-stage build, Ansible.

## Global Constraints

- No new customer/location can be silently dropped — `validate_device()`'s allow-all-by-default semantics (from `roles/systems_one_ingest/files/app/mqtt_ingest/validation.py`) must be preserved exactly.
- `MQTT_CLEAN_SESSION` stays `false` for the merged service (matches `mqtt-ingestor`'s current persistent-session behavior) so no messages are lost across reconnects.
- Device tables (`dbo.devices` and friends) keep `mqtt-ingestor`'s `(customer, location, machine_name)` device-resolution key with placeholder-then-upgrade serials — do not regress to `systems_one_ingest`'s serial-first model.
- `secure`/token-shaped values follow existing vault conventions (`vault_<name>`, referenced via `host_vars/<host>.yml` indirection) — see `VAULT_VARS.md`.
- Every new Python module gets unit tests before implementation (TDD) for its pure-function logic; DB/MQTT-integration code is validated via the staging deploy in Task 8, not unit tests (no real MSSQL/broker in CI).

---

## File Structure

```
roles/mqtt_ingestor/
  defaults/main.yml                      # NEW — env var defaults, ~40 vars
  tasks/main.yml                         # NEW — deploy tasks (mirrors systems_one_ingest's pattern)
  templates/
    Dockerfile.j2                        # NEW — vendored from worktree, unchanged
    docker-compose.yml.j2                # NEW — extends the vendored one with broker DB vars
  files/app/
    main.py                              # NEW — vendored, unchanged
    healthcheck.py                       # NEW — vendored, unchanged
    requirements.txt                     # NEW — vendored, unchanged
    migrations/001_init_schema.sql       # NEW — vendored (mqtt-ingestor's), unchanged
    ingestor/
      __init__.py                        # NEW — vendored, unchanged (empty)
      config.py                          # NEW — vendored + MODIFIED (multi-topic, allowlist, broker vars)
      models.py                          # NEW — vendored + MODIFIED (adds BrokerSnapshot)
      mqtt_client.py                     # NEW — vendored + MODIFIED (subscribe to a topic list)
      validation.py                      # NEW — ported verbatim from systems_one_ingest
      db.py                              # NEW — vendored + MODIFIED (allowlist gate, broker writer)
      spool.py                           # NEW — vendored, unchanged
      settings_writer.py                 # NEW — vendored, unchanged
      health.py                          # NEW — vendored, unchanged
      metrics.py                         # NEW — vendored + MODIFIED (adds broker counters)
      pipeline.py                        # NEW — vendored + MODIFIED (topic classification, broker buffer+flush)
  tests/
    test_validation.py                   # NEW — copied verbatim from systems_one_ingest's test suite
    test_topic_classification.py         # NEW — tests for the new pipeline.py routing helper
    test_config_multi_topic.py           # NEW — tests for config.py's topic-list parsing
```

All vendored-unchanged files come from the uncommitted worktree at `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/` (device pipeline) and `/home/s1/broker-ingestor/` on the server (broker pipeline, read via `ssh s1_server cat ...` since it has no git repo anywhere). Copy them byte-for-byte where a task says "vendor unchanged" — no edits.

---

### Task 1: Scaffold the role and vendor unchanged files

**Files:**
- Create: `roles/mqtt_ingestor/files/app/main.py`, `healthcheck.py`, `requirements.txt`
- Create: `roles/mqtt_ingestor/files/app/migrations/001_init_schema.sql`
- Create: `roles/mqtt_ingestor/files/app/ingestor/__init__.py`, `spool.py`, `settings_writer.py`, `health.py`
- Create: `roles/mqtt_ingestor/templates/Dockerfile.j2`

**Interfaces:**
- Produces: `Spool`, `HealthServer`, `SettingsWriter` classes — consumed by `pipeline.py` in Task 4, unchanged from the vendored source, imported as `from .spool import Spool`, `from .health import HealthServer`, `from .settings_writer import SettingsWriter`.

- [ ] **Step 1: Copy the six unchanged Python files verbatim**

Source (device pipeline, from the uncommitted worktree — read via the `Read` tool, write via `Write`, do not alter a single line):
- `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/main.py` → `roles/mqtt_ingestor/files/app/main.py`
- `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/healthcheck.py` → `roles/mqtt_ingestor/files/app/healthcheck.py`
- `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/requirements.txt` → `roles/mqtt_ingestor/files/app/requirements.txt`
- `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/migrations/001_init_schema.sql` → `roles/mqtt_ingestor/files/app/migrations/001_init_schema.sql`
- `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/ingestor/spool.py` → `roles/mqtt_ingestor/files/app/ingestor/spool.py`
- `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/ingestor/settings_writer.py` → `roles/mqtt_ingestor/files/app/ingestor/settings_writer.py`
- `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/ingestor/health.py` → `roles/mqtt_ingestor/files/app/ingestor/health.py`

Create `roles/mqtt_ingestor/files/app/ingestor/__init__.py` as an empty file (0 bytes).

- [ ] **Step 2: Copy the Dockerfile template verbatim**

Source: `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/templates/Dockerfile.j2` → `roles/mqtt_ingestor/templates/Dockerfile.j2`, unchanged (it already builds `app/ingestor/`, `app/migrations/`, `app/main.py`, `app/healthcheck.py` — no path changes needed since Task 1's layout matches).

- [ ] **Step 3: Verify the copies are byte-identical**

Run:
```bash
diff .claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/main.py roles/mqtt_ingestor/files/app/main.py
diff .claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/healthcheck.py roles/mqtt_ingestor/files/app/healthcheck.py
diff .claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/requirements.txt roles/mqtt_ingestor/files/app/requirements.txt
diff .claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/migrations/001_init_schema.sql roles/mqtt_ingestor/files/app/migrations/001_init_schema.sql
diff .claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/ingestor/spool.py roles/mqtt_ingestor/files/app/ingestor/spool.py
diff .claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/ingestor/settings_writer.py roles/mqtt_ingestor/files/app/ingestor/settings_writer.py
diff .claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/ingestor/health.py roles/mqtt_ingestor/files/app/ingestor/health.py
diff .claude/worktrees/teams-notifications/roles/mqtt_ingestor/templates/Dockerfile.j2 roles/mqtt_ingestor/templates/Dockerfile.j2
```
Expected: no output from any `diff` (all identical).

- [ ] **Step 4: Commit**

```bash
git add roles/mqtt_ingestor/files/app/main.py roles/mqtt_ingestor/files/app/healthcheck.py \
  roles/mqtt_ingestor/files/app/requirements.txt roles/mqtt_ingestor/files/app/migrations/001_init_schema.sql \
  roles/mqtt_ingestor/files/app/ingestor/__init__.py roles/mqtt_ingestor/files/app/ingestor/spool.py \
  roles/mqtt_ingestor/files/app/ingestor/settings_writer.py roles/mqtt_ingestor/files/app/ingestor/health.py \
  roles/mqtt_ingestor/templates/Dockerfile.j2
git commit -m "feat(mqtt_ingestor): vendor unchanged mqtt-ingestor pipeline files"
```

---

### Task 2: Port validation.py and its test suite

**Files:**
- Create: `roles/mqtt_ingestor/files/app/ingestor/validation.py`
- Create: `roles/mqtt_ingestor/tests/test_validation.py`

**Interfaces:**
- Consumes: nothing (pure function module).
- Produces: `validate_device(serial_number: str, customer: str, location: str, machine_name: str, logger: logging.Logger) -> bool` — consumed by `db.py` in Task 5.

- [ ] **Step 1: Copy validation.py verbatim**

Source: `roles/systems_one_ingest/files/app/mqtt_ingest/validation.py` → `roles/mqtt_ingestor/files/app/ingestor/validation.py`, unchanged. It's already fully self-contained (only imports `logging`, `os`, `re`).

- [ ] **Step 2: Copy and adapt its test suite**

Copy `roles/systems_one_ingest/tests/test_validation.py` → `roles/mqtt_ingestor/tests/test_validation.py`, changing only the module-loading paths (the rest of the test bodies — `SerialChecks`, `AllowAllByDefault`, `ConfiguredAllowlists` — are unchanged):

```python
"""Unit tests for mqtt_ingestor device validation (validation.py)."""
import importlib.machinery
import importlib.util
import logging
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
VALIDATION = os.path.join(HERE, "..", "files", "app", "ingestor", "validation.py")


def load():
    loader = importlib.machinery.SourceFileLoader("mi_validation", VALIDATION)
    spec = importlib.util.spec_from_loader("mi_validation", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


val = load()
log = logging.getLogger("test")


class EnvSandbox(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for k in ("INGEST_ALLOWED_CUSTOMERS", "INGEST_ALLOWED_LOCATIONS"):
            self._saved[k] = os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class SerialChecks(EnvSandbox):
    def test_machine_name_as_serial_rejected(self):
        for serial in ("DIM5", "dim12", "STATIC1"):
            self.assertFalse(val.validate_device(serial, "PEPKOR", "JBH", "DIM5", log), serial)

    def test_short_or_malformed_serial_rejected(self):
        for serial in ("abc", "a!b#c", ""):
            self.assertFalse(val.validate_device(serial, "PEPKOR", "JBH", "DIM5", log), serial)

    def test_real_serials_accepted(self):
        for serial in ("018370-01-2", "019000-01-1", "018389-02-01"):
            self.assertTrue(val.validate_device(serial, "PEPKOR", "JBH", "DIM1", log), serial)


class AllowAllByDefault(EnvSandbox):
    def test_new_customers_accepted_when_env_unset(self):
        cases = [
            ("018370-01-2", "DCB", "DUR"),
            ("019000-01-1", "MADIBANA", "JHB"),
            ("017000-01-1", "SNOWSOFT", "JHB"),
            ("018389-02-01", "BEX", "CPT"),
            ("018377-01-1", "PEP AFRICA", "DUR"),
        ]
        for serial, customer, location in cases:
            self.assertTrue(
                val.validate_device(serial, customer, location, "DIM1", log),
                f"{customer}@{location} must be accepted with no allowlist configured",
            )

    def test_empty_env_means_allow_all(self):
        os.environ["INGEST_ALLOWED_CUSTOMERS"] = ""
        os.environ["INGEST_ALLOWED_LOCATIONS"] = "  "
        self.assertTrue(val.validate_device("018370-01-2", "ANYONE", "ANYWHERE", "DIM1", log))


class ConfiguredAllowlists(EnvSandbox):
    def test_customer_allowlist_enforced(self):
        os.environ["INGEST_ALLOWED_CUSTOMERS"] = "PEPKOR, PEP"
        self.assertTrue(val.validate_device("018370-01-2", "PEPKOR", "JBH", "DIM1", log))
        self.assertFalse(val.validate_device("018370-01-2", "DCB", "DUR", "DIM1", log))

    def test_location_allowlist_enforced(self):
        os.environ["INGEST_ALLOWED_LOCATIONS"] = "JBH,JHB,HDH"
        self.assertTrue(val.validate_device("019000-01-1", "MADIBANA", "JHB", "DIM1", log))
        self.assertFalse(val.validate_device("019000-01-1", "MADIBANA", "PE", "DIM1", log))

    def test_case_and_whitespace_insensitive(self):
        os.environ["INGEST_ALLOWED_CUSTOMERS"] = " pepkor ,dcb "
        self.assertTrue(val.validate_device("018370-01-2", "DCB", "DUR", "DIM1", log))
        self.assertTrue(val.validate_device("018370-01-2", "Pepkor", "DUR", "DIM1", log))
        self.assertFalse(val.validate_device("018370-01-2", "MADIBANA", "DUR", "DIM1", log))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the tests and confirm they pass immediately (this is a port of already-verified code, not new TDD)**

Run: `python roles/mqtt_ingestor/tests/test_validation.py -v`
Expected: `OK` (9 tests pass), since the module and its tests are both direct ports of already-working code.

- [ ] **Step 4: Commit**

```bash
git add roles/mqtt_ingestor/files/app/ingestor/validation.py roles/mqtt_ingestor/tests/test_validation.py
git commit -m "feat(mqtt_ingestor): port validation.py allowlist gate"
```

---

### Task 3: Multi-topic MQTT subscription

**Files:**
- Create: `roles/mqtt_ingestor/tests/test_config_multi_topic.py`
- Create: `roles/mqtt_ingestor/files/app/ingestor/config.py` (modified from vendored source)
- Modify: `roles/mqtt_ingestor/files/app/ingestor/mqtt_client.py` (modified from vendored source, written in Task 4 alongside broker routing — see Task 4 Step 1, which supersedes the subscribe-list change made here)

**Interfaces:**
- Produces: `MqttConfig.topics: tuple[str, ...]` (replaces the vendored `topic_filter: str` field) — consumed by `mqtt_client.py`'s `_on_connect` (subscribes to each topic in the tuple) and by `pipeline.py`'s topic classifier in Task 4.
- Produces: `load_config()` reads `MQTT_TOPICS` (comma-separated, required) instead of `MQTT_TOPIC_FILTER`.

- [ ] **Step 1: Write the failing test for comma-separated topic parsing**

```python
# roles/mqtt_ingestor/tests/test_config_multi_topic.py
import importlib.machinery
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "..", "files", "app", "ingestor", "config.py")


def load():
    loader = importlib.machinery.SourceFileLoader("mi_config", CONFIG)
    spec = importlib.util.spec_from_loader("mi_config", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cfg_mod = load()

REQUIRED_ENV = {
    "MQTT_HOST": "mosquitto",
    "MQTT_TOPICS": "systems-one/#,systemsone/#,$SYS/#",
    "DB_HOST": "mssql",
    "DB_NAME": "S1_Remote_Monitoring",
    "DB_USER": "admin",
    "DB_PASSWORD": "x",
}


class EnvSandbox(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in REQUIRED_ENV}
        os.environ.update(REQUIRED_ENV)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class MultiTopicParsing(EnvSandbox):
    def test_comma_separated_topics_parsed_into_tuple(self):
        cfg = cfg_mod.load_config()
        self.assertEqual(
            cfg.mqtt.topics,
            ("systems-one/#", "systemsone/#", "$SYS/#"),
        )

    def test_whitespace_around_topics_stripped(self):
        os.environ["MQTT_TOPICS"] = " systems-one/# , systemsone/# "
        cfg = cfg_mod.load_config()
        self.assertEqual(cfg.mqtt.topics, ("systems-one/#", "systemsone/#"))

    def test_empty_segments_dropped(self):
        os.environ["MQTT_TOPICS"] = "systems-one/#,,systemsone/#"
        cfg = cfg_mod.load_config()
        self.assertEqual(cfg.mqtt.topics, ("systems-one/#", "systemsone/#"))

    def test_missing_mqtt_topics_raises(self):
        os.environ.pop("MQTT_TOPICS")
        with self.assertRaises(RuntimeError):
            cfg_mod.load_config()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python roles/mqtt_ingestor/tests/test_config_multi_topic.py -v`
Expected: `FAIL` — `config.py` doesn't exist yet in `roles/mqtt_ingestor/files/app/ingestor/` (only in the vendored worktree source), so the loader raises `FileNotFoundError`, not `AssertionError` — confirms the module needs to be created, not just modified in place.

- [ ] **Step 3: Create config.py — vendored source with `topic_filter`/`MQTT_TOPIC_FILTER` replaced by `topics`/`MQTT_TOPICS`**

Copy `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/ingestor/config.py` to `roles/mqtt_ingestor/files/app/ingestor/config.py`, then apply this change to `MqttConfig` and `load_config()`:

```python
@dataclass(frozen=True)
class MqttConfig:
    connection_name: str
    protocol: str
    host: str
    port: int
    basepath: str
    username: str
    password: str
    use_tls: bool
    validate_cert: bool
    url: str
    topics: tuple[str, ...]          # was: topic_filter: str
    client_id: str
    keepalive_sec: int
    clean_session: bool
    topic_prefix_depth: int
```

In `load_config()`, replace:
```python
        topic_filter=_env("MQTT_TOPIC_FILTER", required=True),
```
with:
```python
        topics=tuple(
            t.strip() for t in _env("MQTT_TOPICS", required=True).split(",") if t.strip()
        ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python roles/mqtt_ingestor/tests/test_config_multi_topic.py -v`
Expected: `OK` (4 tests pass).

- [ ] **Step 5: Commit**

```bash
git add roles/mqtt_ingestor/files/app/ingestor/config.py roles/mqtt_ingestor/tests/test_config_multi_topic.py
git commit -m "feat(mqtt_ingestor): parse MQTT_TOPICS as a comma-separated list"
```

---

### Task 4: Topic classification + broker-stats buffer/flush, wired into pipeline.py

**Files:**
- Create: `roles/mqtt_ingestor/tests/test_topic_classification.py`
- Create: `roles/mqtt_ingestor/files/app/ingestor/models.py` (vendored + `BrokerSnapshot` added)
- Create: `roles/mqtt_ingestor/files/app/ingestor/mqtt_client.py` (vendored + multi-topic subscribe)
- Create: `roles/mqtt_ingestor/files/app/ingestor/metrics.py` (vendored + broker counters)
- Create: `roles/mqtt_ingestor/files/app/ingestor/pipeline.py` (vendored + topic classification + broker buffer/flush thread)

**Interfaces:**
- Consumes: `Spool`, `HealthServer`, `SettingsWriter` (Task 1), `MqttConfig.topics` (Task 3), `DbWriter` (extended in Task 5 — this task calls `self._db.write_broker_snapshot(snapshot)`, defined there).
- Produces: `is_broker_topic(topic: str) -> bool` — the pure classification function, unit tested here, used by `pipeline.py`'s `_handle_event`.
- Produces: `Pipeline` now starts a second daemon thread (`broker-flush-loop`) alongside the existing `writer-loop`.

**Design note on why broker stats don't use the spool:** `broker-ingestor`'s existing model collects each `$SYS/broker/...` sub-topic's latest value into an in-memory dict, and flushes ONE aggregated `BrokerSnapshot` row every `COLLECT_INTERVAL_SEC` (default 60s) — it's fundamentally a periodic gauge sample, not a discrete historical event like a device telemetry message. Routing it through the spool (built for "one row per received message, replayed until a DB write succeeds") would mean spooling ~14 sub-topic messages per interval and reassembling them into a snapshot at write time — more complexity for no real durability gain, since a missed flush interval just means one gap in a time series, not lost telemetry. This task keeps the in-memory-buffer-plus-flush model, sharing only the DB connection/writer, health endpoint, and metrics with the device pipeline.

- [ ] **Step 1: Write the failing test for topic classification**

```python
# roles/mqtt_ingestor/tests/test_topic_classification.py
import importlib.machinery
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINE = os.path.join(HERE, "..", "files", "app", "ingestor", "pipeline.py")


def load():
    loader = importlib.machinery.SourceFileLoader("mi_pipeline", PIPELINE)
    spec = importlib.util.spec_from_loader("mi_pipeline", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


pipeline_mod = load()


class TopicClassification(unittest.TestCase):
    def test_sys_topics_are_broker_topics(self):
        self.assertTrue(pipeline_mod.is_broker_topic("$SYS/broker/clients/connected"))
        self.assertTrue(pipeline_mod.is_broker_topic("$SYS/broker/version"))

    def test_device_topics_are_not_broker_topics(self):
        self.assertFalse(pipeline_mod.is_broker_topic("systems-one/PEPKOR/JBH/DIM1/status"))
        self.assertFalse(pipeline_mod.is_broker_topic("systemsone/PEPKOR/JBH/DIM1/OS/cpu"))

    def test_empty_topic_is_not_a_broker_topic(self):
        self.assertFalse(pipeline_mod.is_broker_topic(""))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python roles/mqtt_ingestor/tests/test_topic_classification.py -v`
Expected: `FAIL` — `pipeline.py` doesn't exist yet at that path (only in the vendored worktree), `FileNotFoundError`.

- [ ] **Step 3: Create models.py — vendored + BrokerSnapshot**

Copy `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/ingestor/models.py` to `roles/mqtt_ingestor/files/app/ingestor/models.py`, then append (using the same simple-field style as the existing dataclasses, not the vendored broker-ingestor version's `field(default_factory=...)` lambda — pass `collected_utc` explicitly at construction time instead, matching how `pipeline.py` will call it in Step 6):

```python
@dataclass
class BrokerSnapshot:
    """Aggregated Mosquitto $SYS broker-health snapshot."""

    collected_utc: datetime
    clients_connected: int | None = None
    clients_total: int | None = None
    clients_inactive: int | None = None
    clients_max: int | None = None
    msgs_received: int | None = None
    msgs_sent: int | None = None
    msgs_stored: int | None = None
    bytes_received: int | None = None
    bytes_sent: int | None = None
    subscriptions: int | None = None
    uptime_seconds: int | None = None
    version: str | None = None
    load_msgs_recv_1min: float | None = None
    load_msgs_sent_1min: float | None = None
```

- [ ] **Step 4: Create mqtt_client.py — vendored + subscribe to every topic in the list**

Copy `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/ingestor/mqtt_client.py` to `roles/mqtt_ingestor/files/app/ingestor/mqtt_client.py`, then in `_on_connect`, replace:
```python
        logger.info(
            "MQTT connected — subscribing",
            extra={"topic": self._cfg.topic_filter},
        )
        client.subscribe(self._cfg.topic_filter, qos=1)
```
with:
```python
        logger.info(
            "MQTT connected — subscribing",
            extra={"topics": list(self._cfg.topics)},
        )
        for topic in self._cfg.topics:
            client.subscribe(topic, qos=1)
```

- [ ] **Step 5: Create metrics.py — vendored + broker counters**

Copy `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/ingestor/metrics.py` to `roles/mqtt_ingestor/files/app/ingestor/metrics.py`, then add to `Metrics.__init__`, alongside the existing counters:

```python
        self.broker_snapshots_flushed_total = Counter(
            "mqtt_ingestor_broker_snapshots_flushed_total",
            "Total broker-health snapshots successfully written to broker.broker_stats",
        )
        self.broker_last_flush_age_seconds = Gauge(
            "mqtt_ingestor_broker_last_flush_age_seconds",
            "Seconds since the last successful broker-stats flush",
        )
```

- [ ] **Step 6: Create pipeline.py — vendored + topic classification + broker buffer/flush thread**

Copy `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/ingestor/pipeline.py` to `roles/mqtt_ingestor/files/app/ingestor/pipeline.py`. Apply these changes:

Add near the top, after the existing imports (module-level, not inside the class — this is what Step 1's test loads):
```python
from .models import BrokerSnapshot

# $SYS topic -> BrokerSnapshot field mapping (from broker-ingestor's pipeline.py)
_BROKER_TOPIC_MAP: dict[str, str] = {
    "$SYS/broker/clients/connected": "clients_connected",
    "$SYS/broker/clients/total": "clients_total",
    "$SYS/broker/clients/inactive": "clients_inactive",
    "$SYS/broker/clients/maximum": "clients_max",
    "$SYS/broker/messages/received": "msgs_received",
    "$SYS/broker/messages/sent": "msgs_sent",
    "$SYS/broker/messages/stored": "msgs_stored",
    "$SYS/broker/bytes/received": "bytes_received",
    "$SYS/broker/bytes/sent": "bytes_sent",
    "$SYS/broker/subscriptions/count": "subscriptions",
    "$SYS/broker/uptime": "uptime_seconds",
    "$SYS/broker/version": "version",
    "$SYS/broker/load/messages/received/1min": "load_msgs_recv_1min",
    "$SYS/broker/load/messages/sent/1min": "load_msgs_sent_1min",
}
_BROKER_INT_FIELDS = {
    "clients_connected", "clients_total", "clients_inactive", "clients_max",
    "msgs_received", "msgs_sent", "msgs_stored", "bytes_received", "bytes_sent",
    "subscriptions", "uptime_seconds",
}
_BROKER_FLOAT_FIELDS = {"load_msgs_recv_1min", "load_msgs_sent_1min"}
_UPTIME_RE = re.compile(r"(\d+)\s*seconds?", re.IGNORECASE)


def is_broker_topic(topic: str) -> bool:
    """True for Mosquitto's own $SYS/# broker-health topic space."""
    return topic.startswith("$SYS/")


def _coerce_broker_field(field: str, raw: str):
    if field in _BROKER_INT_FIELDS:
        if field == "uptime_seconds":
            m = _UPTIME_RE.match(raw)
            if m:
                return int(m.group(1))
        try:
            return int(raw)
        except ValueError:
            try:
                return int(float(raw))
            except ValueError:
                return None
    elif field in _BROKER_FLOAT_FIELDS:
        try:
            return float(raw)
        except ValueError:
            return None
    return raw
```
(add `import re` to the existing import block if not already present, matching broker-ingestor's pipeline.py).

In `Pipeline.__init__`, after the existing `self._mqtt = MqttIngestor(...)` block, add the broker-side state:
```python
        self._broker_buffer: dict[str, Any] = {}
        self._broker_buffer_lock = threading.Lock()
        self._broker_last_flush_utc: datetime | None = None
        self._broker_flush_interval_sec = cfg.app.broker_flush_interval_sec
```
(`broker_flush_interval_sec` is added to `AppConfig` in Task 5's config.py follow-up — for now this task assumes it exists; Task 5 Step 1 adds it before this code is exercised end-to-end.)

Replace `_handle_event` (currently just `self._spool.enqueue(event)`) with:
```python
    def _handle_event(self, event: CanonicalEvent) -> None:
        if is_broker_topic(event.topic):
            self._handle_broker_event(event)
        else:
            self._spool.enqueue(event)

    def _handle_broker_event(self, event: CanonicalEvent) -> None:
        field = _BROKER_TOPIC_MAP.get(event.topic)
        if field is None or event.payload_text is None:
            return
        value = _coerce_broker_field(field, event.payload_text)
        if value is None:
            return
        with self._broker_buffer_lock:
            self._broker_buffer[field] = value
```

Add a new daemon-thread loop, mirroring `_writer_loop`'s structure:
```python
    def _broker_flush_loop(self) -> None:
        interval = self._broker_flush_interval_sec
        logger.info("Broker flush loop started", extra={"interval_sec": interval})

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=interval)
            if self._stop_event.is_set():
                break
            self._flush_broker_snapshot()

        logger.info("Broker flush loop stopping")

    def _flush_broker_snapshot(self) -> None:
        with self._broker_buffer_lock:
            snapshot_data = dict(self._broker_buffer)

        if not snapshot_data:
            return

        snapshot = BrokerSnapshot(collected_utc=datetime.now(UTC), **snapshot_data)
        if self._db.write_broker_snapshot(snapshot):
            self._broker_last_flush_utc = datetime.now(UTC)
            self._metrics.broker_snapshots_flushed_total.inc()
            self._metrics.broker_last_flush_age_seconds.set(0)
```

In `run()`, after the existing `writer_thread.start()` block, start the second thread:
```python
        broker_flush_thread = threading.Thread(
            target=self._broker_flush_loop,
            daemon=True,
            name="broker-flush-loop",
        )
        broker_flush_thread.start()
```
And in the shutdown sequence (after `writer_thread.join(timeout=15)`), join it too:
```python
        broker_flush_thread.join(timeout=10)
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python roles/mqtt_ingestor/tests/test_topic_classification.py -v`
Expected: `OK` (3 tests pass).

- [ ] **Step 8: Commit**

```bash
git add roles/mqtt_ingestor/files/app/ingestor/models.py roles/mqtt_ingestor/files/app/ingestor/mqtt_client.py \
  roles/mqtt_ingestor/files/app/ingestor/metrics.py roles/mqtt_ingestor/files/app/ingestor/pipeline.py \
  roles/mqtt_ingestor/tests/test_topic_classification.py
git commit -m "feat(mqtt_ingestor): route \$SYS/# to an in-memory broker-stats buffer+flush"
```

---

### Task 5: db.py — device dispatcher (allowlist gate) + broker_flush_interval_sec + broker writer

**Files:**
- Create: `roles/mqtt_ingestor/files/app/ingestor/config.py` (Task 3's file, extended)
- Create: `roles/mqtt_ingestor/files/app/ingestor/db.py` (vendored `mqtt-ingestor` device dispatcher + allowlist gate + broker writer folded in)
- Create: `roles/mqtt_ingestor/tests/test_device_allowlist_gate.py`

**Interfaces:**
- Consumes: `validate_device()` (Task 2), `BrokerSnapshot` (Task 4).
- Produces: `DbWriter.write_broker_snapshot(snapshot: BrokerSnapshot) -> bool` — consumed by `pipeline.py`'s `_flush_broker_snapshot` (Task 4, already written assuming this exists).
- Produces: `DbConfig` unchanged; `AppConfig.broker_flush_interval_sec: int` added.

- [ ] **Step 1: Add `broker_flush_interval_sec` to config.py's AppConfig**

In `roles/mqtt_ingestor/files/app/ingestor/config.py` (from Task 3), add to `AppConfig`:
```python
@dataclass(frozen=True)
class AppConfig:
    batch_size: int
    flush_interval_ms: int
    max_retries: int
    retry_base_ms: int
    retry_max_ms: int
    deadletter_enabled: bool
    dedupe_mode: str
    settings_dir: Path
    broker_flush_interval_sec: int          # NEW
```
And in `load_config()`'s `app = AppConfig(...)` block, add:
```python
        broker_flush_interval_sec=_env_int("BROKER_FLUSH_INTERVAL_SEC", 60),
```

- [ ] **Step 2: Write the failing test for the allowlist gate inside db.py's dispatch**

```python
# roles/mqtt_ingestor/tests/test_device_allowlist_gate.py
import importlib.machinery
import importlib.util
import os
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "files", "app", "ingestor", "db.py")


def load():
    loader = importlib.machinery.SourceFileLoader("mi_db", DB)
    spec = importlib.util.spec_from_loader("mi_db", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


db_mod = load()


@dataclass
class FakeEntry:
    id: int
    topic: str
    payload_json: str | None
    payload_text: str | None = None
    enqueued_utc: str = "2026-08-12T00:00:00+00:00"
    source_timestamp_utc: str | None = None


class EnvSandbox(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("INGEST_ALLOWED_CUSTOMERS", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["INGEST_ALLOWED_CUSTOMERS"] = self._saved
        else:
            os.environ.pop("INGEST_ALLOWED_CUSTOMERS", None)


class AllowlistGateInDispatch(EnvSandbox):
    def test_disallowed_customer_never_resolves_a_device(self):
        os.environ["INGEST_ALLOWED_CUSTOMERS"] = "PEPKOR"
        writer = db_mod.DbWriter.__new__(db_mod.DbWriter)
        writer._prefix_depth = 1
        writer._resolve_device = MagicMock()

        entry = FakeEntry(id=1, topic="systems-one/DCB/DUR/DIM1/status", payload_json='{"serial_number":"018370-01-2"}')
        conn = MagicMock()
        writer._dispatch_entry(conn, entry)

        writer._resolve_device.assert_not_called()

    def test_allowed_customer_resolves_a_device(self):
        os.environ["INGEST_ALLOWED_CUSTOMERS"] = "PEPKOR"
        writer = db_mod.DbWriter.__new__(db_mod.DbWriter)
        writer._prefix_depth = 1
        writer._resolve_device = MagicMock(return_value=1)
        writer._handle_status = MagicMock()

        entry = FakeEntry(id=1, topic="systems-one/PEPKOR/JBH/DIM1/status", payload_json='{"serial_number":"018370-01-2"}')
        conn = MagicMock()
        writer._dispatch_entry(conn, entry)

        writer._resolve_device.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python roles/mqtt_ingestor/tests/test_device_allowlist_gate.py -v`
Expected: `FAIL` — `db.py` doesn't exist yet at that path, `FileNotFoundError`.

- [ ] **Step 4: Create db.py — vendored mqtt-ingestor device dispatcher, with the allowlist gate and broker writer added**

Copy `.claude/worktrees/teams-notifications/roles/mqtt_ingestor/files/app/ingestor/db.py` to `roles/mqtt_ingestor/files/app/ingestor/db.py`. Apply these changes:

Add to the imports:
```python
from .models import BrokerSnapshot
from .validation import validate_device
```

Add the broker INSERT SQL template near the other `_INSERT_*`/`_MERGE_*` constants (matching broker-ingestor's `db.py`):
```python
_INSERT_BROKER_STATS = """
INSERT INTO [broker].[broker_stats] (
    collected_utc, clients_connected, clients_total, clients_inactive,
    clients_max, msgs_received, msgs_sent, msgs_stored, bytes_received,
    bytes_sent, subscriptions, uptime_seconds, version,
    load_msgs_recv_1min, load_msgs_sent_1min
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
```

Remove the existing `_validate_device` method — `validation.py`'s `MACHINE_NAME_ONLY_RE` + `SERIAL_RE` supersede its rejection behavior and add the allowlist. **Do not remove `_MACHINE_NAME_RE`** — `_resolve_device` (unchanged, vendored) also uses that same class attribute independently, to detect whether an incoming serial looks like a machine-name placeholder before deciding whether to treat it as a real serial worth upgrading a stored `UNKNOWN-` placeholder to. That's a distinct purpose from the validation gate's rejection check, even though both happen to use the same pattern — removing it breaks device resolution, not just validation. In `_dispatch_entry`, replace:
```python
        if not self._validate_device(customer, location, machine_name, serial_number):
            return
```
with:
```python
        if serial_number and not validate_device(serial_number, customer, location, machine_name, logger):
            return
        if not self._validate_customer_location(customer, location):
            return
```
Add the allowlist-only half as a small helper (serial-format checking is skipped here since `_resolve_device` legitimately handles the no-serial-yet case with a placeholder — only the customer/location allowlist applies unconditionally):
```python
    @staticmethod
    def _validate_customer_location(customer: str, location: str) -> bool:
        import os as _os

        def _allowlist(name: str) -> set[str]:
            raw = _os.environ.get(name, "")
            return {v.strip().upper() for v in raw.split(",") if v.strip()}

        allowed_customers = _allowlist("INGEST_ALLOWED_CUSTOMERS")
        if allowed_customers and customer.strip().upper() not in allowed_customers:
            return False
        allowed_locations = _allowlist("INGEST_ALLOWED_LOCATIONS")
        if allowed_locations and location.strip().upper() not in allowed_locations:
            return False
        return True
```

Add `write_broker_snapshot` as a new public method on `DbWriter`, following the same retry/backoff shape as `write_batch`:
```python
    def write_broker_snapshot(self, snapshot: BrokerSnapshot) -> bool:
        params = (
            snapshot.collected_utc, snapshot.clients_connected, snapshot.clients_total,
            snapshot.clients_inactive, snapshot.clients_max, snapshot.msgs_received,
            snapshot.msgs_sent, snapshot.msgs_stored, snapshot.bytes_received,
            snapshot.bytes_sent, snapshot.subscriptions, snapshot.uptime_seconds,
            snapshot.version, snapshot.load_msgs_recv_1min, snapshot.load_msgs_sent_1min,
        )
        for attempt in range(self._max_retries + 1):
            try:
                conn = self._get_conn()
                conn.execute(_INSERT_BROKER_STATS, params)
                conn.commit()
                if self._metrics:
                    self._metrics.db_write_success_total.inc()
                return True
            except pyodbc.Error as exc:
                if self._metrics:
                    self._metrics.db_write_failure_total.inc()
                try:
                    self._get_conn().rollback()
                except Exception:
                    pass
                self._close_conn()
                logger.warning(
                    "Broker stats write failed -- retrying",
                    extra={"attempt": attempt + 1, "error": str(exc)},
                )
                if attempt < self._max_retries:
                    time.sleep(self._backoff(attempt))

        logger.error("Broker stats write exhausted retries -- snapshot dropped")
        return False
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python roles/mqtt_ingestor/tests/test_device_allowlist_gate.py -v`
Expected: `OK` (2 tests pass).

- [ ] **Step 6: Add the broker schema guard to migrations**

Append to `roles/mqtt_ingestor/files/app/migrations/001_init_schema.sql` (copied in Task 1) — the safety-net `IF NOT EXISTS` guard for `broker.broker_stats`, matching the guard style already used in that file for `dbo.*` tables (copy verbatim from `/home/s1/broker-ingestor/migrations/001_init_schema.sql`, adding the `broker` schema + `broker_stats` table `IF NOT EXISTS` blocks at the end of the file):
```sql
-- ---------------------------------------------------------------------------
-- broker schema (broker-health snapshots)
-- ---------------------------------------------------------------------------

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'broker')
    EXEC('CREATE SCHEMA broker')
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    WHERE s.name = 'broker' AND t.name = 'broker_stats'
)
BEGIN
    CREATE TABLE [broker].[broker_stats] (
        id                   BIGINT        IDENTITY(1,1) NOT NULL,
        collected_utc        DATETIME2(3)  NOT NULL,
        clients_connected    INT           NULL,
        clients_total        INT           NULL,
        clients_inactive     INT           NULL,
        clients_max          INT           NULL,
        msgs_received        BIGINT        NULL,
        msgs_sent            BIGINT        NULL,
        msgs_stored          INT           NULL,
        bytes_received       BIGINT        NULL,
        bytes_sent           BIGINT        NULL,
        subscriptions        INT           NULL,
        uptime_seconds       INT           NULL,
        version              NVARCHAR(100) NULL,
        load_msgs_recv_1min  FLOAT         NULL,
        load_msgs_sent_1min  FLOAT         NULL,
        CONSTRAINT PK_broker_stats PRIMARY KEY CLUSTERED (id)
    )
END
GO
```

- [ ] **Step 7: Commit**

```bash
git add roles/mqtt_ingestor/files/app/ingestor/config.py roles/mqtt_ingestor/files/app/ingestor/db.py \
  roles/mqtt_ingestor/files/app/migrations/001_init_schema.sql roles/mqtt_ingestor/tests/test_device_allowlist_gate.py
git commit -m "feat(mqtt_ingestor): wire allowlist gate + broker_stats writer into db.py"
```

---

### Task 6: Ansible role — defaults, tasks, docker-compose

**Files:**
- Create: `roles/mqtt_ingestor/defaults/main.yml`
- Create: `roles/mqtt_ingestor/tasks/main.yml`
- Create: `roles/mqtt_ingestor/templates/docker-compose.yml.j2`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure Ansible/YAML).
- Produces: the deployable role, added to a play in Task 7.

- [ ] **Step 1: Write defaults/main.yml**

```yaml
---
mqtt_ingestor_install_dir: /opt/mqtt_ingestor
mqtt_ingestor_image: mqtt-ingestor:latest
mqtt_ingestor_docker_network: "{{ docker_shared_network | default('infra') }}"

# MQTT — subscribes to device telemetry (both prefix variants) and broker health
mqtt_ingestor_mqtt_connection_name: mqtt-ingestor
mqtt_ingestor_mqtt_protocol: tcp
mqtt_ingestor_mqtt_host: mosquitto
mqtt_ingestor_mqtt_port: "{{ mosquitto_mqtt_port | default(1883) }}"
mqtt_ingestor_mqtt_basepath: ""
mqtt_ingestor_mqtt_url: ""
mqtt_ingestor_mqtt_username: "{{ mqtt_username | default('') }}"
mqtt_ingestor_mqtt_password: "{{ mqtt_password | default('') }}"
mqtt_ingestor_mqtt_use_tls: false
mqtt_ingestor_mqtt_validate_cert: true
mqtt_ingestor_mqtt_topics: "systems-one/#,systemsone/#,$SYS/#"
mqtt_ingestor_mqtt_client_id: mqtt-ingestor
mqtt_ingestor_mqtt_keepalive_sec: 60
mqtt_ingestor_mqtt_clean_session: false
mqtt_ingestor_mqtt_topic_prefix_depth: 1

# Database — shares MSSQL with everything else on this box
mqtt_ingestor_db_host: mssql
mqtt_ingestor_db_port: "{{ mssql_port | default(1433) }}"
mqtt_ingestor_db_name: "{{ mssql_rm_database | default('S1_Remote_Monitoring') }}"
mqtt_ingestor_db_user: sa
mqtt_ingestor_db_password: "{{ mssql_sa_password | default('') }}"
mqtt_ingestor_db_driver: "ODBC Driver 18 for SQL Server"
mqtt_ingestor_db_trust_server_certificate: true
mqtt_ingestor_db_connect_timeout_sec: 5
mqtt_ingestor_db_command_timeout_sec: 30

# Device allowlist — unset/empty means allow all (matches validation.py default)
mqtt_ingestor_allowed_customers: ""
mqtt_ingestor_allowed_locations: ""

# SQLite spool (device-telemetry durability)
mqtt_ingestor_spool_dir: /var/lib/mqtt-ingestor/spool
mqtt_ingestor_spool_max_bytes: 1073741824
mqtt_ingestor_spool_critical_pct: 95
mqtt_ingestor_spool_full_mode: drop_oldest
mqtt_ingestor_spool_drop_log_interval_sec: 30

# Application
mqtt_ingestor_app_batch_size: 100
mqtt_ingestor_app_flush_interval_ms: 500
mqtt_ingestor_app_max_retries: 8
mqtt_ingestor_app_retry_base_ms: 250
mqtt_ingestor_app_retry_max_ms: 30000
mqtt_ingestor_app_deadletter_enabled: true
mqtt_ingestor_app_dedupe_mode: "off"
mqtt_ingestor_settings_dir: /var/lib/mqtt-ingestor/settings
mqtt_ingestor_broker_flush_interval_sec: 60

# Observability
mqtt_ingestor_log_level: INFO
mqtt_ingestor_log_format: json
mqtt_ingestor_health_stale_db_write_sec: 120
mqtt_ingestor_metrics_enabled: true
mqtt_ingestor_metrics_port: 9108
mqtt_ingestor_health_port: 8080
```

- [ ] **Step 2: Write tasks/main.yml**

```yaml
- name: Ensure mqtt_ingestor directories exist
  file:
    path: "{{ item }}"
    state: directory
    owner: root
    group: root
    mode: "0755"
  loop:
    - "{{ mqtt_ingestor_install_dir }}"

- name: Ensure shared Docker network exists
  shell: "docker network inspect {{ mqtt_ingestor_docker_network }} >/dev/null 2>&1 || docker network create {{ mqtt_ingestor_docker_network }}"
  changed_when: false

- name: Validate required variables
  assert:
    that:
      - mqtt_ingestor_mqtt_host is defined
      - mqtt_ingestor_mqtt_host | length > 0
      - mqtt_ingestor_mqtt_topics is defined
      - mqtt_ingestor_mqtt_topics | length > 0
      - mqtt_ingestor_db_host is defined
      - mqtt_ingestor_db_host | length > 0
      - mqtt_ingestor_db_name is defined
      - mqtt_ingestor_db_name | length > 0
      - mqtt_ingestor_db_user is defined
      - mqtt_ingestor_db_user | length > 0
    fail_msg: >-
      Missing required mqtt_ingestor variables. Provide MQTT host + topics,
      and DB host/name/user.

- name: Copy mqtt_ingestor service source (kept identical)
  copy:
    src: app/
    dest: "{{ mqtt_ingestor_install_dir }}/app/"
    owner: root
    group: root
    mode: "0644"

- name: Render Dockerfile
  template:
    src: Dockerfile.j2
    dest: "{{ mqtt_ingestor_install_dir }}/Dockerfile"
    owner: root
    group: root
    mode: "0644"

- name: Render docker compose file
  template:
    src: docker-compose.yml.j2
    dest: "{{ mqtt_ingestor_install_dir }}/docker-compose.yml"
    owner: root
    group: root
    mode: "0644"

- name: Start/Update mqtt_ingestor container
  command: docker compose up -d --build --remove-orphans
  args:
    chdir: "{{ mqtt_ingestor_install_dir }}"
```

- [ ] **Step 3: Write docker-compose.yml.j2**

```yaml
services:
  mqtt-ingestor:
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    build:
      context: .
      dockerfile: Dockerfile
    image: "{{ mqtt_ingestor_image }}"
    container_name: mqtt-ingestor
    restart: unless-stopped
    environment:
      MQTT_HOST: "{{ mqtt_ingestor_mqtt_host }}"
      MQTT_PORT: "{{ mqtt_ingestor_mqtt_port }}"
      MQTT_PROTOCOL: "{{ mqtt_ingestor_mqtt_protocol }}"
      MQTT_URL: "{{ mqtt_ingestor_mqtt_url }}"
      MQTT_BASEPATH: "{{ mqtt_ingestor_mqtt_basepath }}"
      MQTT_USERNAME: "{{ mqtt_ingestor_mqtt_username }}"
      MQTT_PASSWORD: "{{ mqtt_ingestor_mqtt_password }}"
      MQTT_USE_TLS: "{{ mqtt_ingestor_mqtt_use_tls | ternary('true','false') }}"
      MQTT_VALIDATE_CERT: "{{ mqtt_ingestor_mqtt_validate_cert | ternary('true','false') }}"
      MQTT_TOPICS: "{{ mqtt_ingestor_mqtt_topics }}"
      MQTT_CONNECTION_NAME: "{{ mqtt_ingestor_mqtt_connection_name }}"
      MQTT_CLIENT_ID: "{{ mqtt_ingestor_mqtt_client_id }}"
      MQTT_KEEPALIVE_SEC: "{{ mqtt_ingestor_mqtt_keepalive_sec }}"
      MQTT_CLEAN_SESSION: "{{ mqtt_ingestor_mqtt_clean_session | ternary('true','false') }}"
      MQTT_TOPIC_PREFIX_DEPTH: "{{ mqtt_ingestor_mqtt_topic_prefix_depth }}"

      DB_HOST: "{{ mqtt_ingestor_db_host }}"
      DB_PORT: "{{ mqtt_ingestor_db_port }}"
      DB_NAME: "{{ mqtt_ingestor_db_name }}"
      DB_USER: "{{ mqtt_ingestor_db_user }}"
      DB_PASSWORD: "{{ mqtt_ingestor_db_password }}"
      DB_DRIVER: "{{ mqtt_ingestor_db_driver }}"
      DB_TRUST_SERVER_CERTIFICATE: "{{ mqtt_ingestor_db_trust_server_certificate | ternary('true','false') }}"
      DB_CONNECT_TIMEOUT_SEC: "{{ mqtt_ingestor_db_connect_timeout_sec }}"
      DB_COMMAND_TIMEOUT_SEC: "{{ mqtt_ingestor_db_command_timeout_sec }}"

      INGEST_ALLOWED_CUSTOMERS: "{{ mqtt_ingestor_allowed_customers }}"
      INGEST_ALLOWED_LOCATIONS: "{{ mqtt_ingestor_allowed_locations }}"

      SPOOL_DIR: "{{ mqtt_ingestor_spool_dir }}"
      SPOOL_MAX_BYTES: "{{ mqtt_ingestor_spool_max_bytes }}"
      SPOOL_CRITICAL_PCT: "{{ mqtt_ingestor_spool_critical_pct }}"
      SPOOL_FULL_MODE: "{{ mqtt_ingestor_spool_full_mode }}"
      SPOOL_DROP_LOG_INTERVAL_SEC: "{{ mqtt_ingestor_spool_drop_log_interval_sec }}"

      APP_BATCH_SIZE: "{{ mqtt_ingestor_app_batch_size }}"
      APP_FLUSH_INTERVAL_MS: "{{ mqtt_ingestor_app_flush_interval_ms }}"
      APP_MAX_RETRIES: "{{ mqtt_ingestor_app_max_retries }}"
      APP_RETRY_BASE_MS: "{{ mqtt_ingestor_app_retry_base_ms }}"
      APP_RETRY_MAX_MS: "{{ mqtt_ingestor_app_retry_max_ms }}"
      APP_DEADLETTER_ENABLED: "{{ mqtt_ingestor_app_deadletter_enabled | ternary('true','false') }}"
      APP_DEDUPE_MODE: "{{ mqtt_ingestor_app_dedupe_mode }}"
      SETTINGS_DIR: "{{ mqtt_ingestor_settings_dir }}"
      BROKER_FLUSH_INTERVAL_SEC: "{{ mqtt_ingestor_broker_flush_interval_sec }}"

      LOG_LEVEL: "{{ mqtt_ingestor_log_level }}"
      LOG_FORMAT: "{{ mqtt_ingestor_log_format }}"
      HEALTH_STALE_DB_WRITE_SEC: "{{ mqtt_ingestor_health_stale_db_write_sec }}"
      METRICS_ENABLED: "{{ mqtt_ingestor_metrics_enabled | ternary('true','false') }}"
      METRICS_PORT: "{{ mqtt_ingestor_metrics_port }}"
      HEALTH_PORT: "{{ mqtt_ingestor_health_port }}"
    volumes:
      - spool-data:/var/lib/mqtt-ingestor/spool
    ports:
      - "8083:8080"
      - "9110:9108"
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request,sys; r=urllib.request.urlopen('http://localhost:8080/health',timeout=4); sys.exit(0 if r.status==200 else 1)\""]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    networks:
      - {{ mqtt_ingestor_docker_network }}

volumes:
  spool-data:

networks:
  {{ mqtt_ingestor_docker_network }}:
    external: true
```

(Ports `8083`/`9110` avoid the existing `8080`/`8082`/`9108`/`9109` already bound by `wetty`/`mqtt-ingestor`(legacy)/`systems_one_ingest`/`mqtt-ingestor`(legacy) — confirmed free via `ss -tlnp` the same way the `scan_fleet_dashboard` port collision was resolved earlier this session; re-check with `ss -tlnp` on the actual staging box before deploying, since legacy services may still be running there too.)

- [ ] **Step 4: Validate all YAML/Jinja renders**

Run:
```bash
python -c "import yaml; yaml.safe_load(open('roles/mqtt_ingestor/defaults/main.yml')); yaml.safe_load(open('roles/mqtt_ingestor/tasks/main.yml')); print('yaml ok')"
```
Expected: `yaml ok` (the `docker-compose.yml.j2` template can't be validated as plain YAML due to Jinja `{{ }}` blocks — it's checked at deploy time by Ansible's own templating).

- [ ] **Step 5: Commit**

```bash
git add roles/mqtt_ingestor/defaults/main.yml roles/mqtt_ingestor/tasks/main.yml roles/mqtt_ingestor/templates/docker-compose.yml.j2
git commit -m "feat(mqtt_ingestor): add Ansible role deploy tasks"
```

---

### Task 7: Wire into sysone_staging (alongside the three existing services, not replacing them yet)

**Files:**
- Modify: `webservers.yml` (add `mqtt_ingestor` to the roles list, tagged so it can be deployed independently)
- Modify: `host_vars/sysone_staging.yml` (host-specific overrides)

**Interfaces:**
- Consumes: the `mqtt_ingestor` role from Task 6.
- Produces: nothing further downstream — this is the deployment target.

- [ ] **Step 1: Add the role to webservers.yml, tagged**

In `webservers.yml`'s `roles:` list, add after `systems_one_ingest`:
```yaml
    - role: mqtt_ingestor
      tags: mqtt_ingestor
```
(Tagged, unlike most existing roles, so it can be deployed/redeployed in isolation during staging verification without re-running the whole play — matches the pattern already used for `s1_reporter` and `scan_fleet_dashboard`.)

- [ ] **Step 2: Add staging host_vars overrides**

In `host_vars/sysone_staging.yml`, add:
```yaml
# mqtt_ingestor — staged alongside the existing systems_one_ingest/mqtt-ingestor/
# broker-ingestor for comparison before cutover. See docs/superpowers/specs/2026-08-12-ingestor-consolidation-design.md.
mqtt_ingestor_mqtt_client_id: mqtt-ingestor-staging-merged
```
(A distinct client ID so it doesn't collide with the legacy services' sessions on the same broker during the comparison window.)

- [ ] **Step 3: Validate YAML**

Run:
```bash
python -c "import yaml; yaml.safe_load(open('webservers.yml')); yaml.safe_load(open('host_vars/sysone_staging.yml')); print('yaml ok')"
```
Expected: `yaml ok`

- [ ] **Step 4: Commit**

```bash
git add webservers.yml host_vars/sysone_staging.yml
git commit -m "feat(mqtt_ingestor): wire into sysone_staging alongside legacy ingestors"
```

---

### Task 8: Deploy to staging and verify

**Files:** none (operational task, run against the live `sysone_staging` host).

- [ ] **Step 1: Run the full test suite locally first**

```bash
python roles/mqtt_ingestor/tests/test_validation.py -v
python roles/mqtt_ingestor/tests/test_config_multi_topic.py -v
python roles/mqtt_ingestor/tests/test_topic_classification.py -v
python roles/mqtt_ingestor/tests/test_device_allowlist_gate.py -v
```
Expected: all four suites report `OK`.

- [ ] **Step 2: Deploy the role, scoped to just this service**

```bash
ssh s1_staging "cd /home/s1/Systems-One-Server && git fetch origin && git merge origin/master && ansible-playbook -i staging webservers.yml --tags mqtt_ingestor"
```
(Adjust the inventory/host alias to whatever `sysone_staging`'s actual SSH target is — confirm via `host_vars/sysone_staging.yml`'s `ansible_host`/connection settings before running.)

- [ ] **Step 3: Confirm the container is healthy**

```bash
ssh s1_staging "docker ps --filter name=mqtt-ingestor --format '{{.Names}}: {{.Status}}'"
```
Expected: `mqtt-ingestor: Up ... (healthy)` within `start_period` (20s) + a few retries.

- [ ] **Step 4: Confirm it's receiving all three topic spaces**

```bash
ssh s1_staging "docker logs mqtt-ingestor --tail 50 | grep -i subscribing"
```
Expected: one log line listing `["systems-one/#", "systemsone/#", "$SYS/#"]` (or however the JSON logger renders the list) — confirms Task 3/4's multi-topic subscribe took effect.

- [ ] **Step 5: Compare device-table writes against the legacy services over a 15+ minute window**

```bash
ssh s1_staging "docker exec mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P \"\$MSSQL_SA_PASSWORD\" -C -Q \"SELECT TOP 20 device_id, ts_datetime FROM dbo.device_status ORDER BY id DESC\""
```
Confirm new rows are appearing with recent timestamps, from devices whose `customer`/`location` match what's actually configured on `sysone_staging`'s test topics.

- [ ] **Step 6: Confirm broker-stats rows are appearing**

```bash
ssh s1_staging "docker exec mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P \"\$MSSQL_SA_PASSWORD\" -C -Q \"SELECT TOP 5 collected_utc, clients_connected, msgs_received FROM broker.broker_stats ORDER BY id DESC\""
```
Expected: rows appearing roughly every 60s (`BROKER_FLUSH_INTERVAL_SEC` default), with plausible `clients_connected`/`msgs_received` values.

- [ ] **Step 7: Confirm the allowlist gate didn't regress — check for any customer that was previously being silently dropped**

```bash
ssh s1_staging "docker logs mqtt-ingestor 2>&1 | grep -i 'rejecting device' | tail -20"
```
Expected: either no output, or rejections only for genuinely malformed serials/machine-name placeholders — never a rejection citing `INGEST_ALLOWED_CUSTOMERS`/`INGEST_ALLOWED_LOCATIONS` unless those are explicitly set in `host_vars/sysone_staging.yml` (they aren't, per Task 7 — so this should never fire on staging).

- [ ] **Step 8: Record results**

Write the outcome (healthy/unhealthy, row counts observed, any discrepancies found) into a short note at the end of `docs/superpowers/plans/2026-08-12-mqtt-ingestor-consolidation-build.md` (this file) under a new `## Staging Results` heading, dated. This becomes the input for the follow-up production-cutover plan.

- [ ] **Step 9: Commit the results note**

```bash
git add docs/superpowers/plans/2026-08-12-mqtt-ingestor-consolidation-build.md
git commit -m "docs(mqtt_ingestor): record staging verification results"
```

---

## What's deliberately NOT in this plan

- **Production cutover** (stopping/removing `systems_one_ingest`, `mqtt-ingestor`, `broker-ingestor` on `sysone`, and the Mosquitto session cleanup for `mqtt-ingestor`'s persistent client ID) — per the spec, this only happens after staging has run clean for a full day-night cycle. Task 8's results become the input for that plan, written separately once they exist.
- **Deploying to `sysone` (production) at all** — this plan only touches `sysone_staging`.
