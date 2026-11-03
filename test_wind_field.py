import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from map_layer import map_point
from wind_field import (WIND_BATCH_COUNT, WIND_BATCH_SECONDS, WIND_BATCH_SIZE, WIND_COLUMNS,
                        WIND_ROWS, WIND_SAMPLE_COLUMNS, WIND_SAMPLE_ROWS, WindField, WindSample,
                        fetch_wind_batch, fetch_wind_field, grid_coordinates,
                        interpolate_wind_field, merge_wind_batch, parse_wind_field,
                        wind_color, wind_to_vector)


class WindFieldTests(unittest.TestCase):
    def test_grid_spans_map_and_round_trips(self):
        points = grid_coordinates(900, 520)
        self.assertEqual(len(points), WIND_COLUMNS * WIND_ROWS)
        xs, ys = zip(*(map_point(lon, lat, 900, 520) for lon, lat in points))
        self.assertAlmostEqual(min(xs), 72, delta=1)
        self.assertAlmostEqual(max(xs), 828, delta=1)
        self.assertAlmostEqual(min(ys), 83.2, delta=1)
        self.assertAlmostEqual(max(ys), 436.8, delta=1)

    def test_wind_to_vector_uses_direction_of_travel(self):
        self.assertAlmostEqual(wind_to_vector(0)[1], 1)
        self.assertAlmostEqual(wind_to_vector(90)[0], -1)
        self.assertAlmostEqual(wind_to_vector(180)[1], -1)

    def test_colour_moves_from_blue_to_purple(self):
        calm = wind_color(0)
        breeze = wind_color(15)
        strong = wind_color(50)
        self.assertLess(calm[0], breeze[0])
        self.assertLess(breeze[0], strong[0])
        self.assertGreater(calm[2], calm[0])
        self.assertGreater(strong[0], strong[1])
        self.assertEqual(wind_color(80), strong)

    def test_parse_multi_location_response(self):
        coords = ((12.0, 54.0), (13.0, 54.0))
        data = [{"current": {"wind_speed_10m": 16, "wind_direction_10m": 270, "time": "2026-10-12T21:45"}},
                {"current": {"wind_speed_10m": 10, "wind_direction_10m": 0, "time": "2026-10-12T21:45"}}]
        field = parse_wind_field(data, coords)
        self.assertEqual(len(field.samples), 2)
        self.assertEqual(field.model_time, "2026-10-12T21:45")
        self.assertAlmostEqual(field.samples[0].direction, 270)
        with self.assertRaises(ValueError):
            parse_wind_field(data[:1], coords)

    def test_interpolation_keeps_dense_arrows_and_wraps_north(self):
        coordinates = grid_coordinates(900, 520, WIND_SAMPLE_COLUMNS, WIND_SAMPLE_ROWS)
        samples = tuple(WindSample(lon, lat, 20, 350 if index % WIND_SAMPLE_COLUMNS == 0 else 10)
                        for index, (lon, lat) in enumerate(coordinates))
        field = interpolate_wind_field(WindField(samples, "2026-11-02T20:00"), 900, 520)
        self.assertEqual(len(field.samples), WIND_COLUMNS * WIND_ROWS)
        self.assertAlmostEqual(field.samples[0].direction, 350)
        self.assertLess(field.samples[1].direction, 20)
        self.assertAlmostEqual(field.samples[-1].direction, 10)

    def test_fetch_retries_503_once_with_only_30_api_locations(self):
        data = [{"current": {"wind_speed_10m": 12, "wind_direction_10m": 90,
                             "time": "2026-11-02T20:00"}}
                for _ in range(WIND_SAMPLE_COLUMNS * WIND_SAMPLE_ROWS)]
        failure = urllib.error.HTTPError("https://api.open-meteo.com", 503, "busy", None, None)
        with patch("wind_field.urllib.request.urlopen",
                   side_effect=[failure, io.BytesIO(json.dumps(data).encode())]) as opener, \
                patch("wind_field.time.sleep") as sleeper:
            field = fetch_wind_field(900, 520)
        self.assertEqual(len(field.samples), WIND_COLUMNS * WIND_ROWS)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(sleeper.call_count, 1)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(opener.call_args.args[0].full_url).query)
        self.assertEqual(len(query["latitude"][0].split(",")), WIND_SAMPLE_COLUMNS * WIND_SAMPLE_ROWS)

    def test_rolling_batches_cover_all_sites_and_redraw_after_complete_field(self):
        self.assertEqual(WIND_BATCH_COUNT, 6)
        self.assertEqual(WIND_BATCH_SECONDS, 150)
        coordinates = grid_coordinates(900, 520, WIND_SAMPLE_COLUMNS, WIND_SAMPLE_ROWS)
        samples = [None] * len(coordinates)
        for batch_index in range(WIND_BATCH_COUNT):
            start = batch_index * WIND_BATCH_SIZE
            batch = WindField(tuple(WindSample(lon, lat, 5 + batch_index, 90)
                                    for lon, lat in coordinates[start:start + WIND_BATCH_SIZE]),
                              "2026-11-02T20:00")
            field = merge_wind_batch(samples, batch_index, batch, 900, 520, batch.model_time)
            if batch_index < WIND_BATCH_COUNT - 1:
                self.assertIsNone(field)
        self.assertEqual(len(field.samples), WIND_COLUMNS * WIND_ROWS)
        self.assertEqual(samples[-1].speed, 10)

    def test_batch_fetch_requests_only_five_locations(self):
        data = [{"current": {"wind_speed_10m": 12, "wind_direction_10m": 90,
                             "time": "2026-11-02T20:00"}} for _ in range(WIND_BATCH_SIZE)]
        with patch("wind_field.urllib.request.urlopen",
                   return_value=io.BytesIO(json.dumps(data).encode())) as opener:
            batch = fetch_wind_batch(900, 520, 2)
        self.assertEqual(len(batch.samples), WIND_BATCH_SIZE)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(opener.call_args.args[0].full_url).query)
        self.assertEqual(len(query["latitude"][0].split(",")), WIND_BATCH_SIZE)


if __name__ == "__main__":
    unittest.main()
