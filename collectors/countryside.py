from __future__ import annotations

import re
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.request import Request, urlopen

CALENDAR_URL = "https://www.countrysideschool.org/calendar"
SOURCE_NAME = "Countryside School Calendar"
SCHOOL_ID = "countryside"

SPORT_CATEGORIES = {
    "athletics",
    "boys basketball",
    "girls basketball",
    "scholastic bowl",
    "soccer",
    "volleyball",
}
KNOWN_CATEGORIES = SPORT_CATEGORIES | {"admissions", "alumni"}

MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

MONTH_YEAR_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})$",
    re.I,
)
DAY_RE = re.compile(
    r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})$",
    re.I,
)
TIME_RANGE_RE = re.compile(
    r"^(\d{1,2}:\d{2}\s*[AP]M)\s*[-–]\s*(\d{1,2}:\d{2}\s*[AP]M)$",
    re.I,
)
ONE_TIME_RE = re.compile(r"^(\d{1,2}:\d{2}\s*[AP]M)$", re.I)

MONTH_YEAR_SEARCH_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{4})\b",
    re.I,
)
DAY_FINDER_RE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s+"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(\d{1,2})\b",
    re.I,
)


class _VisibleTextParser(HTMLParser):
    """Extract server-rendered visible text without relying on Finalsite JS."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.lines: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        low = tag.casefold()
        if low in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        low = tag.casefold()
        if low in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        for part in str(data or "").splitlines():
            clean = re.sub(r"\s+", " ", unescape(part)).strip()
            if clean:
                self.lines.append(clean)


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _to_24h(value: str) -> str:
    return datetime.strptime(_compact(value).upper(), "%I:%M %p").strftime("%H:%M")


def _infer_year(month: int, visible_month: int, visible_year: int) -> int:
    # Finalsite's month grid shows a few days from the month before/after.
    if visible_month == 1 and month == 12:
        return visible_year - 1
    if visible_month == 12 and month == 1:
        return visible_year + 1
    return visible_year


def _category_for(title: str, category_label: str | None) -> str:
    low_title = title.casefold()
    low_cat = (category_label or "").casefold()

    if low_cat in SPORT_CATEGORIES:
        return "athletics"
    if any(token in low_title for token in (
        "no school",
        "dismissal",
        "inservice",
        "first day",
        "no extended day",
        "parent-teacher conference",
    )):
        return "schedule"
    return "general"


def _slug(day: date, title: str, start: str | None) -> str:
    core = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:52]
    stamp = (start or "all-day").replace(":", "")
    return f"countryside-{day.isoformat()}-{stamp}-{core}"


def _make_event(
    *,
    event_day: date,
    title: str,
    category_label: str | None = None,
    start: str | None = None,
    end: str | None = None,
    location: str | None = None,
) -> dict:
    event = {
        "id": _slug(event_day, title, start),
        "title": title,
        "date": event_day.isoformat(),
        "schools": [SCHOOL_ID],
        "scope": "school",
        "category": _category_for(title, category_label),
        "source": SOURCE_NAME,
        "sourceUrl": CALENDAR_URL,
    }
    if start:
        event["start"] = start
        if end:
            event["end"] = end
    else:
        event["allDay"] = True
    if location:
        event["location"] = location
    return event


TIME_FRAGMENT_RE = re.compile(r"^[0-9APMapm:.\-–\s]+$")
ADDRESS_LIKE_RE = re.compile(
    r"(?:\b\d{2,6}\s+\w+)|(?:\b(?:Ave|Avenue|St|Street|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane)\b)|"
    r"(?:\bChampaign\b)|(?:\bUrbana\b)|(?:\bIL\s+\d{5}\b)|(?:\bUSA\b)",
    re.I,
)


def _normalize_time_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s*:\s*", ":", value)
    value = re.sub(r"\s*[-–]\s*", " - ", value)
    value = re.sub(r"\b([AP])\s+M\b", r"\1M", value, flags=re.I)
    return value.strip()


def _repair_fragmented_times(lines: list[str]) -> list[str]:
    """Reassemble Finalsite times split across adjacent HTML text nodes."""
    repaired: list[str] = []
    i = 0

    while i < len(lines):
        current = _compact(lines[i])

        # Already a complete time: keep it.
        normalized = _normalize_time_text(current)
        if TIME_RANGE_RE.match(normalized) or ONE_TIME_RE.match(normalized):
            repaired.append(normalized)
            i += 1
            continue

        # Only attempt joining when the first piece itself looks like a time token.
        if not TIME_FRAGMENT_RE.match(current):
            repaired.append(current)
            i += 1
            continue

        found = None
        pieces = []
        # A range such as "4 : 30 PM - 6 : 00 PM" can span many tiny nodes.
        for j in range(i, min(len(lines), i + 12)):
            piece = _compact(lines[j])
            if not TIME_FRAGMENT_RE.match(piece):
                break
            pieces.append(piece)
            candidate = _normalize_time_text(" ".join(pieces))
            if TIME_RANGE_RE.match(candidate) or ONE_TIME_RE.match(candidate):
                found = (candidate, j + 1)

        if found:
            repaired.append(found[0])
            i = found[1]
        else:
            repaired.append(current)
            i += 1

    return repaired


def _can_be_all_day_title(line: str) -> bool:
    """Reject HTML/time/location debris as standalone calendar events."""
    clean = _compact(line)
    if not clean:
        return False

    low = clean.casefold()
    if low in {
        "am", "pm", "a.m.", "p.m.", ":", "-", "–",
        "all day", "calendar & category legend:", "calendar & category legend",
    }:
        return False

    # Numbers or punctuation alone are never event names.
    if re.fullmatch(r"[\d\s:.\-–]+", clean):
        return False

    # A stray time fragment should never be promoted to an all-day event.
    normalized = _normalize_time_text(clean)
    if (
        TIME_FRAGMENT_RE.fullmatch(clean)
        or TIME_RANGE_RE.match(normalized)
        or ONE_TIME_RE.match(normalized)
    ):
        return False

    # Locations belong to timed event cards, not to independent all-day events.
    if ADDRESS_LIKE_RE.search(clean):
        return False

    return True


def _parse_day_block(lines: list[str], event_day: date) -> list[dict]:
    """Parse one Finalsite day cell from its server-rendered text."""
    lines = [_compact(x) for x in lines if _compact(x)]
    lines = _repair_fragmented_times(lines)
    if not lines:
        return []

    # Identify all time-bearing events first. The line immediately before a
    # time is the title; a recognized category immediately before that title
    # is metadata, not a separate event.
    timed = []
    title_indices: set[int] = set()
    time_indices: set[int] = set()
    category_indices: set[int] = set()

    for i, line in enumerate(lines):
        range_match = TIME_RANGE_RE.match(line)
        one_match = ONE_TIME_RE.match(line)
        if not range_match and not one_match:
            continue

        title_i = i - 1
        while title_i >= 0 and lines[title_i].casefold() in KNOWN_CATEGORIES:
            category_indices.add(title_i)
            title_i -= 1
        if title_i < 0:
            continue

        title = lines[title_i]
        title_indices.add(title_i)
        time_indices.add(i)

        category = None
        if title_i - 1 >= 0 and lines[title_i - 1].casefold() in KNOWN_CATEGORIES:
            category = lines[title_i - 1]
            category_indices.add(title_i - 1)

        if range_match:
            start = _to_24h(range_match.group(1))
            end = _to_24h(range_match.group(2))
        else:
            start = _to_24h(one_match.group(1))
            end = None

        timed.append({
            "title_i": title_i,
            "time_i": i,
            "title": title,
            "category": category,
            "start": start,
            "end": end,
        })

    consumed: set[int] = set(title_indices) | set(time_indices) | set(category_indices)
    events: list[dict] = []

    # Attach obvious location text following each time until the next event title,
    # time, or category. In the public Countryside calendar this captures entries
    # such as "in our backyard" and full away-game addresses.
    all_title_indices = sorted(title_indices)
    for record in timed:
        i = record["time_i"] + 1
        boundary = len(lines)
        later_titles = [idx for idx in all_title_indices if idx > record["time_i"]]
        if later_titles:
            boundary = min(boundary, later_titles[0])

        location_parts = []
        while i < boundary:
            low = lines[i].casefold()
            if (
                i in consumed
                or low in KNOWN_CATEGORIES
                or TIME_RANGE_RE.match(lines[i])
                or ONE_TIME_RE.match(lines[i])
            ):
                break
            location_parts.append(lines[i])
            consumed.add(i)
            i += 1

        events.append(_make_event(
            event_day=event_day,
            title=record["title"],
            category_label=record["category"],
            start=record["start"],
            end=record["end"],
            location=" · ".join(location_parts) if location_parts else None,
        ))

    # Anything still unconsumed is an all-day event. This correctly handles
    # days such as Aug. 18, where "Inservice - faculty/staff" is followed by
    # the separately timed "Welcome Back Night".
    current_category = None
    for i, line in enumerate(lines):
        if i in consumed:
            continue
        low = line.casefold()
        if low in KNOWN_CATEGORIES:
            current_category = line
            continue
        if low in {
            "all day",
            "calendar & category legend:",
            "calendar & category legend",
        }:
            continue
        if TIME_RANGE_RE.match(line) or ONE_TIME_RE.match(line):
            continue
        if not _can_be_all_day_title(line):
            continue

        events.append(_make_event(
            event_day=event_day,
            title=line,
            category_label=current_category,
        ))
        current_category = None

    return events


def parse_countryside_html(html: str) -> list[dict]:
    parser = _VisibleTextParser()
    parser.feed(html)
    lines = parser.lines

    # Finalsite sometimes splits a displayed date across adjacent text nodes
    # (for example "Sunday," + "August 9"). Scan the combined visible-text
    # stream instead of requiring the whole date to live in one HTML text node.
    visible_text = "\n".join(lines)

    month_match = MONTH_YEAR_SEARCH_RE.search(visible_text)
    if not month_match:
        raise RuntimeError("Countryside calendar month heading was not found in server HTML")

    visible_month = MONTHS[month_match.group(1).casefold()]
    visible_year = int(month_match.group(2))

    # Exclude the category legend/footer from event parsing.
    legend_match = re.search(
        r"Calendar\s*&\s*Category\s*Legend\s*:?",
        visible_text,
        flags=re.I,
    )
    calendar_text = visible_text[:legend_match.start()] if legend_match else visible_text

    day_matches = list(DAY_FINDER_RE.finditer(calendar_text))
    if not day_matches:
        # Tiny diagnostic only: enough to reveal how Finalsite formatted the
        # calendar heading/date area without dumping the page.
        sample = re.sub(r"\s+", " ", calendar_text[calendar_text.find(str(visible_year)):])[:500]
        raise RuntimeError(
            "Countryside calendar contained no dated day cells; "
            f"calendar text sample={sample!r}"
        )

    events: list[dict] = []

    for pos, match in enumerate(day_matches):
        month = MONTHS[match.group(1).casefold()]
        year = _infer_year(month, visible_month, visible_year)
        try:
            event_day = date(year, month, int(match.group(2)))
        except ValueError:
            continue

        next_start = day_matches[pos + 1].start() if pos + 1 < len(day_matches) else len(calendar_text)
        block_text = calendar_text[match.end():next_start]

        # Preserve meaningful line boundaries where Finalsite provides them,
        # while eliminating empty/whitespace-only fragments.
        block_lines = [
            re.sub(r"\s+", " ", part).strip()
            for part in block_text.splitlines()
            if re.sub(r"\s+", " ", part).strip()
        ]
        events.extend(_parse_day_block(block_lines, event_day))

    # Finalsite can duplicate content for responsive layouts. Stable IDs remove
    # those duplicates while keeping same-day events with different titles/times.
    by_id: dict[str, dict] = {}
    for event in events:
        by_id[event["id"]] = event

    return sorted(
        by_id.values(),
        key=lambda e: (e.get("date", ""), e.get("start", ""), e.get("title", "")),
    )


def fetch_countryside_calendar(*, timeout: int = 30, opener=urlopen) -> list[dict]:
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
    events = parse_countryside_html(html)

    if not events:
        raise RuntimeError("Countryside calendar loaded but no events were parsed")

    first_dates = sorted({e["date"] for e in events})[:6]
    print(
        f"countryside-calendar detail: parsed {len(events)} events "
        f"from server-rendered month grid; first event dates "
        f"{', '.join(first_dates)}"
    )
    return events
