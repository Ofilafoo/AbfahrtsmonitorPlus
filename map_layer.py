"""Small, cached OpenStreetMap backdrop for the Rostock rain-radar widget."""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


MAP_CENTER_LAT = 53.75
MAP_CENTER_LON = 12.0


def configured_home_marker() -> tuple[float, float] | None:
    """Return an optional private marker without storing its location in Git."""
    latitude = os.environ.get("RSAG_HOME_LAT")
    longitude = os.environ.get("RSAG_HOME_LON")
    if latitude is None and longitude is None:
        return None
    if latitude is None or longitude is None:
        raise ValueError("RSAG_HOME_LAT und RSAG_HOME_LON müssen zusammen gesetzt sein")
    try:
        lat, lon = float(latitude), float(longitude)
    except ValueError as error:
        raise ValueError("Ungültige Koordinaten für den Wohnort-Punkt") from error
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("Ungültige Koordinaten für den Wohnort-Punkt")
    return lat, lon


HOME_MARKER = configured_home_marker()
MAP_ZOOM = 7
RADAR_ZOOM = 5
RADAR_PIXELS = 512
VIEW_PIXELS = 900
TILE_PIXELS = 256
RADAR_TILE_MAP_PIXELS = TILE_PIXELS * 2 ** (MAP_ZOOM - RADAR_ZOOM)
OSM_TILE_URL = os.environ.get("RSAG_OSM_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
USER_AGENT = "Abfahrtsmonitor-Plus/1.1 (personal Raspberry Pi weather display)"
CACHE_TTL = 7 * 24 * 60 * 60
PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"


def world_pixel(lon: float, lat: float, zoom: int = MAP_ZOOM) -> tuple[float, float]:
    """Project longitude/latitude to Web-Mercator tile pixels."""
    lat = max(-85.0511, min(85.0511, lat))
    size = TILE_PIXELS * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * size
    rad = math.radians(lat)
    y = (1 - math.asinh(math.tan(rad)) / math.pi) / 2 * size
    return x, y


def map_point(lon: float, lat: float, width: int, height: int) -> tuple[float, float]:
    cx, cy = world_pixel(MAP_CENTER_LON, MAP_CENTER_LAT)
    x, y = world_pixel(lon, lat)
    scale = width / VIEW_PIXELS
    return width / 2 + (x - cx) * scale, height / 2 + (y - cy) * scale


def map_coordinate(x: float, y: float, width: int, height: int) -> tuple[float, float]:
    """Return longitude/latitude for a point in the radar viewport."""
    cx, cy = world_pixel(MAP_CENTER_LON, MAP_CENTER_LAT)
    scale = width / VIEW_PIXELS
    world_x = cx + (x - width / 2) / scale
    world_y = cy + (y - height / 2) / scale
    size = TILE_PIXELS * (2 ** MAP_ZOOM)
    lon = world_x / size * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * world_y / size))))
    return lon, lat


def visible_tiles(width: int, height: int) -> list[tuple[int, int]]:
    """Request only map tiles intersecting the one on-screen viewport."""
    width, height = max(1, width), max(1, height)
    cx, cy = world_pixel(MAP_CENTER_LON, MAP_CENTER_LAT)
    scale = width / VIEW_PIXELS
    x0 = math.floor((cx - width / (2 * scale)) / TILE_PIXELS)
    x1 = math.floor((cx + width / (2 * scale) - 0.001) / TILE_PIXELS)
    y0 = math.floor((cy - height / (2 * scale)) / TILE_PIXELS)
    y1 = math.floor((cy + height / (2 * scale) - 0.001) / TILE_PIXELS)
    limit = 2 ** MAP_ZOOM
    return [(x, y) for y in range(max(0, y0), min(limit - 1, y1) + 1)
            for x in range(max(0, x0), min(limit - 1, x1) + 1)]


def visible_radar_tiles(width: int, height: int) -> list[tuple[int, int]]:
    """Return XYZ radar tiles intersecting the viewport, in Web-Mercator space."""
    width, height = max(1, width), max(1, height)
    cx, cy = world_pixel(MAP_CENTER_LON, MAP_CENTER_LAT)
    half_width = VIEW_PIXELS / 2
    half_height = VIEW_PIXELS * height / (2 * width)
    tile_size = RADAR_TILE_MAP_PIXELS
    x0 = math.floor((cx - half_width) / tile_size)
    x1 = math.floor((cx + half_width - 0.001) / tile_size)
    y0 = math.floor((cy - half_height) / tile_size)
    y1 = math.floor((cy + half_height - 0.001) / tile_size)
    limit = 2 ** RADAR_ZOOM
    return [(x, y) for y in range(max(0, y0), min(limit - 1, y1) + 1)
            for x in range(max(0, x0), min(limit - 1, x1) + 1)]


def radar_placement(tile_x: int, tile_y: int, width: int, height: int) -> tuple[float, float, float]:
    """Place a radar mosaic exactly on the OSM tile coordinate grid."""
    cx, cy = world_pixel(MAP_CENTER_LON, MAP_CENTER_LAT)
    map_scale = width / VIEW_PIXELS
    image_scale = map_scale * RADAR_TILE_MAP_PIXELS / RADAR_PIXELS
    left = width / 2 + (tile_x * RADAR_TILE_MAP_PIXELS - cx) * map_scale
    top = height / 2 + (tile_y * RADAR_TILE_MAP_PIXELS - cy) * map_scale
    return left, top, image_scale


def _tile_path(cache_dir: Path, x: int, y: int) -> Path:
    return cache_dir / f"osm-{MAP_ZOOM}-{x}-{y}.png"


def fetch_tile(cache_dir: Path, x: int, y: int, now: float | None = None) -> bytes:
    """Use cached tiles for at least seven days, then revalidate conditionally."""
    now = time.time() if now is None else now
    path = _tile_path(cache_dir, x, y)
    cached = path.read_bytes() if path.exists() else None
    if cached is not None and not (cached.startswith(b"\x89PNG\r\n\x1a\n") and cached.endswith(PNG_END)):
        cached = None
    if cached is not None and now - path.stat().st_mtime < CACHE_TTL:
        return cached
    headers = {"User-Agent": USER_AGENT}
    if cached is not None:
        from email.utils import formatdate
        headers["If-Modified-Since"] = formatdate(path.stat().st_mtime, usegmt=True)
    request = urllib.request.Request(OSM_TILE_URL.format(z=MAP_ZOOM, x=x, y=y), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024 or not data.startswith(b"\x89PNG\r\n\x1a\n") or not data.endswith(PNG_END):
            raise ValueError("Ungültige OSM-Kartenkachel")
        cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".pending")
        temporary.write_bytes(data)
        temporary.replace(path)
        return data
    except urllib.error.HTTPError as error:
        if error.code != 304 or cached is None:
            if cached is not None:
                return cached
            raise
        os.utime(path, (now, now))
        return cached
    except (OSError, ValueError):
        if cached is not None:
            return cached
        raise


def fetch_visible_tiles(width: int, height: int) -> dict[tuple[int, int], bytes]:
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    cache_dir = cache_root / "rsag-dashboard" / "osm"
    result = {}
    for x, y in visible_tiles(width, height):
        try:
            result[(x, y)] = fetch_tile(cache_dir, x, y)
        except (OSError, ValueError):
            continue
    return result


def load_boundaries() -> dict:
    return json.loads(Path(__file__).with_name("map_boundaries.json").read_text(encoding="utf-8"))
