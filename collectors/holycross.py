from __future__ import annotations

import hashlib
import html as html_lib
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SCHOOL_ID = "holycross"

CALENDAR_PAGE = "https://holycrosselem.org/calendars/"
HOMEPAGE_URL = "https://holycrosselem.org/"

GOOGLE_CALENDAR_ID = "c_pm4odrvcggrh58psu1r0j5b9hc@group.calendar.google.com"
GOOGLE_ICAL_URL = (
    "https://calendar.google.com/calendar/ical/"
    "c_pm4odrvcggrh58psu1r0j5b9hc%40group.calendar.google.com/public/basic.ics"
)

CALENDAR_SOURCE_NAME = "Holy Cross Public Calendar"
HOMEPAGE_SOURCE_NAME = "Holy Cross Homepage Upcoming Dates"

CHICAGO = ZoneInfo("America/Chicago")

_MONTHS = {
    "JANUARY": 1,
    "FEBRUARY": 2,
    "MARCH": 3,
    "APRIL": 4,
    "MAY": 5,
    "JUNE": 6,
    "JULY": 7,
    "AUGUST": 8,
    "SEPTEMBER": 9,
    "OCTOBER": 10,
    "NOVEMBER": 11,
    "DECEMBER": 12,
}

_WEEKDAY = (
    r"(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)"
)

_RANGE_RE = re.compile(
    rf"^(?:{_WEEKDAY},?\s+)?"
    r"(?P<month>JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
    r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+"
    r"(?P<day>\d{1,2})(?:ST|ND|RD|TH)?\s*[-–—]\s*"
    r"(?P<endday>\d{1,2})(?:ST|ND|RD|TH)?"
    r"(?:\s*[-–—:]\s*(?P<title>.+))?$",
    re.IGNORECASE,
)

_SINGLE_RE = re.compile(
    rf"^(?:{_WEEKDAY},?\s+)?"
    r"(?P<month>JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
    r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+"
    r"(?P<day>\d{1,2})(?:ST|ND|RD|TH)?"
    r"(?:\s*[-–—:]\s*(?P<title>.+))?$",
    re.IGNORECASE,
)


def _request_text(
    url: str,
    *,
    timeout: int,
    opener=urlopen,
    accept: str = "text/html,*/*;q=0.8",
) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "ChambanaSchoolboard/1.0 "
                "(+public school calendar aggregator)"
            ),
            "Accept": accept,
        },
    )
    with opener(request, timeout=timeout) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return body.decode(charset, errors="replace")


# ---------- Google Calendar / iCalendar ----------

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
        value.replace("\\N", "\n")
        .replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _split_ical_property(line: str):
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


def _parse_ical_dt(value: str, params: dict[str, str]) -> date | datetime:
    value = value.strip()

    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date()

    if value.endswith("Z"):
        dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
        return dt.astimezone(CHICAGO)

    fmt = "%Y%m%dT%H%M%S" if len(value) >= 15 else "%Y%m%dT%H%M"
    clipped = value[:15] if fmt.endswith("%S") else value[:13]
    dt = datetime.strptime(clipped, fmt)

    tzid = params.get("TZID")
    try:
        tz = ZoneInfo(tzid) if tzid else CHICAGO
    except Exception:
        tz = CHICAGO

    return dt.replace(tzinfo=tz).astimezone(CHICAGO)


def _category(title: str) -> str:
    text = str(title or "").casefold()
    schedule_words = (
        "no school",
        "no classes",
        "closed",
        "early dismissal",
        "early out",
        "dismissal",
        "school resumes",
        "first day",
        "last day",
        "break",
        "conference",
        "in-service",
        "inservice",
        "weather day",
        "holiday",
    )
    return "schedule" if any(word in text for word in schedule_words) else "general"


def _first_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", text or "")
    return match.group(0).rstrip(".,);]") if match else None


def _normalize_calendar_event(
    raw: dict[str, tuple[dict[str, str], str]]
) -> dict:
    def val(name: str) -> str:
        return raw.get(name, ({}, ""))[1]

    if val("STATUS").casefold() == "cancelled":
        return {}

    start_prop = raw.get("DTSTART")
    if not start_prop:
        return {}

    title = val("SUMMARY").strip()
    if not title:
        return {}

    start_obj = _parse_ical_dt(start_prop[1], start_prop[0])
    end_obj = None
    if raw.get("DTEND"):
        end_obj = _parse_ical_dt(raw["DTEND"][1], raw["DTEND"][0])

    uid = val("UID") or f"{title}|{start_prop[1]}|{val('LOCATION')}"
    digest = hashlib.sha1(uid.encode("utf-8")).hexdigest()[:18]

    start_day = start_obj.date() if isinstance(start_obj, datetime) else start_obj

    event: dict = {
        "id": f"holycross-calendar-{digest}",
        "title": title,
        "date": start_day.isoformat(),
        "schools": [SCHOOL_ID],
        "scope": "school",
        "category": _category(title),
        "source": CALENDAR_SOURCE_NAME,
        "sourceUrl": val("URL") or _first_url(val("DESCRIPTION")) or CALENDAR_PAGE,
    }

    if isinstance(start_obj, datetime):
        event["start"] = start_obj.strftime("%H:%M")
        if isinstance(end_obj, datetime):
            if end_obj.date() == start_obj.date():
                event["end"] = end_obj.strftime("%H:%M")
            elif end_obj.date() > start_obj.date():
                event["endDate"] = end_obj.date().isoformat()
    else:
        event["allDay"] = True
        # RFC 5545 all-day DTEND is exclusive.
        if isinstance(end_obj, date) and not isinstance(end_obj, datetime):
            inclusive_end = end_obj - timedelta(days=1)
            if inclusive_end > start_day:
                event["endDate"] = inclusive_end.isoformat()

    location = val("LOCATION").strip()
    if location:
        event["location"] = location

    description = val("DESCRIPTION").strip()
    if description:
        # Avoid dumping Google Calendar plumbing into the interface.
        clean = re.sub(r"https?://\S+", "", description)
        clean = re.sub(r"\s+", " ", clean).strip(" -–—")
        if clean and clean.casefold() != title.casefold():
            event["detail"] = clean[:500]

    return event


def parse_google_ics(text: str) -> list[dict]:
    events: list[dict] = []
    current: dict[str, tuple[dict[str, str], str]] | None = None

    for line in _unfold_ical(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue

        if line == "END:VEVENT":
            if current is not None:
                event = _normalize_calendar_event(current)
                if event:
                    events.append(event)
            current = None
            continue

        if current is None:
            continue

        prop = _split_ical_property(line)
        if not prop:
            continue

        name, params, value = prop
        # Keep the first occurrence of each field. That is enough for the
        # fields SchoolBoard consumes.
        if name not in current:
            current[name] = (params, value)

    unique: dict[tuple, dict] = {}
    for event in events:
        key = (
            event["date"],
            event.get("start", ""),
            event.get("endDate", ""),
            re.sub(r"[^a-z0-9]+", " ", event["title"].casefold()).strip(),
        )
        unique[key] = event

    return sorted(
        unique.values(),
        key=lambda e: (e["date"], e.get("start", ""), e["title"]),
    )


def fetch_holycross_calendar(*, timeout: int = 30, opener=urlopen) -> list[dict]:
    text = _request_text(
        GOOGLE_ICAL_URL,
        timeout=timeout,
        opener=opener,
        accept="text/calendar,text/plain;q=0.9,*/*;q=0.8",
    )

    if "BEGIN:VCALENDAR" not in text.upper():
        sample = " ".join(text.split())[:180]
        raise RuntimeError(
            "Holy Cross Google Calendar response was not valid iCalendar"
            + (f": {sample}" if sample else "")
        )

    events = parse_google_ics(text)

    print(
        "holycross-calendar detail: public Google calendar parsed "
        f"{len(events)} events"
    )
    if events:
        preview = "; ".join(
            f"{event['date']} {event['title']}"
            for event in events[:10]
        )
        print(f"holycross-calendar detail: first events: {preview}")

    return events


# ---------- Homepage upcoming dates ----------

class _VisibleTextParser(HTMLParser):
    BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "div", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "main", "nav",
        "p", "section", "td", "th", "tr",
    }
    SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return html_lib.unescape("".join(self.parts))


def _visible_lines(page_html: str) -> list[str]:
    parser = _VisibleTextParser()
    parser.feed(page_html)
    text = parser.text().replace("\xa0", " ")

    lines: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" \t•")
        if line:
            lines.append(line)
    return lines


def _resolve_event_date(month: int, day: int, reference: date) -> date:
    """
    Homepage dates omit the year. Choose the nearest valid occurrence in the
    previous/current/next year so the parser behaves sensibly around New Year.
    """
    candidates: list[date] = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue

    if not candidates:
        raise ValueError(f"Invalid homepage date: month={month}, day={day}")

    return min(candidates, key=lambda candidate: abs((candidate - reference).days))


def _homepage_event_id(start_day: date, title: str) -> str:
    key = f"{start_day.isoformat()}|{title.casefold()}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"holycross-home-{digest}"


def _event_from_date_line(
    line: str,
    *,
    reference: date,
    fallback_title: str | None = None,
) -> dict | None:
    line = re.sub(r"\s+", " ", line).strip()
    match = _RANGE_RE.match(line) or _SINGLE_RE.match(line)
    if not match:
        return None

    month = _MONTHS[match.group("month").upper()]
    day = int(match.group("day"))
    title = (match.groupdict().get("title") or fallback_title or "").strip()
    if not title:
        return None

    start_day = _resolve_event_date(month, day, reference)

    event: dict = {
        "id": _homepage_event_id(start_day, title),
        "title": title,
        "date": start_day.isoformat(),
        "schools": [SCHOOL_ID],
        "scope": "school",
        "category": _category(title),
        "allDay": True,
        "source": HOMEPAGE_SOURCE_NAME,
        "sourceUrl": HOMEPAGE_URL,
    }

    endday_text = match.groupdict().get("endday")
    if endday_text:
        endday = int(endday_text)
        try:
            end_day = date(start_day.year, month, endday)
        except ValueError:
            end_day = start_day
        if end_day >= start_day and end_day != start_day:
            event["endDate"] = end_day.isoformat()

    return event


def parse_homepage_upcoming_dates(
    page_html: str,
    *,
    reference: date,
) -> list[dict]:
    lines = _visible_lines(page_html)
    joined = "\n".join(lines).casefold()

    has_family_portal_notice = (
        "holy cross families" in joined
        and "family portal" in joined
        and "calendar" in joined
    )

    events: list[dict] = []
    pending_date_line: str | None = None

    for line in lines:
        # A complete date-led line, e.g.
        # "SATURDAY, SEPTEMBER 5 - LABOR DAY PARADE"
        complete = _event_from_date_line(line, reference=reference)
        if complete:
            events.append(complete)
            pending_date_line = None
            continue

        # Some WordPress layouts put the date and event title in separate
        # elements. Preserve a date-only line and pair it with the next useful
        # text line.
        if _RANGE_RE.match(line) or _SINGLE_RE.match(line):
            pending_date_line = line
            continue

        if pending_date_line:
            lower = line.casefold()
            if (
                "holy cross families" not in lower
                and "family portal" not in lower
                and len(line) <= 180
            ):
                paired = _event_from_date_line(
                    pending_date_line,
                    reference=reference,
                    fallback_title=line,
                )
                if paired:
                    events.append(paired)
            pending_date_line = None

    unique: dict[tuple, dict] = {}
    for event in events:
        key = (
            event["date"],
            event.get("endDate", ""),
            re.sub(r"[^a-z0-9]+", " ", event["title"].casefold()).strip(),
        )
        unique[key] = event

    result = sorted(
        unique.values(),
        key=lambda e: (e["date"], e.get("endDate", ""), e["title"]),
    )

    # If the expected public-facing notice is present but nothing parses,
    # assume the page structure changed rather than silently declaring the
    # source empty. A genuinely removed notice may return zero events.
    if has_family_portal_notice and not result:
        raise RuntimeError(
            "Holy Cross homepage still contains its Family Portal calendar "
            "notice, but no public upcoming dates could be parsed"
        )

    return result


def fetch_holycross_homepage(
    *,
    reference: date | None = None,
    timeout: int = 30,
    opener=urlopen,
) -> list[dict]:
    if reference is None:
        reference = datetime.now(CHICAGO).date()

    page_html = _request_text(
        HOMEPAGE_URL,
        timeout=timeout,
        opener=opener,
    )
    events = parse_homepage_upcoming_dates(page_html, reference=reference)

    print(
        "holycross-homepage detail: parsed "
        f"{len(events)} public upcoming-date events"
    )
    if events:
        preview = "; ".join(
            f"{event['date']} {event['title']}"
            for event in events[:10]
        )
        print(f"holycross-homepage detail: first events: {preview}")

    return events
