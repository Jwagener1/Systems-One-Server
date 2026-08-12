"""Unit tests for mqtt_ingestor spool.py resource-cap enforcement.

Loaded directly via SourceFileLoader (matching the pattern used in
roles/systems_one_ingest/tests/test_validation.py) so the test does not
require the full `ingestor` package to be importable. spool.py does
`from .models import CanonicalEvent, SpoolEntry` at import time; models.py
has not been vendored into this role yet (a later consolidation task adds
it), so a minimal stand-in is registered in sys.modules before loading.
"""

import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SPOOL = os.path.join(HERE, "..", "files", "app", "ingestor", "spool.py")


def _install_models_stub() -> types.ModuleType:
    stub = types.ModuleType("ingestor.models")

    class CanonicalEvent:
        def __init__(
            self,
            topic="t",
            qos=0,
            retain=False,
            payload_hash_sha256="hash",
            payload_text=None,
            payload_json=None,
            source_id=None,
            source_timestamp_utc=None,
            payload_bytes_len=0,
        ):
            self.topic = topic
            self.qos = qos
            self.retain = retain
            self.payload_hash_sha256 = payload_hash_sha256
            self.payload_text = payload_text
            self.payload_json = payload_json
            self.source_id = source_id
            self.source_timestamp_utc = source_timestamp_utc
            self.payload_bytes_len = payload_bytes_len

    class SpoolEntry:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    stub.CanonicalEvent = CanonicalEvent
    stub.SpoolEntry = SpoolEntry
    sys.modules["ingestor.models"] = stub
    return stub


def load():
    _install_models_stub()
    loader = importlib.machinery.SourceFileLoader("ingestor.spool", SPOOL)
    spec = importlib.util.spec_from_loader("ingestor.spool", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


spool_mod = load()


class SpoolCapEnforcement(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "spool.db"
        # Shrink the per-pass scan limit so a handful of rows is enough to
        # force multiple drop passes -- without this, reproducing the bug
        # (needing > _DROP_SCAN_LIMIT=500 rows) would require an unwieldy
        # number of inserts.
        self._orig_scan_limit = spool_mod._DROP_SCAN_LIMIT
        spool_mod._DROP_SCAN_LIMIT = 3

    def tearDown(self):
        spool_mod._DROP_SCAN_LIMIT = self._orig_scan_limit
        self._tmp.cleanup()

    def _event(self, payload_bytes_len):
        return spool_mod.CanonicalEvent(payload_bytes_len=payload_bytes_len)

    def test_cap_never_exceeded_when_drop_needs_multiple_passes(self):
        # _ROW_OVERHEAD_BYTES is 256; each small event below costs 256 bytes.
        sp = spool_mod.Spool(
            db_path=self.db_path,
            max_bytes=2600,
            full_mode="drop_oldest",
            drop_log_interval_sec=0,
        )
        try:
            # Fill the spool with 10 small rows (2560 bytes), leaving only
            # 40 bytes of headroom.
            for _ in range(10):
                sp.enqueue(self._event(0))
            self.assertEqual(sp.current_bytes(), 2560)

            # A single _DROP_SCAN_LIMIT=3 pass can free at most 3*256=768
            # bytes -- not enough for this 1500-byte event on its own. The
            # old code called _drop_oldest_to_fit exactly once and then
            # inserted unconditionally, which would have pushed total_bytes
            # to 1792 + 1500 = 3292, over the 2600 cap.
            sp.enqueue(self._event(1500 - 256))  # event_bytes == 1500

            self.assertLessEqual(sp.current_bytes(), 2600)
        finally:
            sp.close()

    def test_bounded_retry_falls_back_to_reject_when_dropping_cant_help(self):
        # A single event bigger than the entire cap can never fit, even
        # after the spool is fully drained. The bounded retry loop must
        # give up and reject rather than looping forever or exceeding the
        # cap.
        sp = spool_mod.Spool(
            db_path=self.db_path,
            max_bytes=1000,
            full_mode="drop_oldest",
            drop_log_interval_sec=0,
        )
        try:
            sp.enqueue(self._event(2000 - 256))  # event_bytes == 2000 > max_bytes

            self.assertEqual(sp.current_bytes(), 0)
            self.assertEqual(sp.current_count(), 0)
        finally:
            sp.close()

    def test_reject_new_mode_still_rejects_on_first_full_check(self):
        sp = spool_mod.Spool(
            db_path=self.db_path,
            max_bytes=256,
            full_mode="reject_new",
            drop_log_interval_sec=0,
        )
        try:
            sp.enqueue(self._event(0))  # fills the spool exactly (256 bytes)
            self.assertEqual(sp.current_bytes(), 256)

            sp.enqueue(self._event(0))  # would exceed cap -- must be rejected
            self.assertEqual(sp.current_bytes(), 256)
            self.assertEqual(sp.current_count(), 1)
        finally:
            sp.close()


if __name__ == "__main__":
    unittest.main()
