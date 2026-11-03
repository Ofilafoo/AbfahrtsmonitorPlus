#!/usr/bin/env python3
"""Large, browser-free GTK departure monitor for the Raspberry Pi display."""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import cairo
import gi

gi.require_foreign("cairo")
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

from dashboard import (
    DashboardState,
    Departure,
    STOPS,
    alerts_for_all_stops,
    demo_data,
    departures_for_all_stops,
    get_weather,
    weather_description,
)
from map_layer import (HOME_MARKER, MAP_CENTER_LAT, MAP_CENTER_LON, RADAR_PIXELS, TILE_PIXELS, VIEW_PIXELS,
                       fetch_visible_tiles, load_boundaries, map_point, radar_placement, world_pixel)
from radar import RADAR_REFRESH_SECONDS, RadarFrame, fetch_latest_radar, has_visible_rain, hide_weak_echoes, radar_status
from system_health import HealthSampler
from wifi_health import read_outage_count
from wind_field import (WIND_BATCH_COUNT, WIND_BATCH_SECONDS, WIND_BATCH_SIZE, WindField, WindSample,
                        fetch_wind_batch, merge_wind_batch, wind_color, wind_to_vector)


PRIMARY_ROWS = 10
SECONDARY_ROWS = 4


def styled(widget: Gtk.Widget, *classes: str) -> Gtk.Widget:
    context = widget.get_style_context()
    for name in classes:
        if name:
            context.add_class(name)
    return widget


def label(text: str = "", *classes: str, align: float = 0) -> Gtk.Label:
    widget = Gtk.Label(label=text)
    widget.set_xalign(align)
    widget.set_yalign(0.5)
    widget.set_ellipsize(Pango.EllipsizeMode.END)
    return styled(widget, *classes)


def box(vertical: bool = False, spacing: int = 0, *classes: str) -> Gtk.Box:
    widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)
    return styled(widget, *classes)


def clear(widget: Gtk.Container) -> None:
    for child in widget.get_children():
        widget.remove(child)


class Monitor(Gtk.Window):
    def __init__(self, demo: bool = False, demo_offline: bool = False) -> None:
        super().__init__(title="Abfahrtsmonitor+")
        self.set_default_size(1920, 1080)
        self.connect("destroy", self.on_destroy)
        self.connect("key-press-event", self.on_key)
        self.css()

        self.running = True
        self.demo = demo
        self.lock = threading.Lock()
        self.state = DashboardState()
        self.health_sampler = HealthSampler()
        self.wifi_outages: int | None = None
        self.radar_mosaic: tuple[int, int, GdkPixbuf.Pixbuf] | None = None
        self.radar_frame_time: int | None = None
        self.radar_failed = False
        self.radar_fetching = False
        self.radar_no_rain = False
        self.map_tiles: dict[tuple[int, int], GdkPixbuf.Pixbuf] = {}
        self.map_tile_data: dict[tuple[int, int], bytes] = {}
        self.map_mosaic: tuple[int, int, GdkPixbuf.Pixbuf] | None = None
        self.map_boundaries = load_boundaries()
        self.wind_field: WindField | None = None
        self.wind_fetching = False
        self.wind_failed = False
        self.wind_received_at = 0.0
        self.wind_samples: list[WindSample | None] = [None] * (WIND_BATCH_COUNT * WIND_BATCH_SIZE)
        self.wind_sample_times = [0.0] * WIND_BATCH_COUNT
        self.wind_model_time = ""
        self.wind_next_batch = 0
        self.display_on = True
        self.display_power_checking = False
        if demo:
            self.state.departures, self.state.weather, self.state.alerts = demo_data()
            self.state.status = "Beispieldaten"
            if demo_offline:
                self.state.errors = {"departures", "weather", "alerts"}
                self.state.last_success_clock["departures"] = "10:15"
        self.snapshot = self.copy_state()
        self.start_time = time.monotonic()
        self.last_departures: dict[str, list[Departure]] = {}
        self.last_weather: dict | None = None

        self.build()
        self.show_all()
        self.warning_bar.hide()
        self.fullscreen()
        self.update_view()
        self.update_health()
        GLib.timeout_add(1000, self.update_view)
        GLib.timeout_add(5000, self.update_health)
        GLib.timeout_add_seconds(60, self.start_wifi_count_update)
        GLib.timeout_add_seconds(30, self.update_radar_status)
        GLib.timeout_add_seconds(30, self.update_wind_status)
        GLib.timeout_add(125, self.animate_wind)
        GLib.timeout_add_seconds(5, self.start_display_power_check)
        GLib.timeout_add(250, self.update_ticker)
        self.start_wifi_count_update()
        self.update_radar_status()
        if not demo:
            GLib.timeout_add(1000, self.start_map_update_once)
            GLib.timeout_add_seconds(15 * 60, self.start_map_update)
            GLib.timeout_add(1500, self.start_wind_update_once)
            GLib.timeout_add_seconds(2, self.warm_wind_update)
            GLib.timeout_add_seconds(30, self.retry_missing_wind)
            GLib.timeout_add_seconds(WIND_BATCH_SECONDS, self.start_wind_update)
        if not demo:
            self.start_radar_update()
            GLib.timeout_add_seconds(RADAR_REFRESH_SECONDS, self.start_radar_update)
        if not demo:
            threading.Thread(target=self.fetch_loop, name="dashboard-fetch", daemon=True).start()

    def css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).with_name("dashboard.css")))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def copy_state(self) -> tuple[dict, dict, list[str], str, str, tuple]:
        return (
            self.state.departures.copy(), self.state.weather.copy(), self.state.alerts[:],
            self.state.status, self.state.warning(), tuple(sorted(self.state.errors)),
        )

    def fetch_loop(self) -> None:
        producers = {
            "departures": departures_for_all_stops,
            "weather": get_weather,
            "alerts": alerts_for_all_stops,
        }
        last_attempts: dict[str, float] = {}
        while self.running:
            self.state.refresh(time.monotonic(), producers)
            if self.state.last_attempt != last_attempts:
                last_attempts = self.state.last_attempt.copy()
                with self.lock:
                    self.snapshot = self.copy_state()
            time.sleep(1)

    def build(self) -> None:
        root = box(True, 0, "root")
        if Gdk.Screen.get_default().get_height() < 900:
            styled(root, "compact-screen")
        self.add(root)

        header = box(False, 18, "header")
        header.pack_start(label("ABFAHRTSMONITOR+ · VORSCHAU" if self.demo else "ABFAHRTSMONITOR+", "brand"), True, True, 0)
        close_button = Gtk.Button(label="BEENDEN")
        styled(close_button, "close-button")
        close_button.connect("clicked", lambda _button: self.close())
        header.pack_end(close_button, False, False, 0)
        self.clock = label("", "clock", align=1)
        header.pack_end(self.clock, False, False, 0)
        self.health_status = label("", "health-status", align=1)
        header.pack_end(self.health_status, False, False, 0)
        root.pack_start(header, False, False, 0)

        self.warning_bar = box(False, 0, "warning-bar")
        self.warning_label = label("", "warning-text")
        self.warning_bar.pack_start(self.warning_label, True, True, 0)
        root.pack_start(self.warning_bar, False, False, 0)

        body = box(False, 24, "body")
        body.set_homogeneous(True)
        root.pack_start(body, True, True, 0)

        left_column = box(True, 16, "left-column")
        body.pack_start(left_column, True, True, 0)
        left = box(True, 8, "card", "primary-card")
        left_column.pack_start(left, True, True, 0)
        title_row = box(False, 12)
        title_row.pack_start(label(STOPS["14"], "card-title", "primary-title"), True, True, 0)
        left.pack_start(title_row, False, False, 0)
        self.primary_list = box(True, 0)
        left.pack_start(self.primary_list, True, True, 0)

        weather = box(True, 10, "card", "weather-card")
        body.pack_start(weather, True, True, 0)
        weather.pack_start(label("WETTER · ROSTOCK", "card-title", "weather-title"), False, False, 0)
        current = box(False, 20, "weather-current")
        weather.pack_start(current, False, False, 0)
        self.weather_icon = Gtk.DrawingArea()
        self.weather_icon.set_size_request(110 if Gdk.Screen.get_default().get_height() < 900 else 160,
                                           100 if Gdk.Screen.get_default().get_height() < 900 else 150)
        self.weather_icon.connect("draw", self.draw_weather_icon)
        current.pack_start(self.weather_icon, False, False, 0)
        current_text = box(True, 0)
        current.pack_start(current_text, True, True, 0)
        self.temperature = label("–", "temperature")
        self.feels = label("", "feels")
        self.condition = label("", "condition")
        self.wind = label("", "wind")
        current_text.pack_start(self.temperature, False, False, 0)
        current_text.pack_start(self.feels, False, False, 0)
        current_text.pack_start(self.condition, False, False, 0)
        current_text.pack_start(self.wind, False, False, 0)

        outlooks = box(True, 7, "outlooks")
        outlooks.set_size_request(215 if Gdk.Screen.get_default().get_height() < 900 else 275, -1)
        self.outlook_boxes = []
        for _ in (3, 6):
            tile = box(True, 2, "outlook-tile")
            heading = label("", "outlook-heading")
            value = label("", "outlook-value")
            rain = label("", "outlook-rain")
            tile.pack_start(heading, False, False, 0)
            tile.pack_start(value, False, False, 0)
            tile.pack_start(rain, False, False, 0)
            outlooks.pack_start(tile, False, False, 0)
            self.outlook_boxes.append((heading, value, rain))
        current.pack_end(outlooks, False, False, 0)
        self.weather_advice = label("", "weather-advice")
        weather.pack_start(self.weather_advice, False, False, 0)
        wind_legend = box(False, 12, "wind-legend")
        wind_scale = Gtk.Label()
        wind_scale.set_markup('WINDPFEILE · km/h   <span foreground="#147aca">● 0</span>   '
                              '<span foreground="#4969dc">● 15</span>   '
                              '<span foreground="#8647d2">● 30</span>   '
                              '<span foreground="#bc2cba">● 50+</span>')
        wind_scale.set_xalign(0)
        styled(wind_scale, "wind-scale")
        wind_legend.pack_start(wind_scale, False, False, 0)
        update_interval = label("Abfrage: Wind 2½ Min. · Regen 5 Min.", "update-interval", align=1)
        wind_legend.pack_end(update_interval, False, False, 0)
        weather.pack_start(wind_legend, False, False, 0)

        radar = Gtk.Overlay()
        styled(radar, "radar")
        self.radar_area = Gtk.DrawingArea()
        self.radar_area.set_size_request(-1, 300 if Gdk.Screen.get_default().get_height() >= 900 else 160)
        self.radar_area.connect("draw", self.draw_radar)
        radar.add(self.radar_area)
        self.wind_area = Gtk.DrawingArea()
        styled(self.wind_area, "wind-overlay")
        self.wind_area.set_hexpand(True)
        self.wind_area.set_vexpand(True)
        self.wind_area.set_halign(Gtk.Align.FILL)
        self.wind_area.set_valign(Gtk.Align.FILL)
        self.wind_area.connect("draw", self.draw_wind)
        radar.add_overlay(self.wind_area)
        radar.set_overlay_pass_through(self.wind_area, True)
        radar_header = box(False, 8, "radar-header")
        radar_header.set_halign(Gtk.Align.FILL)
        radar_header.set_valign(Gtk.Align.START)
        radar_header.set_margin_top(9)
        radar_header.set_margin_start(10)
        radar_header.set_margin_end(10)
        radar_header.pack_start(label("NIEDERSCHLAGSRADAR · MV", "radar-title"), False, False, 0)
        self.wind_status_label = label("WIND LÄDT …", "radar-status")
        radar_header.pack_start(self.wind_status_label, False, False, 0)
        self.radar_status_label = label("", "radar-status", align=1)
        radar_header.pack_end(self.radar_status_label, False, False, 0)
        radar.add_overlay(radar_header)
        self.radar_clear_label = label("Kein deutlicher Regen im Ausschnitt", "radar-clear")
        self.radar_clear_label.set_halign(Gtk.Align.START)
        self.radar_clear_label.set_valign(Gtk.Align.END)
        self.radar_clear_label.set_margin_start(10)
        self.radar_clear_label.set_margin_bottom(8)
        radar.add_overlay(self.radar_clear_label)
        credit = Gtk.Label()
        credit.set_markup('<span foreground="#07528c">Karte: </span><a href="https://www.openstreetmap.org/copyright"><span foreground="#07528c">© OpenStreetMap</span></a><span foreground="#07528c"> · Radar: </span><a href="https://www.rainviewer.com/"><span foreground="#07528c">RainViewer</span></a><span foreground="#07528c"> · Wind: </span><a href="https://open-meteo.com/"><span foreground="#07528c">Open-Meteo</span></a>')
        styled(credit, "radar-credit")
        credit.set_halign(Gtk.Align.END)
        credit.set_valign(Gtk.Align.END)
        credit.set_margin_end(10)
        credit.set_margin_bottom(8)
        radar.add_overlay(credit)
        weather.pack_start(radar, True, True, 0)

        secondary = box(False, 16, "secondary-stops")
        secondary.set_homogeneous(True)
        left_column.pack_start(secondary, False, False, 0)
        self.secondary_lists: dict[str, Gtk.Box] = {}
        for stop_id in ("15", "179"):
            card = box(True, 6, "card", "secondary-card")
            secondary.pack_start(card, True, True, 0)
            card.pack_start(label(STOPS[stop_id], "card-title", "secondary-title"), False, False, 0)
            listing = box(True, 0)
            card.pack_start(listing, True, True, 0)
            self.secondary_lists[stop_id] = listing

        ticker = box(False, 22, "ticker")
        ticker.pack_start(label("RSAG", "ticker-brand"), False, False, 0)
        self.ticker_text = label("", "ticker-text")
        ticker.pack_start(self.ticker_text, True, True, 0)
        self.sensor_status = label("", "sensor-status", align=1)
        ticker.pack_end(self.sensor_status, False, False, 0)
        root.pack_end(ticker, False, False, 0)

    def departure_row(self, item: Departure, compact: bool) -> Gtk.Widget:
        vehicle_class = "bus-pill" if item.mode == "bus" or (not item.mode and item.line not in {"1", "2", "3", "4", "5", "6"}) else "tram-pill"
        if compact:
            row = box(False, 7, "departure-row", "mini-row")
            pill = label(item.line, "line-pill", vehicle_class, align=0.5)
            pill.set_size_request(46, -1)
            row.pack_start(pill, False, False, 0)
            direction = label(item.direction, "destination", "mini-destination")
            row.pack_start(direction, True, True, 0)
            if item.note:
                warning = label("!", "mini-alert")
                warning.set_tooltip_text(item.note)
                row.pack_start(warning, False, False, 0)
            time_text = "AUSFALL" if item.cancelled else item.departure
            row.pack_end(label(time_text, "departure-time", "cancelled" if item.cancelled else "", align=1), False, False, 0)
            if item.delay and not item.cancelled:
                row.pack_end(label(item.delay, "mini-delay"), False, False, 0)
            details = [item.direction, item.via, item.note, item.delay]
            if item.occupancy:
                details.append({"low": "wenig belegt", "mid": "mäßig belegt", "high": "stark belegt"}[item.occupancy])
            row.set_tooltip_text(" · ".join(part for part in details if part))
            return row
        row = box(False, 15 if compact else 13, "departure-row", "mini-row" if compact else "large-row")
        pill = label(item.line, "line-pill", vehicle_class, align=0.5)
        pill.set_size_request(75 if compact else 72, -1)
        row.pack_start(pill, False, False, 0)

        destination = box(True, 0)
        row.pack_start(destination, True, True, 0)
        main = label(item.direction, "destination", "mini-destination" if compact else "large-destination")
        destination.pack_start(main, False, False, 0)
        details = [part for part in (item.via, item.note) if part]
        if item.occupancy:
            dots = {"low": "●○○ wenig belegt", "mid": "●●○ mäßig belegt", "high": "●●● stark belegt"}[item.occupancy]
            details.append(dots)
        if details:
            destination.pack_start(label("   ·   ".join(details), "route-detail",
                                         "alert-detail" if item.note or item.cancelled else "via-detail"), False, False, 0)

        time_box = box(True, 0, "departure-time-box")
        row.pack_end(time_box, False, False, 0)
        time_text = "AUSFALL" if item.cancelled else item.departure
        time_label = label(time_text, "departure-time", "cancelled" if item.cancelled else "", align=1)
        time_label.set_size_request(125 if compact else 150, -1)
        time_box.pack_start(time_label, False, False, 0)
        if item.delay:
            time_box.pack_start(label(item.delay, "delay", align=1), False, False, 0)
        elif item.platform and not compact:
            time_box.pack_start(label("Steig " + item.platform, "platform", align=1), False, False, 0)
        return row

    def fill_departures(self, container: Gtk.Box, entries: list[Departure], count: int, compact: bool) -> None:
        clear(container)
        shown = entries[:count]
        if not shown:
            container.pack_start(label("Keine Abfahrten verfügbar", "empty"), False, False, 12)
        for item in shown:
            container.pack_start(self.departure_row(item, compact), False, False, 0)
        container.show_all()

    def update_view(self) -> bool:
        if not self.display_on:
            return self.running
        with self.lock:
            departures, weather, alerts, status, warning, errors = self.snapshot
        clock_text = datetime.now().strftime("%d.%m.  ·  %H:%M")
        if self.clock.get_text() != clock_text:
            self.clock.set_text(clock_text)
        self.update_sensor_status()
        if warning:
            warning_text = "!  " + warning
            if self.warning_label.get_text() != warning_text:
                self.warning_label.set_text(warning_text)
            if not self.warning_bar.get_visible():
                self.warning_bar.show_all()
        elif self.warning_bar.get_visible():
            self.warning_bar.hide()

        for stop_id in ("14", "15", "179"):
            entries = departures.get(stop_id, [])
            if entries == self.last_departures.get(stop_id):
                continue
            container = self.primary_list if stop_id == "14" else self.secondary_lists[stop_id]
            self.fill_departures(container, entries, PRIMARY_ROWS if stop_id == "14" else SECONDARY_ROWS,
                                 stop_id != "14")
            self.last_departures[stop_id] = entries
        if weather != self.last_weather:
            self.update_weather(weather)
            self.last_weather = weather
        return self.running

    def update_sensor_status(self) -> None:
        runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        pid_file = runtime_dir / "rsag-pir.pid"
        active = False
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            active = Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
        except (OSError, ValueError, IndexError):
            pass
        sensor_text = "● SENSOR AKTIV" if active else "● SENSOR INAKTIV / FEHLER"
        if self.sensor_status.get_text() != sensor_text:
            self.sensor_status.set_text(sensor_text)
        context = self.sensor_status.get_style_context()
        context.remove_class("sensor-active" if not active else "sensor-inactive")
        context.add_class("sensor-active" if active else "sensor-inactive")

    def update_health(self) -> bool:
        reading = self.health_sampler.sample()
        if not self.display_on:
            return self.running
        reading = type(reading)(reading.cpu, reading.memory, reading.temperature, self.wifi_outages)
        if self.health_status.get_text() != reading.text:
            self.health_status.set_text(reading.text)
        context = self.health_status.get_style_context()
        for level in ("health-good", "health-warn", "health-hot"):
            context.remove_class(level)
        context.add_class("health-" + reading.level)
        return self.running

    def start_wifi_count_update(self) -> bool:
        def worker() -> None:
            GLib.idle_add(self.set_wifi_count, read_outage_count())

        threading.Thread(target=worker, name="wifi-outage-count", daemon=True).start()
        return self.running

    def set_wifi_count(self, count: int | None) -> bool:
        self.wifi_outages = count
        self.update_health()
        return False

    def start_map_update_once(self) -> bool:
        self.start_map_update()
        return False

    def start_map_update(self) -> bool:
        if not self.running:
            return False
        width = max(1, self.radar_area.get_allocated_width())
        height = max(1, self.radar_area.get_allocated_height())

        def worker() -> None:
            tiles = fetch_visible_tiles(width, height)
            GLib.idle_add(self.set_map_tiles, tiles)

        threading.Thread(target=worker, name="osm-map", daemon=True).start()
        return self.running

    def set_map_tiles(self, tiles: dict[tuple[int, int], bytes]) -> bool:
        if not self.running:
            return False
        changed = False
        for coordinates, png in tiles.items():
            if self.map_tile_data.get(coordinates) == png:
                continue
            loader = GdkPixbuf.PixbufLoader.new_with_type("png")
            try:
                loader.write(png)
                loader.close()
                self.map_tiles[coordinates] = loader.get_pixbuf()
                self.map_tile_data[coordinates] = png
                changed = True
            except GLib.Error as error:
                print(f"Kartenkachel {coordinates} ungültig: {error}", flush=True)
        if changed and self.map_tiles:
            x0 = min(x for x, _ in self.map_tiles)
            x1 = max(x for x, _ in self.map_tiles)
            y0 = min(y for _, y in self.map_tiles)
            y1 = max(y for _, y in self.map_tiles)
            mosaic = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8,
                                          (x1 - x0 + 1) * TILE_PIXELS,
                                          (y1 - y0 + 1) * TILE_PIXELS)
            mosaic.fill(0xe6f2faff)
            for (x, y), tile in self.map_tiles.items():
                tile.copy_area(0, 0, min(TILE_PIXELS, tile.get_width()),
                               min(TILE_PIXELS, tile.get_height()), mosaic,
                               (x - x0) * TILE_PIXELS, (y - y0) * TILE_PIXELS)
            self.map_mosaic = (x0, y0, mosaic)
        if changed and self.display_on:
            self.radar_area.queue_draw()
        return False

    def start_wind_update_once(self) -> bool:
        self.start_wind_update()
        return False

    def warm_wind_update(self) -> bool:
        if self.wind_next_batch < WIND_BATCH_COUNT:
            self.start_wind_update()
        return self.running and self.wind_next_batch < WIND_BATCH_COUNT

    def retry_missing_wind(self) -> bool:
        if self.wind_next_batch < WIND_BATCH_COUNT or self.wind_field is not None:
            return self.running and self.wind_field is None
        for batch_index, timestamp in enumerate(self.wind_sample_times):
            if not timestamp:
                self.start_wind_update(batch_index)
                break
        return self.running and self.wind_field is None

    def start_wind_update(self, batch_index: int | None = None) -> bool:
        if self.wind_fetching or not self.running:
            return self.running
        self.wind_fetching = True
        if batch_index is None:
            batch_index = self.wind_next_batch % WIND_BATCH_COUNT
            self.wind_next_batch += 1
        width = max(1, self.radar_area.get_allocated_width())
        height = max(1, self.radar_area.get_allocated_height())

        def worker() -> None:
            try:
                batch = fetch_wind_batch(width, height, batch_index)
            except Exception as error:
                print(f"Wind-Aktualisierung fehlgeschlagen: {error}", flush=True)
                GLib.idle_add(self.set_wind_error)
            else:
                GLib.idle_add(self.set_wind_batch, batch_index, batch, width, height)

        threading.Thread(target=worker, name="wind-field", daemon=True).start()
        return self.running

    def set_wind_batch(self, batch_index: int, batch: WindField, width: int, height: int) -> bool:
        now = time.monotonic()
        self.wind_sample_times[batch_index] = now
        self.wind_model_time = max(self.wind_model_time, batch.model_time)
        field = merge_wind_batch(self.wind_samples, batch_index, batch, width, height, self.wind_model_time)
        if field is not None:
            self.wind_field = field
            self.wind_received_at = min(self.wind_sample_times)
        self.wind_failed = False
        self.wind_fetching = False
        self.update_wind_status()
        if self.display_on and field is not None:
            self.wind_area.queue_draw()
        return False

    def set_wind_error(self) -> bool:
        self.wind_failed = True
        self.wind_fetching = False
        self.update_wind_status()
        if self.display_on:
            self.wind_area.queue_draw()
        return False

    def wind_is_stale(self) -> bool:
        return ((self.wind_field is None and self.wind_failed) or
                (self.wind_field is not None and time.monotonic() - self.wind_received_at > 30 * 60))

    def update_wind_status(self) -> bool:
        if not self.display_on:
            return self.running
        stale = self.wind_is_stale()
        if self.wind_field is None:
            status = "WIND NICHT VERFÜGBAR" if stale else "WIND LÄDT …"
        else:
            clock = self.wind_field.model_time[-5:]
            status = f"WIND ALT · {clock}" if stale else f"WIND · {clock}"
        self.wind_status_label.set_text(status)
        context = self.wind_status_label.get_style_context()
        context.remove_class("radar-stale")
        if stale:
            context.add_class("radar-stale")
        return self.running

    def animate_wind(self) -> bool:
        if self.display_on and self.wind_field is not None and not self.wind_is_stale():
            self.wind_area.queue_draw()
        return self.running

    def start_display_power_check(self) -> bool:
        if self.display_power_checking or not self.running:
            return self.running
        self.display_power_checking = True

        def worker() -> None:
            try:
                result = subprocess.run(["vcgencmd", "display_power"], capture_output=True,
                                        text=True, timeout=2, check=True)
                is_on = result.stdout.strip() == "display_power=1"
            except (OSError, subprocess.SubprocessError):
                is_on = True
            GLib.idle_add(self.set_display_power, is_on)

        threading.Thread(target=worker, name="display-power", daemon=True).start()
        return self.running

    def set_display_power(self, is_on: bool) -> bool:
        was_on = self.display_on
        self.display_on = is_on
        self.display_power_checking = False
        if is_on and not was_on:
            self.last_departures.clear()
            self.last_weather = None
            self.update_view()
            self.update_health()
            self.update_ticker()
            self.update_radar_status()
            self.update_wind_status()
            self.radar_area.queue_draw()
            self.wind_area.queue_draw()
        return False

    def start_radar_update(self) -> bool:
        if self.radar_fetching or not self.running:
            return self.running
        self.radar_fetching = True
        width = max(1, self.radar_area.get_allocated_width())
        height = max(1, self.radar_area.get_allocated_height())
        known_timestamp = self.radar_frame_time

        def worker() -> None:
            try:
                frame = fetch_latest_radar(known_timestamp, width, height)
            except Exception as error:
                print(f"Radar-Aktualisierung fehlgeschlagen: {error}", flush=True)
                GLib.idle_add(self.set_radar_error)
            else:
                GLib.idle_add(self.set_radar_frame, frame)

        threading.Thread(target=worker, name="rain-radar", daemon=True).start()
        return self.running

    def set_radar_frame(self, frame: RadarFrame) -> bool:
        try:
            if frame.tiles is not None:
                if not frame.tiles:
                    raise ValueError("Keine Radarkacheln für den Kartenausschnitt")
                tiles = {}
                for coordinates, png in frame.tiles.items():
                    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
                    loader.write(png)
                    loader.close()
                    pixbuf = loader.get_pixbuf()
                    if (pixbuf.get_width() != RADAR_PIXELS or pixbuf.get_height() != RADAR_PIXELS
                            or pixbuf.get_n_channels() != 4):
                        raise ValueError(f"Ungültige Radarkachel {coordinates}")
                    pixels = hide_weak_echoes(pixbuf.get_pixels(), pixbuf.get_width(),
                                              pixbuf.get_height(), pixbuf.get_rowstride())
                    tiles[coordinates] = GdkPixbuf.Pixbuf.new_from_bytes(
                        GLib.Bytes.new(pixels), GdkPixbuf.Colorspace.RGB, True, 8,
                        pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride(),
                    )
                x0 = min(x for x, _ in tiles)
                x1 = max(x for x, _ in tiles)
                y0 = min(y for _, y in tiles)
                y1 = max(y for _, y in tiles)
                mosaic = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8,
                                              (x1 - x0 + 1) * RADAR_PIXELS,
                                              (y1 - y0 + 1) * RADAR_PIXELS)
                mosaic.fill(0)
                for (x, y), tile in tiles.items():
                    tile.copy_area(0, 0, RADAR_PIXELS, RADAR_PIXELS, mosaic,
                                   (x - x0) * RADAR_PIXELS, (y - y0) * RADAR_PIXELS)
                view_width = self.radar_area.get_allocated_width()
                view_height = self.radar_area.get_allocated_height()
                left, top, scale = radar_placement(x0, y0, view_width, view_height)
                self.radar_no_rain = not has_visible_rain(
                    mosaic.get_pixels(), mosaic.get_width(), mosaic.get_height(), mosaic.get_rowstride(),
                    math.floor(-left / scale), math.floor(-top / scale),
                    math.ceil((view_width - left) / scale), math.ceil((view_height - top) / scale),
                )
                self.radar_mosaic = (x0, y0, mosaic)
                self.radar_frame_time = frame.timestamp
                if self.display_on:
                    self.radar_area.queue_draw()
            self.radar_failed = False
        except (GLib.Error, ValueError) as error:
            print(f"Radarbild konnte nicht geöffnet werden: {error}", flush=True)
            self.radar_failed = True
        self.radar_fetching = False
        self.update_radar_status()
        return False

    def set_radar_error(self) -> bool:
        self.radar_failed = True
        self.radar_fetching = False
        self.update_radar_status()
        return False

    def update_radar_status(self) -> bool:
        if not self.display_on:
            return self.running
        if self.demo:
            status, stale = "VORSCHAU · kein Live-Radar", True
        else:
            status, stale = radar_status(self.radar_frame_time, self.radar_failed)
        self.radar_status_label.set_text(status)
        context = self.radar_status_label.get_style_context()
        context.remove_class("radar-stale")
        if stale:
            context.add_class("radar-stale")
        if self.radar_no_rain and not stale:
            self.radar_clear_label.show()
        else:
            self.radar_clear_label.hide()
        return self.running

    def update_weather(self, weather: dict) -> None:
        self.temperature.set_text(weather.get("now", "–"))
        comparison = weather.get("comparison", "")
        advice = weather.get("advice", "")
        self.weather_advice.set_text("   ·   ".join(filter(None, (comparison, f"Tipp: {advice}" if advice else ""))))
        yesterday_feels = weather.get("yesterday_feels")
        self.weather_advice.set_tooltip_text(
            f"Gestern zur gleichen Stunde gefühlt: {yesterday_feels:.0f} °C" if yesterday_feels is not None else None
        )
        advice_context = self.weather_advice.get_style_context()
        for name in ("comparison-warmer", "comparison-cooler", "comparison-same", "advice-rain", "advice-maybe"):
            advice_context.remove_class(name)
        umbrella = weather.get("umbrella")
        if umbrella == "mitnehmen":
            advice_context.add_class("advice-rain")
        elif umbrella == "erwägen":
            advice_context.add_class("advice-maybe")
        else:
            advice_context.add_class("comparison-" + weather.get("comparison_direction", "same"))
        self.feels.set_text(weather.get("feels", "Daten werden geladen"))
        self.condition.set_text(weather.get("description", ""))
        self.wind.set_text(weather.get("wind", ""))
        self.weather_icon.queue_draw()
        for widgets, outlook in zip(self.outlook_boxes, weather.get("outlooks", [])):
            heading, value, rain = widgets
            heading.set_text(f"+{outlook['hours']} h · {outlook['time']}")
            value.set_text(f"{outlook['temperature']} °C  ·  {weather_description(outlook['code'])}")
            rain.set_text(f"Regenrisiko {outlook['rain_probability']} %")

    def update_ticker(self) -> bool:
        if not self.display_on:
            return self.running
        with self.lock:
            alerts = self.snapshot[2]
        if not alerts:
            next_text = "Zurzeit keine RSAG-Meldungen"
        else:
            message = "     •     ".join(alerts)
            gap = "                  "
            offset = int((time.monotonic() - self.start_time) * 2) % (len(message) + len(gap))
            cycle = message + gap
            next_text = cycle[offset:] + cycle[:offset]
        if self.ticker_text.get_text() != next_text:
            self.ticker_text.set_text(next_text)
        return self.running

    def draw_weather_icon(self, area: Gtk.DrawingArea, cr: cairo.Context) -> bool:
        with self.lock:
            code = self.snapshot[1].get("code", 3)
        allocation = area.get_allocation()
        cr.save()
        scale = min(allocation.width / 160, allocation.height / 150)
        cr.translate((allocation.width - 160 * scale) / 2, (allocation.height - 150 * scale) / 2)
        cr.scale(scale, scale)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        if code in (0, 1):
            cr.set_source_rgb(0.97, 0.65, 0.09)
            cr.arc(80, 75, 32, 0, math.tau)
            cr.fill()
            cr.set_line_width(6)
            for index in range(8):
                angle = index * math.pi / 4
                cr.move_to(80 + math.cos(angle) * 43, 75 + math.sin(angle) * 43)
                cr.line_to(80 + math.cos(angle) * 55, 75 + math.sin(angle) * 55)
                cr.stroke()
        else:
            if code == 2:
                cr.set_source_rgb(0.98, 0.69, 0.13)
                cr.arc(63, 55, 25, 0, math.tau)
                cr.fill()
            cr.set_source_rgb(0.42, 0.61, 0.76)
            cr.arc(59, 77, 24, math.pi, math.tau)
            cr.arc(88, 65, 31, math.pi, math.tau)
            cr.arc(115, 78, 22, math.pi, math.tau)
            cr.line_to(137, 96)
            cr.line_to(35, 96)
            cr.close_path()
            cr.fill()
            if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99):
                cr.set_source_rgb(0.07, 0.40, 0.73)
                cr.set_line_width(6)
                for x in (60, 89, 118):
                    cr.move_to(x, 109)
                    cr.line_to(x - 7, 126)
                    cr.stroke()
            elif code in (71, 73, 75, 77, 85, 86):
                cr.set_source_rgb(0.09, 0.44, 0.72)
                for x in (58, 88, 118):
                    cr.arc(x, 118, 4, 0, math.tau)
                    cr.fill()
        cr.restore()
        return False

    def draw_radar(self, area: Gtk.DrawingArea, cr: cairo.Context) -> bool:
        allocation = area.get_allocation()
        width, height = allocation.width, allocation.height
        radius = 16
        cr.new_sub_path()
        cr.arc(width - radius, radius, radius, -math.pi / 2, 0)
        cr.arc(width - radius, height - radius, radius, 0, math.pi / 2)
        cr.arc(radius, height - radius, radius, math.pi / 2, math.pi)
        cr.arc(radius, radius, radius, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.clip()
        cr.set_source_rgb(0.91, 0.95, 0.98)
        cr.paint()
        centre_x, centre_y = world_pixel(MAP_CENTER_LON, MAP_CENTER_LAT)
        scale = width / VIEW_PIXELS
        if self.map_mosaic is not None:
            tile_x, tile_y, pixbuf = self.map_mosaic
            cr.save()
            cr.translate(width / 2 + (tile_x * TILE_PIXELS - centre_x) * scale,
                         height / 2 + (tile_y * TILE_PIXELS - centre_y) * scale)
            cr.scale(scale, scale)
            Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
            cr.paint()
            cr.restore()

        def boundary(name: str, red: float, green: float, blue: float,
                     line_width: float, fill_alpha: float) -> None:
            polygon = self.map_boundaries[name]
            for ring in polygon["coordinates"]:
                for index, (lon, lat) in enumerate(ring):
                    x, y = map_point(lon, lat, width, height)
                    if index == 0:
                        cr.move_to(x, y)
                    else:
                        cr.line_to(x, y)
                cr.close_path()
            if fill_alpha:
                cr.set_source_rgba(red, green, blue, fill_alpha)
                cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 0.92)
            cr.set_line_width(line_width + 2)
            cr.stroke_preserve()
            cr.set_source_rgba(red, green, blue, 0.88)
            cr.set_line_width(line_width)
            cr.stroke()

        if self.radar_mosaic is not None:
            tile_x, tile_y, pixbuf = self.radar_mosaic
            left, top, scale = radar_placement(tile_x, tile_y, width, height)
            cr.save()
            cr.translate(left, top)
            cr.scale(scale, scale)
            Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
            cr.paint()
            cr.restore()
        boundary("rostock", 0.77, 0.14, 0.23, 2.5, 0.085)
        if HOME_MARKER is not None:
            home_x, home_y = map_point(HOME_MARKER[1], HOME_MARKER[0], width, height)
            cr.arc(home_x, home_y, 6.5, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.96)
            cr.fill()
            cr.arc(home_x, home_y, 4.5, 0, 2 * math.pi)
            cr.set_source_rgb(0.9, 0.08, 0.13)
            cr.fill()
        return False

    def draw_wind(self, area: Gtk.DrawingArea, cr: cairo.Context) -> bool:
        if self.wind_field is None:
            return False
        width = area.get_allocated_width()
        height = area.get_allocated_height()
        stale = self.wind_is_stale()
        phase = time.monotonic()
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        for index, sample in enumerate(self.wind_field.samples):
            x, y = map_point(sample.lon, sample.lat, width, height)
            if not (18 < x < width - 18 and 18 < y < height - 18):
                continue
            dx, dy = wind_to_vector(sample.direction)
            length = min(49, max(30, 30 + sample.speed * 0.5))
            x0, y0 = x - dx * length / 2, y - dy * length / 2
            x1, y1 = x + dx * length / 2, y + dy * length / 2
            colour = wind_color(sample.speed)
            alpha = 0.42 if stale else 0.92

            def arrow_path() -> None:
                cr.move_to(x0, y0)
                cr.line_to(x1, y1)
                cr.move_to(x1 - dx * 9 - dy * 6, y1 - dy * 9 + dx * 6)
                cr.line_to(x1, y1)
                cr.line_to(x1 - dx * 9 + dy * 6, y1 - dy * 9 - dx * 6)

            arrow_path()
            cr.set_source_rgba(1, 1, 1, alpha)
            cr.set_line_width(6)
            cr.stroke()
            arrow_path()
            cr.set_source_rgba(*colour, alpha)
            cr.set_line_width(3)
            cr.stroke()

            if not stale and sample.speed >= 2:
                for offset in (0, 0.5):
                    progress = (phase * (0.16 + sample.speed / 120) + index * 0.13 + offset) % 1
                    px = x0 + dx * length * progress
                    py = y0 + dy * length * progress
                    cr.arc(px, py, 4.2, 0, math.tau)
                    cr.set_source_rgba(1, 1, 1, 0.98)
                    cr.fill()
                    cr.arc(px, py, 2.4, 0, math.tau)
                    cr.set_source_rgba(*colour, 0.98)
                    cr.fill()
        return False

    def on_key(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.keyval in (Gdk.KEY_q, Gdk.KEY_Q, Gdk.KEY_Escape):
            self.close()
            return True
        return False

    def on_destroy(self, *_args: object) -> None:
        self.running = False
        Gtk.main_quit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Show representative data without network access")
    parser.add_argument("--demo-offline", action="store_true", help="Show the offline warning with sample data")
    args = parser.parse_args()
    Monitor(demo=args.demo or args.demo_offline, demo_offline=args.demo_offline)
    Gtk.main()


if __name__ == "__main__":
    main()
