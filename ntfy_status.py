#!/usr/bin/python3
"""Send one compact morning health summary via an ntfy topic."""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from system_health import HealthSampler
from wifi_health import read_outage_count


PROC = Path("/proc")
MEMORY_LOG = Path("/var/log/rsag-xorg-memory.csv")
STATE = Path(os.environ.get("RSAG_NTFY_STATE", str(Path.home() / ".local/state/rsag-dashboard/ntfy-last-sent")))


def process_pids() -> dict[str, int]:
    found: dict[str, int] = {}
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text().strip()
            if name == "Xorg":
                found["xorg"] = int(entry.name)
            elif name == "python3":
                command = (entry / "cmdline").read_bytes().split(b"\0")
                if str(Path(__file__).resolve().with_name("dashboard_gui.py")).encode() in command:
                    found["dashboard"] = int(entry.name)
                elif str(Path.home() / "Desktop/pir2.py").encode() in command:
                    found["sensor"] = int(entry.name)
        except OSError:
            continue
    return found


def proc_rss_mb(pid: int | None) -> int | None:
    if pid is None:
        return None
    try:
        for line in (PROC / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024)
    except (OSError, ValueError, IndexError):
        pass
    return None


def xorg_delta_24h(pid: int | None, now: datetime) -> int | None:
    """Use only same-process samples from roughly one day ago."""
    if pid is None:
        return None
    try:
        with MEMORY_LOG.open(newline="", encoding="utf-8") as source:
            samples = [row for row in csv.DictReader(source) if row.get("xorg_pid") == str(pid)]
        target = now - timedelta(hours=24)
        candidates = []
        for row in samples:
            recorded = datetime.fromisoformat(row["time"])
            if timedelta(hours=22) <= now - recorded <= timedelta(hours=26):
                candidates.append((abs(recorded - target), int(row["xorg_rss_kb"])))
        if not candidates:
            return None
        baseline_kb = min(candidates)[1]
        current_mb = proc_rss_mb(pid)
        return round(current_mb - baseline_kb / 1024) if current_mb is not None else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def wifi_connected() -> bool | None:
    try:
        result = subprocess.run(
            ["/usr/bin/nmcli", "-t", "-g", "GENERAL.STATE", "device", "show", "wlan0"],
            capture_output=True, text=True, check=True, timeout=3,
        )
        return result.stdout.strip().startswith("100 ")
    except (OSError, subprocess.SubprocessError):
        return None


def uptime_text() -> str:
    try:
        hours = int(float((PROC / "uptime").read_text().split()[0]) // 3600)
        return f"{hours // 24} d {hours % 24} h" if hours >= 24 else f"{hours} h"
    except (OSError, ValueError, IndexError):
        return "–"


def compose_status(now: datetime | None = None) -> tuple[str, str, bool]:
    now = now or datetime.now().astimezone()
    pids = process_pids()
    sampler = HealthSampler()
    sampler.sample()
    time.sleep(0.3)
    health = sampler.sample()
    memory = (PROC / "meminfo").read_text()
    available = next((round(int(line.split()[1]) / 1024) for line in memory.splitlines()
                      if line.startswith("MemAvailable:")), None)
    disk = shutil.disk_usage("/")
    disk_used = round(100 * disk.used / disk.total)
    disk_free_gb = disk.free / (1024 ** 3)
    wlan = wifi_connected()
    outages = read_outage_count()
    xorg_mb = proc_rss_mb(pids.get("xorg"))
    xorg_delta = xorg_delta_24h(pids.get("xorg"), now)
    concern = ("dashboard" not in pids or "sensor" not in pids or wlan is False
               or (health.memory is not None and health.memory >= 85)
               or (health.temperature is not None and health.temperature >= 75)
               or disk_used >= 90)

    def value(number: int | None, unit: str = "") -> str:
        return f"{number}{unit}" if number is not None else "–"

    app = "läuft" if "dashboard" in pids else "AUS"
    sensor = "läuft" if "sensor" in pids else "AUS"
    wifi = "verbunden" if wlan is True else "GETRENNT" if wlan is False else "unbekannt"
    trend = f" ({xorg_delta:+d} MB/24 h)" if xorg_delta is not None else ""
    title = "Abfahrtsmonitor+: Achtung" if concern else "Abfahrtsmonitor+: Morgenstatus"
    body = "\n".join((
        f"Anzeige {app} · Sensor {sensor} · Laufzeit {uptime_text()}",
        f"WLAN {wifi} · Ausfälle 24 h: {value(outages)}",
        f"RAM {value(health.memory, '%')} · frei {value(available, ' MB')} · Xorg {value(xorg_mb, ' MB')}{trend}",
        f"CPU {value(health.cpu, '%')} · Temperatur {value(health.temperature, ' °C')}",
        f"Speicherkarte {disk_used}% belegt · {disk_free_gb:.1f} GB frei",
    ))
    return title, body, concern


def publish(url: str, token: str, title: str, body: str, concern: bool) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.path.strip("/") or parsed.query or parsed.fragment:
        raise ValueError("RSAG_NTFY_URL muss eine HTTPS-Topic-Adresse sein")
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Title": title,
        "Priority": "4" if concern else "3",
        "Tags": "warning,computer" if concern else "computer",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=12) as response:
        if response.status >= 300:
            raise RuntimeError(f"ntfy HTTP {response.status}")


def main() -> int:
    today = datetime.now().astimezone().date().isoformat()
    if "--send" in sys.argv and STATE.exists() and STATE.read_text().strip() == today:
        return 0
    title, body, concern = compose_status()
    if "--send" not in sys.argv:
        print(title + "\n" + body)
        return 0
    url = os.environ.get("RSAG_NTFY_URL", "")
    token = os.environ.get("RSAG_NTFY_TOKEN", "")
    try:
        publish(url, token, title, body, concern)
    except urllib.error.HTTPError as error:
        print(f"ntfy HTTP {error.code}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
        print(f"ntfy-Versand fehlgeschlagen: {type(error).__name__}", file=sys.stderr)
        return 1
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(today + "\n")
    print("ntfy-Statusmeldung versendet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
