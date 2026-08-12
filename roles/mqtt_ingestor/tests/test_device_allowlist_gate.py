"""Unit tests for mqtt_ingestor's db.py allowlist gate in _dispatch_entry.

db.py does package-relative imports (`from .config import DbConfig`,
`from .models import BrokerSnapshot, SpoolEntry`, `from .validation import
validate_device`) that are all real, already-vendored sibling files in this
package -- so, like test_topic_classification.py, this loads db.py under a
dotted, package-qualified name ("ingestor.db") with the app dir on
sys.path, letting those relative imports resolve against the real files.

db.py also does `import pyodbc` at module scope; pyodbc requires a native
ODBC driver manager and is absent on dev machines (see
roles/systems_one_ingest/tests/test_validation.py's docstring for the same
constraint), so a minimal stand-in is registered in sys.modules before
loading. Neither test here exercises a real DB connection.
"""
import importlib.machinery
import importlib.util
import os
import sys
import types
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(HERE, "..", "files", "app")
DB = os.path.join(HERE, "..", "files", "app", "ingestor", "db.py")


def _install_pyodbc_stub() -> bool:
    """Register a minimal pyodbc stand-in if it isn't already importable.

    Returns True if this call added the stub (so the caller can remove it
    again afterwards and not leak it into sibling test files), False if a
    real or previously-stubbed pyodbc was already present.
    """
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
    """
    Capture (and remove) every 'ingestor' / 'ingestor.*' entry currently in
    sys.modules, mirroring test_topic_classification.py's load(). Sibling
    test files in this suite register their own incomplete
    sys.modules["ingestor.*"] stubs at module-import time and never clean
    them up, so under a combined run (`pytest roles/mqtt_ingestor/tests/`)
    those stale stubs would otherwise be reused here instead of the real
    config.py/models.py/validation.py. Clearing the namespace first forces
    a fresh, correct import graph regardless of collection order.
    """
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
        # Undo everything this load() added to sys.modules so it doesn't
        # leak "ingestor.*" state into sibling test files collected after
        # it, then restore whatever was there before. The db module keeps
        # its own reference to the pyodbc module object via `import pyodbc`
        # at exec time, so it's safe to drop the stub from sys.modules here
        # -- db_mod.pyodbc.Error etc. keep working afterwards.
        for k in list(sys.modules):
            if k == "ingestor" or k.startswith("ingestor."):
                del sys.modules[k]
        sys.modules.update(saved)
        if added_pyodbc_stub:
            del sys.modules["pyodbc"]


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


class SerialFormatGateInDispatch(EnvSandbox):
    """
    db.py's _dispatch_entry should only reject serials that look like a
    machine-name placeholder (MACHINE_NAME_ONLY_RE, e.g. "DIM5"/"STATIC1"),
    matching the vendored pipeline's original scope -- it must NOT also
    enforce validation.py's stricter SERIAL_RE minimum-length/format check,
    which would wrongly reject short/non-standard-but-real serials like
    "N/A" that the vendored pipeline auto-registered via the
    placeholder-then-upgrade mechanism in _resolve_device.
    """

    def test_short_non_standard_serial_is_not_rejected(self):
        writer = db_mod.DbWriter.__new__(db_mod.DbWriter)
        writer._prefix_depth = 1
        writer._resolve_device = MagicMock(return_value=1)
        writer._handle_status = MagicMock()

        entry = FakeEntry(id=1, topic="systems-one/PEPKOR/JBH/DIM1/status", payload_json='{"serial_number":"N/A"}')
        conn = MagicMock()
        writer._dispatch_entry(conn, entry)

        writer._resolve_device.assert_called_once()

    def test_machine_name_placeholder_serial_is_still_rejected(self):
        for placeholder in ("DIM5", "STATIC1"):
            with self.subTest(placeholder=placeholder):
                writer = db_mod.DbWriter.__new__(db_mod.DbWriter)
                writer._prefix_depth = 1
                writer._resolve_device = MagicMock()

                entry = FakeEntry(
                    id=1,
                    topic="systems-one/PEPKOR/JBH/DIM1/status",
                    payload_json='{"serial_number":"%s"}' % placeholder,
                )
                conn = MagicMock()
                writer._dispatch_entry(conn, entry)

                writer._resolve_device.assert_not_called()

    def test_allowlist_gate_still_applies_alongside_narrower_serial_check(self):
        os.environ["INGEST_ALLOWED_CUSTOMERS"] = "PEPKOR"
        writer = db_mod.DbWriter.__new__(db_mod.DbWriter)
        writer._prefix_depth = 1
        writer._resolve_device = MagicMock()

        entry = FakeEntry(id=1, topic="systems-one/OTHERCO/JBH/DIM1/status", payload_json='{"serial_number":"N/A"}')
        conn = MagicMock()
        writer._dispatch_entry(conn, entry)

        writer._resolve_device.assert_not_called()


if __name__ == "__main__":
    unittest.main()
