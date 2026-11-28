import socket
import ssl
import threading
import time
import unittest
from unittest.mock import Mock, patch

from mind import weather


SYDNEY_RESULTS = {
    "results": [
        {"name": "Sydney", "latitude": -33.87, "longitude": 151.21,
         "country": "Australia", "country_code": "AU", "admin1": "New South Wales"},
        {"name": "Sydney", "latitude": 46.14, "longitude": -60.18,
         "country": "Canada", "country_code": "CA", "admin1": "Nova Scotia"},
    ],
}

FORECAST = {
    "current": {"time": "2026-11-21T15:45", "temperature_2m": 21.1, "weather_code": 2},
    "daily": {
        "time": ["2026-11-21", "2026-11-22"],
        "weather_code": [3, 61],
        "temperature_2m_max": [25, 19],
        "temperature_2m_min": [13, -2],
        "precipitation_probability_max": [0, 80],
    },
}


def fake_lookup(host, path, params, timeout):
    if host == weather.GEOCODER:
        return SYDNEY_RESULTS if params["name"].casefold() == "sydney" else {}
    if host == weather.FORECAST:
        return FORECAST
    raise AssertionError(f"Unexpected request to {host}")


class WeatherTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_location_never_uses_network(self):
        fetch = Mock()
        skill = weather.Weather(fetch_json=fetch)
        self.assertEqual(await skill.reply(), weather.NO_LOCATION)
        self.assertEqual(await skill.reply(location="https://example.com"), weather.NO_LOCATION)
        fetch.assert_not_called()

    async def test_disabled_lookup_never_dispatches_a_fetch(self):
        fetch = Mock()
        skill = weather.Weather("Sydney", fetch_json=fetch, permitted=lambda: False)
        with self.assertRaises(weather.LookupDenied):
            await skill.reply()
        fetch.assert_not_called()

    async def test_worker_rechecks_permission_before_fetching(self):
        fetch = Mock()
        caller = threading.current_thread()
        skill = weather.Weather("Sydney", fetch_json=fetch, permitted=lambda: threading.current_thread() is caller)
        with self.assertRaises(weather.LookupDenied):
            await skill.reply()
        fetch.assert_not_called()

    async def test_revoked_lookup_never_fetches_the_forecast(self):
        allowed = True

        def get_place(*args):
            nonlocal allowed
            allowed = False
            return fake_lookup(*args)

        fetch = Mock(side_effect=get_place)
        skill = weather.Weather("Sydney", fetch_json=fetch, permitted=lambda: allowed)
        with self.assertRaises(weather.LookupDenied):
            await skill.reply()
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], weather.GEOCODER)

    async def test_current_uses_configured_city_and_location_local_time(self):
        fetch = Mock(side_effect=fake_lookup)
        skill = weather.Weather("Sydney, Australia", fetch_json=fetch)
        self.assertEqual(
            await skill.reply(),
            "Open-Meteo reports 21 degrees Celsius with partly cloudy skies in Sydney, Australia at 3:45 PM local time.",
        )
        host, path, query, timeout = fetch.call_args_list[0].args
        self.assertEqual((host, path, query["name"], timeout),
                         (weather.GEOCODER, "/v1/search", "Sydney", weather.REQUEST_TIMEOUT))
        host, path, query, _ = fetch.call_args.args
        self.assertEqual((host, path, query["latitude"], query["longitude"]),
                         (weather.FORECAST, "/v1/forecast", -33.87, 151.21))
        self.assertEqual(query["timezone"], "auto")
        self.assertNotIn("name", query)

    async def test_today_and_tomorrow_use_the_correct_day_and_reuse_geocode(self):
        fetch = Mock(side_effect=fake_lookup)
        skill = weather.Weather("Sydney", "fahrenheit", fetch_json=fetch)
        today = await skill.reply("today")
        tomorrow = await skill.reply("tomorrow")
        self.assertIn("overcast skies", today)
        self.assertIn("today, Saturday.", today)
        self.assertIn("light rain", tomorrow)
        self.assertIn("a high of 19 and a low of minus 2 degrees Fahrenheit", tomorrow)
        self.assertIn("tomorrow, Sunday. The chance of precipitation is up to 80 percent.", tomorrow)
        self.assertEqual([call.args[0] for call in fetch.call_args_list],
                         [weather.GEOCODER, weather.FORECAST, weather.FORECAST])
        params = fetch.call_args.args[2]
        self.assertEqual(params["forecast_days"], 2)
        self.assertEqual(params["temperature_unit"], "fahrenheit")

    async def test_spoken_qualifier_can_override_the_default_location(self):
        fetch = Mock(side_effect=fake_lookup)
        skill = weather.Weather("Sydney, Australia", fetch_json=fetch)
        self.assertIn("Sydney, Canada", await skill.reply(location="Sydney Canada"))
        self.assertEqual(fetch.call_args.args[2]["latitude"], 46.14)
        self.assertIn("Sydney, Nova Scotia, Canada", await skill.reply(location="Sydney, NS, CA"))

    async def test_unmatched_place_does_not_fetch_a_forecast(self):
        fetch = Mock(side_effect=fake_lookup)
        skill = weather.Weather(fetch_json=fetch)
        self.assertIn("couldn't find", await skill.reply(location="Sydney, France"))
        self.assertEqual(fetch.call_count, 1)

    async def test_network_and_invalid_data_fail_gracefully(self):
        for fetch in (
            Mock(side_effect=OSError("The network is down")),
            Mock(side_effect=[SYDNEY_RESULTS, {}]),
        ):
            with self.subTest(fetch=fetch):
                skill = weather.Weather("Sydney", fetch_json=fetch)
                self.assertEqual(await skill.reply(), weather.UNAVAILABLE)

    async def test_overall_timeout_bounds_the_reply(self):
        unblock = threading.Event()

        def slow_fetch(*args):
            unblock.wait(1)
            return SYDNEY_RESULTS

        try:
            with patch.object(weather, "LOOKUP_TIMEOUT", 0.02):
                skill = weather.Weather("Sydney", fetch_json=slow_fetch)
                self.assertEqual(await skill.reply(), weather.UNAVAILABLE)
        finally:
            unblock.set()


class WeatherHTTPTest(unittest.TestCase):
    def test_fetch_rejects_redirects_and_overlong_responses(self):
        for status, body in ((302, b""), (200, b"x" * (weather.MAX_RESPONSE + 1))):
            with self.subTest(status=status), patch.object(weather, "_PublicHTTPSConnection") as connect:
                connection = connect.return_value
                response = connection.getresponse.return_value.__enter__.return_value
                response.status = status
                response.read.return_value = body
                with self.assertRaises(ValueError):
                    weather._fetch_json(weather.GEOCODER, "/v1/search", {"name": "Sydney"}, 2.5)
                connect.assert_called_once()
                self.assertEqual(connect.call_args.args, (weather.GEOCODER,))
                connection.close.assert_called_once()

    def test_connects_only_to_validated_sockaddr_and_verifies_original_hostname(self):
        for family, public in (
            (socket.AF_INET, ("1.1.1.1", 443)),
            (socket.AF_INET6, ("2606:4700:4700::1111", 443, 0, 0)),
        ):
            with self.subTest(public=public):
                records = [
                    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
                    (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", public),
                ]
                with patch.object(socket, "getaddrinfo", return_value=records) as resolve:
                    with patch.object(socket, "socket") as new_socket:
                        connection = weather._PublicHTTPSConnection(
                            weather.GEOCODER, deadline=time.monotonic() + 2.5, permitted=None,
                        )
                        self.assertTrue(connection.tls.check_hostname)
                        self.assertEqual(connection.tls.verify_mode, ssl.CERT_REQUIRED)
                        with patch.object(connection.tls, "wrap_socket") as wrap:
                            connection.connect()
                        resolve.assert_called_once_with(
                            weather.GEOCODER, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
                        )
                        new_socket.assert_called_once_with(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
                        new_socket.return_value.connect.assert_called_once_with(public)
                        wrap.assert_called_once_with(new_socket.return_value, server_hostname=weather.GEOCODER)
                        connection.close()

    def test_private_and_special_use_destinations_never_open_a_socket(self):
        ipv4 = ("10.0.0.7", "100.64.0.2", "169.254.169.254", "192.0.2.1", "224.0.0.1")
        ipv6 = (
            "::1", "::ffff:127.0.0.1", "fc00::1", "fe80::1", "fec0::1", "ff02::1",
            "2001:db8::1", "2606:4700:4700::1111%en0", "1.1.1.1",
        )
        records = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443)) for ip in ipv4
        ] + [
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443, 0, 0)) for ip in ipv6
        ]
        with patch.object(socket, "getaddrinfo", return_value=records), patch.object(socket, "socket") as new_socket:
            with self.assertRaisesRegex(OSError, "no public address"):
                weather._fetch_json(weather.FORECAST, "/v1/forecast", {}, 2.5)
            new_socket.assert_not_called()

    def test_unapproved_endpoint_never_creates_a_connection(self):
        with patch.object(socket, "getaddrinfo") as resolve:
            with self.assertRaisesRegex(ValueError, "Unknown public lookup endpoint"):
                weather._fetch_json("example.com", "/v1/forecast", {}, 2.5)
            resolve.assert_not_called()

    def test_deadline_interrupts_a_slow_full_response(self):
        interrupted = threading.Event()

        def read(*args):
            if not interrupted.wait(1):
                self.fail("The response deadline did not shut down the socket")
            raise OSError("Socket interrupted")

        with patch.object(weather, "_PublicHTTPSConnection") as connect:
            connection = connect.return_value
            response = connection.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.read.side_effect = read
            connection.sock.shutdown.side_effect = lambda how: interrupted.set()
            with self.assertRaisesRegex(OSError, "Socket interrupted"):
                weather._fetch_json(weather.FORECAST, "/v1/forecast", {}, 0.05)
            connection.sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
            connection.close.assert_called_once()

    def test_revoked_while_resolving_never_opens_a_socket(self):
        allowed = True

        def resolve(*args, **kwargs):
            nonlocal allowed
            allowed = False
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443))]

        with patch.object(socket, "getaddrinfo", side_effect=resolve), patch.object(socket, "socket") as new_socket:
            with self.assertRaises(weather.LookupDenied):
                weather._fetch_json(weather.FORECAST, "/v1/forecast", {}, 2.5, permitted=lambda: allowed)
            new_socket.assert_not_called()
