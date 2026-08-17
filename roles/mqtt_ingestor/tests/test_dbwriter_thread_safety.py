"""Unit tests for db.py DbWriter cross-thread connection serialization.

pipeline.py runs the writer loop and the broker-stats flush loop as two
daemon threads that share one DbWriter, whose single pyodbc connection is
NOT safe for concurrent statements (MARS is off). Unsynchronized use
produces HY000 "Connection is busy with results for another command"
errors and native-code segfaults (container exit 139) in production.
These tests pin the required behavior: DbWriter's public write methods
must never let two threads touch the shared connection at the same time.

Loads db.py under a dotted, package-qualified name with a pyodbc stub,
following the pattern in test_device_allowlist_gate.py.
"""

import importlib.machinery
import importlib.util
import os
import sys
import threading
import time
import types
import unittest
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(HERE, "..", "files", "app")
DB = os.path.join(HERE, "..", "files", "app", "ingestor", "db.py")


def _install_pyodbc_stub() -> bool:
    if "pyodbc" in sys.modules:
        return False
    stub = types.ModuleType("pyodbc")

    class Error(Exception):
        pass

    stub.Error = Error
    stub.Connection = object
    sys.modules["pyodbc"] = stub
    return True


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
    added_pyodbc_stub = _install_pyodbc_stub()
    sys.path.insert(0, APP_DIR)
    try:
        loader = importlib.machinery.SourceFileLoader("ingestor.db", DB)
        spec = importlib.util.spec_from_loader("ingestor.db", loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ingestor.db"] = mod
        loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(APP_DIR)
        for k in list(sys.modules):
            if k == "ingestor" or k.startswith("ingestor."):
                del sys.modules[k]
        sys.modules.update(saved)
        if added_pyodbc_stub:
            del sys.modules["pyodbc"]


db_mod = load()

# models.py is a real vendored sibling; import it the same way for BrokerSnapshot
MODELS = os.path.join(HERE, "..", "files", "app", "ingestor", "models.py")
_models_loader = importlib.machinery.SourceFileLoader("_thread_test_models", MODELS)
_models_spec = importlib.util.spec_from_loader("_thread_test_models", _models_loader)
models_mod = importlib.util.module_from_spec(_models_spec)
# dataclass processing resolves cls.__module__ via sys.modules, so the module
# must be registered before exec; the unique name avoids clobbering siblings.
sys.modules["_thread_test_models"] = models_mod
_models_loader.exec_module(models_mod)


@dataclass
class FakeEntry:
    id: int
    topic: str = "systems-one/CUST/LOC/DIM1/status"
    payload_json: str | None = None
    payload_text: str | None = None
    enqueued_utc: str = "2026-08-17T00:00:00+00:00"
    source_timestamp_utc: str | None = None


class ConcurrencyTrackingConn:
    """Fake pyodbc connection that records overlapping use across threads."""

    def __init__(self):
        self._guard = threading.Lock()
        self._active = 0
        self.max_active = 0

    def _track(self):
        with self._guard:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        time.sleep(0.005)  # widen the race window
        with self._guard:
            self._active -= 1

    def execute(self, *args, **kwargs):
        self._track()
        return self

    def commit(self):
        self._track()

    def rollback(self):
        self._track()

    def close(self):
        pass

    def fetchone(self):
        return None


class DbWriterSerializesConnectionAccess(unittest.TestCase):
    def _make_writer(self, conn):
        writer = db_mod.DbWriter(
            cfg=None,  # never used: _get_conn is stubbed below
            prefix_depth=1,
            max_retries=0,
            retry_base_ms=1,
            retry_max_ms=1,
            metrics=None,
        )
        writer._get_conn = lambda: conn
        # Bypass topic routing/device resolution: the behavior under test is
        # connection-level serialization, not dispatch.
        writer._dispatch_entry = lambda c, entry: c.execute("SELECT 1")
        return writer

    def test_concurrent_write_batch_and_broker_snapshot_never_share_connection(self):
        conn = ConcurrencyTrackingConn()
        writer = self._make_writer(conn)
        snapshot = models_mod.BrokerSnapshot(
            collected_utc=__import__("datetime").datetime.now(
                __import__("datetime").UTC
            )
        )

        barrier = threading.Barrier(2)
        errors = []

        def batch_worker():
            barrier.wait()
            try:
                for i in range(15):
                    writer.write_batch([FakeEntry(id=i)])
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def broker_worker():
            barrier.wait()
            try:
                for _ in range(15):
                    writer.write_broker_snapshot(snapshot)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        t1 = threading.Thread(target=batch_worker)
        t2 = threading.Thread(target=broker_worker)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(
            conn.max_active,
            1,
            "DbWriter allowed two threads to use the shared pyodbc "
            f"connection concurrently (max_active={conn.max_active})",
        )


if __name__ == "__main__":
    unittest.main()
