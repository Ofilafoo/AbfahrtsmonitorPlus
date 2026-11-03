import json
import unittest

from wifi_health import outage_count


def event(second, old, new):
    return json.dumps({
        "__REALTIME_TIMESTAMP": str(second * 1_000_000),
        "MESSAGE": f"device (wlan0): state change: {old} -> {new} (reason 'none')",
    })


class WifiHealthTests(unittest.TestCase):
    def test_counts_long_outage_only(self):
        lines = "\n".join([
            event(100, "activated", "deactivating"),
            event(110, "deactivating", "disconnected"),
            event(170, "secondaries", "activated"),
            event(200, "activated", "disconnected"),
            event(330, "secondaries", "activated"),
        ])
        self.assertEqual(outage_count(lines, now=400), 1)

    def test_counts_ongoing_outage_and_ignores_old_one(self):
        lines = "\n".join([
            event(100, "activated", "disconnected"),
            event(300, "secondaries", "activated"),
            event(900, "activated", "unavailable"),
        ])
        self.assertEqual(outage_count(lines, now=1100, window_seconds=500), 1)

    def test_ignores_other_device_and_invalid_json(self):
        lines = "bad json\n" + event(100, "activated", "disconnected").replace("wlan0", "eth0")
        self.assertEqual(outage_count(lines, now=300), 0)


if __name__ == "__main__":
    unittest.main()
