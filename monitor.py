import json
import logging
from datetime import date, timedelta
from pathlib import Path

from checkers import StockResult, get_checker
from checkers.smarticket import SmartTicketEvent, fetch_events_range, is_relevant_for_toddler
from checkers.kehilatayim import fetch_events as fetch_kehilatayim_events, is_relevant_for_toddler_kehilatayim
from checkers.flights import FlightDeparture, fetch_departures
from config import STATE_FILE

log = logging.getLogger(__name__)


def load_state() -> dict:
    path = Path(STATE_FILE)
    if path.exists():
        return json.loads(path.read_text())
    return {"items": [], "paused": False}


def save_state(state: dict):
    Path(STATE_FILE).write_text(json.dumps(state, indent=2, ensure_ascii=False))


def add_item(source: str, item_id: str) -> tuple[dict, bool]:
    """Add item to monitoring. Returns (state, is_new)."""
    state = load_state()
    for item in state["items"]:
        if item["source"] == source and item["item_id"] == item_id:
            return state, False
    state["items"].append({
        "source": source,
        "item_id": item_id,
        "last_in_stock": False,
    })
    save_state(state)
    return state, True


def remove_item(source: str, item_id: str) -> bool:
    """Remove an item from monitoring. Returns True if item was found and removed."""
    state = load_state()
    before = len(state["items"])
    state["items"] = [i for i in state["items"] if not (i["source"] == source and i["item_id"] == item_id)]
    save_state(state)
    return len(state["items"]) < before


def get_items() -> list[dict]:
    """Return all monitored items."""
    return load_state()["items"]


def check_all() -> list[tuple[dict, StockResult]]:
    """Check all items. Returns list of (item, result) for items that transitioned to in-stock."""
    state = load_state()
    if state.get("paused"):
        return []

    alerts = []
    for item in state["items"]:
        checker = get_checker(item["source"])
        if not checker:
            log.warning("No checker for source: %s", item["source"])
            continue
        try:
            result = checker.check(item["item_id"])
            was_in_stock = item.get("last_in_stock", False)
            if result.in_stock and not was_in_stock:
                alerts.append((item, result))
            item["last_in_stock"] = result.in_stock
        except Exception:
            log.exception("Error checking %s/%s", item["source"], item["item_id"])

    save_state(state)
    return alerts


def toggle_pause() -> bool:
    state = load_state()
    state["paused"] = not state.get("paused", False)
    save_state(state)
    return state["paused"]


def is_paused() -> bool:
    return load_state().get("paused", False)


def toggle_toddler_filter() -> bool:
    state = load_state()
    state["toddler_filter"] = not state.get("toddler_filter", True)
    save_state(state)
    return state["toddler_filter"]


def is_toddler_filter_on() -> bool:
    return load_state().get("toddler_filter", True)


# ── Filter settings ─────────────────────────────────────────────────

DEFAULT_FILTER_SETTINGS = {
    "max_age": 2.5,
    "sources": {"smarticket": True, "kehilatayim": True},
    "include_terms": {
        "זחילה": True,
        "הליכה": True,
        "טיטולים": True,
        "אמהות אחרי לידה": True,
    },
}


def get_filter_settings() -> dict:
    state = load_state()
    settings = state.get("filter_settings", {})
    # Merge defaults for any missing keys
    merged = {**DEFAULT_FILTER_SETTINGS, **settings}
    merged["sources"] = {**DEFAULT_FILTER_SETTINGS["sources"], **settings.get("sources", {})}
    merged["include_terms"] = {**DEFAULT_FILTER_SETTINGS["include_terms"], **settings.get("include_terms", {})}
    return merged


def update_filter_setting(key: str, value) -> dict:
    state = load_state()
    settings = state.setdefault("filter_settings", {})
    if key == "max_age":
        settings["max_age"] = float(value)
    elif key.startswith("source:"):
        source = key.split(":", 1)[1]
        settings.setdefault("sources", {})[source] = bool(value)
    elif key.startswith("term:"):
        term = key.split(":", 1)[1]
        settings.setdefault("include_terms", {})[term] = bool(value)
    save_state(state)
    return get_filter_settings()


def is_source_enabled(source: str) -> bool:
    settings = get_filter_settings()
    return settings["sources"].get(source, True)


# ── Flight filter settings ───────────────────────────────────────────

DEFAULT_FLIGHT_FILTER_SETTINGS: dict = {
    # Airlines: empty dict = watch ALL. Set specific keys to False to exclude.
    "airlines": {},
    # Destinations: empty dict = watch ALL
    "destinations": {},
    # Hour-of-day window (supports wrap-around, e.g. min=22 max=6)
    "min_hour": 1,
    "max_hour": 6,
}


def get_flight_filter_settings() -> dict:
    state = load_state()
    stored = state.get("flights", {}).get("filter_settings", {})
    merged = {**DEFAULT_FLIGHT_FILTER_SETTINGS, **stored}
    merged["airlines"] = {
        **DEFAULT_FLIGHT_FILTER_SETTINGS["airlines"],
        **stored.get("airlines", {}),
    }
    merged["destinations"] = {
        **DEFAULT_FLIGHT_FILTER_SETTINGS["destinations"],
        **stored.get("destinations", {}),
    }
    return merged


def update_flight_filter_setting(key: str, value) -> dict:
    state = load_state()
    flights = state.setdefault("flights", {})
    settings = flights.setdefault("filter_settings", {})
    if key == "min_hour":
        settings["min_hour"] = int(value)
    elif key == "max_hour":
        settings["max_hour"] = int(value)
    elif key.startswith("airline:"):
        iata = key.split(":", 1)[1]
        settings.setdefault("airlines", {})[iata] = bool(value)
    elif key.startswith("dest:"):
        dest_code = key.split(":", 1)[1]
        settings.setdefault("destinations", {})[dest_code] = bool(value)
    save_state(state)
    return get_flight_filter_settings()


def is_flights_enabled() -> bool:
    return load_state().get("flights", {}).get("enabled", True)


def toggle_flights() -> bool:
    state = load_state()
    flights = state.setdefault("flights", {})
    flights["enabled"] = not flights.get("enabled", True)
    save_state(state)
    return flights["enabled"]


_REJECTED_FLIGHT_STATUSES = {"landed", "departed", "canceled", "cancelled", "delayed", "diverted", "unknown"}
_MIN_LEAD_TIME_SECS = 7200  # 2 hours


def _passes_flight_filter(flight: FlightDeparture, settings: dict) -> bool:
    """Return True if this flight passes current filter settings."""
    import time as _time
    from datetime import datetime

    status_lower = (flight.status or "").strip().lower()
    if any(status_lower.startswith(r) for r in _REJECTED_FLIGHT_STATUSES):
        return False

    if flight.scheduled_ts - _time.time() < _MIN_LEAD_TIME_SECS:
        return False

    airlines = settings.get("airlines", {})
    if airlines and not airlines.get(flight.airline_iata, False):
        return False

    destinations = settings.get("destinations", {})
    if destinations and not destinations.get(flight.destination_iata, False):
        return False

    dep_hour = datetime.fromtimestamp(flight.scheduled_ts).hour
    min_h = settings.get("min_hour", 0)
    max_h = settings.get("max_hour", 23)
    if min_h <= max_h:
        if not (min_h <= dep_hour <= max_h):
            return False
    else:
        if max_h < dep_hour < min_h:
            return False

    return True


def check_flights() -> list[FlightDeparture]:
    """Fetch TLV departures from FR24. Return newly-discovered flights matching filters."""
    import time as _time

    state = load_state()
    if state.get("paused"):
        return []

    flights_state = state.setdefault("flights", {})
    if not flights_state.get("enabled", True):
        return []

    # known_flight_ids: maps flight_key -> scheduled_ts (for expiry cleanup)
    known: dict[str, int] = flights_state.setdefault("known_flight_ids", {})

    now_ts = int(_time.time())
    expired = [k for k, ts in list(known.items()) if ts < now_ts - 3600]
    for k in expired:
        del known[k]

    departures = fetch_departures()
    if not departures:
        save_state(state)
        return []

    settings = get_flight_filter_settings()

    # Silent-seed: first run seeds only filter-matching flights (so they
    # don't alert) but leaves non-matching flights untracked entirely.
    if not known:
        seeded = 0
        for flight in departures:
            if _passes_flight_filter(flight, settings):
                known[flight.flight_key] = flight.scheduled_ts
                seeded += 1
        log.info("FLIGHTS: first run - seeded %d/%d filter-matching flights silently", seeded, len(departures))
        save_state(state)
        return []

    new_flights: list[FlightDeparture] = []

    for flight in departures:
        if flight.flight_key in known:
            known[flight.flight_key] = flight.scheduled_ts
            continue

        if not _passes_flight_filter(flight, settings):
            continue

        known[flight.flight_key] = flight.scheduled_ts
        new_flights.append(flight)

    save_state(state)

    if new_flights:
        log.info("FLIGHTS: %d new TLV departures detected", len(new_flights))

    return new_flights


# ── User registration ────────────────────────────────────────────────

def register_user(chat_id: int) -> bool:
    """Add chat_id to registered users. Returns True if newly added."""
    state = load_state()
    users = state.setdefault("registered_users", [])
    if chat_id in users:
        return False
    users.append(chat_id)
    save_state(state)
    return True


def get_registered_users() -> list[int]:
    """Return all registered user chat IDs."""
    return load_state().get("registered_users", [])


# ── Smarticket scanner ───────────────────────────────────────────────

SMARTICKET_DAYS_AHEAD = 1


def check_smarticket() -> list[SmartTicketEvent]:
    """Scan tomorrow's Smarticket events. Returns newly-discovered available events."""
    state = load_state()
    if state.get("paused"):
        return []

    st = state.setdefault("smarticket", {"known_events": {}})

    # Migrate from old flat format — preserve old IDs so they don't re-alert
    if "known_event_ids" in st:
        legacy_ids = list(st.pop("known_event_ids"))
        st.pop("target_date", None)
        # Seed legacy IDs into a sentinel key so they're recognized on all dates
        st.setdefault("known_events", {})
        st["known_events"]["_legacy"] = legacy_ids

    known_events: dict[str, list[str]] = st.setdefault("known_events", {})

    # Clean up past dates (keep _legacy sentinel)
    today_str = date.today().isoformat()
    for d in list(known_events.keys()):
        if d != "_legacy" and d < today_str:
            del known_events[d]

    legacy_ids = set(known_events.pop("_legacy", []))

    # Fetch tomorrow's events
    try:
        events = fetch_events_range(SMARTICKET_DAYS_AHEAD)
    except Exception:
        log.exception("Error fetching Smarticket events")
        save_state(state)
        return []

    # Group fetched events by date
    events_by_date: dict[str, list[SmartTicketEvent]] = {}
    for e in events:
        events_by_date.setdefault(e.date, []).append(e)

    new_available: list[SmartTicketEvent] = []

    for event_date, day_events in events_by_date.items():
        known_ids = set(known_events.get(event_date, []))
        is_new_date = event_date not in known_events

        if is_new_date:
            # Silent seed for dates we haven't seen — no alerts
            log.info("Smarticket: seeding known events for %s", event_date)
        else:
            # Only alert for genuinely new available events
            toddler_filter = is_toddler_filter_on()
            for e in day_events:
                if e.id not in known_ids and e.id not in legacy_ids and e.available:
                    if not toddler_filter or is_relevant_for_toddler(e.name):
                        new_available.append(e)

        # Track ALL seen IDs for this date
        known_events[event_date] = list(known_ids | {e.id for e in day_events})

    save_state(state)

    if new_available:
        log.info("Smarticket: %d new available events across %d days",
                 len(new_available), SMARTICKET_DAYS_AHEAD)

    return new_available


# ── Kehilatayim (Givatayim) scanner ────────────────────────────────


def check_kehilatayim() -> list[SmartTicketEvent]:
    """Scan tomorrow's Kehilatayim events. Returns newly-discovered available events."""
    state = load_state()
    if state.get("paused"):
        return []

    kt = state.setdefault("kehilatayim", {"known_events": {}})
    known_events: dict[str, list[str]] = kt.setdefault("known_events", {})

    # Clean up past dates
    today_str = date.today().isoformat()
    for d in list(known_events.keys()):
        if d < today_str:
            del known_events[d]

    try:
        events = fetch_kehilatayim_events()
    except Exception:
        log.exception("Error fetching Kehilatayim events")
        save_state(state)
        return []

    # Only keep tomorrow's events
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
    events = [e for e in events if e.date == tomorrow_str]

    # Group by date
    events_by_date: dict[str, list[SmartTicketEvent]] = {}
    for e in events:
        if e.date:
            events_by_date.setdefault(e.date, []).append(e)

    new_available: list[SmartTicketEvent] = []

    for event_date, day_events in events_by_date.items():
        known_ids = set(known_events.get(event_date, []))
        is_new_date = event_date not in known_events

        if is_new_date:
            log.info("Kehilatayim: seeding known events for %s", event_date)
        else:
            toddler_filter = is_toddler_filter_on()
            for e in day_events:
                if e.id not in known_ids and e.available:
                    if not toddler_filter or is_relevant_for_toddler(e.name):
                        new_available.append(e)

        known_events[event_date] = list(known_ids | {e.id for e in day_events})

    save_state(state)

    if new_available:
        log.info("Kehilatayim: %d new available events", len(new_available))

    return new_available
