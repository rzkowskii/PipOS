"""Read-only, on-demand Open-Meteo lookup for a named city."""

import asyncio
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlencode

GEOCODER = "geocoding-api.open-meteo.com"
FORECAST = "api.open-meteo.com"
REQUEST_TIMEOUT = 2.5
LOOKUP_TIMEOUT = 5
MAX_RESPONSE = 65536
NO_LOCATION = "What city should I check? Try asking for the weather in Sydney."
UNAVAILABLE = "I couldn't get the weather from Open-Meteo just now."

CONDITIONS = {
    0: "clear skies", 1: "mostly clear skies", 2: "partly cloudy skies", 3: "overcast skies",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


class LookupDenied(Exception):
    """Owner policy currently disallows an Internet lookup."""


def _require_permission(permitted):
    if permitted is not None and not permitted():
        raise LookupDenied


def normalize_location(value):
    """Accept a city with optional comma-separated region and country."""
    if not isinstance(value, str):
        return ""
    parts = [" ".join(part.split()) for part in unicodedata.normalize("NFC", value).split(",")]
    if not 1 <= len(parts) <= 3 or any(
        len(part) < 2 or not any(char.isalnum() for char in part) for part in parts
    ):
        return ""
    location = ", ".join(parts)
    if len(location) > 80 or not all(char.isalnum() or char in " .,-'’" for char in location):
        return ""
    return location


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("Weather connection timed out")
    return seconds


def _is_public(family, address):
    if family not in (socket.AF_INET, socket.AF_INET6):
        return False
    ip = ipaddress.ip_address(address[0])
    if family == socket.AF_INET:
        if ip.version != 4:
            return False
    else:
        if ip.version != 6 or address[3] or ip.scope_id is not None:
            return False
        if ip.is_site_local or ip.sixtofour is not None or ip.teredo is not None:
            return False
        ip = ip.ipv4_mapped or ip
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, deadline, permitted):
        self.tls = ssl.create_default_context()
        super().__init__(host, timeout=_remaining(deadline), context=self.tls)
        self.deadline = deadline
        self.permitted = permitted

    def connect(self):
        _require_permission(self.permitted)
        # The system resolver has its own timeout. A late result cannot dial.
        _remaining(self.deadline)
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        _require_permission(self.permitted)
        last_error = None
        for family, kind, proto, _, address in addresses:
            if not _is_public(family, address):
                continue
            _require_permission(self.permitted)
            remaining = _remaining(self.deadline)
            candidate = socket.socket(family, kind, proto)
            try:
                candidate.settimeout(remaining)
                # Connect to the vetted numeric sockaddr. The TLS certificate
                # and SNI still refer to the original provider hostname.
                candidate.connect(address)
                _require_permission(self.permitted)
                candidate.settimeout(_remaining(self.deadline))
                self.sock = self.tls.wrap_socket(candidate, server_hostname=self.host)
                return
            except OSError as exc:
                last_error = exc
            finally:
                if self.sock is None:
                    candidate.close()
        raise last_error or OSError("Weather host has no public address")


def _end_socket_io(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _fetch_json(host, path, params, timeout, *, permitted=None):
    if (host, path) not in ((GEOCODER, "/v1/search"), (FORECAST, "/v1/forecast")):
        raise ValueError("Unknown weather service endpoint")
    _require_permission(permitted)
    deadline = time.monotonic() + timeout
    connection = _PublicHTTPSConnection(host, deadline=deadline, permitted=permitted)
    watchdog = None
    try:
        connection.connect()
        # A peer sending small chunks could otherwise keep resetting the
        # socket's inactivity timeout during the HTTP response.
        watchdog = threading.Timer(_remaining(deadline), _end_socket_io, args=(connection.sock,))
        watchdog.daemon = True
        watchdog.start()
        _require_permission(permitted)
        connection.request("GET", f"{path}?{urlencode(params)}", headers={"Accept": "application/json"})
        with connection.getresponse() as response:
            if response.status != 200:
                raise ValueError("Weather service failed")
            body = response.read(MAX_RESPONSE + 1)
            _remaining(deadline)
            if len(body) > MAX_RESPONSE:
                raise ValueError("Weather response exceeded limit")
            return json.loads(body)
    finally:
        if watchdog is not None:
            watchdog.cancel()
            if watchdog.is_alive():
                watchdog.join()
        connection.close()


def _name_key(value):
    return "".join(
        char for char in unicodedata.normalize("NFKD", value.casefold()) if char.isalnum()
    )


def _number(value, minimum, maximum):
    if type(value) not in (int, float) or not minimum <= value <= maximum:
        raise ValueError("Invalid weather measurement")
    return value


def _temperature(value):
    degrees = round(_number(value, -160, 180))
    return f"minus {-degrees}" if degrees < 0 else str(degrees)


@dataclass(frozen=True)
class Place:
    label: str
    latitude: float
    longitude: float


def _matching_place(response, city, qualifiers):
    city_key = _name_key(city)
    requested = {_name_key(part) for part in qualifiers}
    for row in response.get("results", []):
        name = normalize_location(row.get("name"))
        if not name or _name_key(name) != city_key:
            continue
        country = normalize_location(row.get("country"))
        country_code = normalize_location(row.get("country_code"))
        region = normalize_location(row.get("admin1"))
        region_initials = "".join(word[0] for word in region.split())
        region_keys = {_name_key(region), _name_key(region_initials)}
        if not requested <= region_keys | {_name_key(country), _name_key(country_code)}:
            continue
        label = ", ".join(part for part in (name, region if requested & region_keys else "", country) if part)
        return Place(label, _number(row["latitude"], -90, 90), _number(row["longitude"], -180, 180))
    return None


class Weather:
    def __init__(self, default_location="", units="celsius", *, fetch_json=None, permitted=None):
        self.default_location = normalize_location(default_location)
        self.units = "fahrenheit" if units == "fahrenheit" else "celsius"
        self._fetch_json = fetch_json
        self._permitted = permitted
        self._places = {}

    def _fetch(self, host, path, params):
        _require_permission(self._permitted)
        if self._fetch_json is not None:
            return self._fetch_json(host, path, params, REQUEST_TIMEOUT)
        return _fetch_json(host, path, params, REQUEST_TIMEOUT, permitted=self._permitted)

    async def _get(self, host, path, **params):
        data = await asyncio.to_thread(self._fetch, host, path, params)
        if not isinstance(data, dict):
            raise ValueError("Invalid weather response")
        return data

    async def _geocode(self, city, qualifiers):
        response = await self._get(GEOCODER, "/v1/search", name=city, count=30, language="en", format="json")
        return _matching_place(response, city, qualifiers)

    async def _place(self, location):
        key = location.casefold()
        if key in self._places:
            return self._places[key]
        city, *qualifiers = location.split(", ")
        place = await self._geocode(city, qualifiers)
        if place is None and not qualifiers and " " in city:
            city, qualifier = city.rsplit(" ", 1)
            place = await self._geocode(city, [qualifier])
        if place is not None:
            if len(self._places) >= 8:
                del self._places[next(iter(self._places))]
            self._places[key] = place
        return place

    async def _report(self, period, location):
        place = await self._place(location)
        if place is None:
            return "I couldn't find that city. Try adding its state or country."
        params = {
            "latitude": place.latitude,
            "longitude": place.longitude,
            "temperature_unit": self.units,
            "timezone": "auto",
            "forecast_days": 1 if period != "tomorrow" else 2,
        }
        if period == "current":
            params["current"] = "temperature_2m,weather_code"
        else:
            params["daily"] = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        data = await self._get(FORECAST, "/v1/forecast", **params)
        unit = f"degrees {self.units.capitalize()}"
        if period == "current":
            current = data["current"]
            at = datetime.fromisoformat(current["time"])
            local_time = f"{at.hour % 12 or 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"
            temperature = _temperature(current["temperature_2m"])
            condition = CONDITIONS.get(current["weather_code"])
            conditions = f" with {condition}" if condition else ""
            return (
                f"Open-Meteo reports {temperature} {unit}{conditions} in {place.label} "
                f"at {local_time} local time."
            )

        day = int(period == "tomorrow")
        daily = data["daily"]
        weekday = date.fromisoformat(daily["time"][day]).strftime("%A")
        high = _temperature(daily["temperature_2m_max"][day])
        low = _temperature(daily["temperature_2m_min"][day])
        condition = CONDITIONS.get(daily["weather_code"][day])
        summary = f"{condition}, with a" if condition else "a"
        forecast = f"Open-Meteo forecasts {summary} high of {high} and a low of {low} {unit}"
        probability = daily["precipitation_probability_max"][day]
        chance = f" The chance of precipitation is up to {round(_number(probability, 0, 100))} percent." if probability else ""
        return f"{forecast} in {place.label} {period}, {weekday}.{chance}"

    async def reply(self, period="current", location=""):
        _require_permission(self._permitted)
        if period not in ("current", "today", "tomorrow"):
            return "I can check the weather now, today, or tomorrow."
        chosen = normalize_location(location) if location else self.default_location
        if not chosen:
            return NO_LOCATION
        try:
            return await asyncio.wait_for(self._report(period, chosen), timeout=LOOKUP_TIMEOUT)
        except (OSError, asyncio.TimeoutError, http.client.HTTPException,
                ValueError, AttributeError, KeyError, IndexError, TypeError):
            return UNAVAILABLE
