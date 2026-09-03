from __future__ import annotations

import io
import json
import re
import time
from dataclasses import dataclass
from datetime import date
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from pypdf import PdfReader


CALENDAR_PAGE = "https://stjohnls.com/calendar/school-calendar"
ACTIVITY_CALENDAR_PAGE = "https://stjohnls.com/calendar/activity-calendar"
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



def _activity_candidate_url(url: str, mime_type: str = "") -> bool:
    low = (url or "").casefold()
    mime = (mime_type or "").casefold()

    # Ignore obvious static assets unless the URL itself looks calendar/event
    # related. We want a short, useful Action log rather than every font/image.
    static_ext = (
        ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2",
        ".ttf", ".ico",
    )
    calendarish = any(
        token in low
        for token in (
            "calendar", "event", "ical", ".ics", "agenda", "schedule",
            "/api/", "api.", "feed", "fullcalendar",
        )
    )
    jsonish = "json" in mime

    if low.endswith(static_ext) and not calendarish:
        return False
    return calendarish or jsonish


def diagnose_activity_calendar(
    *,
    timeout: int = 12,
    opener=urlopen,
) -> None:
    """
    TEMPORARY DIAGNOSTIC.

    St. John's public Beehively Activity Calendar currently renders
    "Invalid date". This function inspects only the public page and its public
    network traffic for likely calendar/event endpoints. It does not log in or
    access the parent portal.

    The collector continues to use the annual PDF after this diagnostic.
    """
    print("stjohn-activity detail: inspecting public Beehively Activity Calendar")

    # First inspect ordinary server HTML. Sometimes the endpoint or calendar ID
    # is embedded directly in scripts/data attributes even when the widget UI
    # fails.
    try:
        raw = _request(
            ACTIVITY_CALENDAR_PAGE,
            timeout=timeout,
            accept="text/html,application/xhtml+xml",
            opener=opener,
        ).decode("utf-8", errors="replace")

        # Absolute URLs embedded in HTML/JS.
        html_urls = sorted(set(re.findall(
            r'https?://[^"\'<>\\s]+',
            unescape(raw),
            flags=re.I,
        )))
        candidates = [u for u in html_urls if _activity_candidate_url(u)]

        # Relative URL strings that look like endpoints.
        relative = sorted(set(re.findall(
            r'["\']((?:/[^"\']*)?(?:calendar|events?|ical|api|feed)[^"\']*)["\']',
            raw,
            flags=re.I,
        )))
        for item in relative:
            full = urljoin(ACTIVITY_CALENDAR_PAGE, unescape(item))
            if _activity_candidate_url(full):
                candidates.append(full)

        candidates = list(dict.fromkeys(candidates))[:20]
        if candidates:
            for url in candidates:
                print(f"stjohn-activity html-candidate: {url}")
        else:
            print("stjohn-activity detail: server HTML exposed no obvious calendar/event endpoint")
    except Exception as exc:
        print(
            "stjohn-activity detail: raw HTML inspection failed; "
            f"{type(exc).__name__}: {exc}"
        )

    # Then watch Chrome's public network requests. Beehively widgets often
    # fetch their content after page load, so the endpoint may never appear in
    # server-rendered HTML.
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        print("stjohn-activity detail: Selenium unavailable; skipping network diagnostic")
        return

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,1200")
    options.add_argument("--lang=en-US")
    options.page_load_strategy = "eager"
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(15)

    try:
        try:
            driver.get(ACTIVITY_CALENDAR_PAGE)
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        time.sleep(3)

        # Visible state is a useful sanity check.
        try:
            body_text = driver.execute_script(
                "return document.body ? document.body.innerText : '';"
            ) or ""
            if "Invalid date" in body_text:
                print("stjohn-activity detail: public widget rendered 'Invalid date'")
        except Exception:
            pass

        requests: dict[str, dict] = {}
        for raw_entry in driver.get_log("performance"):
            try:
                message = json.loads(raw_entry["message"])["message"]
            except Exception:
                continue

            method = message.get("method")
            params = message.get("params", {})

            if method == "Network.requestWillBeSent":
                request = params.get("request", {})
                url = request.get("url", "")
                if not _activity_candidate_url(url):
                    continue
                rec = requests.setdefault(url, {})
                rec["method"] = request.get("method", "GET")
                post_data = request.get("postData")
                if post_data:
                    # Keep only a short public request payload excerpt.
                    rec["postData"] = re.sub(r"\s+", " ", post_data)[:500]

            elif method == "Network.responseReceived":
                response = params.get("response", {})
                url = response.get("url", "")
                mime = response.get("mimeType", "")
                if not _activity_candidate_url(url, mime):
                    continue
                rec = requests.setdefault(url, {})
                rec["status"] = response.get("status")
                rec["mime"] = mime

        if requests:
            for url, info in list(requests.items())[:30]:
                detail = []
                if info.get("method"):
                    detail.append(str(info["method"]))
                if info.get("status") is not None:
                    detail.append(f"status={info['status']}")
                if info.get("mime"):
                    detail.append(f"mime={info['mime']}")
                print(
                    "stjohn-activity network-candidate: "
                    + (" ".join(detail) + " " if detail else "")
                    + url
                )
                if info.get("postData"):
                    print(
                        "stjohn-activity request-body: "
                        f"{url} :: {info['postData']}"
                    )
        else:
            print("stjohn-activity detail: Chrome saw no calendar/event/API-like requests")

        # Finally log script/iframe sources with relevant names. This often
        # reveals a widget bundle even if its XHR fails before firing.
        try:
            resources = driver.execute_script(
                """
                return Array.from(document.querySelectorAll('script[src], iframe[src]'))
                  .map(el => el.src)
                  .filter(Boolean);
                """
            ) or []
        except Exception:
            resources = []

        relevant_resources = [
            url for url in resources
            if _activity_candidate_url(url)
            or "beehively" in url.casefold()
        ]
        for url in list(dict.fromkeys(relevant_resources))[:20]:
            print(f"stjohn-activity resource: {url}")

    except Exception as exc:
        print(
            "stjohn-activity detail: Chrome network diagnostic failed; "
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        driver.quit()


def fetch_stjohn_calendar(
    *,
    timeout: int = 25,
    opener=urlopen,
    reference: date | None = None,
) -> list[dict]:
    reference = reference or date.today()

    # Temporary: inspect the public Activity Calendar for a live endpoint while
    # retaining the annual PDF as the actual data source/fallback.
    diagnose_activity_calendar(
        timeout=min(timeout, 12),
        opener=opener,
    )

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
