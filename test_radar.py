import io
import json
import unittest
from unittest.mock import patch

from radar import RadarFrame, fetch_latest_radar, has_visible_rain, hide_weak_echoes, radar_status, radar_tile_urls


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.metadata = {
            "host": "https://tilecache.rainviewer.com",
            "radar": {"past": [
                {"time": 1000, "path": "/v2/radar/1000"},
                {"time": 1600, "path": "/v2/radar/1600"},
            ]},
        }

    def test_uses_latest_frame_and_exact_xyz_tiles(self):
        timestamp, urls = radar_tile_urls(self.metadata, 900, 520)
        self.assertEqual(timestamp, 1600)
        self.assertEqual(urls, {
            (16, 10): "https://tilecache.rainviewer.com/v2/radar/1600/512/5/16/10/2/1_1.png",
            (17, 10): "https://tilecache.rainviewer.com/v2/radar/1600/512/5/17/10/2/1_1.png",
        })

    def test_rejects_untrusted_host_and_path(self):
        self.metadata["host"] = "https://example.com"
        with self.assertRaises(ValueError):
            radar_tile_urls(self.metadata, 900, 520)
        self.metadata["host"] = "https://tilecache.rainviewer.com"
        self.metadata["radar"]["past"][-1]["path"] = "/v2/radar/1600/../../other"
        with self.assertRaises(ValueError):
            radar_tile_urls(self.metadata, 900, 520)

    def test_skips_download_when_frame_is_unchanged(self):
        metadata = json.dumps(self.metadata).encode()
        with patch("radar.urllib.request.urlopen", return_value=io.BytesIO(metadata)) as opener:
            self.assertEqual(fetch_latest_radar(1600, 900, 520), RadarFrame(1600, None))
        self.assertEqual(opener.call_count, 1)

    def test_fetches_png_and_rejects_wrong_content(self):
        metadata = json.dumps(self.metadata).encode()
        png = b"\x89PNG\r\n\x1a\n" + b"sample"
        with patch("radar.urllib.request.urlopen", side_effect=[io.BytesIO(metadata), io.BytesIO(png), io.BytesIO(png)]):
            self.assertEqual(fetch_latest_radar(None, 900, 520), RadarFrame(1600, {(16, 10): png, (17, 10): png}))
        with patch("radar.urllib.request.urlopen", side_effect=[io.BytesIO(metadata), io.BytesIO(b"not png")]):
            with self.assertRaises(ValueError):
                fetch_latest_radar(None, 900, 520)

    def test_stale_and_offline_labels(self):
        self.assertEqual(radar_status(None, True), ("RADAR NICHT VERFÜGBAR", True))
        self.assertFalse(radar_status(1600, now=1600 + 20 * 60)[1])
        self.assertTrue(radar_status(1600, now=1600 + 21 * 60)[1])
        self.assertTrue(radar_status(1600, failed=True, now=1600)[1])

    def test_grey_weak_echoes_are_hidden_but_blue_and_yellow_rain_remain(self):
        pixels = bytes((130, 123, 105, 73, 0, 163, 224, 255, 255, 238, 0, 255))
        self.assertEqual(
            hide_weak_echoes(pixels, width=3, height=1, rowstride=12),
            bytes((130, 123, 105, 0, 0, 163, 224, 255, 255, 238, 0, 255)),
        )

    def test_rain_indicator_checks_only_the_visible_rectangle(self):
        rows = [bytes((0, 0, 255, 255, 0, 0, 0, 0))] + [bytes(8)] * 3
        self.assertFalse(has_visible_rain(b"".join(rows), 2, 4, 8, 0, 1, 2, 4))
        rows[2] = bytes((0, 0, 255, 255, 0, 0, 0, 0))
        self.assertFalse(has_visible_rain(b"".join(rows), 2, 4, 8, 1, 1, 2, 4))
        self.assertTrue(has_visible_rain(b"".join(rows), 2, 4, 8, 0, 1, 2, 4))


if __name__ == "__main__":
    unittest.main()
