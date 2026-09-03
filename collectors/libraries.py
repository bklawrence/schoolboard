from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

HISTORY_DAYS = 30
HORIZON_DAYS = 60


@dataclass(frozen=True)
class LibraryCalendar:
    id: str
    name: str
    base_url: str
    source: str
    band_school_ids: dict[str, str]
    age_filters: dict[str, tuple[str, ...]]

    @property
    def log_id(self) -> str:
        return f"{self.id}-library"


URBANA_LIBRARY = LibraryCalendar(
    id="urbana",
    name="Urbana Free Library",
    base_url="https://urbanafreelibrary.libnet.info",
    source="Urbana Free Library Youth Events",
    band_school_ids={
        "early": "urbana-lib-early",
        "elementary": "urbana-lib-elementary",
        "teens": "urbana-lib-teens",
    },
    age_filters={
        "early": ("Babies", "Toddlers", "Pre-Schoolers"),
        "elementary": ("Elementary Students",),
        "teens": ("Middle School Students", "High School Students"),
    },
)

CHAMPAIGN_LIBRARY = LibraryCalendar(
    id="champaign",
    name="Champaign Public Library",
    base_url="https://champaign.libnet.info",
    source="Champaign Public Library Youth Events",
    band_school_ids={
        "early": "champaign-lib-early",
        "elementary": "champaign-lib-elementary",
        "teens": "champaign-lib-teens",
    },
    age_filters={
        "early": ("Preschool",),
        "elementary": ("School age",),
        "teens": ("Teens",),
    },
)

LIBRARIES = (URBANA_LIBRARY, CHAMPAIGN_LIBRARY)
BAND_ORDER = ("early", "elementary", "teens")


class _EventPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_h1 = False
        self.h1_parts: list[str] = []
        self.text_parts: list[str] = []
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        attr = {str(k).casefold(): str(v) for k, v in attrs if k and v is not None}
        if tag == "h1":
            self._in_h1 = True
        if tag == "meta":
            key = attr.get("property") or attr.get("name")
            content = attr.get("content")
            if key and content:
                self.meta[key.casefold()] = content.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "h1":
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        clean = re.sub(r"\s+", " ", data).strip()
        if not clean:
            return
        self.text_parts.append(clean)
        if self._in_h1:
            self.h1_parts.append(clean)


def _request_text(
    url: str,
    *,
    timeout: int = 30,
    opener=urlopen,
) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "ChambanaSchoolboard/1.0 (+public community calendar aggregator)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with opener(request, timeout=timeout) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return body.decode(charset, errors="replace")


def _new_driver():
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:
        raise RuntimeError("Selenium is required for Communico event-list discovery") from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--lang=en-US")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(45)
    return driver


def _event_id_from_url(url: str) -> str | None:
    match = re.search(r"/event/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)", url)
    if not match:
        # Communico hosted pages normally use /event/12345.
        match = re.search(r"/event/(\d+)(?:[/?#]|$)", url)
    return match.group(1) if match else None


def _filtered_url(
    calendar: LibraryCalendar,
    *,
    age: str,
    start_day: date,
    end_day: date,
) -> str:
    params = urlencode({
        "a": age,
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "v": "list",
    })
    return f"{calendar.base_url}/events?{params}"


def discover_youth_event_links(
    calendar: LibraryCalendar,
    *,
    reference: date,
    wait_seconds: float = 2.0,
) -> dict[str, dict[str, Any]]:
    """
    Use Communico's documented age/date filters, then merge membership by
    event id. An event tagged for multiple selected ages therefore becomes
    one SchoolBoard record with multiple audience IDs.
    """
    try:
        from selenium.webdriver.common.by import By
    except ImportError as exc:
        raise RuntimeError("Selenium is required for Communico event-list discovery") from exc

    start_day = reference - timedelta(days=HISTORY_DAYS)
    end_day = reference + timedelta(days=HORIZON_DAYS)
    found: dict[str, dict[str, Any]] = {}
    driver = _new_driver()

    try:
        for band in BAND_ORDER:
            band_links: set[str] = set()
            for age in calendar.age_filters[band]:
                url = _filtered_url(
                    calendar,
                    age=age,
                    start_day=start_day,
                    end_day=end_day,
                )
                driver.get(url)
                time.sleep(wait_seconds)

                for anchor in driver.find_elements(By.CSS_SELECTOR, "a[href*='/event/']"):
                    try:
                        href = anchor.get_attribute("href") or ""
                    except Exception:
                        continue
                    if not href:
                        continue

                    full = urljoin(calendar.base_url, href)
                    if urlparse(full).netloc != urlparse(calendar.base_url).netloc:
                        continue

                    event_id = _event_id_from_url(full)
                    if not event_id:
                        continue

                    canonical = f"{calendar.base_url}/event/{event_id}"
                    entry = found.setdefault(event_id, {
                        "url": canonical,
                        "bands": set(),
                    })
                    entry["bands"].add(band)
                    band_links.add(event_id)

            print(
                f"{calendar.log_id} detail: {band} filter found "
                f"{len(band_links)} unique event links"
            )
    finally:
        driver.quit()

    if not found:
        raise RuntimeError(
            f"{calendar.name} Communico youth filters returned zero event links"
        )

    return found


def _json_string_value(html: str, key: str) -> str:
    pattern = re.compile(
        rf'["\']{re.escape(key)}["\']\s*:\s*["\'](.*?)(?<!\\)["\']',
        re.I | re.S,
    )
    match = pattern.search(html)
    if not match:
        return ""
    value = match.group(1)
    try:
        # Decode ordinary JSON escapes without treating arbitrary HTML as JSON.
        value = json.loads(f'"{value}"')
    except Exception:
        value = value.replace(r"\/", "/").replace(r"\"", '"')
    return unescape(re.sub(r"\s+", " ", value)).strip()


def _meta_title(parser: _EventPageParser) -> str:
    return (
        " ".join(parser.h1_parts).strip()
        or parser.meta.get("og:title", "").strip()
        or parser.meta.get("twitter:title", "").strip()
    )


def _machine_datetimes(html: str) -> list[datetime]:
    decoded = unescape(html).replace(r"\/", "/")
    matches = re.findall(
        r"(?<!\d)(20\d{2}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)(?!\d)",
        decoded,
    )
    values: list[datetime] = []
    for day_text, clock_text in matches:
        fmt = "%Y-%m-%d %H:%M:%S" if len(clock_text) == 8 else "%Y-%m-%d %H:%M"
        try:
            dt = datetime.strptime(f"{day_text} {clock_text}", fmt)
        except ValueError:
            continue
        if not values or dt != values[-1]:
            values.append(dt)
    return values


def _parse_clock(text: str) -> str | None:
    clean = text.strip().lower().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(clean, fmt).strftime("%H:%M")
        except ValueError:
            pass
    return None


def _visible_date_time_fallback(
    parser: _EventPageParser,
    *,
    reference: date,
) -> tuple[date | None, str | None, str | None]:
    joined = " | ".join(parser.text_parts)

    date_match = re.search(
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(\d{1,2})(?:,\s*(20\d{2}))?",
        joined,
        re.I,
    )
    event_day: date | None = None
    if date_match:
        month_name, day_num, explicit_year = date_match.groups()
        month = datetime.strptime(month_name[:3], "%b").month
        if explicit_year:
            years = [int(explicit_year)]
        else:
            years = [reference.year - 1, reference.year, reference.year + 1]
        candidates: list[date] = []
        for year in years:
            try:
                candidates.append(date(year, month, int(day_num)))
            except ValueError:
                pass
        if candidates:
            event_day = min(candidates, key=lambda d: abs((d - reference).days))

    time_match = re.search(
        r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*[-–—]\s*"
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
        joined,
        re.I,
    )
    if time_match:
        return event_day, _parse_clock(time_match.group(1)), _parse_clock(time_match.group(2))

    single = re.search(
        r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
        joined,
        re.I,
    )
    return event_day, _parse_clock(single.group(1)) if single else None, None


def _event_types(parser: _EventPageParser) -> set[str]:
    parts = parser.text_parts
    values: list[str] = []

    for i, part in enumerate(parts):
        label = part.casefold().strip()
        if label.startswith("event type"):
            # Sometimes the first value is on the same text node.
            same = re.sub(r"^event\s*type\s*:?\s*", "", part, flags=re.I).strip(" |")
            if same:
                values.extend(x.strip() for x in same.split("|") if x.strip())

            for following in parts[i + 1:i + 8]:
                low = following.casefold().strip()
                if (
                    low.startswith("tags")
                    or low.startswith("age group")
                    or low.startswith("the urbana free library")
                    or low.startswith("main library")
                    or low.startswith("douglass branch")
                ):
                    break
                if following != "|":
                    values.extend(x.strip() for x in following.split("|") if x.strip())
            break

    # Keep labels short; this prevents a description paragraph from being
    # mistaken for an event type if page markup changes.
    return {v for v in values if 0 < len(v) <= 40}


def parse_event_page(
    html: str,
    *,
    calendar: LibraryCalendar,
    event_id: str,
    event_url: str,
    bands: set[str],
    reference: date,
) -> dict | None:
    parser = _EventPageParser()
    parser.feed(html)

    title = _meta_title(parser)
    if not title:
        title = _json_string_value(html, "title")
    title = re.sub(
        rf"\s*[-|]\s*{re.escape(calendar.name)}\s*$",
        "",
        title,
        flags=re.I,
    ).strip()
    if not title:
        return None

    machine = _machine_datetimes(html)
    if machine:
        start_dt = machine[0]
        end_dt = machine[1] if len(machine) > 1 else None
        event_day = start_dt.date()
        start = start_dt.strftime("%H:%M")
        end = (
            end_dt.strftime("%H:%M")
            if end_dt and end_dt.date() == start_dt.date() and end_dt > start_dt
            else None
        )
    else:
        event_day, start, end = _visible_date_time_fallback(
            parser,
            reference=reference,
        )
        if event_day is None:
            return None

    event_types = _event_types(parser)

    # Urbana tags some truly generic library business (for example book
    # sales/meetings) for every age. Keep youth programming, but omit
    # library-meeting/adult records unless they are also explicitly typed
    # Children or Teen.
    if calendar.id == "urbana":
        lowered_types = {v.casefold() for v in event_types}
        youth_type = bool(lowered_types & {"children", "teen"})
        generic_type = bool(lowered_types & {"library meeting", "adult"})
        if generic_type and not youth_type:
            return None

    location_name = (
        _json_string_value(html, "locationName")
        or _json_string_value(html, "branchName")
    )
    room_name = _json_string_value(html, "roomName")

    if location_name and room_name and room_name.casefold() not in location_name.casefold():
        location = f"{location_name} — {room_name}"
    elif location_name:
        location = location_name
    elif room_name:
        location = f"{calendar.name} — {room_name}"
    else:
        location = calendar.name

    school_ids = [
        calendar.band_school_ids[band]
        for band in BAND_ORDER
        if band in bands
    ]
    if not school_ids:
        return None

    event: dict[str, Any] = {
        "id": f"library-{calendar.id}-{event_id}",
        "title": title,
        "date": event_day.isoformat(),
        "schools": school_ids,
        "scope": "community",
        "category": "general",
        "source": calendar.source,
        "sourceUrl": event_url,
        "location": location,
    }
    if start:
        event["start"] = start
    else:
        event["allDay"] = True
    if end:
        event["end"] = end

    return event


def fetch_library_calendar(
    calendar: LibraryCalendar,
    *,
    reference: date | None = None,
    timeout: int = 30,
    opener=urlopen,
) -> list[dict]:
    reference = reference or date.today()
    discovered = discover_youth_event_links(
        calendar,
        reference=reference,
    )

    events: list[dict] = []
    failed = 0
    excluded = 0

    for event_id, info in sorted(discovered.items(), key=lambda item: int(item[0])):
        event_url = info["url"]
        html = None
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                html = _request_text(
                    event_url,
                    timeout=timeout,
                    opener=opener,
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.4)

        if html is None:
            failed += 1
            continue

        event = parse_event_page(
            html,
            calendar=calendar,
            event_id=event_id,
            event_url=event_url,
            bands=set(info["bands"]),
            reference=reference,
        )
        if event is None:
            excluded += 1
            continue
        events.append(event)

    # A partial event-detail outage should not silently replace a good cache.
    if failed and failed > max(3, len(discovered) // 10):
        raise RuntimeError(
            f"{calendar.name} event detail fetch failed for "
            f"{failed} of {len(discovered)} discovered events"
        )

    print(
        f"{calendar.log_id} detail: {len(discovered)} unique youth-tagged links; "
        f"kept {len(events)} events"
        + (f"; excluded {excluded} generic/non-parseable records" if excluded else "")
        + (f"; {failed} detail fetches failed" if failed else "")
    )
    return sorted(
        events,
        key=lambda e: (e.get("date", ""), e.get("start", ""), e.get("title", "")),
    )
