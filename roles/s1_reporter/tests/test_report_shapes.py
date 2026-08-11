"""Unit tests for the pure data-shaping helpers in report.py.

report.py cannot be imported normally on a dev machine or in CI: it imports
pymssql, matplotlib and numpy at module scope. It is loaded here via
SourceFileLoader (same technique as roles/systems_one_ingest/tests/test_validation.py)
with minimal stand-ins registered in sys.modules for those three packages. Only the
stdlib-only helpers are exercised — nothing here touches the DB, matplotlib or disk
beyond a temp dir.
"""
import importlib.machinery
import importlib.util
import os
import sys
import types
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "..", "files", "report.py")


def _install_stubs():
    """Register stand-ins for report.py's heavy third-party imports. Returns their names."""
    installed = []

    def stub(name, module):
        if name not in sys.modules:
            sys.modules[name] = module
            installed.append(name)

    stub("pymssql", types.ModuleType("pymssql"))

    matplotlib = types.ModuleType("matplotlib")
    matplotlib.use = lambda *a, **k: None
    stub("matplotlib", matplotlib)

    pyplot = types.ModuleType("matplotlib.pyplot")
    stub("matplotlib.pyplot", pyplot)
    matplotlib.pyplot = pyplot

    stub("numpy", types.ModuleType("numpy"))
    return installed


def _load_report():
    installed = _install_stubs()
    try:
        loader = importlib.machinery.SourceFileLoader("s1_report", REPORT)
        spec = importlib.util.spec_from_loader("s1_report", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod
    finally:
        for name in installed:
            sys.modules.pop(name, None)


rpt = _load_report()


class TestAnomalyLines(unittest.TestCase):
    def test_tuples_become_icon_prefixed_plain_strings(self):
        lines = rpt._anomaly_lines([
            ("bad", "<b>DIM1 @ JBH</b> — good read dropped to <b>82.0%</b> today"),
            ("warn", "<b>DIM2 @ JBH</b> — no-dimension spike <b>7.1%</b>"),
        ])
        self.assertEqual(lines, [
            "🔴 DIM1 @ JBH — good read dropped to 82.0% today",
            "⚠️ DIM2 @ JBH — no-dimension spike 7.1%",
        ])

    def test_every_line_is_a_string_with_no_html(self):
        lines = rpt._anomaly_lines([("bad", "<b>x</b>"), ("warn", "<b>y</b>")])
        for line in lines:
            self.assertIsInstance(line, str)
            self.assertNotIn("<b>", line)
            self.assertNotIn("</b>", line)

    def test_unknown_severity_falls_back_to_bullet(self):
        self.assertEqual(rpt._anomaly_lines([("info", "hello")]), ["• hello"])

    def test_empty_list(self):
        self.assertEqual(rpt._anomaly_lines([]), [])


class TestDowntimeMinutes(unittest.TestCase):
    def test_computes_minutes_since_alerted_at(self):
        alerted = (datetime.now() - timedelta(minutes=45, seconds=30)).isoformat()
        self.assertEqual(rpt._downtime_minutes(alerted), 45)

    def test_missing_or_unparseable_is_zero(self):
        self.assertEqual(rpt._downtime_minutes(None), 0)
        self.assertEqual(rpt._downtime_minutes(""), 0)
        self.assertEqual(rpt._downtime_minutes("not-a-timestamp"), 0)

    def test_future_timestamp_clamps_to_zero(self):
        future = (datetime.now() + timedelta(minutes=10)).isoformat()
        self.assertEqual(rpt._downtime_minutes(future), 0)


class TestTrySaveChart(unittest.TestCase):
    def setUp(self):
        self._orig = rpt.save_chart

    def tearDown(self):
        rpt.save_chart = self._orig

    def test_returns_url_on_success(self):
        rpt.save_chart = lambda b, d, u: "https://charts.example.com/a.png"
        self.assertEqual(rpt._try_save_chart(b"x", "volume"), "https://charts.example.com/a.png")

    def test_oserror_is_swallowed_and_returns_none(self):
        def boom(b, d, u):
            raise OSError("No space left on device")
        rpt.save_chart = boom
        self.assertIsNone(rpt._try_save_chart(b"x", "volume"))

    def test_none_from_save_chart_is_passed_through(self):
        rpt.save_chart = lambda b, d, u: None
        self.assertIsNone(rpt._try_save_chart(b"x", "volume"))


if __name__ == "__main__":
    unittest.main()
