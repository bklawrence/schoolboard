from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class SchoolFeed:
    id: str
    name: str
    home: str


SCHOOL_FEEDS = [
    SchoolFeed("yankee", "Yankee Ridge Multilingual", "https://yridge.usd116.org/"),
    SchoolFeed("leal", "Leal Elementary", "https://leal.usd116.org/"),
    SchoolFeed("paine", "Thomas Paine Elementary", "https://tmspaine.usd116.org/"),
    SchoolFeed("williams", "Dr. Williams Elementary", "https://dpw.usd116.org/"),
    SchoolFeed("king", "Dr. Martin Luther King Jr. Elementary", "https://drking.usd116.org/"),
    SchoolFeed("sgc", "Urbana Sixth Grade Center", "https://sgc.usd116.org/"),
    SchoolFeed("ums", "Urbana Middle School", "https://ums.usd116.org/"),
    SchoolFeed("uhs", "Urbana High School", "https://uhs.usd116.org/"),
]

SOURCE_PREFIX = "USD 116 School Feed"
ARTICLE_RE = re.compile(
    r"""href=["']([^"']*index\.php\?articleID=\d+(?:&amp;|&)pageID=smartSiteFeed(?:&amp;|&)psqFeed=true[^"']*)["']""",
    re.I,
)

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

DATE_LINE_RE = re.compile(
    r"""^\s*
    (?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|
       Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)
    \.?\s+
    (?P<day>\d{1,2})
    (?:
      \s*[-–]\s*
      (?:
        (?P<month2>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|
          Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+
      )?
      (?P<day2>\d{1,2})
    )?
    \s*(?:[-–:]\s*|\s+)
    (?P<title>.+?)
    \s*$
    """,
    re.I | re.X,
)

TIME_RANGE_RE = re.compile(
    r"""(?<!\d)
    (?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*
    (?P<ampm1>a\.?m\.?|p\.?m\.?)?
    \s*[-–]\s*
    (?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\s*
    (?P<ampm2>a\.?m\.?|p\.?m\.?)
    """,
    re.I | re.X,
)

SINGLE_TIME_RE = re.compile(
    r"""(?<!\d)
    (?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*
    (?P<ampm>a\.?m\.?|p\.?m\.?)
    \b
    """,
    re.I | re.X,
)

DISTRICT_DUPLICATE_PHRASES = (
    "labor day",
    "fall break",
    "winter break",
    "spring break",
    "memorial day",
    "no school",
    "institute day",
    "staff development",
    "first day",
    "all students in attendance",
    "end of quarter",
    "early dismissal",
    "student-led conferences",
)

SKIP_PHRASES = (
    "assessment window",
    "nwea",
    "mclass",
    "map testing",
    "fire drill",
    "tornado drill",
    "lockdown drill",
)


class _BlockTextParser(HTMLParser):
    BLOCK_TAGS = {
        "p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "tr", "td",
    }

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def lines(self) -> list[str]:
        text = unescape("".join(self.parts))
        lines = []
        for raw in text.splitlines():
            clean = re.sub(r"\s+", " ", raw).strip(" \t\r\n•*")
            if clean:
                lines.append(clean)
        return lines


def _request(url: str, *, timeout: int = 30, opener=urlopen) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "ChambanaSchoolboard/1.0 (+public school information aggregator)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with opener(req, timeout=timeout) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return body.decode(charset, errors="replace")


def _article_urls(home_html: str, home_url: str, *, limit: int = 12) -> list[str]:
    urls = []
    for raw in ARTICLE_RE.findall(home_html):
        href = unescape(raw)
        url = urljoin(home_url, href)
        if url not in urls:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def _year_for_month(month: int, reference: date) -> int:
    start_year = reference.year if reference.month >= 7 else reference.year - 1
    return start_year if month >= 7 else start_year + 1


def _clock(hour_s: str, minute_s: str | None, ampm: str | None) -> str:
    hour = int(hour_s)
    minute = int(minute_s or "00")
    marker = (ampm or "").casefold().replace(".", "")
    if marker == "pm" and hour < 12:
        hour += 12
    if marker == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _extract_time(title: str) -> tuple[str, str | None, str | None]:
    match = TIME_RANGE_RE.search(title)
    if match:
        ampm1 = match.group("ampm1") or match.group("ampm2")
        start = _clock(match.group("h1"), match.group("m1"), ampm1)
        end = _clock(match.group("h2"), match.group("m2"), match.group("ampm2"))
        cleaned = (title[:match.start()] + title[match.end():]).strip(" -–,:")
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned, start, end

    match = SINGLE_TIME_RE.search(title)
    if match:
        start = _clock(match.group("h"), match.group("m"), match.group("ampm"))
        cleaned = (title[:match.start()] + title[match.end():]).strip(" -–,:")
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned, start, None

    return title.strip(), None, None


def _event_from_line(line: str, school: SchoolFeed, article_url: str, reference: date):
    match = DATE_LINE_RE.match(line)
    if not match:
        return None

    month = MONTHS[match.group("month").casefold().rstrip(".")]
    day = int(match.group("day"))
    year = _year_for_month(month, reference)
    try:
        start_date = date(year, month, day)
    except ValueError:
        return None

    end_date = None
    if match.group("day2"):
        month2_name = match.group("month2")
        month2 = MONTHS[month2_name.casefold().rstrip(".")] if month2_name else month
        year2 = year + (1 if month2 < month else 0)
        try:
            end_date = date(year2, month2, int(match.group("day2")))
        except ValueError:
            end_date = None

    raw_title = re.sub(r"\s+", " ", match.group("title")).strip()
    lower = raw_title.casefold()

    if any(phrase in lower for phrase in DISTRICT_DUPLICATE_PHRASES):
        return None
    if any(phrase in lower for phrase in SKIP_PHRASES):
        return None

    title, start, end = _extract_time(raw_title)
    if not title:
        return None

    low_title = title.casefold()
    if low_title.startswith(("http://", "https://")) or "lunch menu" in low_title:
        return None

    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:54]
    event = {
        "id": f"usd-schoolfeed-{school.id}-{start_date.isoformat()}-{slug}",
        "title": title,
        "date": start_date.isoformat(),
        "schools": [school.id],
        "scope": "school",
        "category": "general",
        "source": f"{SOURCE_PREFIX} — {school.name}",
        "sourceUrl": article_url,
    }
    if start:
        event["start"] = start
        if end:
            event["end"] = end
    else:
        event["allDay"] = True

    if end_date and end_date != start_date:
        event["endDate"] = end_date.isoformat()
        event["weekdaysOnly"] = True

    return event


def parse_school_post(
    html: str,
    school: SchoolFeed,
    article_url: str,
    *,
    reference: date | None = None,
) -> list[dict]:
    reference = reference or date.today()
    parser = _BlockTextParser()
    parser.feed(html)

    events = []
    seen = set()
    for line in parser.lines():
        event = _event_from_line(line, school, article_url, reference)
        if not event:
            continue
        key = (event["date"], event["title"], tuple(event["schools"]), event.get("start"))
        if key in seen:
            continue
        seen.add(key)
        events.append(event)
    return events


def fetch_school_feed(
    school: SchoolFeed,
    *,
    timeout: int = 30,
    opener=urlopen,
    article_limit: int = 12,
    reference: date | None = None,
) -> list[dict]:
    reference = reference or date.today()
    home_html = _request(school.home, timeout=timeout, opener=opener)
    urls = _article_urls(home_html, school.home, limit=article_limit)

    if not urls:
        raise RuntimeError(f"{school.name} homepage returned no public ParentSquare article links")

    events = []
    seen = set()
    successes = 0

    for url in urls:
        try:
            html = _request(url, timeout=timeout, opener=opener)
            successes += 1
        except Exception:
            continue

        for event in parse_school_post(html, school, url, reference=reference):
            key = (event["date"], event["title"], tuple(event["schools"]), event.get("start"))
            if key in seen:
                continue
            seen.add(key)
            events.append(event)

    if successes == 0:
        raise RuntimeError(f"{school.name} public ParentSquare article pages could not be loaded")

    floor = date(reference.year if reference.month >= 7 else reference.year - 1, 7, 1)
    events = [e for e in events if date.fromisoformat(e["date"]) >= floor]
    return sorted(events, key=lambda e: (e["date"], e.get("start", ""), e["title"]))
