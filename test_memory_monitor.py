"""Tests for the low-overhead Xorg diagnostic sampler."""

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "deploy" / "memory-monitor.py"
SPEC = importlib.util.spec_from_file_location("memory_monitor", SCRIPT)
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


class MemoryMonitorTests(unittest.TestCase):
    def test_largest_anonymous_mapping(self):
        smaps = """1000-2000 rw-p 00000000 00:00 0
Anonymous:             32 kB
2000-3000 rw-p 00000000 00:00 0
Anonymous:            512 kB
3000-4000 r--p 00000000 00:00 0
Anonymous:              0 kB
"""
        self.assertEqual(monitor.largest_anonymous_mapping_kb(smaps), 512)

    def test_detail_log_has_separate_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "detail.csv"
            row = dict.fromkeys(monitor.DETAIL_FIELDS, "")
            row["time"] = "2026-11-01T22:00:00+01:00"
            monitor.append_snapshot(path, row, monitor.DETAIL_FIELDS)
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("xorg_largest_anon_mapping_kb", lines[0])


if __name__ == "__main__":
    unittest.main()
