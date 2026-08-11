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
