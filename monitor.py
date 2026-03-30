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

SMARTICKET_DAYS_AHEAD = 7


def check_smarticket() -> list[SmartTicketEvent]:
    """Scan next 7 days of Smarticket events. Returns newly-discovered available events."""
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

    # Fetch all events for the next 7 days
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
    """Scan Kehilatayim events. Returns newly-discovered available events."""
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
