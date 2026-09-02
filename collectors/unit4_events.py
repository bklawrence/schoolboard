from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")
SOURCE_PREFIX = "Unit 4 School Calendar"


@dataclass(frozen=True)
class Unit4Calendar:
    school_id: str
    name: str
    events_url: str

    @property
    def source(self) -> str:
        return f"{SOURCE_PREFIX} — {self.name}"

    @property
    def log_id(self) -> str:
        return f"u4-events-{self.school_id}"


UNIT4_CALENDARS = (
    Unit4Calendar("barkstall", "Barkstall Elementary", "https://www.champaignschools.org/o/barkstall/events"),
    Unit4Calendar("btw", "Booker T. Washington STEM Academy", "https://www.champaignschools.org/o/btw/events"),
    Unit4Calendar("bottenfield", "Bottenfield Elementary", "https://www.champaignschools.org/o/bottenfield/events"),
    Unit4Calendar("busey", "Carrie Busey Elementary", "https://www.champaignschools.org/o/carriebusy/events"),
    Unit4Calendar("howard", "Dr. Howard Elementary", "https://www.champaignschools.org/o/drhoward/events"),
    Unit4Calendar("kenwood", "Kenwood Elementary", "https://www.champaignschools.org/o/kenwood/events"),
    Unit4Calendar("robeson", "Robeson Elementary", "https://www.champaignschools.org/o/robeson/events"),
    Unit4Calendar("southside", "South Side Elementary", "https://www.champaignschools.org/o/southside/events"),
    Unit4Calendar("stratton", "Stratton Academy of the Arts", "https://www.champaignschools.org/o/stratton/events"),
    Unit4Calendar("westview", "Westview Elementary", "https://www.champaignschools.org/o/westview/events"),
    Unit4Calendar("garden", "Garden Hills Academy", "https://www.champaignschools.org/o/gardenhills/events"),
    Unit4Calendar("ipa", "International Prep Academy", "https://www.champaignschools.org/o/ipa/events"),
    Unit4Calendar("edison", "Edison Middle School", "https://www.champaignschools.org/o/edison/events"),
    Unit4Calendar("franklin", "Franklin STEAM Academy", "https://www.champaignschools.org/o/franklin/events"),
    Unit4Calendar("jefferson", "Jefferson Middle School", "https://www.champaignschools.org/o/jefferson/events"),
    Unit4Calendar("central", "Central High School", "https://www.champaignschools.org/o/central/events"),
    Unit4Calendar("centennial", "Centennial High School", "https://www.champaignschools.org/o/centennial/events"),
)


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() != "a":
            return
        for key, value in attrs:
            if key.casefold() == "href" and value:
                # convert_charrefs=True already decoded &amp; exactly once.
                self.hrefs.append(value)


def _request_text(url: str, *, timeout: int = 30, opener=urlopen, accept: str = "*/*") -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "ChambanaSchoolboard/1.0 (+public school calendar aggregator)",
            "Accept": accept,
        },
    )
    with opener(request, timeout=timeout) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return body.decode(charset, errors="replace")


def _ical_links_from_html(html: str, base_url: str) -> list[str]:
    parser = _HrefParser()
    parser.feed(html)

    links: list[str] = []
    for href in parser.hrefs:
        full = urljoin(base_url, href)
        if "generate_ical" in full.casefold() and full not in links:
            links.append(full)

    # Apptegy can also serialize the URL into page data rather than an anchor.
    decoded = unescape(html).replace("\\/", "/")
    pattern = re.compile(
        r"https?://[^\"'<>\s]+/api/v4/o/\d+/cms/events/generate_ical[^\"'<>\s]*",
        re.I,
    )
    for match in pattern.findall(decoded):
        url = match.rstrip("\\")
        if url not in links:
            links.append(url)

    return links


def _rendered_ical_links(events_url: str, *, wait_seconds: float = 4.0) -> list[str]:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
    except ImportError as exc:
        raise RuntimeError("Selenium is required for Unit 4 rendered calendar discovery") from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--lang=en-US")

    driver = webdriver.Chrome(options=options)
    try:
        driver.get(events_url)
        time.sleep(wait_seconds)

        links: list[str] = []
        for anchor in driver.find_elements(By.TAG_NAME, "a"):
            try:
                href = anchor.get_attribute("href") or ""
            except Exception:
                continue
            if "generate_ical" in href.casefold() and href not in links:
                links.append(href)

        if links:
            return links

        # Apptegy sometimes exposes the actual link after activating the visible
        # "Click to Download Calendar" control.
        elements = driver.find_elements(
            By.XPATH,
            "//*[contains(normalize-space(.), 'Click to Download Calendar')]",
        )
        for element in elements[:4]:
            try:
                driver.execute_script("arguments[0].click();", element)
                time.sleep(0.75)
            except Exception:
                continue

            for anchor in driver.find_elements(By.TAG_NAME, "a"):
                try:
                    href = anchor.get_attribute("href") or ""
                except Exception:
                    continue
                if "generate_ical" in href.casefold() and href not in links:
                    links.append(href)
            if links:
                break

        if not links:
            links = _ical_links_from_html(driver.page_source, driver.current_url)
        return links
    finally:
        driver.quit()


def discover_ical_url(
    calendar: Unit4Calendar,
    *,
    timeout: int = 30,
    opener=urlopen,
) -> tuple[str, str]:
    html = _request_text(
        calendar.events_url,
        timeout=timeout,
        opener=opener,
        accept="text/html,application/xhtml+xml",
    )
    links = _ical_links_from_html(html, calendar.events_url)
    method = "page HTML"

    if not links:
        links = _rendered_ical_links(calendar.events_url)
        method = "rendered page"

    if not links:
        raise RuntimeError(
            f"{calendar.name} Events page did not expose an Apptegy iCalendar download URL"
        )

    links.sort(
        key=lambda url: (
            "thrillshare-cmsv2.services.thrillshare.com" not in url.casefold(),
            len(url),
        )
    )
    return links[0], method


def _unfold_ical(text: str) -> list[str]:
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for line in raw:
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _unescape_ical(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    if ":" not in line:
        return None
    left, value = line.split(":", 1)
    bits = left.split(";")
    name = bits[0].upper()
    params: dict[str, str] = {}
    for bit in bits[1:]:
        if "=" in bit:
            key, val = bit.split("=", 1)
            params[key.upper()] = val.strip('"')
    return name, params, _unescape_ical(value)


def _parse_dt(value: str, params: dict[str, str]) -> date | datetime:
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date()

    if value.endswith("Z"):
        dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(CHICAGO)

    fmt = "%Y%m%dT%H%M%S" if len(value) >= 15 else "%Y%m%dT%H%M"
    raw = value[:15] if fmt.endswith("%S") else value[:13]
    dt = datetime.strptime(raw, fmt)

    tzid = params.get("TZID")
    try:
        tz = ZoneInfo(tzid) if tzid else CHICAGO
    except Exception:
        tz = CHICAGO
    return dt.replace(tzinfo=tz).astimezone(CHICAGO)


def _category(title: str) -> str:
    low = title.casefold()

    if any(token in low for token in (
        "no school", "early dismissal", "dismissal", "institute day",
        "first day", "last day", "e-learning", "e learning", "holiday",
    )):
        return "schedule"

    if any(token in low for token in (
        " volleyball", " soccer", " football", " baseball", " softball",
        " basketball", " cross country", " wrestling", " track", " tennis",
        " golf", " swimming", " swim", " cheer", " lacrosse", " bowling",
        " vs ", " @ ",
    )):
        return "sport"

    return "general"


def _first_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,);]")


def parse_apptegy_ics(
    text: str,
    *,
    calendar: Unit4Calendar,
    source_url: str,
) -> list[dict]:
    events: list[dict] = []
    current: dict[str, tuple[dict[str, str], str]] | None = None

    for line in _unfold_ical(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue

        if line == "END:VEVENT":
            if current:
                event = _normalize_event(
                    current,
                    calendar=calendar,
                    source_url=source_url,
                )
                if event:
                    events.append(event)
            current = None
            continue

        if current is None:
            continue

        prop = _split_property(line)
        if not prop:
            continue
        name, params, value = prop
        if name not in current:
            current[name] = (params, value)

    return events


def _normalize_event(
    raw: dict[str, tuple[dict[str, str], str]],
    *,
    calendar: Unit4Calendar,
    source_url: str,
) -> dict | None:
    def val(name: str) -> str:
        return raw.get(name, ({}, ""))[1]

    start_prop = raw.get("DTSTART")
    if not start_prop:
        return None

    start_obj = _parse_dt(start_prop[1], start_prop[0])
    end_obj = None
    if raw.get("DTEND"):
        end_obj = _parse_dt(raw["DTEND"][1], raw["DTEND"][0])

    title = val("SUMMARY").strip() or "School event"
    uid = val("UID") or f"{title}|{val('DTSTART')}|{val('LOCATION')}"
    digest = hashlib.sha1(uid.encode("utf-8")).hexdigest()[:18]

    event_day = start_obj.date() if isinstance(start_obj, datetime) else start_obj
    event: dict[str, Any] = {
        "id": f"u4-{calendar.school_id}-{digest}",
        "title": title,
        "date": event_day.isoformat(),
        "schools": [calendar.school_id],
        "scope": "school",
        "category": _category(title),
        "source": calendar.source,
        "sourceUrl": source_url,
    }

    if isinstance(start_obj, datetime):
        event["start"] = start_obj.strftime("%H:%M")
        if isinstance(end_obj, datetime) and end_obj.date() == start_obj.date():
            event["end"] = end_obj.strftime("%H:%M")
    else:
        event["allDay"] = True
        if isinstance(end_obj, date) and not isinstance(end_obj, datetime):
            inclusive_end = end_obj - timedelta(days=1)
            if inclusive_end > start_obj:
                event["endDate"] = inclusive_end.isoformat()
                event["weekdaysOnly"] = False

    location = val("LOCATION").strip()
    if location:
        event["location"] = location

    explicit_url = val("URL").strip()
    description_url = _first_url(val("DESCRIPTION"))
    if explicit_url:
        event["sourceUrl"] = explicit_url
    elif description_url:
        event["sourceUrl"] = description_url

    return event


def fetch_unit4_calendar(
    calendar: Unit4Calendar,
    *,
    timeout: int = 30,
    opener=urlopen,
) -> list[dict]:
    ical_url, method = discover_ical_url(
        calendar,
        timeout=timeout,
        opener=opener,
    )

    text = _request_text(
        ical_url,
        timeout=timeout,
        opener=opener,
        accept="text/calendar,text/plain;q=0.9,*/*;q=0.8",
    )
    if "BEGIN:VCALENDAR" not in text.upper():
        sample = re.sub(r"\s+", " ", text)[:180]
        raise RuntimeError(
            f"{calendar.name} calendar download was not valid iCalendar"
            + (f": {sample}" if sample else "")
        )

    events = parse_apptegy_ics(
        text,
        calendar=calendar,
        source_url=calendar.events_url,
    )

    print(
        f"{calendar.log_id} detail: discovered iCalendar via {method}; "
        f"{len(events)} events"
    )
    return events
