"""Unit tests for derive_thresholds in compute_baselines.py.

compute_baselines.py imports pymssql at module scope, which is not installed in
CI, so it is loaded via SourceFileLoader with a stand-in registered in
sys.modules (same technique as test_report_shapes.py). Nothing here touches a DB.
"""
import importlib.machinery
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.join(HERE, "..", "files", "compute_baselines.py")


def _load_baselines():
    installed = False
    if "pymssql" not in sys.modules:
        sys.modules["pymssql"] = types.ModuleType("pymssql")
        installed = True
    try:
        loader = importlib.machinery.SourceFileLoader("s1_baselines", BASELINES)
        spec = importlib.util.spec_from_loader("s1_baselines", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod
    finally:
        if installed:
            sys.modules.pop("pymssql", None)


baselines = _load_baselines()


class TestGoodReadWarn(unittest.TestCase):
    def test_warn_is_15_percent_below_mean(self):
        warn, _ = baselines.derive_thresholds("good_read_pct", 80.0, 2.0, 74.0, 76.0, 0, 0)
        self.assertAlmostEqual(warn, 68.0, places=4)

    def test_warn_is_relative_not_percentage_points(self):
        # 99.0 * 0.85 = 84.15, not 99.0 - 15 = 84.0
        warn, _ = baselines.derive_thresholds("good_read_pct", 99.0, 0.5, 98.0, 98.5, 0, 0)
        self.assertAlmostEqual(warn, 84.15, places=4)

    def test_no_50_floor_for_weak_devices(self):
        warn, _ = baselines.derive_thresholds("good_read_pct", 40.0, 5.0, 30.0, 33.0, 0, 0)
        self.assertAlmostEqual(warn, 34.0, places=4)

    def test_bad_kept_strictly_below_warn(self):
        # p05 of 98.0 would otherwise sit far above the new warn line of 84.15
        warn, bad = baselines.derive_thresholds("good_read_pct", 99.0, 0.5, 98.0, 98.5, 0, 0)
        self.assertLess(bad, warn)
        self.assertAlmostEqual(bad, warn - 1.0, places=4)

    def test_bad_keeps_statistical_derivation_when_below_warn(self):
        # max(p05, mean - 3*stddev) = max(50.0, 55.0) = 55.0, which is under warn 68.0
        warn, bad = baselines.derive_thresholds("good_read_pct", 80.0, 8.333333, 50.0, 60.0, 0, 0)
        self.assertAlmostEqual(warn, 68.0, places=4)
        self.assertAlmostEqual(bad, 55.0, places=3)

    def test_bad_never_negative(self):
        _, bad = baselines.derive_thresholds("good_read_pct", 0.0, 0.0, 0.0, 0.0, 0, 0)
        self.assertGreaterEqual(bad, 0.0)


class TestNoDimUnchanged(unittest.TestCase):
    def test_uses_percentiles_not_the_mean(self):
        warn, bad = baselines.derive_thresholds("no_dim_pct", 0.5, 0.2, 0, 0, 2.0, 3.0)
        self.assertAlmostEqual(warn, 3.0, places=4)
        self.assertAlmostEqual(bad, 6.0, places=4)

    def test_defaults_when_no_spread(self):
        warn, bad = baselines.derive_thresholds("no_dim_pct", 0.0, 0.0, 0, 0, 0.0, 0.0)
        self.assertAlmostEqual(warn, 3.0, places=4)
        self.assertAlmostEqual(bad, 5.0, places=4)


class TestUnknownMetric(unittest.TestCase):
    def test_returns_none_pair(self):
        self.assertEqual(baselines.derive_thresholds("bogus", 1, 1, 1, 1, 1, 1), (None, None))


if __name__ == "__main__":
    unittest.main()
