#!/usr/bin/env python3
"""
Scraper for Commonwealth Club events → ICS calendar file.

Fetches all upcoming events from https://www.commonwealthclub.org/events,
enriches each with location and description from individual event pages,
and writes a .ics file you can subscribe to in Google Calendar.
"""

import argparse
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

BASE_URL = "https://www.commonwealthclub.org"
EVENTS_URL = f"{BASE_URL}/events"
LA_TZ = ZoneInfo("America/Los_Angeles")
UTC = ZoneInfo("UTC")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Matches /events/2026-05-26/some-slug
_DATE_IN_URL = re.compile(r"/events/(\d{4}-\d{2}-\d{2})/")
# Matches "Tue, May 26 / 6:00 PM PDT"
_TIME_IN_STR = re.compile(r"/\s*(\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE)


def extract_start(url: str, date_str: str) -> datetime | None:
    """
    Build a timezone-aware datetime from the event URL (which contains
    YYYY-MM-DD) and the listing-page date string (which contains hh:mm AM/PM).
    Falls back gracefully if either part is missing.
    """
    date_match = _DATE_IN_URL.search(url)
    time_match = _TIME_IN_STR.search(date_str)

    if not date_match:
        return None

    date_part = date_match.group(1)  # "2026-05-26"
    time_part = time_match.group(1).strip() if time_match else "12:00 PM"

    try:
        naive = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %I:%M %p")
        return naive.replace(tzinfo=LA_TZ)
    except ValueError:
        return None


def fetch_listing_page(page: int, session: requests.Session) -> list[dict]:
    """Return a list of {title, url, date_str} dicts for one listing page."""
    url = EVENTS_URL if page == 0 else f"{EVENTS_URL}?page={page}"
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Failed to fetch listing page %d: %s", page, exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    events: list[dict] = []

    for node in soup.select("div.node--type-event.node--view-mode-teaser"):
        date_el = node.select_one(".field--name-field-event-date")
        title_el = node.select_one("h3 a")
        if not date_el or not title_el:
            continue
        href = title_el.get("href", "")
        if not href:
            continue
        events.append(
            {
                "title": title_el.get_text(strip=True),
                "url": href if href.startswith("http") else BASE_URL + href,
                "date_str": date_el.get_text(strip=True),
            }
        )

    return events


def fetch_event_details(url: str, session: requests.Session) -> dict:
    """
    Fetch an event's detail page and return the JSON-LD Event object,
    which includes startDate (UTC), description, and location.
    Returns an empty dict on failure.
    """
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Skipping details for %s: %s", url, exc)
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, AttributeError):
            continue
        for item in data.get("@graph", [data]):
            if isinstance(item, dict) and item.get("@type") == "Event":
                return item

    return {}


def scrape(fetch_details: bool = True, delay: float = 0.4) -> list[dict]:
    """
    Scrape all upcoming events and return a list of merged dicts.
    If fetch_details=True, each event is enriched with JSON-LD data
    (description, location, precise startDate in UTC).
    """
    session = requests.Session()
    all_events: list[dict] = []

    page = 0
    while True:
        log.info("Fetching listing page %d …", page)
        page_events = fetch_listing_page(page, session)
        if not page_events:
            log.info("No events found on page %d — stopping.", page)
            break
        all_events.extend(page_events)
        page += 1
        time.sleep(delay)

    log.info("Found %d events across %d listing pages.", len(all_events), page)

    if fetch_details:
        for i, ev in enumerate(all_events, 1):
            log.info("[%d/%d] Fetching details: %s", i, len(all_events), ev["title"][:60])
            details = fetch_event_details(ev["url"], session)
            ev.update(details)  # JSON-LD keys overlay the listing keys
            time.sleep(delay)

    return all_events


def build_calendar(events: list[dict]) -> Calendar:
    """Convert a list of raw event dicts into an icalendar.Calendar."""
    cal = Calendar()
    cal.add("prodid", "-//Commonwealth Club World Affairs Calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Commonwealth Club Events")
    cal.add("x-wr-timezone", "America/Los_Angeles")
    cal.add(
        "x-wr-caldesc",
        "Upcoming events from the Commonwealth Club World Affairs (San Francisco)",
    )
    cal.add("refresh-interval;value=duration", "P1D")
    cal.add("x-published-ttl", "P1D")

    skipped = 0
    for raw in events:
        title = raw.get("name") or raw.get("title") or "Untitled Event"
        url = raw.get("url", "")

        # --- Start datetime ---
        start_dt: datetime | None = None

        # Try precise UTC datetime from JSON-LD first
        if raw.get("startDate"):
            try:
                naive = datetime.fromisoformat(raw["startDate"])
                # Drupal outputs UTC without a 'Z' marker
                start_dt = naive.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                pass

        # Fall back to parsing URL date + listing time string
        if start_dt is None:
            start_dt = extract_start(url, raw.get("date_str", ""))

        if start_dt is None:
            log.warning("Skipping (no parseable date): %s", title)
            skipped += 1
            continue

        ev = Event()
        ev.add("summary", title)
        ev.add("dtstart", start_dt)
        ev.add("dtend", start_dt + timedelta(hours=2))
        ev.add("dtstamp", datetime.now(UTC))

        if url:
            ev.add("url", url)

        # Description
        desc_parts: list[str] = []
        if raw.get("description"):
            desc_parts.append(raw["description"])
        if url:
            desc_parts.append(f"More info & tickets: {url}")
        if desc_parts:
            ev.add("description", "\n\n".join(desc_parts))

        # Location from JSON-LD
        loc = raw.get("location")
        if isinstance(loc, dict):
            addr = loc.get("address") or {}
            parts = [
                loc.get("name", ""),
                addr.get("streetAddress", ""),
                addr.get("addressLocality", ""),
                addr.get("addressRegion", ""),
            ]
            loc_str = ", ".join(p for p in parts if p)
            if loc_str:
                ev.add("location", loc_str)

        # Stable UID: deterministic from the event URL so re-runs don't duplicate
        ev.add("uid", str(uuid.uuid5(uuid.NAMESPACE_URL, url or title)))

        cal.add_component(ev)

    added = len(cal.subcomponents)
    log.info("Calendar built: %d events added, %d skipped.", added, skipped)
    return cal


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape Commonwealth Club events to an ICS calendar file."
    )
    parser.add_argument(
        "--output",
        default="commonwealth_calendar.ics",
        help="Output ICS file path (default: commonwealth_calendar.ics)",
    )
    parser.add_argument(
        "--no-details",
        action="store_true",
        help="Skip fetching individual event pages (faster but no descriptions/locations)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.4,
        help="Seconds to wait between HTTP requests (default: 0.4)",
    )
    args = parser.parse_args()

    events = scrape(fetch_details=not args.no_details, delay=args.delay)
    if not events:
        log.error("No events scraped. Exiting.")
        sys.exit(1)

    cal = build_calendar(events)

    with open(args.output, "wb") as fh:
        fh.write(cal.to_ical())

    log.info("Wrote %s", args.output)


if __name__ == "__main__":
    main()
