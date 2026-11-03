"""A small multi-point wind field for the precipitation map."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from map_layer import map_coordinate


WIND_REFRESH_SECONDS = 15 * 60
WIND_COLUMNS = 10
WIND_ROWS = 7
WIND_SAMPLE_COLUMNS = 6
WIND_SAMPLE_ROWS = 5
WIND_BATCH_SIZE = 5
WIND_BATCH_COUNT = WIND_SAMPLE_COLUMNS * WIND_SAMPLE_ROWS // WIND_BATCH_SIZE
WIND_BATCH_SECONDS = WIND_REFRESH_SECONDS // WIND_BATCH_COUNT
WIND_API = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "Abfahrtsmonitor-Plus/1.2 (personal Raspberry Pi weather display)"


@dataclass(frozen=True)
class WindSample:
    lon: float
    lat: float
    speed: float
    direction: float  # Meteorological direction: where the wind comes from.


@dataclass(frozen=True)
class WindField:
    samples: tuple[WindSample, ...]
    model_time: str


def grid_coordinates(width: int, height: int, columns: int = WIND_COLUMNS,
                     rows: int = WIND_ROWS) -> tuple[tuple[float, float], ...]:
    """Evenly spread model requests across the visible map without covering labels."""
    points = []
    for row in range(rows):
        y = height * (0.16 + row * 0.68 / (rows - 1))
        for column in range(columns):
            x = width * (0.08 + column * 0.84 / (columns - 1))
            points.append(map_coordinate(x, y, width, height))
    return tuple(points)


def wind_to_vector(direction: float) -> tuple[float, float]:
    """Convert north-clockwise wind-from degrees into a screen wind-to vector."""
    radians = math.radians(direction)
    return -math.sin(radians), math.cos(radians)


def wind_color(speed: float) -> tuple[float, float, float]:
    """Blue through indigo to purple, with smooth transitions in km/h."""
    anchors = (
        (0, (0x14, 0x7A, 0xCA)),
        (15, (0x49, 0x69, 0xDC)),
        (30, (0x86, 0x47, 0xD2)),
        (50, (0xBC, 0x2C, 0xBA)),
    )
    for (low_speed, low_rgb), (high_speed, high_rgb) in zip(anchors, anchors[1:]):
        if speed <= high_speed:
            fraction = max(0.0, (speed - low_speed) / (high_speed - low_speed))
            return tuple((low + (high - low) * fraction) / 255 for low, high in zip(low_rgb, high_rgb))
    return tuple(value / 255 for value in anchors[-1][1])


def parse_wind_field(data: object, coordinates: tuple[tuple[float, float], ...]) -> WindField:
    if not isinstance(data, list) or len(data) != len(coordinates):
        raise ValueError("Unvollständige Winddaten")
    samples = []
    model_times = set()
    for item, (lon, lat) in zip(data, coordinates):
        current = item["current"]
        speed = float(current["wind_speed_10m"])
        direction = float(current["wind_direction_10m"])
        if not math.isfinite(speed) or not math.isfinite(direction) or not 0 <= speed <= 250:
            raise ValueError("Ungültige Windwerte")
        samples.append(WindSample(lon, lat, speed, direction % 360))
        model_times.add(str(current["time"]))
    if len(model_times) != 1:
        raise ValueError("Windzeiten passen nicht zusammen")
    return WindField(tuple(samples), model_times.pop())


def interpolate_wind_field(field: WindField, width: int, height: int) -> WindField:
    """Draw a dense field from fewer API sites, blending vector components."""
    if len(field.samples) != WIND_SAMPLE_COLUMNS * WIND_SAMPLE_ROWS:
        raise ValueError("Unvollständiges Windraster")
    display_coordinates = grid_coordinates(width, height)
    samples = []
    for index, (lon, lat) in enumerate(display_coordinates):
        row, column = divmod(index, WIND_COLUMNS)
        source_y = row * (WIND_SAMPLE_ROWS - 1) / (WIND_ROWS - 1)
        source_x = column * (WIND_SAMPLE_COLUMNS - 1) / (WIND_COLUMNS - 1)
        y0, x0 = math.floor(source_y), math.floor(source_x)
        y1, x1 = min(y0 + 1, WIND_SAMPLE_ROWS - 1), min(x0 + 1, WIND_SAMPLE_COLUMNS - 1)
        fy, fx = source_y - y0, source_x - x0
        east = south = 0.0
        for sample_row, sample_column, weight in (
            (y0, x0, (1 - fy) * (1 - fx)),
            (y0, x1, (1 - fy) * fx),
            (y1, x0, fy * (1 - fx)),
            (y1, x1, fy * fx),
        ):
            sample = field.samples[sample_row * WIND_SAMPLE_COLUMNS + sample_column]
            dx, dy = wind_to_vector(sample.direction)
            east += weight * sample.speed * dx
            south += weight * sample.speed * dy
        speed = math.hypot(east, south)
        direction = math.degrees(math.atan2(-east, south)) % 360 if speed > 0 else 0.0
        samples.append(WindSample(lon, lat, speed, direction))
    return WindField(tuple(samples), field.model_time)


def fetch_wind_field(width: int, height: int) -> WindField:
    coordinates = grid_coordinates(width, height, WIND_SAMPLE_COLUMNS, WIND_SAMPLE_ROWS)
    return interpolate_wind_field(_fetch_wind_coordinates(coordinates), width, height)


def fetch_wind_batch(width: int, height: int, batch_index: int) -> WindField:
    """Fetch five neighboring model sites for a rolling wind update."""
    if not 0 <= batch_index < WIND_BATCH_COUNT:
        raise ValueError("Ungültige Windgruppe")
    coordinates = grid_coordinates(width, height, WIND_SAMPLE_COLUMNS, WIND_SAMPLE_ROWS)
    start = batch_index * WIND_BATCH_SIZE
    return _fetch_wind_coordinates(coordinates[start:start + WIND_BATCH_SIZE])


def merge_wind_batch(samples: list[WindSample | None], batch_index: int,
                     batch: WindField, width: int, height: int, model_time: str) -> WindField | None:
    """Keep other groups' last values and redraw only after the first full field."""
    if len(samples) != WIND_SAMPLE_COLUMNS * WIND_SAMPLE_ROWS or len(batch.samples) != WIND_BATCH_SIZE:
        raise ValueError("Unvollständige Windgruppe")
    if not 0 <= batch_index < WIND_BATCH_COUNT:
        raise ValueError("Ungültige Windgruppe")
    start = batch_index * WIND_BATCH_SIZE
    samples[start:start + WIND_BATCH_SIZE] = batch.samples
    if any(sample is None for sample in samples):
        return None
    return interpolate_wind_field(WindField(tuple(samples), model_time), width, height)


def _fetch_wind_coordinates(coordinates: tuple[tuple[float, float], ...]) -> WindField:
    params = urllib.parse.urlencode({
        "latitude": ",".join(f"{lat:.4f}" for _, lat in coordinates),
        "longitude": ",".join(f"{lon:.4f}" for lon, _ in coordinates),
        "current": "wind_speed_10m,wind_direction_10m",
        "timezone": "Europe/Berlin",
        "forecast_days": "1",
    })
    request = urllib.request.Request(f"{WIND_API}?{params}", headers={"User-Agent": USER_AGENT})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(256 * 1024 + 1)
            break
        except urllib.error.HTTPError as error:
            if attempt or error.code not in (502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt:
                raise
        time.sleep(2)
    if len(raw) > 256 * 1024:
        raise ValueError("Windantwort ist zu groß")
    return parse_wind_field(json.loads(raw), coordinates)
