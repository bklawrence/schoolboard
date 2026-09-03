from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from pypdf import PdfReader


CALENDAR_PAGE = "https://stjohnls.com/calendar/school-calendar"
SOURCE_NAME = "St. John Lutheran School Calendar v2"
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
    combined = re.sub(r"[ \t]+", " ", combined)
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


def parse_calendar_text(
    text: str,
    *,
    label: str = "",
    url: str = "",
    reference: date | None = None,
) -> list[dict]:
    reference = reference or date.today()
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
            "St. John PDF text was readable but calendar parsing produced only "
            f"{len(events)} plausible events; treating this as an under-read. "
            f"DATE-LIKE PDF TEXT: {date_excerpt or '[none]'}; "
            f"OPENING PDF TEXT: {text_excerpt or '[none]'}"
        )

    return events


def fetch_stjohn_calendar(
    *,
    timeout: int = 25,
    opener=urlopen,
    reference: date | None = None,
) -> list[dict]:
    reference = reference or date.today()
    candidate = discover_current_pdf(timeout=timeout, opener=opener)

    pdf_bytes = _request(
        candidate.url,
        timeout=timeout,
        accept="application/pdf,*/*;q=0.8",
        opener=opener,
    )
    text = _extract_pdf_text(pdf_bytes)
    events = parse_calendar_text(
        text,
        label=candidate.label,
        url=candidate.url,
        reference=reference,
    )

    preview = "; ".join(
        f"{event['date']} {event['title']}"
        for event in events[:6]
    )
    print(
        f"stjohn-calendar detail: extracted {len(text)} text characters; "
        f"parsed {len(events)} school-year events"
    )
    if preview:
        print(f"stjohn-calendar detail: first parsed events: {preview}")

    return events
