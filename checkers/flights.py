"""Flightradar24 TLV departure scanner — detects new outbound flights from Ben Gurion."""

import logging
from dataclasses import dataclass

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

AIRLINE_BOOKING_URLS: dict[str, str] = {
    "LY": "https://www.elal.com",
    "IZ": "https://www.arkia.co.il",
    "6H": "https://www.israirairlines.com",
}


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
    def booking_url(self) -> str:
        return AIRLINE_BOOKING_URLS.get(self.airline_iata, "https://www.google.com/travel/flights")


def fetch_departures() -> list[FlightDeparture]:
    """Fetch current TLV departures from Flightradar24. Returns empty list on error."""
    try:
        r = requests.get(FR24_URL, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception:
        log.exception("FR24: fetch failed")
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

    log.info("FR24: %d departures from TLV", len(flights))
    return flights
