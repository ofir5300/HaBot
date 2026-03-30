"""Kehilatayim (Givatayim) event scanner — scrapes community events for toddlers."""

import json
import logging
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from checkers.smarticket import SmartTicketEvent, is_relevant_for_toddler

log = logging.getLogger(__name__)

BASE_URL = "https://events.kehilatayim.org.il"
COMMUNITY = "Kehilatayim_Taf"
COMMUNITY_ID = 9938
API_URL = f"{BASE_URL}/site/communities/groups"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Age-group tags from Kehilatayim that are relevant for toddlers (~1.5yo)
_RELEVANT_TAGS = {
    "גיל זחילה עד שנה",
    "לידה עד זחילה",
    "שנה עד שנתיים",
    "שנתיים עד שלוש",
}


def _is_relevant_by_tags(tags: list[str]) -> bool | None:
    """Check relevance using structured data-tags. Returns None if no age tags found."""
    age_tags = [t for t in tags if t in _RELEVANT_TAGS or "עד" in t]
    if not age_tags:
        return None  # no structured age info — fall back to name-based filter
    return any(t in _RELEVANT_TAGS for t in age_tags)


def is_relevant_for_toddler_kehilatayim(name: str, tags: list[str]) -> bool:
    """Check if a Kehilatayim event is relevant for a toddler.

    Uses structured tags first, falls back to the Smarticket name-based filter.
    """
    by_tags = _is_relevant_by_tags(tags)
    if by_tags is not None:
        return by_tags
    return is_relevant_for_toddler(name)


def fetch_events() -> list[SmartTicketEvent]:
    """Fetch all upcoming events from Kehilatayim."""
    session = requests.Session()
    session.headers.update(HEADERS)

    # Establish session cookie
    session.get(f"{BASE_URL}/{COMMUNITY}", timeout=15)

    all_events: list[SmartTicketEvent] = []
    page = 1

    while True:
        r = session.get(
            API_URL,
            params={
                "cid": COMMUNITY_ID,
                "type": "regular",
                "view": "auto",
                "gid": 0,
                "page": page,
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        items = data.get("items", [])
        if not items:
            break

        for html in items:
            event = _parse_event(html)
            if event:
                all_events.append(event)

        page += 1
        if page > 50:  # safety limit
            break

    log.info("Kehilatayim: %d events fetched", len(all_events))
    return all_events


def _parse_event(html: str) -> SmartTicketEvent | None:
    """Parse a single event <li> HTML into a SmartTicketEvent."""
    soup = BeautifulSoup(html, "html.parser")
    li = soup.find("li")
    if not li:
        return None

    event_id = li.get("data-key", "")
    if not event_id:
        return None

    # Parse tags
    tags_raw = li.get("data-tags", "[]")
    try:
        tags = json.loads(tags_raw)
    except (json.JSONDecodeError, TypeError):
        tags = []

    # Parse date from timestamp
    timestamp = li.get("data-date", "")
    event_date = ""
    if timestamp:
        try:
            dt = datetime.fromtimestamp(int(timestamp))
            event_date = dt.date().isoformat()
        except (ValueError, OSError):
            pass

    # Name
    name_el = li.find(class_="activity-name")
    name = name_el.get_text(strip=True) if name_el else ""

    # Time
    time_el = li.find(class_="activity-time")
    time_str = ""
    if time_el:
        time_text = time_el.get_text(strip=True)
        # Extract start time (first HH:MM)
        m = re.search(r"(\d{2}:\d{2})", time_text)
        time_str = m.group(1) if m else time_text

    # Venue
    venue_el = li.find(class_="activity-address")
    venue = venue_el.get_text(strip=True) if venue_el else ""

    # Availability — check for sold-out badge
    sold_out = False
    for badge in li.find_all(class_="badge"):
        if "האירוע מלא" in badge.get_text():
            sold_out = True
            break

    # URL
    link = li.find("a", class_="community-resource-link")
    url = link.get("href", "") if link else f"{BASE_URL}/{COMMUNITY}/{event_id}"

    return SmartTicketEvent(
        id=event_id,
        name=name,
        time=time_str,
        venue=venue,
        url=url,
        available=not sold_out,
        date=event_date,
    )
