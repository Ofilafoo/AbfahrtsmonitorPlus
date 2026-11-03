#!/usr/bin/env python3
"""Small, browser-free departure and weather dashboard for a Raspberry Pi."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import curses
import json
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Callable, Iterable


STOPS = {
    "14": "Doberaner Platz",
    "15": "Schröderplatz",
    "179": "Deutsche-Med-Platz",
}
WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude=54.0924&longitude=12.0991"
    "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_gusts_10m"
    "&hourly=temperature_2m,apparent_temperature,weather_code,precipitation_probability,precipitation"
    "&daily=temperature_2m_max,temperature_2m_min"
    "&timezone=Europe%2FBerlin&past_days=1&forecast_days=2"
)
REFRESH_SECONDS = 15
WEATHER_REFRESH_SECONDS = 15 * 60
ALERT_REFRESH_SECONDS = 60
RETRY_SECONDS = 15


@dataclass(frozen=True)
class Departure:
    line: str
    direction: str
    departure: str
    platform: str
    occupancy: str = ""
    via: str = ""
    note: str = ""
    delay: str = ""
    cancelled: bool = False
    mode: str = ""


@dataclass
class DashboardState:
    departures: dict[str, list[Departure]] = field(default_factory=dict)
    weather: dict = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)
    last_attempt: dict[str, float] = field(default_factory=dict)
    last_success_clock: dict[str, str] = field(default_factory=dict)
    errors: set[str] = field(default_factory=set)
    status: str = "Lade Daten …"

    def refresh(self, now: float, producers: dict[str, Callable[[], object]]) -> None:
        intervals = {
            "departures": REFRESH_SECONDS,
            "weather": WEATHER_REFRESH_SECONDS,
            "alerts": ALERT_REFRESH_SECONDS,
        }
        labels = {"departures": "Abfahrten", "weather": "Wetter", "alerts": "RSAG-Meldungen"}
        for name, producer in producers.items():
            interval = RETRY_SECONDS if name in self.errors else intervals[name]
            if now - self.last_attempt.get(name, float("-inf")) < interval:
                continue
            self.last_attempt[name] = now
            try:
                value = producer()
            except Exception as error:
                self.errors.add(name)
                self.status = f"{labels[name]}: {type(error).__name__} · erneuter Versuch in {RETRY_SECONDS} s"
            else:
                setattr(self, name, value)
                self.errors.discard(name)
                self.last_success_clock[name] = datetime.now().strftime("%H:%M:%S")
                self.status = f"{labels[name]} aktualisiert: {self.last_success_clock[name]}"

    def warning(self) -> str:
        if not self.errors:
            return ""
        last = self.last_success_clock.get("departures", "noch keine Daten")
        if self.errors == {"departures", "weather", "alerts"}:
            return f"OFFLINE · DATEN NICHT LIVE · Stand {last} · Versuch alle {RETRY_SECONDS} s"
        if "departures" in self.errors:
            return f"ABFAHRTEN NICHT LIVE · Stand {last} · Versuch alle {RETRY_SECONDS} s"
        affected = ", ".join(name for key, name in (("weather", "Wetter"), ("alerts", "Meldungen")) if key in self.errors)
        return f"NICHT ALLES LIVE · {affected} veraltet · Versuch alle {RETRY_SECONDS} s"


class DepartureParser(HTMLParser):
    """Extract the visible departure fields from RSAG's compact HTML fragment."""

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[Departure] = []
        self._stack: list[tuple[str, set[str]]] = []
        self._values: dict[str, list[str]] = {}
        self._occupancy = ""
        self._cancelled = False
        self._mode = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if tag == "div" and "entry" in classes and "tdi" in classes and not self._stack:
            self._stack = [(tag, classes)]
            self._values = {}
            self._occupancy = ""
            self._cancelled = bool(classes & {"cancelled", "canceled", "cancel"})
            self._mode = ""
            return
        if not self._stack:
            return
        if tag not in {"br", "img", "hr", "input", "meta"}:
            self._stack.append((tag, classes))
        if "occupancy" in classes:
            self._occupancy = next((level for level in ("high", "mid", "low") if level in classes), "")
        if "icon" in classes:
            self._mode = next((kind for kind in ("bus", "tram", "subway", "ferry") if kind in classes), self._mode)
        self._cancelled |= bool(classes & {"cancelled", "canceled", "cancel"})

    def handle_endtag(self, tag: str) -> None:
        if not self._stack:
            return
        while self._stack:
            opened_tag, _ = self._stack.pop()
            if opened_tag == tag:
                break
        if not self._stack:
            value = lambda name: " ".join(" ".join(self._values.get(name, [])).split())
            note = " · ".join(filter(None, (value("alert"), value("other"))))
            cancelled = self._cancelled or "fällt aus" in note.lower() or "ausfall" in note.lower()
            if value("line") and value("direction") and (value("departure") or cancelled):
                self.entries.append(Departure(
                    line=value("line"), direction=value("direction"),
                    departure=value("departure"), platform=value("platform"),
                    occupancy=self._occupancy, via=value("via"), note=note,
                    delay=value("delay"), cancelled=cancelled, mode=self._mode,
                ))

    def handle_data(self, data: str) -> None:
        if not self._stack or not data.strip():
            return
        fields = ("line", "direction", "via", "alert", "departure", "platform", "delay")
        field = next((field for _, classes in reversed(self._stack) for field in fields if field in classes), None)
        if field is None and any("destination" in classes for _, classes in self._stack):
            field = "other"
        if field:
            self._values.setdefault(field, []).append(data.strip())


class AlertParser(HTMLParser):
    """Extract messages from RSAG alert fragments."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []
        self._depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if tag == "div" and "alert" in classes and self._depth == 0:
            self._depth = 1
            self._parts = []
        elif tag == "div" and self._depth:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                message = " ".join(" ".join(self._parts).split())
                if message:
                    self.messages.append(message)

    def handle_data(self, data: str) -> None:
        if self._depth and data.strip():
            self._parts.append(data.strip())


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "raspi-dashboard/1.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8")


def get_departures(stop_id: str) -> list[Departure]:
    parser = DepartureParser()
    parser.feed(fetch(f"https://abfahrten-rsag.de/dfi/_/{stop_id}"))
    return parser.entries


def get_alerts(stop_id: str) -> list[str]:
    parser = AlertParser()
    parser.feed(fetch(f"https://abfahrten-rsag.de/dfi/_/a/{stop_id}"))
    return parser.messages


def alerts_for_all_stops() -> list[str]:
    with ThreadPoolExecutor(max_workers=len(STOPS)) as pool:
        results = pool.map(get_alerts, STOPS)
        return list(dict.fromkeys(message for group in results for message in group))


def weather_description(code: int) -> str:
    labels = {
        0: "Klar", 1: "Meist klar", 2: "Wolkig", 3: "Bedeckt", 45: "Nebel",
        48: "Nebel", 51: "Nieselregen", 53: "Nieselregen", 55: "Nieselregen",
        61: "Regen", 63: "Regen", 65: "Starker Regen", 71: "Schnee",
        73: "Schnee", 75: "Starker Schnee", 80: "Regenschauer", 81: "Regenschauer",
        82: "Starke Schauer", 95: "Gewitter", 96: "Gewitter", 99: "Gewitter",
    }
    return labels.get(code, "Unbekannt")


def weather_symbol(code: int) -> str:
    if code == 0:
        return "☀"
    if code == 1:
        return "☀"
    if code in (2, 3):
        return "☁"
    if code in (45, 48):
        return "≋"
    if code in (71, 73, 75, 77, 85, 86):
        return "❄"
    if code in (95, 96, 99):
        return "⚡"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "☂"
    return "?"


def weather_advice(feels: float, wind: float, gusts: float, umbrella: str) -> str:
    windy = wind >= 30 or gusts >= 45
    tips = []
    if umbrella == "mitnehmen":
        tips.append("Regenjacke statt Schirm" if windy else "Schirm mitnehmen")
    elif umbrella == "erwägen":
        tips.append("Schirm eventuell" if not windy else "Regenjacke eventuell")
    if feels <= 5:
        tips.append("Mütze & Schal")
    if windy:
        tips.append("Windjacke")
    elif feels <= 12:
        tips.append("Jacke")
    return " · ".join(tips) if tips else "Keine Extras nötig"


def weather_comparison(current_feels: float, yesterday_feels: float | None) -> tuple[str, str]:
    if yesterday_feels is None:
        return "", "same"
    difference = current_feels - yesterday_feels
    if abs(difference) < 3:
        return "Kleidung wie gestern", "same"
    if difference > 0:
        return "Heute leichter anziehen", "warmer"
    return "Heute wärmer anziehen", "cooler"


def parse_weather(data: dict) -> dict:
    current = data["current"]
    daily = data["daily"]
    hourly = data["hourly"]
    current_time = datetime.fromisoformat(current["time"])
    hourly_times = [datetime.fromisoformat(value) for value in hourly["time"]]
    today = current_time.date().isoformat()
    daily_index = next((i for i, value in enumerate(daily.get("time", [today])) if value == today), 0)

    yesterday_time = (current_time.replace(minute=0, second=0, microsecond=0) - timedelta(days=1))
    yesterday_index = next((i for i, value in enumerate(hourly_times) if value == yesterday_time), None)
    yesterday_temp = hourly["temperature_2m"][yesterday_index] if yesterday_index is not None else None
    feels_history = hourly.get("apparent_temperature", [])
    yesterday_feels = feels_history[yesterday_index] if yesterday_index is not None and yesterday_index < len(feels_history) else None
    comparison, comparison_direction = weather_comparison(current["apparent_temperature"], yesterday_feels)

    def forecast_after(hours: int) -> tuple[int, str, dict]:
        target = current_time + timedelta(hours=hours)
        index = next(i for i, value in enumerate(hourly_times) if value >= target)
        time_label = hourly_times[index].strftime("%H:%M")
        line = (
            f"+{hours} h  {time_label}  {weather_symbol(hourly['weather_code'][index])} "
            f"{hourly['temperature_2m'][index]:.0f}°  {hourly['precipitation_probability'][index]:.0f}%"
        )
        return index, line, {
            "hours": hours, "time": time_label,
            "temperature": round(hourly["temperature_2m"][index]),
            "code": hourly["weather_code"][index],
            "rain_probability": round(hourly["precipitation_probability"][index]),
        }

    _, plus_3h, outlook_3h = forecast_after(3)
    _, plus_6h, outlook_6h = forecast_after(6)
    upcoming = [i for i, value in enumerate(hourly_times) if current_time <= value <= current_time + timedelta(hours=6)]
    rain_risk = max((hourly["precipitation_probability"][i] or 0 for i in upcoming), default=0)
    rain_amount = sum((hourly["precipitation"][i] or 0 for i in upcoming), 0.0)
    raining_now = current["weather_code"] in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
    if raining_now or rain_risk >= 50 or rain_amount >= 0.5:
        umbrella = "mitnehmen"
    elif rain_risk >= 25 or rain_amount >= 0.1:
        umbrella = "erwägen"
    else:
        umbrella = "eher nicht"
    wind = current.get("wind_speed_10m") or 0
    gusts = current.get("wind_gusts_10m") or 0
    return {
        "symbol": weather_symbol(current["weather_code"]),
        "code": current["weather_code"],
        "now": f"{current['temperature_2m']:.0f} °C",
        "feels": f"gefühlt {current['apparent_temperature']:.0f} °C",
        "description": weather_description(current["weather_code"]),
        "range": f"{daily['temperature_2m_min'][daily_index]:.0f}–{daily['temperature_2m_max'][daily_index]:.0f} °C",
        "comparison": comparison,
        "comparison_direction": comparison_direction,
        "yesterday_temperature": yesterday_temp,
        "yesterday_feels": yesterday_feels,
        "plus_3h": plus_3h,
        "plus_6h": plus_6h,
        "outlooks": [outlook_3h, outlook_6h],
        "umbrella": umbrella,
        "rain_risk": round(rain_risk),
        "wind": f"Wind {wind:.0f} km/h · Böen {gusts:.0f} km/h",
        "wind_speed": round(wind),
        "wind_gusts": round(gusts),
        "advice": weather_advice(current["apparent_temperature"], wind, gusts, umbrella),
    }


def get_weather() -> dict:
    return parse_weather(json.loads(fetch(WEATHER_URL)))


def clipped(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def put(screen: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if 0 <= y < height and x < width:
        try:
            screen.addstr(y, max(0, x), clipped(text, width - max(0, x)), attr)
        except curses.error:
            pass


def departures_for_all_stops() -> dict[str, list[Departure]]:
    with ThreadPoolExecutor(max_workers=len(STOPS)) as pool:
        results = pool.map(get_departures, STOPS)
        return dict(zip(STOPS, results))


def panel_title(screen: curses.window, y: int, x: int, title: str, width: int) -> None:
    put(screen, y, x, clipped(title.upper(), width), curses.A_BOLD | curses.color_pair(2))
    put(screen, y + 1, x, "─" * max(1, width), curses.color_pair(3))


def occupancy_indicator(level: str) -> str:
    return {"low": "●○○", "mid": "●●○", "high": "●●●"}.get(level, "   ")


def departure_details(item: Departure) -> str:
    parts = [item.via, item.note]
    if item.cancelled and not any("aus" in part.lower() for part in parts):
        parts.insert(0, "Fahrt fällt aus")
    return "  ·  ".join(part for part in parts if part)


def scrolling_text(text: str, width: int, elapsed: float) -> str:
    if len(text) <= width:
        return text
    loop = text + "       •       "
    offset = int(elapsed * 5) % len(loop)
    return (loop + loop)[offset : offset + width]


def departure_rows(
    screen: curses.window,
    departures: Iterable[Departure],
    y: int,
    x: int,
    width: int,
    maximum: int,
    compact: bool = False,
    elapsed: float = 0.0,
) -> None:
    used = 0
    for item in departures:
        if used >= maximum:
            break
        line_y = y + used
        details = departure_details(item)
        time_text = "AUSFALL" if item.cancelled else " ".join(part for part in (item.departure, item.delay) if part)
        suffix = time_text + (f" {item.platform}" if item.platform and not compact else "")
        indicator = occupancy_indicator(item.occupancy)
        if compact:
            line_x, indicator_x, direction_x = x, x + 4, x + 8
        else:
            line_x, indicator_x, direction_x = x, x + 5, x + 10
        time_x = x + width - len(suffix)
        direction_width = max(0, time_x - direction_x - 1)
        direction = item.direction
        if details and used + 1 == maximum:
            direction = scrolling_text(f"{item.direction} · {details}", direction_width, elapsed)
        put(screen, line_y, line_x, f"{item.line:>3}", curses.A_BOLD | curses.color_pair(1))
        occupancy_color = {"low": 4, "mid": 2, "high": 5}.get(item.occupancy, 0)
        put(screen, line_y, indicator_x, indicator, curses.color_pair(occupancy_color))
        put(screen, line_y, direction_x, clipped(direction, direction_width), curses.A_DIM if item.cancelled else 0)
        time_attr = curses.A_BOLD | (curses.color_pair(5) if item.cancelled or item.note or "+" in time_text else 0)
        put(screen, line_y, time_x, suffix, time_attr)
        used += 1

        if details and used < maximum:
            prefix = "  ↳ "
            detail_attr = curses.color_pair(5) if item.cancelled or item.note else curses.A_DIM
            put(screen, y + used, x, prefix + scrolling_text(details, width - len(prefix), elapsed), detail_attr)
            used += 1


def weather_art(code: int) -> tuple[str, str, str]:
    if code == 0:
        return ("   \\ | /   ", "  -- O --  ", "   / | \\   ")
    if code in (1, 2):
        return ("  \\ | .--.", "  - O(   )", "   /  '---'")
    if code in (45, 48):
        return ("  ~~~~~~~  ", " ~~~~~~~~  ", "  ~~~~~~~  ")
    if code in (71, 73, 75, 77, 85, 86):
        return ("   .--.    ", "  (____)   ", "   * * *   ")
    if code in (95, 96, 99):
        return ("   .--.    ", "  (____)   ", "    /\\     ")
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return ("   .--.    ", "  (____)   ", "   / / /   ")
    return ("   .--.    ", "  (____)   ", "   '----'  ")


def ticker_text(messages: list[str], width: int, elapsed: float) -> str:
    if not messages:
        return "Keine aktuellen RSAG-Meldungen"
    return scrolling_text("   •   ".join(messages), width, elapsed)


def render(
    screen: curses.window,
    departures: dict[str, list[Departure]],
    weather: dict,
    alerts: list[str],
    status: str,
    ticker_elapsed: float,
    warning: str = "",
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    if height < 24 or width < 72:
        put(screen, 1, 2, "Fenster bitte mindestens 72 × 24 Zeichen groß machen.")
        screen.refresh()
        return

    now = datetime.now()
    left_width = max(36, width // 2)
    right_x = left_width + 3
    right_width = width - right_x - 2
    put(screen, 0, 0, " " * width, curses.color_pair(6))
    put(screen, 1, 0, " " * width, curses.color_pair(6))
    put(screen, 1, 2, "ABFAHRTSMONITOR+", curses.A_BOLD | curses.color_pair(6))
    put(screen, 1, max(2, width - 26), now.strftime("%a, %d.%m.  %H:%M"), curses.A_BOLD | curses.color_pair(6))
    if warning:
        put(screen, 2, 0, " " * width, curses.color_pair(7))
        put(screen, 2, 2, clipped("!  " + warning, width - 4), curses.A_BOLD | curses.color_pair(7))
    for y in range(3, height - 2):
        put(screen, y, left_width, "│", curses.color_pair(3))

    panel_title(screen, 3, 2, STOPS["14"], left_width - 4)
    put(screen, 5, 2, "Lin.", curses.A_DIM)
    put(screen, 5, 7, "Bel.", curses.A_DIM)
    put(screen, 5, 12, "Richtung", curses.A_DIM)
    put(screen, 5, left_width - 11, "Ab", curses.A_DIM)
    departure_rows(screen, departures.get("14", []), 7, 2, left_width - 4, height - 10, elapsed=ticker_elapsed)

    panel_title(screen, 3, right_x, "Wetter · Rostock", right_width)
    for index, art_line in enumerate(weather_art(weather.get("code", -1))):
        put(screen, 5 + index, right_x, art_line, curses.A_BOLD | curses.color_pair(1))
    put(screen, 5, right_x + 12, weather.get("now", "–"), curses.A_BOLD | curses.color_pair(1))
    put(screen, 6, right_x + 12, weather.get("feels", "–"))
    put(screen, 7, right_x + 12, weather.get("description", "Lade Daten"))
    put(screen, 8, right_x, "Heute " + weather.get("range", "–"))
    put(screen, 9, right_x, weather.get("plus_3h", "+3 h  –"))
    put(screen, 10, right_x, weather.get("plus_6h", "+6 h  –"))
    umbrella = weather.get("umbrella", "–")
    umbrella_color = 5 if umbrella == "mitnehmen" else 2 if umbrella == "erwägen" else 4
    put(screen, 11, right_x, f"Schirm: {umbrella} ({weather.get('rain_risk', '–')} % / 6 h)", curses.A_BOLD | curses.color_pair(umbrella_color))

    compact_start = 13
    compact_height = max(3, (height - compact_start - 2) // 2)
    panel_title(screen, compact_start, right_x, STOPS["15"], right_width)
    departure_rows(screen, departures.get("15", []), compact_start + 2, right_x, right_width, compact_height - 2, True, ticker_elapsed)
    second_start = compact_start + compact_height
    panel_title(screen, second_start, right_x, STOPS["179"], right_width)
    departure_rows(screen, departures.get("179", []), second_start + 2, right_x, right_width, height - second_start - 4, True, ticker_elapsed)

    put(screen, height - 2, 2, "RSAG  " + ticker_text(alerts, width - 10, ticker_elapsed), curses.color_pair(2))
    put(screen, height - 1, 2, status, curses.A_DIM)
    put(screen, height - 1, width - 18, "q beendet", curses.A_DIM)
    screen.refresh()


def demo_data() -> tuple[dict[str, list[Departure]], dict[str, str], list[str]]:
    """Representative cases for checking the layout without waiting for live incidents."""
    return (
        {
            "14": [
                Departure("1", "Mecklenburger Allee", "3 min", "A", occupancy="low"),
                Departure("6", "Neuer Friedhof", "8 min", "C", occupancy="mid", delay="+2 min"),
                Departure("25", "Thomas-Morus-Str.", "12 min", "G", occupancy="high", note="Ersatzhaltestelle am Platz"),
                Departure("3", "Dierkower Allee", "15 min", "D", via="via Hauptbahnhof"),
                Departure("5", "Südblick", "18 min", "B", note="Fahrt fällt aus", cancelled=True),
                Departure("2", "Reutershagen", "21 min", "A", via="via Hauptbahnhof"),
                Departure("1", "Rügener Straße", "24 min", "C", occupancy="low"),
                Departure("6", "Mensa", "27 min", "D", delay="+1 min"),
                Departure("3", "Neuer Markt", "31 min", "B", via="via Stadthalle"),
                Departure("25", "Südstadt Center", "35 min", "G", occupancy="mid"),
            ],
            "15": [
                Departure("25", "Thomas-Morus-Str.", "5 min", "C", occupancy="high"),
                Departure("3", "Platz der Jugend", "9 min", "B", delay="+1 min"),
            ],
            "179": [
                Departure("25", "Thomas-Morus-Str.", "7 min", "", note="Haltestelle verlegt"),
            ],
        },
        {
            "code": 63, "symbol": "☂", "now": "11 °C", "feels": "gefühlt 8 °C",
            "description": "Regen", "range": "9–13 °C",
            "comparison": "Heute wärmer anziehen", "comparison_direction": "cooler",
            "yesterday_temperature": 13.0, "yesterday_feels": 12.0,
            "wind": "Wind 24 km/h · Böen 38 km/h", "wind_speed": 24, "wind_gusts": 38,
            "advice": "Schirm mitnehmen · Jacke",
            "plus_3h": "+3 h  16:00  ☂ 12°  75%",
            "plus_6h": "+6 h  19:00  ☁ 10°  20%",
            "outlooks": [
                {"hours": 3, "time": "16:00", "temperature": 12, "code": 63, "rain_probability": 75},
                {"hours": 6, "time": "19:00", "temperature": 10, "code": 3, "rain_probability": 20},
            ],
            "umbrella": "mitnehmen", "rain_risk": 75,
        },
        ["Beispielmeldung: Umleitung der Linien 45 und F2 in beiden Richtungen."],
    )


def run(screen: curses.window, demo: bool = False, demo_offline: bool = False) -> None:
    curses.curs_set(0)
    screen.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    if curses.can_change_color():
        curses.init_color(curses.COLOR_WHITE, 1000, 1000, 1000)
    curses.init_pair(1, curses.COLOR_BLUE, curses.COLOR_WHITE)
    curses.init_pair(2, curses.COLOR_BLUE, curses.COLOR_WHITE)
    curses.init_pair(3, curses.COLOR_CYAN, curses.COLOR_WHITE)
    curses.init_pair(4, curses.COLOR_GREEN, curses.COLOR_WHITE)
    curses.init_pair(5, curses.COLOR_RED, curses.COLOR_WHITE)
    curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_RED)
    curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_WHITE)
    screen.bkgd(" ", curses.color_pair(8))
    state = DashboardState()
    if demo:
        state.departures, state.weather, state.alerts = demo_data()
        state.status = "DEMO – Beispieldaten"
        if demo_offline:
            state.errors = {"departures", "weather", "alerts"}
            state.last_success_clock["departures"] = "10:15:00"
            state.status = "DEMO – Internet getrennt"
    producers = {
        "departures": departures_for_all_stops,
        "weather": get_weather,
        "alerts": alerts_for_all_stops,
    }

    while True:
        now = time.monotonic()
        if not demo:
            state.refresh(now, producers)
        render(screen, state.departures, state.weather, state.alerts, state.status, now, state.warning())
        if screen.getch() in (ord("q"), ord("Q")):
            return
        time.sleep(0.2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Print fetched data and exit; useful for testing.")
    parser.add_argument("--demo", action="store_true", help="Show sample delay, occupancy and cancellation cases without network.")
    parser.add_argument("--demo-offline", action="store_true", help="Show the prominent offline warning with cached sample data.")
    args = parser.parse_args()
    if args.once:
        weather = get_weather()
        print(f"Rostock · {weather['symbol']} {weather['now']} · {weather['feels']} · {weather['description']} · heute {weather['range']}")
        print(weather["plus_3h"], weather["plus_6h"], f"Schirm: {weather['umbrella']} ({weather['rain_risk']} % / 6 h)", sep="\n")
        for stop_id, stop_name in STOPS.items():
            print(f"\n{stop_name}")
            for item in get_departures(stop_id)[:5]:
                print(f"{item.line:>3}  {item.direction:<28} {item.departure:>7}  {item.platform}")
        print("\nRSAG-Meldungen")
        for message in alerts_for_all_stops():
            print(f"- {message}")
        return
    curses.wrapper(run, args.demo or args.demo_offline, args.demo_offline)


if __name__ == "__main__":
    main()
