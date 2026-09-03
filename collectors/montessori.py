from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SCHOOL_ID = "montessoricu"
CALENDAR_PAGE = "https://montessorischoolofcu.org/schoolcalendar/"
SOURCE_NAME = "Montessori School of C-U Calendar"

CHICAGO = ZoneInfo("America/Chicago")

# Fallback only. Each run first rediscovers the public Google calendar from
# MSCU's own "Import Google Calendar" link.
KNOWN_CALENDAR_ID = (
    "c_698575808b24b686e549f08eb0c6aff899e4228321005fc1acd88206f138b98c"
    "@group.calendar.google.com"
)

MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
MONTH_TOKEN = (
    r"January|Jan|February|Feb|March|Mar|April|Apr|May|June|Jun|July|Jul|"
    r"August|Aug|September|Sept|Sep|October|Oct|November|Nov|December|Dec"
)


def _request_text(url, *, timeout=25, accept="text/html,*/*;q=0.8", opener=urlopen):
    request = Request(
        url,
        headers={
            "User-Agent": "ChambanaSchoolboard/1.0 (+public school calendar aggregator)",
            "Accept": accept,
        },
    )
    with opener(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _academic_year(reference):
    return (
        (reference.year, reference.year + 1)
        if reference.month >= 7
        else (reference.year - 1, reference.year)
    )


def _year_for_month(month, reference):
    start_year, end_year = _academic_year(reference)
    return start_year if month >= 7 else end_year


def _clean(value):
    value = unescape(str(value or ""))
    value = (
        value.replace("\\n", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )
    return re.sub(r"\s+", " ", value).strip(" \t\r\n|;:-")


def _event(
    *,
    title,
    start_day,
    end_day=None,
    start_time="",
    end_time="",
    location="",
):
    title = _clean(title)
    if not title or not re.search(r"[A-Za-z]", title):
        return None

    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:58] or "event"
    event = {
        "id": f"montessoricu-{start_day.isoformat()}-{slug}",
        "title": title,
        "date": start_day.isoformat(),
        "schools": [SCHOOL_ID],
        "scope": "school",
        "category": "general",
        "source": SOURCE_NAME,
        "sourceUrl": CALENDAR_PAGE,
    }

    if start_time:
        event["start"] = start_time
        if end_time:
            event["end"] = end_time
    else:
        event["allDay"] = True

    if end_day and end_day > start_day:
        event["endDate"] = end_day.isoformat()

    if location:
        event["location"] = _clean(location)

    return event


def _discover_calendar_id(html):
    decoded = unescape(html).replace(r"\/", "/")
    urls = re.findall(
        r'https?://calendar\.google\.com/calendar/embed\?[^"\'<>\s]+',
        decoded,
        flags=re.I,
    )
    for raw_url in urls:
        url = raw_url.replace("&amp;", "&")
        try:
            src = (parse_qs(urlparse(url).query).get("src") or [""])[0]
        except Exception:
            src = ""
        src = unquote(src).strip()
        if src.endswith("@group.calendar.google.com"):
            return src

    match = re.search(
        r'(?:[?&]|&amp;)src=([^&"\'<>\s]+%40group\.calendar\.google\.com)',
        decoded,
        flags=re.I,
    )
    if match:
        return unquote(match.group(1)).strip()

    return ""


def _ics_url(calendar_id):
    return (
        "https://calendar.google.com/calendar/ical/"
        + quote(calendar_id, safe="")
        + "/public/basic.ics"
    )


def _unfold_ics(text):
    output = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and output:
            output[-1] += line[1:]
        else:
            output.append(line)
    return output


def _parse_dt(value, params=""):
    value = value.strip()

    if "VALUE=DATE" in params.upper() or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date(), "", True

    tz = CHICAGO
    match = re.search(r"TZID=([^;:]+)", params, flags=re.I)
    if match:
        try:
            tz = ZoneInfo(match.group(1).strip('"'))
        except Exception:
            tz = CHICAGO

    if value.endswith("Z"):
        dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=ZoneInfo("UTC")
        ).astimezone(CHICAGO)
    else:
        fmt = "%Y%m%dT%H%M%S" if len(value) >= 15 else "%Y%m%dT%H%M"
        dt = datetime.strptime(value, fmt).replace(tzinfo=tz).astimezone(CHICAGO)

    return dt.date(), dt.strftime("%H:%M"), False


def parse_ics(text):
    events = []
    current = None

    for line in _unfold_ics(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue

        if line == "END:VEVENT":
            if current:
                status = current.get("STATUS", ("", ""))[1].casefold()
                if status != "cancelled":
                    title = current.get("SUMMARY", ("", ""))[1]
                    start_params, start_value = current.get("DTSTART", ("", ""))
                    end_params, end_value = current.get("DTEND", ("", ""))
                    location = current.get("LOCATION", ("", ""))[1]

                    if title and start_value:
                        try:
                            start_day, start_time, all_day = _parse_dt(
                                start_value, start_params
                            )
                            end_day = None
                            end_time = ""

                            if end_value:
                                parsed_end_day, parsed_end_time, end_all_day = _parse_dt(
                                    end_value, end_params
                                )
                                if all_day and end_all_day:
                                    parsed_end_day -= timedelta(days=1)
                                if parsed_end_day > start_day:
                                    end_day = parsed_end_day
                                elif parsed_end_day == start_day:
                                    end_time = parsed_end_time

                            item = _event(
                                title=title,
                                start_day=start_day,
                                end_day=end_day,
                                start_time=start_time,
                                end_time=end_time,
                                location=location,
                            )
                            if item:
                                events.append(item)
                        except Exception:
                            pass
            current = None
            continue

        if current is None or ":" not in line:
            continue

        left, value = line.split(":", 1)
        name, *params = left.split(";")
        name = name.upper()
        if name in {"DTSTART", "DTEND", "SUMMARY", "LOCATION", "STATUS"}:
            current[name] = (";".join(params), value)

    unique = {}
    for item in events:
        key = (
            item["date"],
            item.get("start", ""),
            item.get("endDate", ""),
            re.sub(r"[^a-z0-9]+", " ", item["title"].casefold()).strip(),
        )
        unique[key] = item

    return sorted(
        unique.values(),
        key=lambda e: (e["date"], e.get("start", ""), e["title"]),
    )


class _TextParser(HTMLParser):
    BREAKS = {"br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.casefold() in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        return "".join(self.parts)


HTML_DATE_RE = re.compile(
    rf"(?P<m1>{MONTH_TOKEN})\s+"
    r"(?P<d1>\d{1,2})(?:st|nd|rd|th)?"
    rf"(?:\s*(?:-|–|—|and|to)\s*(?:(?P<m2>{MONTH_TOKEN})\s+)?"
    r"(?P<d2>\d{1,2})(?:st|nd|rd|th)?)?",
    flags=re.I,
)


def parse_html_calendar(html, *, reference):
    parser = _TextParser()
    parser.feed(html)
    text = unescape(parser.text())
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)

    start = re.search(r"2026\s*-\s*2027\s+Calendar", text, flags=re.I)
    if start:
        text = text[start.end():]

    stop = re.search(
        r"Board of Directors.? Meetings|All dates are subject to change",
        text,
        flags=re.I,
    )
    if stop:
        text = text[:stop.start()]

    matches = list(HTML_DATE_RE.finditer(text))
    events = []

    for i, match in enumerate(matches):
        m1 = MONTHS[match.group("m1").casefold()]
        m2 = MONTHS[(match.group("m2") or match.group("m1")).casefold()]
        d1 = int(match.group("d1"))
        d2 = int(match.group("d2")) if match.group("d2") else None

        year1 = _year_for_month(m1, reference)
        year2 = year1 + 1 if m2 < m1 else _year_for_month(m2, reference)

        try:
            start_day = date(year1, m1, d1)
        except ValueError:
            continue

        end_day = None
        if d2 is not None:
            try:
                end_day = date(year2, m2, d2)
            except ValueError:
                pass

        tail_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        title = text[match.end():tail_end]
        title = re.sub(
            r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
            r"Mon|Tue|Tues|Wed|Thu|Thurs|Fri|Sat|Sun)"
            r"(?:\s*-\s*(?:Mon|Tue|Tues|Wed|Thu|Thurs|Fri))?\b",
            " ",
            title,
            flags=re.I,
        )
        title = _clean(title)

        if not title or len(title) > 180:
            continue

        item = _event(title=title, start_day=start_day, end_day=end_day)
        if item:
            events.append(item)

    unique = {}
    for item in events:
        key = (
            item["date"],
            item.get("endDate", ""),
            re.sub(r"[^a-z0-9]+", " ", item["title"].casefold()).strip(),
        )
        unique[key] = item

    return sorted(
        unique.values(),
        key=lambda e: (e["date"], e.get("endDate", ""), e["title"]),
    )


def fetch_montessori_calendar(
    *,
    reference=None,
    timeout=25,
    opener=urlopen,
):
    reference = reference or date.today()

    html = ""
    try:
        html = _request_text(CALENDAR_PAGE, timeout=timeout, opener=opener)
    except Exception as exc:
        print(
            "montessori-calendar detail: school calendar page fetch failed: "
            f"{type(exc).__name__}: {exc}"
        )

    calendar_id = _discover_calendar_id(html) if html else ""
    if calendar_id:
        print(
            "montessori-calendar detail: discovered public Google calendar "
            "from MSCU Import Google Calendar control"
        )
    else:
        calendar_id = KNOWN_CALENDAR_ID
        print(
            "montessori-calendar detail: using current known public Google "
            "calendar ID fallback"
        )

    try:
        ics = _request_text(
            _ics_url(calendar_id),
            timeout=timeout,
            accept="text/calendar,*/*;q=0.8",
            opener=opener,
        )
        if "BEGIN:VCALENDAR" not in ics:
            raise RuntimeError("Google response was not iCalendar")

        events = parse_ics(ics)
        if len(events) < 8:
            raise RuntimeError(
                f"public Google calendar produced only {len(events)} usable events"
            )

        preview = "; ".join(
            f"{event['date']} {event['title']}"
            for event in events[:10]
        )
        print(
            f"montessori-calendar detail: public Google calendar parsed "
            f"{len(events)} events"
        )
        if preview:
            print(f"montessori-calendar detail: first events: {preview}")
        return events

    except Exception as google_exc:
        if not html:
            raise RuntimeError(
                "MSCU Google calendar failed and HTML fallback was unavailable: "
                f"{type(google_exc).__name__}: {google_exc}"
            ) from google_exc

        events = parse_html_calendar(html, reference=reference)
        if len(events) < 8:
            raise RuntimeError(
                "MSCU Google calendar failed and HTML fallback produced only "
                f"{len(events)} events. Google error: "
                f"{type(google_exc).__name__}: {google_exc}"
            ) from google_exc

        preview = "; ".join(
            f"{event['date']} {event['title']}"
            for event in events[:10]
        )
        print(
            "montessori-calendar detail: Google calendar unavailable; "
            f"HTML fallback parsed {len(events)} events"
        )
        if preview:
            print(f"montessori-calendar detail: first fallback events: {preview}")
        return events
