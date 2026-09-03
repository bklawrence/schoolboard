from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SCHOOL_ID = "academyhigh"

HOME_PAGE = "https://www.academyhigh.org/"
RESOURCES_PAGE = "https://www.academyhigh.org/resources"

CALENDAR_SOURCE_NAME = "Academy High Academic Calendar"
NEWSLETTER_SOURCE_NAME = "Academy High Newsletter Important Dates"

CHICAGO = ZoneInfo("America/Chicago")

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

MONTH_PATTERN = (
    r"January|February|March|April|May|June|July|August|"
    r"September|October|November|December"
)

DATE_LINE_RE = re.compile(
    rf"^\s*(?:(?:Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)"
    rf"(?:day)?[,]?\s+)?"
    rf"(?P<month>{MONTH_PATTERN})\s+"
    rf"(?P<day>\d{{1,2}})"
    rf"(?:st|nd|rd|th)?"
    rf"(?:,\s*(?P<year>20\d{{2}}))?"
    rf"(?:\s*(?:[-–—:|]\s*)?(?P<title>.*))?$",
    re.I,
)

DATE_RANGE_RE = re.compile(
    rf"^\s*(?P<m1>{MONTH_PATTERN})\s+(?P<d1>\d{{1,2}})"
    rf"(?:st|nd|rd|th)?\s*(?:[-–—]|to)\s*"
    rf"(?:(?P<m2>{MONTH_PATTERN})\s+)?(?P<d2>\d{{1,2}})"
    rf"(?:st|nd|rd|th)?"
    rf"(?:,\s*(?P<year>20\d{{2}}))?"
    rf"(?:\s*(?:[-–—:|]\s*)?(?P<title>.*))?$",
    re.I,
)

NUMERIC_DATE_RE = re.compile(
    r"^\s*(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?"
    r"(?:\s*(?:[-–—:|]\s*)?(?P<title>.*))?$"
)

TIME_RE = re.compile(
    r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)\b",
    re.I,
)

GENERIC_SKIP = {
    "important dates",
    "important date",
    "upcoming dates",
    "upcoming events",
    "academy high",
    "august 2026 newsletter",
    "newsletter",
}

STOP_HEADINGS = {
    "athletics",
    "sports",
    "reminders",
    "announcements",
    "college counseling",
    "college counselling",
    "student life",
    "from the head of school",
    "we are so proud of our alumni",
}


@dataclass(frozen=True)
class LinkCandidate:
    url: str
    label: str = ""


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[LinkCandidate] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = {
            str(k).casefold(): str(v)
            for k, v in attrs
            if k and v is not None
        }
        if tag.casefold() == "a":
            self._href = attrs.get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is None:
            return
        clean = re.sub(r"\s+", " ", data).strip()
        if clean:
            self._text.append(clean)

    def handle_endtag(self, tag):
        if tag.casefold() != "a" or self._href is None:
            return
        self.links.append(
            LinkCandidate(
                self._href,
                " ".join(self._text).strip(),
            )
        )
        self._href = None
        self._text = []


def _request_text(url: str, *, timeout: int = 25, opener=urlopen) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "ChambanaSchoolboard/1.0 (+public school calendar aggregator)",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
    )
    with opener(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _request_bytes(url: str, *, timeout: int = 25, opener=urlopen) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "ChambanaSchoolboard/1.0 (+public school calendar aggregator)",
            "Accept": "application/pdf,*/*;q=0.8",
        },
    )
    with opener(request, timeout=timeout) as response:
        return response.read()


def _academic_year(reference: date) -> tuple[int, int]:
    if reference.month >= 7:
        return reference.year, reference.year + 1
    return reference.year - 1, reference.year


def _year_for_month(month: int, *, reference: date) -> int:
    start_year, end_year = _academic_year(reference)
    return start_year if month >= 7 else end_year


def _normalize_title(value: str) -> str:
    value = unescape(str(value or ""))
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n-–—:|•")

    # Smore occasionally leaves an empty time/location delimiter at the end,
    # e.g. "XC @UIUC Arboretum @" or "Homer Lake, Homer @TBD".
    value = re.sub(r"\s+@\s*(?:TBD)?\s*$", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n-–—:|•")
    return value


def _parse_time_from_title(title: str) -> tuple[str, str]:
    match = TIME_RE.search(title)
    if not match:
        return "", title

    hour = int(match.group("h"))
    minute = int(match.group("m") or 0)
    ampm = match.group("ampm").casefold().replace(".", "")
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0

    start = f"{hour:02d}:{minute:02d}"
    clean = _normalize_title(title[:match.start()] + " " + title[match.end():])
    return start, clean


def _event(
    *,
    source: str,
    source_url: str,
    event_date: date,
    title: str,
    end_date: date | None = None,
) -> dict | None:
    title = _normalize_title(title)
    if not title or title.casefold() in GENERIC_SKIP:
        return None
    if not re.search(r"[A-Za-z]", title):
        return None

    # SchoolBoard is for current-family operational information. Academy High's
    # newsletter includes its admissions event in Important Dates, but that is
    # aimed at prospective families rather than enrolled households.
    low_title = title.casefold()
    if "discover academy high" in low_title and (
        "prospective" in low_title or "admissions" in low_title
    ):
        return None

    start, title = _parse_time_from_title(title)
    title = _normalize_title(title)
    if not title:
        return None

    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:54] or "event"
    item = {
        "id": (
            f"academyhigh-{source.casefold().replace(' ', '-')}-"
            f"{event_date.isoformat()}-{slug}"
        ),
        "title": title,
        "date": event_date.isoformat(),
        "schools": [SCHOOL_ID],
        "scope": "school",
        "category": "general",
        "source": source,
        "sourceUrl": source_url,
    }
    if start:
        item["start"] = start
    else:
        item["allDay"] = True
    if end_date and end_date > event_date:
        item["endDate"] = end_date.isoformat()
    return item


def _parse_dated_lines(
    lines: list[str],
    *,
    reference: date,
    source: str,
    source_url: str,
) -> list[dict]:
    events: list[dict] = []
    i = 0

    while i < len(lines):
        raw = _normalize_title(lines[i])
        if not raw:
            i += 1
            continue

        range_match = DATE_RANGE_RE.match(raw)
        single_match = DATE_LINE_RE.match(raw)
        numeric_match = NUMERIC_DATE_RE.match(raw)

        start_day: date | None = None
        end_day: date | None = None
        title = ""

        if range_match:
            m1 = MONTHS[range_match.group("m1").casefold()]
            m2 = MONTHS[(range_match.group("m2") or range_match.group("m1")).casefold()]
            d1 = int(range_match.group("d1"))
            d2 = int(range_match.group("d2"))
            year = int(range_match.group("year") or _year_for_month(m1, reference=reference))
            end_year = year + 1 if m2 < m1 else year
            try:
                start_day = date(year, m1, d1)
                end_day = date(end_year, m2, d2)
            except ValueError:
                start_day = None
            title = _normalize_title(range_match.group("title") or "")

        elif single_match:
            month = MONTHS[single_match.group("month").casefold()]
            day = int(single_match.group("day"))
            year = int(single_match.group("year") or _year_for_month(month, reference=reference))
            try:
                start_day = date(year, month, day)
            except ValueError:
                start_day = None
            title = _normalize_title(single_match.group("title") or "")

        elif numeric_match:
            month = int(numeric_match.group("m"))
            day = int(numeric_match.group("d"))
            raw_year = numeric_match.group("y")
            if raw_year:
                year = int(raw_year)
                if year < 100:
                    year += 2000
            else:
                year = _year_for_month(month, reference=reference)
            try:
                start_day = date(year, month, day)
            except ValueError:
                start_day = None
            title = _normalize_title(numeric_match.group("title") or "")

        if start_day is None:
            i += 1
            continue

        # Newsletters and PDF lists often use a date heading, then put the
        # actual event description on the next line(s).
        if not title:
            lookahead: list[str] = []
            j = i + 1
            while j < min(len(lines), i + 4):
                nxt = _normalize_title(lines[j])
                if not nxt:
                    j += 1
                    continue
                if DATE_LINE_RE.match(nxt) or DATE_RANGE_RE.match(nxt) or NUMERIC_DATE_RE.match(nxt):
                    break
                if nxt.casefold() in STOP_HEADINGS:
                    break
                # Don't swallow obvious newsletter prose.
                if len(nxt) > 180:
                    break
                lookahead.append(nxt)
                if len(" ".join(lookahead)) >= 110:
                    break
                j += 1
            title = _normalize_title(" ".join(lookahead))

        item = _event(
            source=source,
            source_url=source_url,
            event_date=start_day,
            end_date=end_day,
            title=title,
        )
        if item:
            events.append(item)

        i += 1

    unique: dict[tuple[str, str, str], dict] = {}
    for item in events:
        key = (
            item["date"],
            item.get("start", ""),
            re.sub(r"[^a-z0-9]+", " ", item["title"].casefold()).strip(),
        )
        unique[key] = item

    return sorted(
        unique.values(),
        key=lambda e: (e["date"], e.get("start", ""), e["title"]),
    )


# ----------------------------------------------------------------------
# Academic calendar PDF
# ----------------------------------------------------------------------

def _discover_calendar_pdf_from_html(html: str) -> LinkCandidate | None:
    decoded = unescape(html).replace(r"\/", "/")
    parser = _LinkParser()
    parser.feed(decoded)

    # Wix may also put file URLs into embedded JSON.
    for url in re.findall(
        r'https?://[^"\'<>\\\s]+?\.pdf(?:\?[^"\'<>\\\s]*)?',
        decoded,
        flags=re.I,
    ):
        parser.links.append(LinkCandidate(url, ""))

    candidates: list[LinkCandidate] = []
    for item in parser.links:
        full = urljoin(RESOURCES_PAGE, item.url)
        if ".pdf" not in full.casefold():
            continue

        haystack = f"{item.label} {full}".casefold()

        # Aggressively reject the other documents listed on Resources.
        if any(
            token in haystack
            for token in (
                "handbook",
                "bullying",
                "technology",
                "supply",
                "college profile",
                "diabetes",
                "voter",
                "sexual abuse",
                "policy",
            )
        ):
            continue

        candidates.append(LinkCandidate(full, item.label))

    if not candidates:
        return None

    def score(item: LinkCandidate):
        haystack = f"{item.label} {item.url}".casefold()
        return (
            int("academic calendar" in haystack),
            int("26-27" in haystack or "2026-2027" in haystack or "2026-27" in haystack),
            int("calendar" in haystack),
            len(item.label),
        )

    return max(candidates, key=score)


def _discover_calendar_pdf_selenium() -> LinkCandidate | None:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        return None

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,1200")
    options.page_load_strategy = "eager"

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(15)

    try:
        try:
            driver.get(RESOURCES_PAGE)
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        time.sleep(2.5)

        links = driver.execute_script(
            """
            return Array.from(document.querySelectorAll('a[href]')).map(a => ({
              href: a.href || '',
              text: (a.innerText || a.textContent || '').trim()
            }));
            """
        ) or []

        candidates = []
        for item in links:
            href = str(item.get("href") or "")
            label = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
            haystack = f"{label} {href}".casefold()

            if ".pdf" not in href.casefold():
                continue
            if any(
                token in haystack
                for token in (
                    "handbook",
                    "bullying",
                    "technology",
                    "supply",
                    "college profile",
                    "diabetes",
                    "voter",
                    "sexual abuse",
                    "policy",
                )
            ):
                continue
            candidates.append(LinkCandidate(href, label))

        if not candidates:
            return None

        def score(item: LinkCandidate):
            haystack = f"{item.label} {item.url}".casefold()
            return (
                int("academic calendar" in haystack),
                int("26-27" in haystack or "2026-2027" in haystack or "2026-27" in haystack),
                int("calendar" in haystack),
                len(item.label),
            )

        return max(candidates, key=score)

    finally:
        driver.quit()


def discover_calendar_pdf(*, timeout: int = 25, opener=urlopen) -> LinkCandidate:
    try:
        html = _request_text(RESOURCES_PAGE, timeout=timeout, opener=opener)
        candidate = _discover_calendar_pdf_from_html(html)
        if candidate:
            print(
                "academy-calendar detail: discovered academic-calendar PDF from Resources"
                + (f" labeled '{candidate.label}'" if candidate.label else "")
            )
            return candidate
    except Exception as exc:
        print(
            "academy-calendar detail: HTML PDF discovery failed; "
            f"{type(exc).__name__}: {exc}"
        )

    candidate = _discover_calendar_pdf_selenium()
    if candidate:
        print(
            "academy-calendar detail: discovered academic-calendar PDF via rendered Resources page"
            + (f" labeled '{candidate.label}'" if candidate.label else "")
        )
        return candidate

    raise RuntimeError("Academy High Resources page exposed no usable academic-calendar PDF")


def _pdf_text(raw: bytes) -> str:
    if not raw.startswith(b"%PDF"):
        raise RuntimeError("Academy High academic-calendar URL did not return a PDF")

    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for Academy High calendar PDF") from exc

    doc = pymupdf.open(stream=raw, filetype="pdf")
    pages = []
    for page in doc:
        text = page.get_text("text", sort=True) or ""
        if text.strip():
            pages.append(text)

    combined = "\n".join(pages)
    combined = (
        combined
        .replace("\u00a0", " ")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    combined = re.sub(r"\n{3,}", "\n\n", combined)

    if len(combined.strip()) < 100:
        raise RuntimeError("Academy High PDF contained too little extractable text")

    return combined



ACADEMIC_EVENT_LINE_RE = re.compile(
    r"(?<!\d)"
    r"(?P<d1>3[01]|[12]\d|0?[1-9])"
    r"(?:\s*-\s*(?P<d2>3[01]|[12]\d|0?[1-9]))?"
    r"\s+"
    r"(?P<title>[A-Za-z][^\n]*)$"
)


def _parse_academic_half_text(
    half_text: str,
    *,
    reference: date,
) -> list[dict]:
    """
    Parse one vertical half of Academy High's annual calendar PDF.

    The PDF places August-December down the left half and January-May down the
    right half. Each month also contains a conventional numbered mini-calendar.
    We ignore pure number/grid lines and accept only lines where a day/range is
    followed by alphabetic event text.
    """
    half_text = (
        half_text
        .replace("\u00a0", " ")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )

    current_month: int | None = None
    events: list[dict] = []

    for raw_line in half_text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue

        # Month headings are printed alone in each clipped half.
        month_token = re.sub(r"[^A-Za-z]", "", line).casefold()
        if month_token in MONTHS and len(line) <= 16:
            current_month = MONTHS[month_token]
            continue

        if current_month is None:
            continue

        low = line.casefold()
        if low in {"s m t w th f s", "s m t w t f s"}:
            continue

        # Search from the right end of the line. This is intentionally tolerant
        # of stray mini-calendar numbers before the real dated event, e.g.
        # "1 11 Faculty Development Day (No School)".
        matches = list(ACADEMIC_EVENT_LINE_RE.finditer(line))
        if not matches:
            continue

        match = matches[-1]
        d1 = int(match.group("d1"))
        d2 = int(match.group("d2")) if match.group("d2") else None
        title = _normalize_title(match.group("title"))

        # Reject a false match against a grid line.
        if not title or not re.search(r"[A-Za-z]{2,}", title):
            continue

        year = _year_for_month(current_month, reference=reference)
        try:
            start_day = date(year, current_month, d1)
        except ValueError:
            continue

        end_day = None
        if d2 is not None:
            try:
                end_day = date(year, current_month, d2)
            except ValueError:
                end_day = None

        item = _event(
            source=CALENDAR_SOURCE_NAME,
            source_url=RESOURCES_PAGE,
            event_date=start_day,
            end_date=end_day,
            title=title,
        )
        if item:
            events.append(item)

    return events


def _parse_academic_pdf_positionally(
    raw: bytes,
    *,
    reference: date,
) -> tuple[list[dict], str, str]:
    """
    Split the one-page Academy High calendar into left/right halves before text
    extraction. This prevents August/January, September/February, etc. from
    being interleaved into the same line.
    """
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for Academy High calendar PDF") from exc

    doc = pymupdf.open(stream=raw, filetype="pdf")
    events: list[dict] = []
    left_debug: list[str] = []
    right_debug: list[str] = []

    for page in doc:
        rect = page.rect
        midpoint = rect.x0 + rect.width / 2

        # Small overlap protects headings/text that sit directly on the center
        # without meaningfully mixing the two month columns.
        left_rect = pymupdf.Rect(rect.x0, rect.y0, midpoint + 4, rect.y1)
        right_rect = pymupdf.Rect(midpoint - 4, rect.y0, rect.x1, rect.y1)

        left_text = page.get_text("text", clip=left_rect, sort=True) or ""
        right_text = page.get_text("text", clip=right_rect, sort=True) or ""

        left_debug.append(left_text)
        right_debug.append(right_text)

        events.extend(
            _parse_academic_half_text(
                left_text,
                reference=reference,
            )
        )
        events.extend(
            _parse_academic_half_text(
                right_text,
                reference=reference,
            )
        )

    unique: dict[tuple[str, str, str], dict] = {}
    for item in events:
        key = (
            item["date"],
            item.get("endDate", ""),
            re.sub(r"[^a-z0-9]+", " ", item["title"].casefold()).strip(),
        )
        unique[key] = item

    merged = sorted(
        unique.values(),
        key=lambda e: (e["date"], e.get("endDate", ""), e["title"]),
    )

    return merged, "\n".join(left_debug), "\n".join(right_debug)


def fetch_academy_calendar(
    *,
    reference: date | None = None,
    timeout: int = 25,
    opener=urlopen,
) -> list[dict]:
    reference = reference or date.today()
    candidate = discover_calendar_pdf(timeout=timeout, opener=opener)
    raw = _request_bytes(candidate.url, timeout=timeout, opener=opener)

    events, left_text, right_text = _parse_academic_pdf_positionally(
        raw,
        reference=reference,
    )

    if len(events) < 8:
        left_lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in left_text.splitlines()
            if re.sub(r"\s+", " ", line).strip()
        ]
        right_lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in right_text.splitlines()
            if re.sub(r"\s+", " ", line).strip()
        ]
        raise RuntimeError(
            "Academy High positional PDF parser produced only "
            f"{len(events)} plausible events. LEFT HALF: "
            + " | ".join(left_lines[:70])[:2600]
            + " || RIGHT HALF: "
            + " | ".join(right_lines[:70])[:2600]
        )

    preview = "; ".join(
        f"{event['date']} {event['title']}"
        for event in events[:12]
    )
    print(
        f"academy-calendar detail: positional half-page parser found "
        f"{len(events)} academic-calendar events"
    )
    if preview:
        print(f"academy-calendar detail: first parsed events: {preview}")

    return events


# ----------------------------------------------------------------------
# Latest Smore newsletter / Important Dates
# ----------------------------------------------------------------------

def _discover_latest_smore_from_html(html: str) -> list[LinkCandidate]:
    decoded = unescape(html).replace(r"\/", "/")
    parser = _LinkParser()
    parser.feed(decoded)

    results: list[LinkCandidate] = []
    seen: set[str] = set()

    for item in parser.links:
        href = item.url
        if "smore.com/" not in href.casefold():
            continue
        if "/n/" not in href.casefold():
            continue
        full = href
        if full in seen:
            continue
        seen.add(full)
        results.append(LinkCandidate(full, item.label))

    # Wix may store the Smore URL in JSON without a conventional anchor.
    for href in re.findall(
        r'https?://(?:app|secure)\.smore\.com/n/[A-Za-z0-9_-]+',
        decoded,
        flags=re.I,
    ):
        if href not in seen:
            seen.add(href)
            results.append(LinkCandidate(href, ""))

    return results


def discover_latest_newsletter(*, timeout: int = 25, opener=urlopen) -> LinkCandidate:
    candidates: list[LinkCandidate] = []

    try:
        html = _request_text(HOME_PAGE, timeout=timeout, opener=opener)
        candidates.extend(_discover_latest_smore_from_html(html))
    except Exception as exc:
        print(
            "academy-newsletter detail: homepage Smore discovery via HTML failed; "
            f"{type(exc).__name__}: {exc}"
        )

    if not candidates:
        try:
            from selenium import webdriver
            from selenium.common.exceptions import TimeoutException
            from selenium.webdriver.chrome.options import Options
        except ImportError as exc:
            raise RuntimeError("Selenium is required for Academy High newsletter discovery") from exc

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1400,1200")
        options.page_load_strategy = "eager"

        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(15)
        try:
            try:
                driver.get(HOME_PAGE)
            except TimeoutException:
                try:
                    driver.execute_script("window.stop();")
                except Exception:
                    pass

            time.sleep(2.5)
            links = driver.execute_script(
                """
                return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                  href: a.href || '',
                  text: (a.innerText || a.textContent || '').trim()
                }));
                """
            ) or []

            for item in links:
                href = str(item.get("href") or "")
                if "smore.com/" not in href.casefold() or "/n/" not in href.casefold():
                    continue
                label = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
                candidates.append(LinkCandidate(href, label))
        finally:
            driver.quit()

    if not candidates:
        raise RuntimeError("Academy High homepage exposed no public Smore newsletter")

    # Academy High currently presents its latest newsletter on the homepage.
    # Prefer a link explicitly labeled Newsletter, then retain page order.
    candidates = list(dict.fromkeys(candidates))
    candidates.sort(
        key=lambda item: (
            int("newsletter" in item.label.casefold()),
            int("2026" in item.label),
        ),
        reverse=True,
    )
    chosen = candidates[0]
    print(
        "academy-newsletter detail: discovered homepage Smore newsletter "
        + chosen.url
        + (f" labeled '{chosen.label}'" if chosen.label else "")
    )
    return chosen


def _newsletter_text(url: str) -> str:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:
        raise RuntimeError("Selenium is required for Academy High Smore newsletter") from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,1800")
    options.add_argument("--lang=en-US")
    options.page_load_strategy = "eager"

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(18)

    try:
        try:
            driver.get(url)
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        time.sleep(4.0)
        text = driver.execute_script(
            "return document.body ? document.body.innerText : '';"
        ) or ""

        if len(text.strip()) < 200:
            raise RuntimeError("Smore page rendered too little readable text")

        return text

    finally:
        driver.quit()


def _important_dates_section(text: str) -> list[str]:
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
        if re.sub(r"\s+", " ", line).strip()
    ]

    heading_index = None
    for idx, line in enumerate(lines):
        low = line.casefold()
        if low in {"important dates", "important date", "upcoming dates"}:
            heading_index = idx
            break
        if "important dates" in low and len(line) < 80:
            heading_index = idx
            break

    if heading_index is None:
        raise RuntimeError(
            "Academy High Smore rendered successfully but no Important Dates heading was found. "
            "SMORE TEXT: " + " | ".join(lines[:100])[:4500]
        )

    section: list[str] = []
    for line in lines[heading_index + 1 : heading_index + 90]:
        low = line.casefold()

        # Once we've collected actual dated content, a known next-section
        # heading ends the Important Dates block.
        if section and low in STOP_HEADINGS:
            break

        section.append(line)

    return section


def fetch_academy_newsletter(
    *,
    reference: date | None = None,
    timeout: int = 25,
    opener=urlopen,
) -> list[dict]:
    reference = reference or date.today()
    newsletter = discover_latest_newsletter(timeout=timeout, opener=opener)
    text = _newsletter_text(newsletter.url)
    section = _important_dates_section(text)

    events = _parse_dated_lines(
        section,
        reference=reference,
        source=NEWSLETTER_SOURCE_NAME,
        source_url=newsletter.url,
    )

    if len(events) < 3:
        raise RuntimeError(
            "Academy High Important Dates section produced only "
            f"{len(events)} plausible events. SECTION TEXT: "
            + " | ".join(section[:80])[:4000]
        )

    preview = "; ".join(
        f"{event['date']} {event['title']}"
        for event in events[:10]
    )
    print(
        f"academy-newsletter detail: parsed {len(events)} Important Dates "
        f"from latest homepage-linked Smore"
    )
    if preview:
        print(f"academy-newsletter detail: first parsed events: {preview}")

    return events
