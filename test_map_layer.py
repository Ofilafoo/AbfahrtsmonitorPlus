import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from map_layer import (CACHE_TTL, PNG_END, RADAR_PIXELS, RADAR_TILE_MAP_PIXELS, configured_home_marker, fetch_tile,
                       load_boundaries, map_point, radar_placement, visible_radar_tiles,
                       visible_tiles, world_pixel)


class MapLayerTests(unittest.TestCase):
    def test_optional_home_marker_is_private_and_validated(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(configured_home_marker())
        with patch.dict(os.environ, {"RSAG_HOME_LAT": "50.0", "RSAG_HOME_LON": "10.0"}, clear=True):
            self.assertEqual(configured_home_marker(), (50.0, 10.0))
        with patch.dict(os.environ, {"RSAG_HOME_LAT": "50.0"}, clear=True):
            with self.assertRaises(ValueError):
                configured_home_marker()
        with patch.dict(os.environ, {"RSAG_HOME_LAT": "inf", "RSAG_HOME_LON": "10.0"}, clear=True):
            with self.assertRaises(ValueError):
                configured_home_marker()

    def test_city_and_state_boundaries_are_real_polygons(self):
        data = load_boundaries()
        self.assertEqual(data["rostock"]["type"], "Polygon")
        self.assertEqual(data["mecklenburg_vorpommern"]["type"], "Polygon")
        self.assertGreater(len(data["rostock"]["coordinates"][0]), 50)

    def test_hamburg_berlin_and_rostock_are_visible(self):
        self.assertEqual(map_point(12.0, 53.75, 900, 520), (450, 260))
        for lon, lat in ((9.9937, 53.5511), (13.4050, 52.5200), (12.0991, 54.0924)):
            x, y = map_point(lon, lat, 900, 520)
            self.assertGreater(x, 20)
            self.assertLess(x, 880)
            self.assertGreater(y, 20)
            self.assertLess(y, 500)
        self.assertLess(map_point(12.0991, 54.0924, 900, 520)[1], 260)

    def test_tile_set_is_small_and_covers_viewport(self):
        tiles = visible_tiles(900, 520)
        self.assertGreaterEqual(len(tiles), 2)
        self.assertLessEqual(len(tiles), 20)
        cx, cy = world_pixel(12.0, 53.75)
        self.assertIn((int(cx // 256), int(cy // 256)), tiles)

    def test_radar_xyz_tiles_cover_viewport_and_match_map_coordinates(self):
        width, height = 900, 520
        tiles = visible_radar_tiles(width, height)
        self.assertEqual(tiles, [(16, 10), (17, 10)])
        left, top, scale = radar_placement(16, 10, width, height)
        self.assertLessEqual(left, 0)
        self.assertLessEqual(top, 0)
        self.assertGreaterEqual(left + 2 * RADAR_PIXELS * scale, width)
        self.assertGreaterEqual(top + RADAR_PIXELS * scale, height)
        for lon, lat in ((9.9937, 53.5511), (13.4050, 52.5200), (12.0991, 54.0924)):
            map_x, map_y = map_point(lon, lat, width, height)
            world_x, world_y = world_pixel(lon, lat)
            radar_x = left + (world_x - 16 * RADAR_TILE_MAP_PIXELS) / RADAR_TILE_MAP_PIXELS * RADAR_PIXELS * scale
            radar_y = top + (world_y - 10 * RADAR_TILE_MAP_PIXELS) / RADAR_TILE_MAP_PIXELS * RADAR_PIXELS * scale
            self.assertAlmostEqual(radar_x, map_x)
            self.assertAlmostEqual(radar_y, map_y)

    def test_fresh_cache_avoids_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "osm-7-1-2.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + PNG_END)
            with patch("map_layer.urllib.request.urlopen") as opener:
                self.assertEqual(fetch_tile(Path(directory), 1, 2, now=path.stat().st_mtime + CACHE_TTL - 1), path.read_bytes())
            opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
