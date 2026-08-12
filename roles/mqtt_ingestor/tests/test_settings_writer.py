"""Unit tests for mqtt_ingestor settings_writer.py path sanitization.

Loaded directly via SourceFileLoader (matching the pattern used in
roles/systems_one_ingest/tests/test_validation.py) so the test does not
require the full `ingestor` package to be importable. settings_writer.py
does `from .models import SpoolEntry` at import time; models.py has not
been vendored into this role yet (a later consolidation task adds it), so
a minimal stand-in is registered in sys.modules before loading.
"""

import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_WRITER = os.path.join(
    HERE, "..", "files", "app", "ingestor", "settings_writer.py"
)


def _install_models_stub() -> types.ModuleType:
    stub = types.ModuleType("ingestor.models")

    class SpoolEntry:
        def __init__(self, id, topic, payload_json):
            self.id = id
            self.topic = topic
            self.payload_json = payload_json

    stub.SpoolEntry = SpoolEntry
    sys.modules["ingestor.models"] = stub
    return stub


def load():
    _install_models_stub()
    loader = importlib.machinery.SourceFileLoader(
        "ingestor.settings_writer", SETTINGS_WRITER
    )
    spec = importlib.util.spec_from_loader("ingestor.settings_writer", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


sw = load()


class IsSafePathComponent(unittest.TestCase):
    def test_normal_value_is_safe(self):
        self.assertTrue(sw._is_safe_path_component("PEPKOR"))

    def test_dotdot_is_unsafe(self):
        self.assertFalse(sw._is_safe_path_component(".."))

    def test_embedded_dotdot_is_unsafe(self):
        self.assertFalse(sw._is_safe_path_component("foo/../bar"))

    def test_absolute_path_is_unsafe(self):
        self.assertFalse(sw._is_safe_path_component("/etc/passwd"))

    def test_backslash_is_unsafe(self):
        self.assertFalse(sw._is_safe_path_component("foo\\bar"))

    def test_empty_string_is_unsafe(self):
        self.assertFalse(sw._is_safe_path_component(""))

    def test_single_dot_is_unsafe(self):
        self.assertFalse(sw._is_safe_path_component("."))


class WriteEntryPathTraversal(unittest.TestCase):
    """End-to-end: unsafe topic/payload fields must not escape base_dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name) / "settings"
        self.writer = sw.SettingsWriter(self.base_dir, prefix_depth=1)

    def tearDown(self):
        self._tmp.cleanup()

    def _entry(self, id_, topic, payload):
        return sw.SpoolEntry(id=id_, topic=topic, payload_json=json.dumps(payload))

    def test_traversal_in_customer_segment_is_rejected(self):
        entry = self._entry(
            1, "App/../../evil/JHB/DIM1/settings", {"Id": "x.y", "Value": {}}
        )
        # Must not raise, and must not create anything at all.
        self.writer._write_entry(entry)
        self.assertFalse(self.base_dir.exists())
        self.assertFalse((self.base_dir.parent / "evil").exists())

    def test_traversal_via_backslash_in_location_is_rejected(self):
        entry = self._entry(
            2, "App/PEPKOR/foo\\bar/DIM1/settings", {"Id": "x.y", "Value": {}}
        )
        self.writer._write_entry(entry)
        self.assertFalse(self.base_dir.exists())

    def test_traversal_in_payload_id_is_rejected(self):
        entry = self._entry(
            3, "App/PEPKOR/JHB/DIM1/settings", {"Id": "../../escape", "Value": {}}
        )
        self.writer._write_entry(entry)
        # customer/location/machine_name were all safe, but out_dir must
        # still never be created because id_leaf failed validation.
        self.assertFalse((self.base_dir / "PEPKOR" / "JHB" / "DIM1").exists())

    def test_safe_entry_still_writes(self):
        entry = self._entry(
            4,
            "App/PEPKOR/JHB/DIM1/settings",
            {"Id": "app.settings.watchdog", "Value": {"a": 1}},
        )
        self.writer._write_entry(entry)
        out = self.base_dir / "PEPKOR" / "JHB" / "DIM1" / "watchdog.json"
        self.assertTrue(out.exists())
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"a": 1})


if __name__ == "__main__":
    unittest.main()
