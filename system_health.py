"""Small, dependency-free Linux health snapshot for the monitor header."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def cpu_times(line: str) -> tuple[int, int]:
    parts = line.split()
    if not parts or parts[0] != "cpu":
        raise ValueError("missing aggregate CPU line")
    ticks = [int(value) for value in parts[1:9]]
    if len(ticks) < 5:
        raise ValueError("incomplete CPU counters")
    return sum(ticks), ticks[3] + ticks[4]


def cpu_usage(previous: tuple[int, int], current: tuple[int, int]) -> int | None:
    total = current[0] - previous[0]
    idle = current[1] - previous[1]
    return round(max(0, min(100, 100 * (total - idle) / total))) if total > 0 else None


def memory_usage(meminfo: str) -> int:
    values = {}
    for line in meminfo.splitlines():
        name, _, rest = line.partition(":")
        if name in {"MemTotal", "MemAvailable"}:
            values[name] = int(rest.split()[0])
    total = values["MemTotal"]
    available = values["MemAvailable"]
    if total <= 0:
        raise ValueError("invalid memory total")
    return round(max(0, min(100, 100 * (total - available) / total)))


@dataclass(frozen=True)
class HealthReading:
    cpu: int | None
    memory: int | None
    temperature: int | None
    wifi_outages: int | None = None

    @property
    def text(self) -> str:
        value = lambda number, unit: f"{number}{unit}" if number is not None else "–"
        wifi = str(self.wifi_outages) if self.wifi_outages is not None else "–"
        return f"CPU {value(self.cpu, '%')}  ·  RAM {value(self.memory, '%')}  ·  {value(self.temperature, '°C')}  ·  WLAN-Ausfälle 24h: {wifi}"

    @property
    def level(self) -> str:
        if ((self.cpu is not None and self.cpu >= 95)
                or (self.memory is not None and self.memory >= 90)
                or (self.temperature is not None and self.temperature >= 75)):
            return "hot"
        if ((self.cpu is not None and self.cpu >= 85)
                or (self.memory is not None and self.memory >= 80)
                or (self.temperature is not None and self.temperature >= 65)):
            return "warn"
        return "good"


class HealthSampler:
    def __init__(self) -> None:
        self.previous_cpu: tuple[int, int] | None = None

    def sample(self) -> HealthReading:
        cpu = memory = temperature = None
        try:
            current_cpu = cpu_times(Path("/proc/stat").read_text().splitlines()[0])
            if self.previous_cpu is not None:
                cpu = cpu_usage(self.previous_cpu, current_cpu)
            self.previous_cpu = current_cpu
        except (OSError, ValueError, IndexError):
            pass
        try:
            memory = memory_usage(Path("/proc/meminfo").read_text())
        except (OSError, ValueError, KeyError, IndexError):
            pass
        try:
            temperature = round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000)
        except (OSError, ValueError):
            pass
        return HealthReading(cpu, memory, temperature)
