import unittest

from dashboard import (
    AlertParser, DashboardState, Departure, DepartureParser, STOPS, departure_details, occupancy_indicator,
    parse_weather, ticker_text, weather_advice, weather_comparison, weather_description, weather_symbol,
)


class DashboardTests(unittest.TestCase):
    def test_parses_rsag_fragment(self):
        parser = DepartureParser()
        parser.feed('''<div class="entry clearfix tdi"><div class="line">1</div>
        <div class="destination"><div class="direction">Mecklenburger Allee</div></div>
        <div class="departure">9 min</div><div class="platform">A</div></div>''')
        self.assertEqual(len(parser.entries), 1)
        self.assertEqual(parser.entries[0].line, "1")
        self.assertEqual(parser.entries[0].direction, "Mecklenburger Allee")
        self.assertEqual(parser.entries[0].departure, "9 min")
        self.assertEqual(parser.entries[0].platform, "A")

    def test_parses_per_departure_extras_without_mixing_fields(self):
        parser = DepartureParser()
        parser.feed('''<div class="entry clearfix tdi">
            <div class="icon bus">&nbsp;</div><div class="line">25</div>
            <div class="destination clearfix">
              <div class="direction">Thomas-Morus-Str.</div>
              <div class="via"><div class="scrollable">via Hauptbahnhof</div></div>
              <div class="alert"><div class="scrollable">Ersatzhaltestelle am Platz</div></div>
            </div>
            <div class="occupancy high">&nbsp;</div>
            <div class="delay">+2 min</div>
            <div class="departure">12 min</div><div class="platform">G</div>
        </div>''')
        item = parser.entries[0]
        self.assertEqual(item.direction, "Thomas-Morus-Str.")
        self.assertEqual(item.via, "via Hauptbahnhof")
        self.assertEqual(item.note, "Ersatzhaltestelle am Platz")
        self.assertEqual(item.occupancy, "high")
        self.assertEqual(item.delay, "+2 min")
        self.assertEqual(item.mode, "bus")
        self.assertEqual(departure_details(item), "via Hauptbahnhof  ·  Ersatzhaltestelle am Platz")
        self.assertEqual(occupancy_indicator(item.occupancy), "●●●")

    def test_cancellation_from_service_message(self):
        parser = DepartureParser()
        parser.feed('''<div class="entry tdi"><div class="line">5</div>
            <div class="destination"><div class="direction">Südblick</div>
            <div class="alert">Fahrt fällt aus</div></div>
            <div class="departure">18 min</div><div class="platform">B</div></div>''')
        self.assertTrue(parser.entries[0].cancelled)
        self.assertEqual(parser.entries[0].note, "Fahrt fällt aus")

    def test_offline_keeps_old_data_and_recovers_automatically(self):
        old = {"14": [Departure("1", "Alt", "5 min", "A")]}
        fresh = {"14": [Departure("1", "Neu", "2 min", "A")]}
        state = DashboardState(departures=old)

        def unavailable():
            raise OSError("network down")

        state.refresh(1000, {"departures": unavailable, "weather": unavailable, "alerts": unavailable})
        self.assertIs(state.departures, old)
        self.assertIn("OFFLINE", state.warning())

        calls = []
        producers = {
            "departures": lambda: calls.append("departures") or fresh,
            "weather": lambda: calls.append("weather") or {"now": "12 °C"},
            "alerts": lambda: calls.append("alerts") or ["Meldung"],
        }
        state.refresh(1005, producers)
        self.assertEqual(calls, [])
        self.assertIn("OFFLINE", state.warning())

        state.refresh(1015, producers)
        self.assertEqual(calls, ["departures", "weather", "alerts"])
        self.assertIs(state.departures, fresh)
        self.assertEqual(state.warning(), "")

    def test_known_weather_code_is_german(self):
        self.assertEqual(weather_description(61), "Regen")
        self.assertEqual(weather_symbol(0), "☀")
        self.assertEqual(weather_symbol(61), "☂")

    def test_weather_outlook_and_umbrella_advice(self):
        data = {
            "current": {"time": "2026-10-08T10:15", "temperature_2m": 12.2,
                        "apparent_temperature": 8.7, "weather_code": 3,
                        "wind_speed_10m": 22.4, "wind_gusts_10m": 34.2},
            "daily": {"time": ["2026-10-07", "2026-10-08"],
                      "temperature_2m_min": [4.0, 9.0], "temperature_2m_max": [10.0, 15.0]},
            "hourly": {
                "time": ["2026-10-07T10:00"] + [f"2026-10-08T{hour:02d}:00" for hour in range(10, 18)],
                "temperature_2m": [9.0, 12.0, 12.0, 13.0, 14.0, 15.0, 14.0, 12.0, 10.0],
                "apparent_temperature": [4.0, 9.0, 9.0, 10.0, 11.0, 12.0, 11.0, 9.0, 7.0],
                "weather_code": [3, 3, 3, 2, 1, 0, 61, 61, 3],
                "precipitation_probability": [0, 0, 0, 10, 20, 30, 65, 60, 20],
                "precipitation": [0, 0, 0, 0, 0, 0, 0.6, 0.2, 0],
            },
        }
        result = parse_weather(data)
        self.assertEqual(result["feels"], "gefühlt 9 °C")
        self.assertIn("14:00", result["plus_3h"])
        self.assertIn("17:00", result["plus_6h"])
        self.assertEqual(result["umbrella"], "mitnehmen")
        self.assertEqual(result["rain_risk"], 65)
        self.assertEqual(result["wind_speed"], 22)
        self.assertEqual(result["wind_gusts"], 34)
        self.assertEqual(result["advice"], "Schirm mitnehmen · Jacke")
        self.assertEqual(result["range"], "9–15 °C")
        self.assertEqual(result["comparison"], "Heute leichter anziehen")
        self.assertEqual(result["yesterday_temperature"], 9.0)
        self.assertEqual(result["yesterday_feels"], 4.0)

    def test_yesterday_comparison_handles_cooler_same_and_missing(self):
        self.assertEqual(weather_comparison(6.0, 9.0), ("Heute wärmer anziehen", "cooler"))
        self.assertEqual(weather_comparison(11.9, 9.0), ("Kleidung wie gestern", "same"))
        self.assertEqual(weather_comparison(12.0, 9.0), ("Heute leichter anziehen", "warmer"))
        self.assertEqual(weather_comparison(9.0, None), ("", "same"))

    def test_weather_advice_accounts_for_cold_and_strong_wind(self):
        self.assertEqual(weather_advice(2, 33, 50, "mitnehmen"),
                         "Regenjacke statt Schirm · Mütze & Schal · Windjacke")
        self.assertEqual(weather_advice(19, 10, 15, "eher nicht"), "Keine Extras nötig")

    def test_alert_fragment(self):
        parser = AlertParser()
        parser.feed('<div class="alert"><div class="scrollable">Umleitung Linien 45 &amp; F2</div></div>')
        self.assertEqual(parser.messages, ["Umleitung Linien 45 & F2"])

    def test_ticker_scrolls_long_message(self):
        message = "Umleitung Linien 45 und F2 in beide Richtungen"
        self.assertNotEqual(ticker_text([message], 20, 0), ticker_text([message], 20, 2))

    def test_expected_stops_are_configured(self):
        self.assertEqual(STOPS, {"14": "Doberaner Platz", "15": "Schröderplatz", "179": "Deutsche-Med-Platz"})


if __name__ == "__main__":
    unittest.main()
