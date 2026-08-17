"""Unit tests for pipeline.py broker-stats buffer retention across flushes.

Mosquitto publishes many $SYS topics (clients/connected, clients/total,
subscriptions/count, messages/stored, version) only when the value CHANGES,
not every sys_interval. Clearing the broker buffer after each flush
therefore produces NULL columns in broker.broker_stats for every minute in
which mosquitto stayed quiet — which is almost all of them — and the
Grafana broker-health dashboard's "TOP 1 ... ORDER BY id DESC" stat panels
render empty. The legacy broker-ingestor carried last-known values forward
(zero NULLs through Aug 11; ~95% NULLs from the Aug 12 consolidation on).
These tests pin the required behavior: a flushed value persists into
subsequent snapshots until a newer $SYS message replaces it.

Loads pipeline.py under a dotted, package-qualified name with a db stub,
following the pattern in test_topic_classification.py.
"""

import importlib.machinery
import importlib.util
import os
import sys
import threading
import types
import unittest
from datetime import UTC, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(HERE, "..", "files", "app")
PIPELINE = os.path.join(HERE, "..", "files", "app", "ingestor", "pipeline.py")


def _install_db_stub() -> types.ModuleType:
    stub = types.ModuleType("ingestor.db")

    class DbWriter:
        pass

    stub.DbWriter = DbWriter
    sys.modules["ingestor.db"] = stub
    return stub


def _snapshot_ingestor_modules() -> dict:
    stale = {
        k: v
        for k, v in sys.modules.items()
        if k == "ingestor" or k.startswith("ingestor.")
    }
    for k in stale:
        del sys.modules[k]
    return stale


def load():
    saved = _snapshot_ingestor_modules()
    _install_db_stub()
    sys.path.insert(0, APP_DIR)
    try:
        loader = importlib.machinery.SourceFileLoader("ingestor.pipeline", PIPELINE)
        spec = importlib.util.spec_from_loader("ingestor.pipeline", loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ingestor.pipeline"] = mod
        loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(APP_DIR)
        for k in list(sys.modules):
            if k == "ingestor" or k.startswith("ingestor."):
                del sys.modules[k]
        sys.modules.update(saved)


pipeline_mod = load()


class RecordingDb:
    """Fake DbWriter capturing every snapshot passed to write_broker_snapshot."""

    def __init__(self):
        self.snapshots = []

    def write_broker_snapshot(self, snapshot):
        self.snapshots.append(snapshot)
        return True


class FakeCounter:
    def inc(self, *args):
        pass


class FakeMetrics:
    def __init__(self):
        self.broker_snapshots_flushed_total = FakeCounter()


def make_pipeline(db):
    p = pipeline_mod.Pipeline.__new__(pipeline_mod.Pipeline)
    p._broker_buffer = {}
    p._broker_buffer_lock = threading.Lock()
    p._broker_last_flush_utc = None
    p._db = db
    p._metrics = FakeMetrics()
    return p


class BrokerBufferRetainsLastKnownValues(unittest.TestCase):
    def test_flushed_value_persists_into_next_snapshot(self):
        db = RecordingDb()
        p = make_pipeline(db)

        # $SYS/broker/clients/connected arrives once (change-driven topic)
        with p._broker_buffer_lock:
            p._broker_buffer["clients_connected"] = 18

        p._flush_broker_snapshot()
        # A minute passes; mosquitto publishes nothing new for this topic,
        # only an interval-driven counter ticks.
        with p._broker_buffer_lock:
            p._broker_buffer["msgs_received"] = 4312
        p._flush_broker_snapshot()

        self.assertEqual(len(db.snapshots), 2)
        self.assertEqual(
            db.snapshots[1].clients_connected,
            18,
            "last-known clients_connected was dropped between flushes; "
            "change-driven $SYS values must carry forward",
        )
        self.assertEqual(db.snapshots[1].msgs_received, 4312)

    def test_newer_value_replaces_carried_forward_value(self):
        db = RecordingDb()
        p = make_pipeline(db)

        with p._broker_buffer_lock:
            p._broker_buffer["clients_connected"] = 18
        p._flush_broker_snapshot()

        with p._broker_buffer_lock:
            p._broker_buffer["clients_connected"] = 19
        p._flush_broker_snapshot()

        self.assertEqual(db.snapshots[1].clients_connected, 19)


if __name__ == "__main__":
    unittest.main()
