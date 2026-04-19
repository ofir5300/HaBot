"""Flightradar24 TLV departure scanner — detects new outbound flights from Ben Gurion."""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

log = logging.getLogger(__name__)

FR24_URL = (
    "https://api.flightradar24.com/common/v1/airport.json"
    "?code=tlv&plugin[]=schedule&plugin-setting[schedule][mode]=departures&limit=100"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Exponential backoff state for FR24 errors
_consecutive_failures = 0
_backoff_until = 0.0


@dataclass
class FlightDeparture:
    flight_key: str       # unique: "{airline_iata}_{flight_number}_{scheduled_ts}"
    flight_number: str    # e.g. "LY583"
    airline_iata: str     # e.g. "LY"
    airline_name: str     # e.g. "El Al"
    destination_iata: str # e.g. "ATH"
    destination_city: str # e.g. "Athens"
    scheduled_ts: int     # Unix timestamp of scheduled departure
    status: str           # e.g. "Scheduled"

    @property
    def fr24_url(self) -> str:
        """Flightradar24 flight page (source of truth)."""
        return f"https://www.flightradar24.com/{self.flight_number}"

    @property
    def airline_url(self) -> str:
        """Direct link to the airline's own booking/manage page when known."""
        date_str = datetime.fromtimestamp(self.scheduled_ts).strftime("%Y-%m-%d")
        urls = {
            "LY": f"https://www.elal.com/en/flight-deals/TLV-{self.destination_iata}/",
            "IZ": "https://www.arkia.com/en",
            "6H": "https://www.israirairlines.com/en",
            "U2": f"https://www.easyjet.com/en/cheap-flights/TLV/to-{self.destination_iata}",
            "W6": f"https://wizzair.com/en-gb/flights/timetable/TLV/{self.destination_iata}",
            "FR": f"https://www.ryanair.com/gb/en/cheap-flights/TLV/{self.destination_iata}",
            "PC": "https://www.flypgs.com/en",
        }
        return urls.get(self.airline_iata, self.fr24_url)

    def booking_url(self, num_adults: int = 2, num_infants: int = 1, trip_days: int = 4) -> str:
        """Kayak round-trip deep link pre-filled with passengers and dates."""
        out_date = datetime.fromtimestamp(self.scheduled_ts).strftime("%Y-%m-%d")
        ret_date = (datetime.fromtimestamp(self.scheduled_ts) + timedelta(days=trip_days)).strftime("%Y-%m-%d")
        pax = f"{num_adults}adults"
        if num_infants:
            pax += f"/{num_infants}infant-lap"
        return f"https://www.kayak.com/flights/TLV-{self.destination_iata}/{out_date}/{ret_date}/{pax}?sort=bestflight_a"


def fetch_departures() -> list[FlightDeparture]:
    """Fetch current TLV departures from Flightradar24. Returns empty list on error."""
    global _consecutive_failures, _backoff_until

    if time.time() < _backoff_until:
        log.debug("FR24: backing off for %ds more", int(_backoff_until - time.time()))
        return []

    try:
        r = requests.get(FR24_URL, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        _consecutive_failures = 0
    except Exception:
        _consecutive_failures += 1
        wait = min(60 * (2 ** _consecutive_failures), 1800)
        _backoff_until = time.time() + wait
        log.exception("FR24: fetch failed (attempt %d, backing off %ds)", _consecutive_failures, wait)
        return []

    try:
        items = (
            data
            .get("result", {})
            .get("response", {})
            .get("airport", {})
            .get("pluginData", {})
            .get("schedule", {})
            .get("departures", {})
            .get("data", [])
        )
    except Exception:
        log.exception("FR24: unexpected response structure")
        return []

    flights: list[FlightDeparture] = []
    for item in items or []:
        try:
            f = item.get("flight", {})
            flight_number = (
                f.get("identification", {})
                .get("number", {})
                .get("default", "") or ""
            )
            airline = f.get("airline") or {}
            airline_iata = (airline.get("code") or {}).get("iata", "") or ""
            airline_name = airline.get("name") or airline_iata
            dest = (f.get("airport") or {}).get("destination") or {}
            dest_iata = (dest.get("code") or {}).get("iata", "") or ""
            dest_city = ((dest.get("position") or {}).get("region") or {}).get("city", "") or dest_iata
            scheduled_ts = ((f.get("time") or {}).get("scheduled") or {}).get("departure", 0) or 0
            status = (f.get("status") or {}).get("text", "") or ""

            if not flight_number or not airline_iata or not scheduled_ts:
                continue

            flight_key = f"{airline_iata}_{flight_number}_{scheduled_ts}"
            flights.append(FlightDeparture(
                flight_key=flight_key,
                flight_number=flight_number,
                airline_iata=airline_iata,
                airline_name=airline_name,
                destination_iata=dest_iata,
                destination_city=dest_city,
                scheduled_ts=scheduled_ts,
                status=status,
            ))
        except Exception:
            log.debug("FR24: skipping malformed entry")
            continue

    flights = _filter_cargo(flights)
    log.info("FR24: %d passenger departures from TLV", len(flights))
    return flights


# Cargo/freight airlines that never sell passenger tickets
_CARGO_AIRLINES = {"5C", "7L", "5F", "X7"}
# Airports that are cargo hubs with no meaningful passenger service
_CARGO_AIRPORTS = {"LGG", "LEJ", "EMA", "SHJ", "DWC"}


def _filter_cargo(flights: list[FlightDeparture]) -> list[FlightDeparture]:
    """Strip known cargo airlines and cargo-only destinations."""
    result = []
    for f in flights:
        if f.airline_iata in _CARGO_AIRLINES:
            continue
        if f.destination_iata in _CARGO_AIRPORTS:
            continue
        result.append(f)
    return result
