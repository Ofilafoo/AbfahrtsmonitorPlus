#!/usr/bin/python3
"""Append one compact Raspberry Pi memory snapshot for later leak analysis."""

from __future__ import annotations

import csv
import subprocess
import sys
from datetime import datetime
from pathlib import Path


PROC = Path("/proc")
LOG = Path("/var/log/rsag-dashboard-memory.csv")
DETAIL_LOG = Path("/var/log/rsag-xorg-memory.csv")
FIELDS = (
    "time", "boot_id", "uptime_s", "mem_available_kb", "swap_used_kb",
    "xorg_pid", "xorg_rss_kb", "xorg_private_dirty_kb",
    "dashboard_pid", "dashboard_rss_kb", "dashboard_private_dirty_kb",
    "sensor_pid", "sensor_rss_kb", "display_on",
)
DETAIL_FIELDS = FIELDS + (
    "xorg_anonymous_kb", "xorg_largest_anon_mapping_kb",
    "xorg_fd_count", "dashboard_fd_count",
)


def field_kb(contents: str, name: str) -> int | None:
    for line in contents.splitlines():
        key, separator, value = line.partition(":")
        if separator and key == name:
            try:
                return int(value.split()[0])
            except (IndexError, ValueError):
                return None
    return None


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def process_info(pid: int) -> tuple[int | None, int | None]:
    process = PROC / str(pid)
    rss = field_kb(read_text(process / "status"), "VmRSS")
    private = field_kb(read_text(process / "smaps_rollup"), "Private_Dirty")
    return rss, private


def largest_anonymous_mapping_kb(contents: str) -> int:
    """Find the largest anonymous mapping in a process's smaps data."""
    largest = 0
    for line in contents.splitlines():
        if line.startswith("Anonymous:"):
            largest = max(largest, field_kb(line, "Anonymous") or 0)
    return largest


def fd_count(pid: int | str) -> int | str:
    if not pid:
        return ""
    try:
        return sum(1 for _ in (PROC / str(pid) / "fd").iterdir())
    except OSError:
        return ""


def processes() -> dict[str, tuple[int, int | None, int | None]]:
    selected: dict[str, tuple[int, int | None, int | None]] = {}
    for process in PROC.iterdir():
        if not process.name.isdigit():
            continue
        pid = int(process.name)
        name = read_text(process / "comm").strip()
        if name == "Xorg":
            role = "xorg"
        elif name == "python3":
            try:
                command = (process / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if any(arg.endswith(b"/dashboard_gui.py") for arg in command):
                role = "dashboard"
            elif any(arg.endswith(b"/pir2.py") for arg in command):
                role = "sensor"
            else:
                continue
        else:
            continue
        rss, private = process_info(pid)
        if role not in selected or (rss or 0) > (selected[role][1] or 0):
            selected[role] = (pid, rss, private)
    return selected


def display_on() -> str:
    try:
        result = subprocess.run(
            ["/usr/bin/vcgencmd", "display_power"], capture_output=True,
            text=True, check=True, timeout=3,
        )
        return "1" if result.stdout.strip() == "display_power=1" else "0"
    except (OSError, subprocess.SubprocessError):
        return ""


def snapshot() -> dict[str, object]:
    memory = read_text(PROC / "meminfo")
    swap_total = field_kb(memory, "SwapTotal")
    swap_free = field_kb(memory, "SwapFree")
    found = processes()
    xorg = found.get("xorg", ("", "", ""))
    dashboard = found.get("dashboard", ("", "", ""))
    sensor = found.get("sensor", ("", "", ""))
    return {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "boot_id": read_text(PROC / "sys/kernel/random/boot_id").strip(),
        "uptime_s": int(float(read_text(PROC / "uptime").split()[0])),
        "mem_available_kb": field_kb(memory, "MemAvailable"),
        "swap_used_kb": swap_total - swap_free if swap_total is not None and swap_free is not None else "",
        "xorg_pid": xorg[0],
        "xorg_rss_kb": xorg[1],
        "xorg_private_dirty_kb": xorg[2],
        "dashboard_pid": dashboard[0],
        "dashboard_rss_kb": dashboard[1],
        "dashboard_private_dirty_kb": dashboard[2],
        "sensor_pid": sensor[0],
        "sensor_rss_kb": sensor[1],
        "display_on": display_on(),
    }


def detail_snapshot() -> dict[str, object]:
    row = snapshot()
    xorg_pid = row["xorg_pid"]
    rollup = read_text(PROC / str(xorg_pid) / "smaps_rollup") if xorg_pid else ""
    smaps = read_text(PROC / str(xorg_pid) / "smaps") if xorg_pid else ""
    row.update({
        "xorg_anonymous_kb": field_kb(rollup, "Anonymous") if rollup else "",
        "xorg_largest_anon_mapping_kb": largest_anonymous_mapping_kb(smaps) if smaps else "",
        "xorg_fd_count": fd_count(xorg_pid),
        "dashboard_fd_count": fd_count(row["dashboard_pid"]),
    })
    return row


def append_snapshot(path: Path, row: dict[str, object], fields: tuple[str, ...] = FIELDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        if output.tell() == 0:
            writer.writeheader()
        writer.writerow(row)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--detail":
        append_snapshot(DETAIL_LOG, detail_snapshot(), DETAIL_FIELDS)
    else:
        append_snapshot(Path(sys.argv[1]) if len(sys.argv) > 1 else LOG, snapshot())
