"""Count meaningful wlan0 outages from NetworkManager's persistent journal."""

from __future__ import annotations

import json
import re
import subprocess
import time


STATE_CHANGE = re.compile(r"device \(wlan0\): state change: (\S+) -> (\S+)")
MIN_OUTAGE_SECONDS = 120


def outage_count(journal_lines: str, now: float, window_seconds: int = 86400) -> int:
    """Count outages touching the window, ignoring brief connection transitions."""
    events: list[tuple[float, str, str]] = []
    for line in journal_lines.splitlines():
        try:
            record = json.loads(line)
            match = STATE_CHANGE.search(record.get("MESSAGE", ""))
            if match:
                events.append((int(record["__REALTIME_TIMESTAMP"]) / 1_000_000, *match.groups()))
        except (ValueError, KeyError, TypeError):
            continue
    events.sort()

    count = 0
    start: float | None = None
    cutoff = now - window_seconds
    for timestamp, old, new in events:
        if old == "activated" and new != "activated" and start is None:
            start = timestamp
        elif new == "activated" and start is not None:
            if timestamp - start >= MIN_OUTAGE_SECONDS and timestamp >= cutoff and start <= now:
                count += 1
            start = None
    if start is not None and now - start >= MIN_OUTAGE_SECONDS and now >= cutoff:
        count += 1
    return count


def read_outage_count(now: float | None = None) -> int | None:
    """Return None when journal access fails, never a misleading zero."""
    now = time.time() if now is None else now
    try:
        result = subprocess.run(
            ["journalctl", "-u", "NetworkManager", "--since", "48 hours ago", "-o", "json", "--no-pager", "--quiet"],
            capture_output=True, text=True, timeout=12, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return outage_count(result.stdout, now)
