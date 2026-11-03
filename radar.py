"""Fetch only the precipitation tiles visible in the map viewport."""

from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from map_layer import RADAR_ZOOM, visible_radar_tiles


RADAR_API = "https://api.rainviewer.com/public/weather-maps.json"
RADAR_REFRESH_SECONDS = 5 * 60
USER_AGENT = "Abfahrtsmonitor-Plus/1.0 (personal Raspberry Pi display)"


@dataclass(frozen=True)
class RadarFrame:
    timestamp: int
    tiles: dict[tuple[int, int], bytes] | None


def _read_limited(response: object, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Radarantwort ist zu groß")
    return data


def radar_tile_urls(metadata: dict, width: int, height: int) -> tuple[int, dict[tuple[int, int], str]]:
    frames = metadata["radar"]["past"]
    latest = max(frames, key=lambda frame: int(frame["time"]))
    timestamp = int(latest["time"])
    host = str(metadata["host"]).rstrip("/")
    parsed = urlsplit(host)
    if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".rainviewer.com")
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("Ungültiger Radar-Host")
    path = str(latest["path"])
    if not re.fullmatch(r"/v2/radar/[A-Za-z0-9_-]+", path):
        raise ValueError("Ungültiger Radar-Pfad")
    tiles = visible_radar_tiles(width, height)
    return timestamp, {
        (x, y): f"{host}{path}/512/{RADAR_ZOOM}/{x}/{y}/2/1_1.png"
        for x, y in tiles
    }


def fetch_latest_radar(known_timestamp: int | None, width: int, height: int) -> RadarFrame:
    request = urllib.request.Request(RADAR_API, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=10) as response:
        metadata = json.loads(_read_limited(response, 256 * 1024))
    timestamp, tile_urls = radar_tile_urls(metadata, width, height)
    if timestamp == known_timestamp:
        return RadarFrame(timestamp, None)
    tiles = {}
    for coordinates, tile_url in tile_urls.items():
        request = urllib.request.Request(tile_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=10) as response:
            png = _read_limited(response, 2 * 1024 * 1024)
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"Radarkachel {coordinates} ist kein PNG")
        tiles[coordinates] = png
    return RadarFrame(timestamp, tiles)


def hide_weak_echoes(pixels: bytes, width: int, height: int, rowstride: int) -> bytes:
    """Hide RainViewer's grey-brown weak echoes so actual rain stands out."""
    result = bytearray(pixels)
    for y in range(height):
        for x in range(width):
            offset = y * rowstride + x * 4
            red, green, blue, alpha = result[offset:offset + 4]
            if alpha and red >= green >= blue and red - green < 40 and green - blue < 70:
                result[offset + 3] = 0
    return bytes(result)


def has_visible_rain(pixels: bytes, width: int, height: int, rowstride: int,
                     left: int, top: int, right: int, bottom: int) -> bool:
    """Check only the source-pixel rectangle actually visible on screen."""
    for y in range(max(0, top), min(height, bottom)):
        for x in range(max(0, left), min(width, right)):
            if pixels[y * rowstride + x * 4 + 3] >= 128:
                return True
    return False


def radar_status(timestamp: int | None, failed: bool = False, now: float | None = None) -> tuple[str, bool]:
    if timestamp is None:
        return ("RADAR NICHT VERFÜGBAR" if failed else "RADAR LÄDT …", failed)
    now = time.time() if now is None else now
    age_minutes = max(0, int((now - timestamp) / 60))
    clock = time.strftime("%H:%M", time.localtime(timestamp))
    if failed or age_minutes > 20:
        return f"NICHT AKTUELL · Stand {clock}", True
    return f"Stand {clock}", False
