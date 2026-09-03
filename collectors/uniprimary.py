from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SCHOOL_ID = "uniprimary"
CALENDAR_ID = "7187"
CALENDAR_PAGE = f"https://calendars.illinois.edu/list/{CALENDAR_ID}"
ICAL_URL = f"https://calendars.illinois.edu/icalOutlook/{CALENDAR_ID}.ics"
SOURCE_NAME = "University Primary School Calendar"

CHICAGO = ZoneInfo("America/Chicago")


def _unfold_ical(text: str) -> list[str]:
    """RFC 5545 line unfolding."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for line in raw:
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _unescape(value: str) -> str:
    return (
        value.replace("\\N", "\n")
        .replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _split_property(line: str):
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
    return name, params, _unescape(value)


def _parse_dt(value: str, params: dict[str, str]) -> date | datetime:
    value = value.strip()

    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date()

    if value.endswith("Z"):
        dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
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
    text = title.casefold()
    schedule_words = (
        "no school",
        "no classes",
        "early dismissal",
        "dismissal",
        "school resumes",
        "first day",
        "first days",
        "last day",
        "faculty day",
        "break",
        "conference",
        "weather day",
        "holiday",
    )
    return "schedule" if any(word in text for word in schedule_words) else "general"


def _first_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", text or "")
    return match.group(0).rstrip(".,);]") if match else None


def _normalize_event(raw: dict[str, tuple[dict[str, str], str]]) -> dict:
    def val(name: str) -> str:
        return raw.get(name, ({}, ""))[1]

    status = val("STATUS").casefold()
    if status == "cancelled":
        return {}

    start_prop = raw.get("DTSTART")
    if not start_prop:
        return {}

    title = val("SUMMARY").strip()
    if not title:
        return {}

    start_obj = _parse_dt(start_prop[1], start_prop[0])

    end_obj = None
    if raw.get("DTEND"):
        end_obj = _parse_dt(raw["DTEND"][1], raw["DTEND"][0])

    uid = val("UID") or f"{title}|{start_prop[1]}|{val('LOCATION')}"
    digest = hashlib.sha1(uid.encode("utf-8")).hexdigest()[:18]

    start_day = (
        start_obj.date()
        if isinstance(start_obj, datetime)
        else start_obj
    )

    event: dict = {
        "id": f"uniprimary-{digest}",
        "title": title,
        "date": start_day.isoformat(),
        "schools": [SCHOOL_ID],
        "scope": "school",
        "category": _category(title),
        "source": SOURCE_NAME,
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
        # All-day iCalendar DTEND is exclusive.
        if isinstance(end_obj, date) and not isinstance(end_obj, datetime):
            inclusive_end = end_obj - timedelta(days=1)
            if inclusive_end > start_day:
                event["endDate"] = inclusive_end.isoformat()

    location = val("LOCATION").strip()
    if location:
        event["location"] = location

    return event


def parse_ics(text: str) -> list[dict]:
    events: list[dict] = []
    current: dict[str, tuple[dict[str, str], str]] | None = None

    for line in _unfold_ical(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue

        if line == "END:VEVENT":
            if current is not None:
                item = _normalize_event(current)
                if item:
                    events.append(item)
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


def fetch_uniprimary_calendar(*, timeout: int = 30, opener=urlopen) -> list[dict]:
    request = Request(
        ICAL_URL,
        headers={
            "User-Agent": "ChambanaSchoolboard/1.0 (+public school calendar aggregator)",
            "Accept": "text/calendar,text/plain;q=0.9,*/*;q=0.8",
        },
    )

    with opener(request, timeout=timeout) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"

    text = body.decode(charset, errors="replace")

    if "BEGIN:VCALENDAR" not in text.upper():
        sample = " ".join(text.split())[:180]
        raise RuntimeError(
            "University Primary calendar response was not valid iCalendar"
            + (f": {sample}" if sample else "")
        )

    events = parse_ics(text)
    if not events:
        raise RuntimeError("University Primary calendar returned zero usable events")

    preview = "; ".join(
        f"{event['date']} {event['title']}"
        for event in events[:10]
    )
    print(
        f"uniprimary-calendar detail: Illinois WebTools calendar parsed "
        f"{len(events)} events"
    )
    if preview:
        print(f"uniprimary-calendar detail: first events: {preview}")

    return events
