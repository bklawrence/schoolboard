from __future__ import annotations

import io
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from pypdf import PdfReader


CALENDAR_PAGE = "https://stjohnls.com/calendar/school-calendar"
ACTIVITY_CALENDAR_PAGE = "https://stjohnls.com/calendar/activity-calendar"
SOURCE_NAME = "St. John Lutheran Activity Calendar"
SCHOOL_ID = "stjohn"

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

SKIP_TITLES = {
    "school calendar",
    "school year calendar",
    "calendar",
    "m", "tu", "tue", "w", "th", "thu", "f", "fri",
    "s", "sat", "sun",
}


@dataclass(frozen=True)
class PdfCandidate:
    url: str
    label: str = ""


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[PdfCandidate] = []
        self._active_href: str | None = None
        self._active_text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = {str(k).casefold(): str(v) for k, v in attrs if k and v is not None}
        low = tag.casefold()

        if low == "a":
            self._active_href = attrs.get("href")
            self._active_text = []

        for key in ("src", "href", "data-src"):
            value = attrs.get(key)
            if value and _looks_calendarish(value):
                self.links.append(PdfCandidate(value, ""))

    def handle_data(self, data: str) -> None:
        if self._active_href is not None:
            clean = re.sub(r"\s+", " ", data).strip()
            if clean:
                self._active_text.append(clean)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._active_href is not None:
            if _looks_calendarish(self._active_href):
                self.links.append(
                    PdfCandidate(
                        self._active_href,
                        " ".join(self._active_text).strip(),
                    )
                )
            self._active_href = None
            self._active_text = []


def _looks_calendarish(url: str) -> bool:
    clean = unescape(str(url or "")).strip()
    low = clean.casefold()
    return (
        ".pdf" in low
        or "cdn_url.pdf" in low
        or ("beehively" in low and "file" in low)
    )


def _request(
    url: str,
    *,
    timeout: int = 25,
    accept: str = "*/*",
    opener=urlopen,
) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "ChambanaSchoolboard/1.0 (+public school calendar aggregator)",
            "Accept": accept,
        },
    )
    with opener(request, timeout=timeout) as response:
        return response.read()


def discover_current_pdf(
    *,
    timeout: int = 25,
    opener=urlopen,
) -> PdfCandidate:
    html = _request(
        CALENDAR_PAGE,
        timeout=timeout,
        accept="text/html,application/xhtml+xml",
        opener=opener,
    ).decode("utf-8", errors="replace")

    parser = _LinkParser()
    parser.feed(html)

    # The same PDF commonly appears twice: once as an iframe/src with no label
    # and once as a clickable anchor with a useful "2026-2027 School Calendar"
    # label. Keep the labeled version when URLs collide.
    by_url: dict[str, PdfCandidate] = {}
    for item in parser.links:
        full = urljoin(CALENDAR_PAGE, unescape(item.url))
        existing = by_url.get(full)
        candidate = PdfCandidate(full, item.label)
        if existing is None or (not existing.label and candidate.label):
            by_url[full] = candidate

    candidates = list(by_url.values())
    if not candidates:
        raise RuntimeError("St. John calendar page exposed no PDF/Beehively calendar link")

    def score(item: PdfCandidate) -> tuple[int, int, int]:
        url = item.url.casefold()
        label = item.label.casefold()
        return (
            int("school calendar" in label),
            int("cdn_url.pdf" in url or ".pdf" in url),
            len(item.label),
        )

    chosen = max(candidates, key=score)
    print(
        "stjohn-calendar detail: discovered current calendar document"
        + (f" labeled '{chosen.label}'" if chosen.label else "")
    )
    return chosen


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    if not pdf_bytes.startswith(b"%PDF"):
        raise RuntimeError("St. John calendar link did not return a PDF")

    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages: list[str] = []
    for page in reader.pages:
        text = ""
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except TypeError:
            text = page.extract_text() or ""
        if text.strip():
            pages.append(text)

    combined = "\n".join(pages)
    combined = combined.replace("\u00a0", " ").replace("\u2011", "-")
    combined = combined.replace("\u2012", "-").replace("\u2013", "-").replace("\u2014", "-")
    # IMPORTANT: do not collapse runs of spaces. pypdf's layout mode uses
    # horizontal whitespace to preserve the two-month calendar columns, and
    # St. John's event legends sit beside the month grids.
    combined = re.sub(r"\n{3,}", "\n\n", combined)

    if len(combined.strip()) < 40:
        raise RuntimeError("St. John PDF contained too little extractable text")
    return combined


def _academic_year(text: str, label: str, url: str, reference: date) -> tuple[int, int]:
    haystack = " ".join([label, url, text[:2500]])
    match = re.search(r"\b(20\d{2})\s*[-/]\s*(20\d{2})\b", haystack)
    if match:
        first, second = int(match.group(1)), int(match.group(2))
        if second == first + 1:
            return first, second

    match = re.search(r"\b(20\d{2})\s*[-/]\s*(\d{2})\b", haystack)
    if match:
        first = int(match.group(1))
        second = (first // 100) * 100 + int(match.group(2))
        if second == first + 1:
            return first, second

    if reference.month >= 7:
        return reference.year, reference.year + 1
    return reference.year - 1, reference.year


def _infer_year(month: int, start_year: int, end_year: int) -> int:
    return start_year if month >= 7 else end_year


def _clean_title(raw: str) -> str:
    title = unescape(raw)
    title = re.sub(r"\s+", " ", title).strip(" |:;,-")
    title = re.sub(r"^[•*·]+\s*", "", title)
    return title.strip()


def _plausible_title(title: str) -> bool:
    low = title.casefold().strip()
    if not title or low in SKIP_TITLES:
        return False
    if len(title) < 3 or len(title) > 140:
        return False
    if not re.search(r"[A-Za-z]", title):
        return False
    if re.fullmatch(
        r"(?:january|february|march|april|may|june|july|august|"
        r"september|october|november|december)\s+20\d{2}",
        low,
    ):
        return False
    if re.fullmatch(r"(?:m|tu|w|th|f|sa|su)(?:\s+(?:m|tu|w|th|f|sa|su)){2,}", low):
        return False
    return True


SLASH_TOKEN = re.compile(
    r"(?<!\d)"
    r"(?P<m1>1[0-2]|0?[1-9])/(?P<d1>3[01]|[12]\d|0?[1-9])"
    r"(?:\s*-\s*"
    r"(?:(?P<m2>1[0-2]|0?[1-9])/)?(?P<d2>3[01]|[12]\d|0?[1-9]))?"
    r"(?!\d)"
)

MONTH_TOKEN = re.compile(
    r"(?<![A-Za-z])"
    r"(?P<month>"
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?|tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(?P<day>3[01]|[12]\d|0?[1-9])"
    r"(?!\d)",
    re.I,
)


def _event(
    *,
    event_id: str,
    title: str,
    start_day: date,
    end_day: date | None = None,
) -> dict:
    result = {
        "id": event_id,
        "title": title,
        "date": start_day.isoformat(),
        "schools": [SCHOOL_ID],
        "scope": "school",
        "category": "general",
        "source": SOURCE_NAME,
        "sourceUrl": CALENDAR_PAGE,
        "allDay": True,
    }
    if end_day and end_day > start_day:
        result["endDate"] = end_day.isoformat()
    return result


def _parse_slash_line(line: str, *, start_year: int, end_year: int) -> list[dict]:
    matches = list(SLASH_TOKEN.finditer(line))
    if not matches:
        return []

    events: list[dict] = []
    for idx, match in enumerate(matches):
        title_start = match.end()
        title_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(line)
        title = _clean_title(line[title_start:title_end])
        if not _plausible_title(title):
            continue

        m1, d1 = int(match.group("m1")), int(match.group("d1"))
        m2 = int(match.group("m2")) if match.group("m2") else m1
        d2 = int(match.group("d2")) if match.group("d2") else None

        try:
            start_day = date(_infer_year(m1, start_year, end_year), m1, d1)
        except ValueError:
            continue

        finish: date | None = None
        if d2 is not None:
            finish_year = _infer_year(m2, start_year, end_year)
            if m1 == 12 and m2 == 1:
                finish_year = end_year
            try:
                finish = date(finish_year, m2, d2)
            except ValueError:
                finish = None
            if finish and finish < start_day:
                finish = None

        slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:48] or "event"
        events.append(
            _event(
                event_id=f"stjohn-{start_day.isoformat()}-{slug}",
                title=title,
                start_day=start_day,
                end_day=finish,
            )
        )
    return events


def _parse_month_line(line: str, *, start_year: int, end_year: int) -> list[dict]:
    matches = list(MONTH_TOKEN.finditer(line))
    if not matches:
        return []

    events: list[dict] = []
    for idx, match in enumerate(matches):
        title_start = match.end()
        title_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(line)
        title = _clean_title(line[title_start:title_end])
        if not _plausible_title(title):
            continue

        month_name = match.group("month").casefold().rstrip(".")
        month = MONTHS.get(month_name) or MONTHS.get(month_name[:3])
        if month is None:
            continue

        day_num = int(match.group("day"))
        try:
            event_day = date(_infer_year(month, start_year, end_year), month, day_num)
        except ValueError:
            continue

        slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:48] or "event"
        events.append(
            _event(
                event_id=f"stjohn-{event_day.isoformat()}-{slug}",
                title=title,
                start_day=event_day,
            )
        )
    return events



MONTH_YEAR_RE = re.compile(
    r"\b("
    r"January|February|March|April|May|June|July|August|September|October|November|December"
    r")\s+(20\d{2})\b",
    re.I,
)

LEGEND_START_RE = re.compile(
    r"^\s*"
    r"(?P<d1>3[01]|[12]\d|0?[1-9])"
    r"(?:\s*[-]\s*(?P<d2>3[01]|[12]\d|0?[1-9]))?"
    r"\s+(?P<title>.*\S)\s*$"
)

LEGEND_SPACE_RANGE_RE = re.compile(
    r"^\s*"
    r"(?P<d1>3[01]|[12]\d|0?[1-9])\s+"
    r"(?P<d2>3[01]|[12]\d|0?[1-9])\s+"
    r"(?P<title>[A-Za-z].*\S)\s*$"
)


def _layout_chunks(region: str) -> list[str]:
    """
    pypdf layout extraction separates the month grid, event legend, and the
    next calendar column with long runs of spaces. Keep only human-text
    chunks; pure calendar-number rows are deliberately ignored later.
    """
    chunks = [
        chunk.strip()
        for chunk in re.split(r"\s{3,}", region.rstrip())
        if chunk.strip()
    ]
    return chunks


def _is_calendar_noise(chunk: str) -> bool:
    clean = re.sub(r"\s+", " ", chunk).strip()
    low = clean.casefold()

    if not clean:
        return True
    if MONTH_YEAR_RE.fullmatch(clean):
        return True
    if re.fullmatch(r"(?:s|m|t|w|th|f|sa|su|tu)(?:\s+(?:s|m|t|w|th|f|sa|su|tu)){3,}", low):
        return True
    if re.fullmatch(r"(?:\d{1,2}\s+){2,}\d{1,2}", clean):
        return True
    return False


def _legend_start(chunk: str) -> tuple[int, int | None, str] | None:
    clean = re.sub(r"\s+", " ", chunk).strip()

    match = LEGEND_START_RE.match(clean)
    if match:
        title = _clean_title(match.group("title"))
        if _plausible_title(title):
            return int(match.group("d1")), (
                int(match.group("d2")) if match.group("d2") else None
            ), title

    # In the St. John PDF, pypdf sometimes extracts a printed range such as
    # "3-4 Teacher In-Service" as "3 4 Teacher In-Service". Treat two leading
    # consecutive day numbers as a range only when meaningful text follows.
    match = LEGEND_SPACE_RANGE_RE.match(clean)
    if match:
        d1, d2 = int(match.group("d1")), int(match.group("d2"))
        title = _clean_title(match.group("title"))
        if d2 == d1 + 1 and _plausible_title(title):
            return d1, d2, title

    return None


def _month_number(name: str) -> int:
    return MONTHS[name.casefold()]


def _make_layout_event(
    *,
    month: int,
    year: int,
    d1: int,
    d2: int | None,
    title: str,
) -> dict | None:
    try:
        start_day = date(year, month, d1)
    except ValueError:
        return None

    finish: date | None = None
    if d2 is not None:
        try:
            finish = date(year, month, d2)
        except ValueError:
            finish = None

    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:48] or "event"
    return _event(
        event_id=f"stjohn-{start_day.isoformat()}-{slug}",
        title=title,
        start_day=start_day,
        end_day=finish,
    )


def _parse_month_legend_region(
    region_lines: list[str],
    *,
    month: int,
    year: int,
) -> list[dict]:
    """
    Parse the legend associated with one month. A legend entry begins with a
    day or day range. Chunks without a new day are treated as wrapped
    continuation text for the preceding entry (e.g. "Little Lamb 3 & 5 Day"
    or "No School").
    """
    raw_entries: list[dict] = []
    pending: dict | None = None

    def finish_pending() -> None:
        nonlocal pending
        if not pending:
            return
        title = _clean_title(pending["title"])
        if _plausible_title(title):
            event = _make_layout_event(
                month=month,
                year=year,
                d1=pending["d1"],
                d2=pending.get("d2"),
                title=title,
            )
            if event:
                raw_entries.append(event)
        pending = None

    for line in region_lines:
        for chunk in _layout_chunks(line):
            if _is_calendar_noise(chunk):
                continue

            start = _legend_start(chunk)
            if start:
                finish_pending()
                d1, d2, title = start
                pending = {"d1": d1, "d2": d2, "title": title}
                continue

            # Human-readable text without a leading day is often a wrapped
            # continuation of the previous legend item. Exclude weekday/month
            # noise and append compactly.
            clean = _clean_title(chunk)
            if (
                pending
                and _plausible_title(clean)
                and not MONTH_YEAR_RE.search(clean)
                and not re.fullmatch(r"(?:No School\s+)?S M T W Th F S", clean, re.I)
            ):
                pending["title"] = f"{pending['title']} {clean}"

    finish_pending()
    return raw_entries


def _parse_paired_month_layout(text: str) -> list[dict]:
    """
    St. John's PDF lays out two months side by side. Each month has a normal
    calendar grid plus a nearby event legend. pypdf layout extraction preserves
    enough horizontal spacing to split the left and right halves reliably.

    The previous parser collapsed that whitespace and therefore mixed grid day
    numbers into event titles. This parser keeps each month's legend separate.
    """
    lines = text.splitlines()
    headers: list[tuple[int, re.Match, re.Match]] = []

    for idx, line in enumerate(lines):
        matches = list(MONTH_YEAR_RE.finditer(line))
        if len(matches) >= 2:
            headers.append((idx, matches[0], matches[1]))

    if not headers:
        return []

    events: list[dict] = []

    for pos, (line_idx, left_head, right_head) in enumerate(headers):
        next_idx = headers[pos + 1][0] if pos + 1 < len(headers) else len(lines)
        section = lines[line_idx + 1:next_idx]

        split_at = right_head.start()
        # A small buffer keeps text immediately preceding the right month
        # heading on the left side while avoiding right-grid leakage.
        left_lines = [line[:split_at] for line in section]
        right_lines = [line[split_at:] for line in section]

        left_month = _month_number(left_head.group(1))
        left_year = int(left_head.group(2))
        right_month = _month_number(right_head.group(1))
        right_year = int(right_head.group(2))

        events.extend(
            _parse_month_legend_region(
                left_lines,
                month=left_month,
                year=left_year,
            )
        )
        events.extend(
            _parse_month_legend_region(
                right_lines,
                month=right_month,
                year=right_year,
            )
        )

    unique: dict[tuple[str, str, str], dict] = {}
    for event in events:
        key = (
            event["date"],
            event.get("endDate", ""),
            re.sub(r"\s+", " ", event["title"]).casefold().strip(),
        )
        unique[key] = event

    return sorted(
        unique.values(),
        key=lambda e: (e["date"], e.get("endDate", ""), e["title"]),
    )


def parse_calendar_text(
    text: str,
    *,
    label: str = "",
    url: str = "",
    reference: date | None = None,
) -> list[dict]:
    reference = reference or date.today()

    # Primary path for the actual St. John two-month grid PDF.
    layout_events = _parse_paired_month_layout(text)
    if len(layout_events) >= 12:
        return layout_events

    start_year, end_year = _academic_year(text, label, url, reference)

    raw_events: list[dict] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if line:
            raw_events.extend(
                _parse_slash_line(line, start_year=start_year, end_year=end_year)
            )

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if line:
            raw_events.extend(
                _parse_month_line(line, start_year=start_year, end_year=end_year)
            )

    unique: dict[tuple[str, str, str], dict] = {}
    for event in raw_events:
        key = (
            event["date"],
            event.get("endDate", ""),
            re.sub(r"\s+", " ", event["title"]).casefold().strip(),
        )
        unique[key] = event

    events = sorted(
        unique.values(),
        key=lambda e: (e["date"], e.get("endDate", ""), e["title"]),
    )

    # A full school-year calendar yielding only a handful of records is an
    # under-read, not success. The first live run produced 5 records, including
    # one visibly contaminated by an adjacent calendar-grid day number.
    #
    # Reject sparse parses and emit enough of pypdf's extracted text to tune
    # the parser to St. John's actual grid. This is intentionally stricter
    # than the first version: no data is better than confidently publishing
    # a few accidental matches.
    if len(events) < 12:
        compact_lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in text.splitlines()
            if re.sub(r"\s+", " ", line).strip()
        ]
        date_like = [
            line
            for line in compact_lines
            if (
                re.search(r"\b\d{1,2}/\d{1,2}\b", line)
                or re.search(
                    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
                    r"[A-Za-z]*\s+\d{1,2}\b",
                    line,
                    re.I,
                )
            )
        ][:24]

        date_excerpt = " | ".join(date_like)[:1800]
        text_excerpt = " | ".join(compact_lines[:45])[:2400]

        raise RuntimeError(
            "St. John PDF text was readable but calendar parsing under-read it "
            f"(layout parser {len(layout_events)} events; fallback parser "
            f"{len(events)} events). "
            f"DATE-LIKE PDF TEXT: {date_excerpt or '[none]'}; "
            f"OPENING PDF TEXT: {text_excerpt or '[none]'}"
        )

    return events



CHICAGO = ZoneInfo("America/Chicago")


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _google_datetime(value: str) -> datetime:
    clean = str(value or "").strip()
    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"
    dt = datetime.fromisoformat(clean)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CHICAGO)
    return dt.astimezone(CHICAGO)


def _activity_event_from_google(item: dict) -> dict | None:
    title = _normalize_title(item.get("summary"))
    if not title:
        return None

    start_obj = item.get("start") or {}
    end_obj = item.get("end") or {}

    # All-day Google Calendar event. Google's end.date is exclusive.
    if start_obj.get("date"):
        try:
            start_day = date.fromisoformat(start_obj["date"])
        except Exception:
            return None

        event = {
            "id": f"stjohn-activity-{item.get('id') or start_day.isoformat()}",
            "title": title,
            "date": start_day.isoformat(),
            "schools": [SCHOOL_ID],
            "scope": "school",
            "category": "general",
            "source": SOURCE_NAME,
            "sourceUrl": ACTIVITY_CALENDAR_PAGE,
            "allDay": True,
        }

        end_date_raw = end_obj.get("date")
        if end_date_raw:
            try:
                exclusive_end = date.fromisoformat(end_date_raw)
                inclusive_end = exclusive_end - timedelta(days=1)
                if inclusive_end > start_day:
                    event["endDate"] = inclusive_end.isoformat()
            except Exception:
                pass

    # Timed event.
    elif start_obj.get("dateTime"):
        try:
            start_dt = _google_datetime(start_obj["dateTime"])
        except Exception:
            return None

        event = {
            "id": f"stjohn-activity-{item.get('id') or start_dt.isoformat()}",
            "title": title,
            "date": start_dt.date().isoformat(),
            "start": start_dt.strftime("%H:%M"),
            "schools": [SCHOOL_ID],
            "scope": "school",
            "category": "general",
            "source": SOURCE_NAME,
            "sourceUrl": ACTIVITY_CALENDAR_PAGE,
        }

        if end_obj.get("dateTime"):
            try:
                end_dt = _google_datetime(end_obj["dateTime"])
                if end_dt.date() == start_dt.date() and end_dt > start_dt:
                    event["end"] = end_dt.strftime("%H:%M")
                elif end_dt > start_dt:
                    event["endDate"] = end_dt.date().isoformat()
                    event["end"] = end_dt.strftime("%H:%M")
            except Exception:
                pass
    else:
        return None

    location = _normalize_title(item.get("location"))
    if location:
        event["location"] = location

    return event


def _activity_title_key(title: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", " ", str(title or "").casefold()).strip()

    # School-wide closure calendars sometimes duplicate the same holiday with
    # titles such as "Labor Day" and "No School - Labor Day".
    closure_noise = {
        "no school",
        "school closed",
        "closed",
    }
    for phrase in closure_noise:
        clean = re.sub(rf"\b{re.escape(phrase)}\b", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def _dedupe_activity_events(events: list[dict]) -> list[dict]:
    """
    St. John's public page loads many Google subcalendars. Closures and
    school-wide events can therefore appear in several feeds. Store one
    SchoolBoard event for one real-world event.
    """
    unique: dict[tuple[str, str, str, str, str], dict] = {}

    for event in events:
        key = (
            event.get("date", ""),
            event.get("start", ""),
            event.get("end", ""),
            event.get("endDate", ""),
            _activity_title_key(event.get("title", "")),
        )
        existing = unique.get(key)
        if existing is None:
            unique[key] = event
            continue

        # Prefer the more parent-actionable closure title.
        existing_no_school = "no school" in existing.get("title", "").casefold()
        event_no_school = "no school" in event.get("title", "").casefold()
        if event_no_school and not existing_no_school:
            replacement = dict(event)
            if not replacement.get("location") and existing.get("location"):
                replacement["location"] = existing["location"]
            unique[key] = replacement
            existing = replacement

        # Prefer whichever duplicate has a location.
        if not existing.get("location") and event.get("location"):
            existing["location"] = event["location"]

    return sorted(
        unique.values(),
        key=lambda e: (
            e.get("date", ""),
            e.get("start", ""),
            e.get("title", ""),
        ),
    )


def _google_request_identity(url: str) -> tuple[str, str] | None:
    """
    Extract the public Google Calendar id + API key from a request the public
    St. John page itself made. Nothing is hard-coded.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None

    if parsed.netloc.casefold() != "www.googleapis.com":
        return None

    match = re.search(r"/calendar/v3/calendars/([^/]+)/events$", parsed.path)
    if not match:
        return None

    calendar_id = unquote(match.group(1))
    key = (parse_qs(parsed.query).get("key") or [""])[0].strip()
    if not calendar_id or not key:
        return None

    return calendar_id, key


def _browser_json_fetch(driver, url: str) -> dict:
    """
    Fetch public Google Calendar JSON from the already-loaded St. John page.
    Doing this in-browser preserves the same public browser context/referrer
    that Beehively uses for its Google API key.
    """
    script = r"""
    const url = arguments[0];
    const done = arguments[arguments.length - 1];
    fetch(url)
      .then(async response => {
        const text = await response.text();
        done({
          ok: response.ok,
          status: response.status,
          text: text
        });
      })
      .catch(error => {
        done({
          ok: false,
          status: 0,
          text: String(error)
        });
      });
    """
    driver.set_script_timeout(20)
    result = driver.execute_async_script(script, url) or {}
    if not result.get("ok"):
        raise RuntimeError(
            f"Google Calendar browser fetch failed with status "
            f"{result.get('status')}: {str(result.get('text') or '')[:300]}"
        )

    try:
        data = json.loads(result.get("text") or "{}")
    except Exception as exc:
        raise RuntimeError("Google Calendar response was not valid JSON") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Google Calendar response was not a JSON object")
    return data


def _rolling_google_url(
    *,
    calendar_id: str,
    key: str,
    reference: date,
) -> str:
    """
    Ask Google for SchoolBoard's 30-day-history / 60-day-ahead window rather
    than inheriting Beehively's current on-screen calendar range.
    """
    start_day = reference - timedelta(days=30)
    end_day = reference + timedelta(days=61)  # exclusive upper bound

    start_dt = datetime.combine(start_day, datetime.min.time(), tzinfo=CHICAGO)
    end_dt = datetime.combine(end_day, datetime.min.time(), tzinfo=CHICAGO)

    params = urlencode({
        "key": key,
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "singleEvents": "true",
        "maxResults": 9999,
        "timeZone": "America/Chicago",
        "orderBy": "startTime",
    })

    encoded_id = quote(calendar_id, safe="")
    return (
        f"https://www.googleapis.com/calendar/v3/calendars/"
        f"{encoded_id}/events?{params}"
    )


def fetch_activity_calendar(
    *,
    reference: date | None = None,
    page_load_timeout: int = 15,
    settle_seconds: float = 5.0,
) -> list[dict]:
    """
    Use St. John's PUBLIC Activity Calendar to discover its public Google
    subcalendars, then fetch SchoolBoard's own rolling date window from each.

    The public page remains the source of truth for:
      * which subcalendars are included
      * each Google calendar id
      * the public browser API key

    None of those values are hard-coded in SchoolBoard.
    """
    reference = reference or date.today()

    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:
        raise RuntimeError("Selenium is required for St. John Activity Calendar") from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,1200")
    options.add_argument("--lang=en-US")
    options.page_load_strategy = "eager"
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(page_load_timeout)

    try:
        try:
            driver.get(ACTIVITY_CALENDAR_PAGE)
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        import time as _time
        _time.sleep(settle_seconds)

        identities: list[tuple[str, str]] = []
        seen_ids: set[str] = set()

        for raw_entry in driver.get_log("performance"):
            try:
                message = json.loads(raw_entry["message"])["message"]
            except Exception:
                continue

            if message.get("method") != "Network.requestWillBeSent":
                continue

            request = message.get("params", {}).get("request", {})
            url = str(request.get("url") or "")
            identity = _google_request_identity(url)
            if identity is None:
                continue

            calendar_id, key = identity
            if calendar_id in seen_ids:
                continue
            seen_ids.add(calendar_id)
            identities.append((calendar_id, key))

        if not identities:
            raise RuntimeError(
                "St. John Activity Calendar exposed no public Google Calendar identities"
            )

        all_events: list[dict] = []
        successful_feeds = 0

        for feed_index, (calendar_id, key) in enumerate(identities, start=1):
            url = _rolling_google_url(
                calendar_id=calendar_id,
                key=key,
                reference=reference,
            )

            try:
                data = _browser_json_fetch(driver, url)
            except Exception as exc:
                print(
                    f"stjohn-activity detail: public Google feed {feed_index} "
                    f"rolling-window fetch failed: {type(exc).__name__}: {exc}"
                )
                continue

            items = data.get("items")
            if not isinstance(items, list):
                items = []

            successful_feeds += 1
            calendar_name = _normalize_title(data.get("summary")) or f"Feed {feed_index}"

            feed_events: list[dict] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("status") or "").casefold() == "cancelled":
                    continue
                event = _activity_event_from_google(item)
                if event is not None:
                    feed_events.append(event)

            all_events.extend(feed_events)

            sample = "; ".join(
                f"{e.get('date')} {e.get('title')}"
                for e in feed_events[:3]
            )
            print(
                f"stjohn-activity detail: {calendar_name} "
                f"({feed_index}/{len(identities)}) returned {len(feed_events)} events"
                + (f"; sample: {sample}" if sample else "")
            )

        if successful_feeds < max(3, len(identities) // 2):
            raise RuntimeError(
                f"Only {successful_feeds} of {len(identities)} public St. John "
                f"Google calendars could be fetched for the rolling window"
            )

        merged = _dedupe_activity_events(all_events)

        if len(merged) < 5:
            raise RuntimeError(
                f"St. John Activity Calendar produced only {len(merged)} unique events "
                f"from {successful_feeds} successful public Google feeds"
            )

        print(
            f"stjohn-activity detail: {successful_feeds} public Google feeds; "
            f"{len(all_events)} rolling-window feed-event records -> "
            f"{len(merged)} unique events"
        )
        if merged:
            preview = "; ".join(
                f"{e.get('date')} {e.get('title')}"
                for e in merged[:10]
            )
            print(f"stjohn-activity detail: first unique events: {preview}")

        return merged

    finally:
        driver.quit()


def fetch_stjohn_calendar(
    *,
    timeout: int = 25,
    opener=urlopen,
    reference: date | None = None,
) -> list[dict]:
    reference = reference or date.today()

    # PRIMARY: the public Activity Calendar's live Google Calendar feeds.
    try:
        events = fetch_activity_calendar(reference=reference)
        print(
            f"stjohn-calendar detail: using live Activity Calendar "
            f"({len(events)} unique events before rolling-window trim)"
        )
        return events
    except Exception as activity_exc:
        print(
            "stjohn-calendar detail: Activity Calendar unavailable; "
            f"falling back to annual PDF: {type(activity_exc).__name__}: {activity_exc}"
        )

    # FALLBACK: automatically rediscover and parse the current annual PDF.
    candidate = discover_current_pdf(timeout=timeout, opener=opener)

    pdf_bytes = _request(
        candidate.url,
        timeout=timeout,
        accept="application/pdf,*/*;q=0.8",
        opener=opener,
    )
    pdf_text = _extract_pdf_text(pdf_bytes)
    events = parse_calendar_text(
        pdf_text,
        label=candidate.label,
        url=candidate.url,
        reference=reference,
    )

    preview = "; ".join(
        f"{event['date']} {event['title']}"
        for event in events[:6]
    )
    print(
        f"stjohn-calendar detail: PDF fallback extracted {len(pdf_text)} text characters; "
        f"parsed {len(events)} school-year events"
    )
    if preview:
        print(f"stjohn-calendar detail: first PDF fallback events: {preview}")

    return events
