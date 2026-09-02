from __future__ import annotations

import re
from datetime import date
from html.parser import HTMLParser
from urllib.request import Request, urlopen

CALENDAR_URL = "https://www.usd116.org/calendar/"
SOURCE_NAME = "USD 116 School Calendar"

ALL_USD116 = ["yankee", "leal", "paine", "williams", "king", "sgc", "ums", "uhs"]
K5 = ["yankee", "leal", "paine", "williams", "king"]

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

DATE_LINE_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2})"
    r"(?:\s*[-–]\s*(?:(January|February|March|April|May|June|July|August|September|October|November|December)\s+)?(\d{1,2}))?"
    r"\s*:\s*(.+)$",
    re.I,
)


class _ListItemParser(HTMLParser):
    """Collect <li> text, including nested child-list text in each parent item."""
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[list[str]] = []
        self.items: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() == "li":
            self.stack.append([])

    def handle_data(self, data: str) -> None:
        if not self.stack:
            return
        clean = re.sub(r"\s+", " ", data).strip()
        if not clean:
            return
        # Nested text is useful to the parent too: e.g. Staff Development Day
        # has child bullets that contain the grade-specific dismissal rules.
        for bucket in self.stack:
            bucket.append(clean)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "li" or not self.stack:
            return
        bucket = self.stack.pop()
        text = re.sub(r"\s+", " ", " ".join(bucket)).strip()
        if text:
            self.items.append(text)


def _school_year_for_month(month: int) -> int:
    # USD 116 school-year calendar runs Aug 2026 through Jun 2027.
    return 2026 if month >= 7 else 2027


def _parse_date_line(text: str):
    text = re.sub(r"\s+", " ", text).strip()
    m = DATE_LINE_RE.match(text)
    if not m:
        return None

    m1_name, d1_s, m2_name, d2_s, description = m.groups()
    m1 = MONTHS[m1_name.casefold()]
    y1 = _school_year_for_month(m1)
    start = date(y1, m1, int(d1_s))

    end = None
    if d2_s:
        m2 = MONTHS[(m2_name or m1_name).casefold()]
        y2 = y1
        if m2 < m1:
            y2 += 1
        end = date(y2, m2, int(d2_s))

    return start, end, re.sub(r"\s+", " ", description).strip()


def _time24(label: str) -> str:
    hour, minute = map(int, label.split(":"))
    # All dismissal times on the district calendar are afternoon times.
    if 1 <= hour <= 11:
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def _event(
    *,
    event_id: str,
    title: str,
    day: date,
    schools: list[str],
    start: str | None = None,
    end: str | None = None,
    end_date: date | None = None,
    category: str = "schedule",
) -> dict:
    out = {
        "id": event_id,
        "title": title,
        "date": day.isoformat(),
        "schools": list(schools),
        "scope": "district",
        "category": category,
        "source": SOURCE_NAME,
        "sourceUrl": CALENDAR_URL,
    }
    if start:
        out["start"] = start
    else:
        out["allDay"] = True
    if end:
        out["end"] = end
    if end_date and end_date != day:
        out["endDate"] = end_date.isoformat()
        out["weekdaysOnly"] = True
    return out


def _slug(day: date, suffix: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", suffix.casefold()).strip("-")
    return f"usd-live-{day.isoformat()}-{clean[:48]}"


def _split_semantics(start_day: date, end_day: date | None, description: str) -> list[dict]:
    lower = description.casefold()
    events: list[dict] = []

    # Ignore entries that apply only to the district's early-childhood school,
    # which is not currently one of SchoolBoard's selectable USD 116 schools.
    if (
        ("urbana early childhood" in lower or "uecs" in lower)
        and not any(token in lower for token in ("grades 1", "grades 1–", "grades 1-", "early dismissal", "all students"))
        and "staff development" not in lower
    ):
        return []

    # Staff-development entries have two different experiences: K-5 is out,
    # while the secondary schools dismiss early at school-specific times.
    if "staff development day" in lower:
        events.append(_event(
            event_id=_slug(start_day, "k5-staff-development"),
            title="Staff Development — No School",
            day=start_day,
            schools=K5,
        ))
        for code, school_id in (("UMS", "ums"), ("UHS", "uhs"), ("SGC", "sgc")):
            match = re.search(rf"{code}\s*@\s*(\d{{1,2}}:\d{{2}})", description, flags=re.I)
            if match:
                t = _time24(match.group(1))
                events.append(_event(
                    event_id=_slug(start_day, f"{school_id}-early-dismissal"),
                    title=f"Early Dismissal — {match.group(1)} PM",
                    day=start_day,
                    schools=[school_id],
                    start=t,
                ))
        return events

    # Last-day entry includes four school-level dismissal times.
    if "early dismissal" in lower and "elementary @" in lower:
        match = re.search(r"Elementary\s*@\s*(\d{1,2}:\d{2})", description, flags=re.I)
        if match:
            events.append(_event(
                event_id=_slug(start_day, "elementary-early-dismissal"),
                title=f"Early Dismissal — {match.group(1)} PM",
                day=start_day,
                schools=K5,
                start=_time24(match.group(1)),
            ))
        for code, school_id in (("UMS", "ums"), ("UHS", "uhs"), ("SGC", "sgc")):
            match = re.search(rf"{code}\s*@\s*(\d{{1,2}}:\d{{2}})", description, flags=re.I)
            if match:
                events.append(_event(
                    event_id=_slug(start_day, f"{school_id}-early-dismissal"),
                    title=f"Early Dismissal — {match.group(1)} PM",
                    day=start_day,
                    schools=[school_id],
                    start=_time24(match.group(1)),
                ))
        if "end of 4th quarter" in lower:
            events.append(_event(
                event_id=_slug(start_day, "end-fourth-quarter"),
                title="End of 4th Quarter",
                day=start_day,
                schools=ALL_USD116,
                category="general",
            ))
        return events

    # Timed conference entries.
    if "student-led conferences" in lower or "student led" in lower:
        if "4-8" in lower or "4–8" in lower:
            return [_event(
                event_id=_slug(start_day, "student-led-conferences-evening"),
                title="Student-Led Conferences",
                day=start_day,
                schools=ALL_USD116,
                start="16:00",
                end="20:00",
                category="general",
            )]
        if "8-11" in lower or "8–11" in lower:
            return [_event(
                event_id=_slug(start_day, "student-led-conferences-no-school"),
                title="No School — Student-Led Conferences",
                day=start_day,
                schools=ALL_USD116,
                start="08:00",
                end="11:00",
            )]

    # First-day/staggered-start language.
    if start_day == date(2026, 8, 17):
        return [_event(
            event_id=_slug(start_day, "grades-1-9-half-k"),
            title="First Day — Grades 1–9 + Half of Kindergarten",
            day=start_day,
            schools=ALL_USD116,
            category="general",
        )]
    if start_day == date(2026, 8, 18):
        return [_event(
            event_id=_slug(start_day, "grades-1-12-half-k"),
            title="Grades 1–12 + Half of Kindergarten in Attendance",
            day=start_day,
            schools=ALL_USD116,
            category="general",
        )]
    if start_day == date(2026, 8, 19):
        return [_event(
            event_id=_slug(start_day, "all-students"),
            title="All Students in Attendance",
            day=start_day,
            schools=ALL_USD116,
            category="general",
        )]

    # Clean recurring district wording.
    title = description
    replacements = [
        (r"\s*[-–]\s*No School for Students\s*$", " — No School"),
        (r"\s*[-–]\s*No School\s*$", " — No School"),
    ]
    for pattern, repl in replacements:
        title = re.sub(pattern, repl, title, flags=re.I)

    category = "schedule" if any(
        token in lower for token in ("no school", "break", "holiday", "institute day")
    ) else "general"

    return [_event(
        event_id=_slug(start_day, title),
        title=title,
        day=start_day,
        end_date=end_day,
        schools=ALL_USD116,
        category=category,
    )]


def parse_usd116_calendar_html(html: str) -> list[dict]:
    parser = _ListItemParser()
    parser.feed(html)

    events: list[dict] = []
    seen: set[tuple] = set()

    for item in parser.items:
        parsed = _parse_date_line(item)
        if not parsed:
            continue
        start_day, end_day, description = parsed

        # Restrict to the published 2026-27 school year; this also ignores
        # unrelated dated navigation/content elsewhere on the page.
        if not (date(2026, 8, 1) <= start_day <= date(2027, 6, 30)):
            continue

        for event in _split_semantics(start_day, end_day, description):
            key = (
                event.get("date"),
                event.get("title"),
                tuple(event.get("schools") or []),
                event.get("start"),
            )
            if key in seen:
                continue
            seen.add(key)
            events.append(event)

    return sorted(events, key=lambda e: (
        e.get("date", ""), e.get("start", ""), e.get("title", "")
    ))


def fetch_usd116_calendar(*, timeout: int = 30, opener=urlopen) -> list[dict]:
    request = Request(
        CALENDAR_URL,
        headers={
            "User-Agent": "ChambanaSchoolboard/1.0 (+public school calendar aggregator)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with opener(request, timeout=timeout) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"

    html = body.decode(charset, errors="replace")
    events = parse_usd116_calendar_html(html)
    if not events:
        raise RuntimeError("USD 116 calendar page loaded but no 2026-27 calendar events were parsed")
    return events
