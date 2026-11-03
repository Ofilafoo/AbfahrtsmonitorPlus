import unittest

from system_health import HealthReading, HealthSampler, cpu_times, cpu_usage, memory_usage


class SystemHealthTests(unittest.TestCase):
    def test_cpu_percent_from_two_samples(self):
        first = cpu_times("cpu  10 0 10 80 0 0 0 0 0 0")
        second = cpu_times("cpu  35 0 35 130 0 0 0 0 0 0")
        self.assertEqual(cpu_usage(first, second), 50)

    def test_memory_uses_available_not_free(self):
        self.assertEqual(memory_usage("MemTotal: 2000 kB\nMemFree: 100 kB\nMemAvailable: 1500 kB"), 25)

    def test_health_summary_and_alert_levels(self):
        self.assertEqual(HealthReading(8, 27, 49, 2).text, "CPU 8%  ·  RAM 27%  ·  49°C  ·  WLAN-Ausfälle 24h: 2")
        self.assertEqual(HealthReading(8, 27, 49).level, "good")
        self.assertEqual(HealthReading(8, 27, 67).level, "warn")
        self.assertEqual(HealthReading(8, 27, 76).level, "hot")

    def test_real_sample_is_non_destructive(self):
        reading = HealthSampler().sample()
        self.assertIsInstance(reading, HealthReading)


if __name__ == "__main__":
    unittest.main()
