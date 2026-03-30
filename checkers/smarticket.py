"""Smarticket event scanner — scrapes event listings and finds new available events."""

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL = "https://mbe-rg.smarticket.co.il"
SEARCH_URL = f"{BASE_URL}/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Recurring daily events that aren't real "events" — substring match on name
EXCLUDED_PATTERNS = [
    "פעילות ספורט לילדים",
    'ר"געים משחקיה התפתחותית',
]

# ── Toddler relevance filter ─────────────────────────────────────────

_EXCLUDE_AGE_RE = re.compile(
    r'כיתות'            # school grades
    r'|טרום חובה'       # pre-mandatory kindergarten (~4-5yo)
    r'|גן חובה'         # mandatory kindergarten (~5-6yo)
    r'|חובה\s*[+א\-]'  # "חובה +א", "חובה-א" etc.
    r'|[א-ו]-[א-ו]'    # grade letter pairs: א-ב, ג-ד, ג-ו …
    r'|[א-ו]\s+ומעלה'  # "ב ומעלה" (grade 2 and above)
    r'|חודשיים'         # 2-month-olds (too young)
    r'|חודשים.*זחילה'   # "3 חודשים-זחילה" (up-to-crawling, too young)
)

_INCLUDE_AGE_RE = re.compile(
    r'שנה וחצי'         # 1.5 years
    r'|שנתיים וחצי'     # 2.5 years (upper bar)
    r'|שנתיים'          # 2 years
    r'|זחילה'           # crawling stage — overlaps with toddler range
    r'|הליכה'           # walking stage — overlaps with toddler range
    r'|טיטולים'         # diaper/potty-training parent workshop
    r'|אמהות אחרי לידה' # post-birth mothers group
)


def is_relevant_for_toddler(name: str, max_age: float | None = None, include_terms: dict[str, bool] | None = None) -> bool:
    """Return True if the event suits a ~1.5-year-old (mature for age).

    Upper bar: events whose age range starts at 1–2 years (up to max_age).
    Developmental milestone terms (זחילה, הליכה) always pass if enabled.
    Drops school-grade events and adult events with no age indicator.
    """
    # Load dynamic settings if not provided
    if max_age is None or include_terms is None:
        from monitor import get_filter_settings
        settings = get_filter_settings()
        if max_age is None:
            max_age = settings["max_age"]
        if include_terms is None:
            include_terms = settings["include_terms"]

    if _EXCLUDE_AGE_RE.search(name):
        return False

    # Check dynamic include terms
    for term, enabled in include_terms.items():
        if enabled and term in name:
            return True

    # Also check static include patterns (שנה וחצי, שנתיים, etc.)
    if _INCLUDE_AGE_RE.search(name):
        return True

    # Numeric age check — dynamic max_age
    max_start = int(max_age)  # e.g., 2.5 -> allow ages 1 and 2
    if max_start >= 1:
        age_pattern = "|".join(str(a) for a in range(1, max_start + 1))
        m = re.search(rf'(?:גילאי|לגילאי|גילאים)\s*({age_pattern})(?!\d)', name)
        if m:
            after = name[m.end(): m.end() + 8].strip()
            if not after.startswith('חודש'):
                return True

    return False


@dataclass
class SmartTicketEvent:
    id: str
    name: str
    time: str
    venue: str
    url: str
    available: bool
    date: str = ""  # ISO date string, e.g. "2026-03-26"


def fetch_events(target_date: date) -> list[SmartTicketEvent]:
    """Fetch all non-excluded events for *target_date*."""
    r = requests.get(
        SEARCH_URL,
        params={"date": target_date.isoformat()},
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    events: list[SmartTicketEvent] = []

    for a in soup.find_all("a", href=re.compile(r"\?id=\d+")):
        id_match = re.search(r"id=(\d+)", a.get("href", ""))
        if not id_match:
            continue

        event_id = id_match.group(1)

        h2 = a.find("h2")
        name = h2.get_text(strip=True) if h2 else ""

        # Skip excluded recurring events
        if any(pat in name for pat in EXCLUDED_PATTERNS):
            continue

        time_div = a.find("div", class_="time_container")
        time_match = re.search(r"(\d{2}:\d{2})", time_div.get_text() if time_div else "")
        time_str = time_match.group(1) if time_match else ""

        venue_div = a.find("div", class_="theater_container")
        venue = venue_div.get_text(strip=True) if venue_div else ""

        sold_out = "הכרטיסים אזלו" in a.get_text()

        events.append(SmartTicketEvent(
            id=event_id,
            name=name,
            time=time_str,
            venue=venue,
            url=f"{BASE_URL}/?id={event_id}",
            available=not sold_out,
        ))

    log.info("Smarticket: %d events for %s (after exclusions)", len(events), target_date)
    return events


def fetch_events_range(days_ahead: int = 7) -> list[SmartTicketEvent]:
    """Fetch events for the next *days_ahead* days (tomorrow through tomorrow+days_ahead-1)."""
    today = date.today()
    all_events: list[SmartTicketEvent] = []
    for offset in range(1, days_ahead + 1):
        target = today + timedelta(days=offset)
        try:
            events = fetch_events(target)
            for e in events:
                e.date = target.isoformat()
            all_events.extend(events)
        except Exception:
            log.exception("Error fetching Smarticket events for %s", target)
    return all_events
