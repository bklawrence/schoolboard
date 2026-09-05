from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import Request, urlopen

SOURCE_PREFIX = "USD 116 School Feed"


@dataclass(frozen=True)
class SchoolFeed:
    id: str
    name: str
    homepage: str


SCHOOL_FEEDS = (
    SchoolFeed("uecs", "Urbana Early Childhood School", "https://uecs.usd116.org/"),
    SchoolFeed("yankee", "Yankee Ridge Multilingual", "https://yridge.usd116.org/"),
    SchoolFeed("leal", "Leal Elementary", "https://leal.usd116.org/"),
    SchoolFeed("paine", "Thomas Paine Elementary", "https://tmspaine.usd116.org/"),
    SchoolFeed("williams", "Dr. Williams Elementary", "https://dpw.usd116.org/"),
    SchoolFeed("king", "Dr. Martin Luther King Jr. Elementary", "https://drking.usd116.org/"),
    SchoolFeed("sgc", "Urbana Sixth Grade Center", "https://sgc.usd116.org/"),
    SchoolFeed("ums", "Urbana Middle School", "https://ums.usd116.org/"),
    SchoolFeed("uhs", "Urbana High School", "https://uhs.usd116.org/"),
)

ALL_USD116_SCHOOLS = tuple(s.id for s in SCHOOL_FEEDS)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "div", "dl", "dt", "dd",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_MONTH_WORD = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.??"
)
_DATE_TOKEN = rf"{_MONTH_WORD}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?"
_DATE_TOKEN_RE = re.compile(_DATE_TOKEN, re.IGNORECASE)
_DATE_MARKER_RE = re.compile(
    rf"(?:{_DATE_TOKEN})\s*(?:[-–—]|\bto\b)\s*(?:{_DATE_TOKEN})|(?:{_DATE_TOKEN})",
    re.IGNORECASE,
)
_POSTED_DATE_RE = re.compile(r"Posted\s+Date:\s*(\d{1,2})/(\d{1,2})/(\d{2,4})", re.IGNORECASE)

_TIME_ATOM = r"(?P<{name}_h>\d{{1,2}})(?::(?P<{name}_m>\d{{2}}))?\s*(?P<{name}_ap>[ap]\.??m\.??)?"
_TIME_RANGE_RE = re.compile(
    _TIME_ATOM.format(name="a") + r"\s*(?:[-–—]|\bto\b)\s*" + _TIME_ATOM.format(name="b"),
    re.IGNORECASE,
)
_SINGLE_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap]\.??m\.??)\b", re.IGNORECASE)

# These are event concepts that families plausibly put on a calendar. The parser is
# intentionally conservative: it requires both a real date and one of these concepts
# before turning ordinary newsletter prose into an event.
_EVENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bfamily neighborhood night\b", re.I), "Family Neighborhood Night"),
    (re.compile(r"\bwelcome back(?: to school)? celebration\b|\bback to school celebration\b", re.I), "Welcome Back Celebration"),
    (re.compile(r"\bvillage investment promise\b", re.I), "Village Investment Promise Meeting"),
    (re.compile(r"\bcurriculum night\b", re.I), "Curriculum Night"),
    (re.compile(r"\b(?:school )?pictures?\b|\bpicture day\b", re.I), "School Pictures"),
    (re.compile(r"\bopen house\b", re.I), "Open House"),
    (re.compile(r"\bback[- ]to[- ]school night\b", re.I), "Back to School Night"),
    (re.compile(r"\bfamily night\b", re.I), "Family Night"),
    (re.compile(r"\bdental days?\b", re.I), "Dental Day"),
    (re.compile(r"\bhealth fair\b", re.I), "Health Fair"),
    (re.compile(r"\b(?:board of education|school board|board)\b.*\bmeeting\b", re.I), "Board of Education Meeting"),
    (re.compile(r"\borientation\b", re.I), "Orientation"),
    (re.compile(r"\bconcert\b", re.I), "Concert"),
    (re.compile(r"\bperformance\b", re.I), "Performance"),
    (re.compile(r"\bpicnic\b", re.I), "Picnic"),
    (re.compile(r"\bfundraiser\b", re.I), "Fundraiser"),
    (re.compile(r"\bcarnival\b", re.I), "Carnival"),
    (re.compile(r"\bfestival\b", re.I), "Festival"),
    (re.compile(r"\bcelebration\b", re.I), "Celebration"),
    (re.compile(r"\btalent show\b", re.I), "Talent Show"),
    (re.compile(r"\bscience fair\b", re.I), "Science Fair"),
    (re.compile(r"\bbook fair\b", re.I), "Book Fair"),
    (re.compile(r"\bparent(?:s)?(?:/guardian)? meeting\b", re.I), "Parent Meeting"),
)

_PARENT_GROUP_RE = re.compile(
    r"\b(PTA|PTO|PTSA|PTF)\b|\bparent teacher association\b|\bparent[- ]teacher fellowship\b|"
    r"\bparent association\b|\bparent group\b",
    re.IGNORECASE,
)
_MEETING_RE = re.compile(r"\bmeet(?:ing|ings)?\b", re.IGNORECASE)

# Date mentions with these cues are usually tasks, paperwork, testing windows, or
# historical references rather than events a family would add to a calendar.
_DEADLINE_RE = re.compile(
    r"\b(?:due|deadline|submit|submitted|submission|turn in|turned in|must be received|"
    r"registration closes?|registration deadline|forms? due|paperwork|proof of|documents? due|required by|exam by|must have .*? by)\b",
    re.IGNORECASE,
)
_TEST_WINDOW_RE = re.compile(
    r"\b(?:testing window|assessment window|benchmark window|screening window|NWEA|MClass|"
    r"MAP testing|MAP assessment|beginning of year assessment|BOY assessment)\b",
    re.IGNORECASE,
)
_STRUCTURAL_RE = re.compile(
    r"\b(?:no school|labor day|fall break|winter break|spring break|end of (?:the )?quarter|"
    r"parent[- ]teacher conferences?|family[- ]teacher conferences?|early dismissal|late start|"
    r"institute day|teacher institute|school improvement day|staff development day)\b",
    re.IGNORECASE,
)
_HISTORICAL_RE = re.compile(
    r"\b(?:at (?:the|our) .*? meeting|during (?:the|our) .*? meeting|met on|held on)\b",
    re.IGNORECASE,
)

_DISTRICT_POST_RE = re.compile(
    r"\b(?:superintendent update|board of education update|boe update|district update|family focus)\b",
    re.IGNORECASE,
)

_FOOTER_MARKERS = (
    "address:",
    "contents ©",
    "powered by parentsquare",
    "privacy policy",
    "terms of use",
)


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[tuple[str, str]] = []
        self._parts: list[str] = []
        self._line_tag = ""
        self._skip_depth = 0

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
        if text:
            self.lines.append((self._line_tag, text))
        self._parts.clear()
        self._line_tag = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        if tag in _HEADING_TAGS:
            self._line_tag = tag

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            clean = re.sub(r"\s+", " ", data).strip()
            if clean:
                self._parts.append(clean)

    def close(self) -> None:
        super().close()
        self._flush()


def _fetch_html(url: str, *, timeout: int = 20) -> str:
    req = Request(url, headers=_HEADERS)
    with urlopen(req, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _canonical_post_url(homepage: str, article_id: str) -> str:
    parsed = urlsplit(homepage)
    return f"{parsed.scheme}://{parsed.netloc}/index.php?pageID=smartSiteFeed&psqFeed=true&articleID={article_id}"


def _discover_post_urls(school: SchoolFeed) -> list[str]:
    page = _fetch_html(school.homepage)
    decoded = html.unescape(page)

    article_ids: list[str] = []
    seen: set[str] = set()

    # Apptegy/ParentSquare links can vary in query-parameter ordering. Article ID is
    # the stable identifier, so discover that first and rebuild a canonical URL.
    for match in re.finditer(r"(?:href\s*=\s*[\"']([^\"']+)[\"'])", decoded, re.I):
        href = match.group(1)
        if "articleID=" not in href:
            continue
        absolute = urljoin(school.homepage, href)
        query = parse_qs(urlsplit(absolute).query)
        ids = query.get("articleID") or query.get("articleid")
        if not ids:
            id_match = re.search(r"[?&]articleID=(\d+)", absolute, re.I)
            ids = [id_match.group(1)] if id_match else []
        for article_id in ids:
            if article_id.isdigit() and article_id not in seen:
                seen.add(article_id)
                article_ids.append(article_id)

    # Fallback for inline JS/JSON where the URL is not literally in an href.
    if len(article_ids) < 4:
        for article_id in re.findall(r"articleID(?:=|%3D|\\u003[dD])(\d+)", decoded, re.I):
            if article_id not in seen:
                seen.add(article_id)
                article_ids.append(article_id)

    urls = [_canonical_post_url(school.homepage, article_id) for article_id in article_ids[:4]]
    print(f"usd-feed-{school.id} detail: found {len(urls)} public ParentSquare posts via homepage HTML")
    if not urls:
        raise RuntimeError(f"{school.name} homepage exposed no public ParentSquare post links")
    return urls


def _visible_lines(page_html: str) -> list[tuple[str, str]]:
    parser = _VisibleTextParser()
    parser.feed(page_html)
    parser.close()
    return parser.lines


def _post_metadata(lines: list[tuple[str, str]], reference: date) -> tuple[str, date, int]:
    posted_idx = -1
    posted_date = reference
    for idx, (_tag, text) in enumerate(lines):
        match = _POSTED_DATE_RE.search(text)
        if not match:
            continue
        year = int(match.group(3))
        if year < 100:
            year += 2000
        try:
            posted_date = date(year, int(match.group(1)), int(match.group(2)))
        except ValueError:
            posted_date = reference
        posted_idx = idx
        break

    title = ""
    if posted_idx >= 0:
        for idx in range(posted_idx - 1, max(-1, posted_idx - 8), -1):
            tag, text = lines[idx]
            if tag in _HEADING_TAGS and len(text) <= 180:
                title = text
                break
        if not title:
            for idx in range(posted_idx - 1, max(-1, posted_idx - 5), -1):
                text = lines[idx][1]
                if len(text) <= 180 and not text.lower().startswith(("menu", "search")):
                    title = text
                    break
    return title, posted_date, posted_idx


def _content_lines(lines: list[tuple[str, str]], posted_idx: int) -> list[tuple[str, str]]:
    start = posted_idx + 1 if posted_idx >= 0 else 0
    result: list[tuple[str, str]] = []
    for tag, text in lines[start:]:
        lower = text.lower().strip()
        if any(lower.startswith(marker) for marker in _FOOTER_MARKERS):
            break
        result.append((tag, text))
    return result


def _normalize_month_word(word: str) -> str:
    return re.sub(r"[^a-z]", "", word.lower())


def _infer_year(month: int, day_num: int, explicit_year: int | None, posted: date) -> int:
    if explicit_year is not None:
        return explicit_year
    year = posted.year
    try:
        candidate = date(year, month, day_num)
    except ValueError:
        return year
    # Newsletters near New Year often mention January from a December post. Prefer a
    # nearby future date rather than sending the event a year into the past.
    if candidate < posted - timedelta(days=120):
        year += 1
    elif candidate > posted + timedelta(days=300):
        year -= 1
    return year


def _parse_date_token(token: str, posted: date) -> date | None:
    match = re.search(
        rf"(?P<month>{_MONTH_WORD})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{{4}}))?",
        token,
        re.IGNORECASE,
    )
    if not match:
        return None
    month_key = _normalize_month_word(match.group("month"))
    month = _MONTHS.get(month_key)
    if not month:
        return None
    day_num = int(match.group("day"))
    explicit_year = int(match.group("year")) if match.group("year") else None
    year = _infer_year(month, day_num, explicit_year, posted)
    try:
        return date(year, month, day_num)
    except ValueError:
        return None


def _dates_from_marker(marker: str, posted: date) -> list[date]:
    result: list[date] = []
    for match in _DATE_TOKEN_RE.finditer(marker):
        parsed = _parse_date_token(match.group(0), posted)
        if parsed:
            result.append(parsed)
    return result


def _ampm(value: str | None) -> str | None:
    if not value:
        return None
    return "p" if value.lower().startswith("p") else "a"


def _clock(hour: int, minute: int, ap: str | None) -> str | None:
    if minute > 59 or hour > 23:
        return None
    if ap:
        if hour < 1 or hour > 12:
            return None
        if hour == 12:
            hour = 0
        if ap == "p":
            hour += 12
    return f"{hour:02d}:{minute:02d}"


def _parse_time_range(text: str) -> tuple[str | None, str | None]:
    for match in _TIME_RANGE_RE.finditer(text):
        a_h = int(match.group("a_h"))
        a_m = int(match.group("a_m") or 0)
        b_h = int(match.group("b_h"))
        b_m = int(match.group("b_m") or 0)
        a_ap = _ampm(match.group("a_ap"))
        b_ap = _ampm(match.group("b_ap"))

        # Ignore bare numeric ranges such as grades 1-5; a real time range needs
        # either minutes (6:00-7:30) or an explicit AM/PM marker.
        if not (match.group("a_m") or match.group("b_m") or match.group("a_ap") or match.group("b_ap")):
            continue

        # If only one end says AM/PM, normal human prose usually intends it for both.
        if a_ap is None and b_ap is not None:
            a_ap = b_ap
        if b_ap is None and a_ap is not None:
            b_ap = a_ap

        # A range like 6:00-7:30 may be followed by “6PM” or “7:30PM” elsewhere in
        # the same sentence/paragraph. Use that cue rather than guessing.
        if a_ap is None and b_ap is None:
            suffixes = {_ampm(m.group(3)) for m in _SINGLE_TIME_RE.finditer(text)}
            suffixes.discard(None)
            if len(suffixes) == 1:
                only = next(iter(suffixes))
                a_ap = b_ap = only
            else:
                continue

        start = _clock(a_h, a_m, a_ap)
        end = _clock(b_h, b_m, b_ap)
        if start and end:
            return start, end

    # If there is no range, a single explicit time can still be useful.
    single = _SINGLE_TIME_RE.search(text)
    if single:
        start = _clock(int(single.group(1)), int(single.group(2) or 0), _ampm(single.group(3)))
        return start, None
    return None, None


def _parse_unsuffixed_evening_range(text: str) -> tuple[str | None, str | None]:
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))\s*[-–—]\s*(\d{1,2})(?::(\d{2}))\b", text)
    if not match:
        return None, None
    a_h, a_m, b_h, b_m = map(int, match.groups())
    if not (1 <= a_h <= 8 and 1 <= b_h <= 9):
        return None, None
    return _clock(a_h, a_m, "p"), _clock(b_h, b_m, "p")


def _strip_times(text: str) -> str:
    def replace_range(match: re.Match[str]) -> str:
        if match.group("a_m") or match.group("b_m") or match.group("a_ap") or match.group("b_ap"):
            return " "
        return match.group(0)

    text = _TIME_RANGE_RE.sub(replace_range, text)
    text = _SINGLE_TIME_RE.sub(" ", text)
    text = re.sub(r"\b(?:from|at|until)\s*(?=[,;:.!?-]|$)", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _clean_title(text: str) -> str:
    text = html.unescape(text)
    text = _strip_times(text)
    text = re.sub(r"^[\s:;,.\-–—|]+|[\s:;,.\-–—|]+$", "", text)
    text = re.sub(r"^(?:on|for|will be|is|are)\s+", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _slugify(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:72] or "event"


def _normalize_title(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _is_noise_title(title: str) -> bool:
    if len(title) < 3 or len(title) > 150:
        return True
    if re.fullmatch(r"(?:to|from|at|until)?\s*\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?", title, re.I):
        return True
    if not re.search(r"[A-Za-z]", title):
        return True
    return False


def _canonical_parent_group_title(text: str, heading: str) -> str | None:
    combined = f"{heading} {text}"
    if not _PARENT_GROUP_RE.search(combined):
        return None
    if not (_MEETING_RE.search(text) or _MEETING_RE.search(heading)):
        return None

    acronym_match = re.search(r"\b(PTA|PTO|PTSA|PTF)\b", combined, re.I)
    if acronym_match:
        return f"{acronym_match.group(1).upper()} Meeting"
    if re.search(r"parent[- ]teacher fellowship", combined, re.I):
        return "Parent Teacher Fellowship Meeting"
    if re.search(r"parent teacher association", combined, re.I):
        return "PTA Meeting"
    if re.search(r"parent association", combined, re.I):
        return "Parent Association Meeting"
    return "Parent Group Meeting"


def _event_title_from_heading(heading: str) -> str | None:
    if not heading:
        return None
    if _PARENT_GROUP_RE.search(heading):
        acronym_match = re.search(r"\b(PTA|PTO|PTSA|PTF)\b", heading, re.I)
        if acronym_match:
            return f"{acronym_match.group(1).upper()} Meeting"
        if re.search(r"parent teacher association", heading, re.I):
            return "PTA Meeting"
        if re.search(r"parent[- ]teacher fellowship", heading, re.I):
            return "Parent Teacher Fellowship Meeting"
        return "Parent Group Meeting"
    for pattern, canonical in _EVENT_PATTERNS:
        if pattern.search(heading):
            return canonical
    match = re.search(r"\b([A-Z][A-Za-z0-9&'’./-]*(?:\s+[A-Z][A-Za-z0-9&'’./-]*){0,5})\s+(Meeting|Night|Open House)\b", heading)
    if match:
        candidate = _clean_title(match.group(0))
        if not _is_noise_title(candidate):
            return candidate
    return None


_DISTRICT_ALLOWED_TITLES = {
    "family neighborhood night",
    "village investment promise meeting",
    "dental day",
    "board of education meeting",
    "welcome back celebration",
    "health fair",
}


def _district_event_allowed(title: str) -> bool:
    return _normalize_title(title) in _DISTRICT_ALLOWED_TITLES


def _event_title_from_prose(text: str, heading: str) -> str | None:
    parent_title = _canonical_parent_group_title(text, heading)
    if parent_title:
        return parent_title

    for pattern, canonical in _EVENT_PATTERNS:
        if pattern.search(text) or (heading and pattern.search(heading) and _MEETING_RE.search(text)):
            return canonical

    # Generic named meetings: capture the few words directly before “meeting” when
    # there is a date in the same sentence. This is deliberately a fallback.
    match = re.search(
        r"\b([A-Z][A-Za-z0-9&'’./-]*(?:\s+[A-Z][A-Za-z0-9&'’./-]*){0,5})\s+meeting\b",
        text,
    )
    if match:
        candidate = _clean_title(match.group(1) + " Meeting")
        if not _is_noise_title(candidate):
            return candidate
    return None


def _exclude_context(text: str, *, prose: bool) -> bool:
    if _DEADLINE_RE.search(text) or _TEST_WINDOW_RE.search(text):
        return True
    if _STRUCTURAL_RE.search(text):
        return True
    if prose and _HISTORICAL_RE.search(text):
        return True
    return False


def _location_for_event(title: str, context: str) -> str | None:
    normalized = _normalize_title(title)
    if normalized == "dental day":
        return "Urbana School Health Center"
    if normalized == "village investment promise meeting":
        return "Urbana Sixth Grade Center"
    if normalized == "family neighborhood night":
        match = re.search(r"\bIvanhoe Estates(?:\s*\([^)]*\))?", context, re.I)
        return match.group(0) if match else "Ivanhoe Estates"
    if normalized == "welcome back celebration":
        if re.search(r"\bUrbana Middle School\b", context, re.I):
            return "Urbana Middle School"

    match = re.search(
        r"\bat\s+(?:the\s+)?([A-Z][A-Za-z0-9&'’./ -]{1,70}?(?:School|Center|Estates|Park|Library|Gym|Auditorium|Field))\b",
        context,
    )
    return _clean_title(match.group(1)) if match else None


def _event_record(
    *,
    school: SchoolFeed,
    title: str,
    event_date: date,
    source_url: str,
    district_post: bool,
    start: str | None,
    end: str | None,
    location: str | None = None,
) -> dict:
    schools = list(ALL_USD116_SCHOOLS) if district_post else [school.id]
    scope = "district" if district_post else "school"
    source_name = f"{SOURCE_PREFIX} — {'District' if district_post else school.name}"
    event_id_school = "district" if district_post else school.id
    record = {
        "id": f"usd-schoolfeed-{event_id_school}-{event_date.isoformat()}-{_slugify(title)}",
        "title": title,
        "date": event_date.isoformat(),
        "schools": schools,
        "scope": scope,
        "category": "general",
        "source": source_name,
        "sourceUrl": source_url,
    }
    if start:
        record["start"] = start
    else:
        record["allDay"] = True
    if end:
        record["end"] = end
    if location:
        record["location"] = location
    return record


def _event_date_ok(event_date: date, reference: date) -> bool:
    # Match the site's rolling horizon closely enough to keep stale newsletter dates
    # from resurfacing, while leaving the final global windowing to build_data.py.
    return reference - timedelta(days=35) <= event_date <= reference + timedelta(days=75)



def _is_calendarish_line(text: str, heading: str) -> bool:
    markers = list(_DATE_MARKER_RE.finditer(text))
    if not markers:
        return False
    prefix = text[:markers[0].start()]
    calendar_words = re.compile(r"\b(?:calendar|important dates|upcoming dates|dates to remember)\b", re.I)
    return bool(
        calendar_words.search(heading)
        or calendar_words.search(prefix)
        or not re.search(r"[A-Za-z]", prefix)
    )

def _calendar_style_events(
    lines: list[tuple[str, str]],
    *,
    school: SchoolFeed,
    source_url: str,
    posted: date,
    reference: date,
    district_post: bool,
) -> list[dict]:
    events: list[dict] = []
    current_heading = ""

    for tag, text in lines:
        if tag in _HEADING_TAGS:
            current_heading = text
            continue
        if not _DATE_TOKEN_RE.search(text):
            continue

        markers = list(_DATE_MARKER_RE.finditer(text))
        if not markers:
            continue

        if not _is_calendarish_line(text, current_heading):
            continue

        # A calendar-style line can contain many entries in a single HTML text node.
        # Split each date marker from the text that follows it up to the next date.
        for idx, marker in enumerate(markers):
            marker_dates = _dates_from_marker(marker.group(0), posted)
            if not marker_dates:
                continue
            event_date = marker_dates[0]
            segment_end = markers[idx + 1].start() if idx + 1 < len(markers) else len(text)
            raw_segment = text[marker.end():segment_end].strip()
            if not raw_segment:
                continue

            context = f"{current_heading} {raw_segment}".strip()
            if _exclude_context(context, prose=False):
                continue

            # Date ranges are usually windows. If a real event concept follows the
            # range, keep the start date; otherwise skip the range.
            if len(marker_dates) > 1:
                if not _event_title_from_prose(raw_segment, current_heading):
                    continue

            start, end = _parse_time_range(raw_segment)
            title = _clean_title(raw_segment)

            # Repair lines whose title is really just a time fragment by using a clear
            # event heading (e.g. a “Dental Day” section) when available.
            if _is_noise_title(title):
                title = _event_title_from_prose(raw_segment, current_heading) or _event_title_from_heading(current_heading) or ""
            if _is_noise_title(title):
                continue
            if not _event_date_ok(event_date, reference):
                continue

            events.append(
                _event_record(
                    school=school,
                    title=title,
                    event_date=event_date,
                    source_url=source_url,
                    district_post=district_post,
                    start=start,
                    end=end,
                    location=_location_for_event(title, context),
                )
            )
    return events


def _section_list_events(
    lines: list[tuple[str, str]],
    *,
    school: SchoolFeed,
    source_url: str,
    posted: date,
    reference: date,
    district_post: bool,
) -> list[dict]:
    events: list[dict] = []
    current_heading = ""
    heading_title: str | None = None

    for idx, (tag, text) in enumerate(lines):
        if tag in _HEADING_TAGS:
            current_heading = text
            heading_title = _event_title_from_heading(text)
            continue

        markers = list(_DATE_TOKEN_RE.finditer(text))
        if not markers:
            continue
        if _exclude_context(text, prose=True):
            continue

        own_title = _event_title_from_prose(text, current_heading)
        title = own_title or heading_title
        if not title:
            continue

        first_prefix = text[:markers[0].start()]
        looks_like_schedule_line = (
            not re.search(r"[A-Za-z]", first_prefix)
            or bool(re.search(r"\b(?:dates?|scheduled|will be held|takes? place|event)\b", text, re.I))
            or own_title is not None
        )
        if not looks_like_schedule_line:
            continue

        # When a section puts the date on one bullet and the time on the next, borrow
        # the first nearby time before the next heading.
        nearby_text = text
        if len(markers) == 1 and not _parse_time_range(nearby_text)[0]:
            extra: list[str] = []
            for next_tag, next_text in lines[idx + 1:idx + 4]:
                if next_tag in _HEADING_TAGS:
                    break
                extra.append(next_text)
            if extra:
                nearby_text = " ".join([text] + extra)

        common_start, common_end = _parse_time_range(text)
        if not common_start and re.search(r"\b(?:meeting|night|open house)\b", title, re.I):
            common_start, common_end = _parse_unsuffixed_evening_range(text)

        for marker in markers:
            event_date = _parse_date_token(marker.group(0), posted)
            if not event_date or not _event_date_ok(event_date, reference):
                continue
            time_text = nearby_text if len(markers) == 1 else text[marker.end():]
            start, end = _parse_time_range(time_text)
            if not start and re.search(r"\b(?:meeting|night|open house)\b", title, re.I):
                start, end = _parse_unsuffixed_evening_range(time_text)
            if not start and common_start:
                start, end = common_start, common_end
            events.append(
                _event_record(
                    school=school,
                    title=title,
                    event_date=event_date,
                    source_url=source_url,
                    district_post=district_post,
                    start=start,
                    end=end,
                    location=_location_for_event(title, f"{current_heading} {nearby_text}"),
                )
            )
    return events


def _sentence_chunks(text: str) -> Iterable[str]:
    # Do not split on periods inside common AM/PM abbreviations or month abbreviations.
    protected = text
    replacements = {
        "a.m.": "a¤m¤",
        "p.m.": "p¤m¤",
        "A.M.": "A¤M¤",
        "P.M.": "P¤M¤",
        "Sept.": "Sept¤",
        "Sep.": "Sep¤",
        "Aug.": "Aug¤",
        "Oct.": "Oct¤",
        "Nov.": "Nov¤",
        "Dec.": "Dec¤",
        "Jan.": "Jan¤",
        "Feb.": "Feb¤",
        "Mar.": "Mar¤",
        "Apr.": "Apr¤",
    }
    for old, new in replacements.items():
        protected = protected.replace(old, new)
    parts = re.split(r"(?<=[.!?])\s+|\s*[•●▪]\s*", protected)
    for part in parts:
        if not part.strip():
            continue
        for old, new in replacements.items():
            part = part.replace(new, old)
        yield part.strip()


def _prose_events(
    lines: list[tuple[str, str]],
    *,
    school: SchoolFeed,
    source_url: str,
    posted: date,
    reference: date,
    district_post: bool,
) -> list[dict]:
    events: list[dict] = []
    current_heading = ""

    for tag, text in lines:
        if tag in _HEADING_TAGS:
            current_heading = text
            continue

        # Calendar/list lines are handled by the date-led parser; skipping them here
        # prevents a second canonicalized prose copy of the same event.
        if _is_calendarish_line(text, current_heading):
            continue

        # Keep the full line for time inference, but evaluate sentence-sized chunks for
        # event meaning. A sentence must contain both a concrete date and an event cue.
        full_line = text
        for sentence in _sentence_chunks(text):
            date_match = _DATE_TOKEN_RE.search(sentence)
            if not date_match:
                continue
            if _exclude_context(sentence, prose=True):
                continue

            title = _event_title_from_prose(sentence, current_heading)
            if not title:
                continue
            event_date = _parse_date_token(date_match.group(0), posted)
            if not event_date or not _event_date_ok(event_date, reference):
                continue

            start, end = _parse_time_range(full_line)
            if not start and re.search(r"\b(?:meeting|night|open house)\b", title, re.I):
                start, end = _parse_unsuffixed_evening_range(full_line)
            events.append(
                _event_record(
                    school=school,
                    title=title,
                    event_date=event_date,
                    source_url=source_url,
                    district_post=district_post,
                    start=start,
                    end=end,
                    location=_location_for_event(title, f"{current_heading} {full_line}"),
                )
            )
    return events


def _dedupe(events: Iterable[dict]) -> list[dict]:
    chosen: dict[tuple, dict] = {}
    for event in events:
        key = (
            event.get("date", ""),
            _normalize_title(event.get("title", "")),
            tuple(sorted(event.get("schools") or [])),
        )
        existing = chosen.get(key)
        if not existing:
            chosen[key] = event
            continue

        # Prefer the version with a start/end time and then the cleaner, shorter title.
        def quality(item: dict) -> tuple[int, int, int, int]:
            return (
                0 if item.get("start") else 1,
                0 if item.get("end") else 1,
                0 if item.get("location") else 1,
                len(item.get("title", "")),
            )

        if quality(event) < quality(existing):
            chosen[key] = event

    return sorted(
        chosen.values(),
        key=lambda e: (e.get("date", ""), e.get("start", "23:59"), e.get("title", "").lower()),
    )


def _parse_post(
    page_html: str,
    *,
    school: SchoolFeed,
    source_url: str,
    reference: date,
) -> list[dict]:
    lines = _visible_lines(page_html)
    post_title, posted, posted_idx = _post_metadata(lines, reference)
    content = _content_lines(lines, posted_idx)
    district_post = bool(_DISTRICT_POST_RE.search(post_title))

    events = _calendar_style_events(
        content,
        school=school,
        source_url=source_url,
        posted=posted,
        reference=reference,
        district_post=district_post,
    )
    events.extend(
        _section_list_events(
            content,
            school=school,
            source_url=source_url,
            posted=posted,
            reference=reference,
            district_post=district_post,
        )
    )
    events.extend(
        _prose_events(
            content,
            school=school,
            source_url=source_url,
            posted=posted,
            reference=reference,
            district_post=district_post,
        )
    )
    events = _dedupe(events)
    if district_post:
        events = [event for event in events if _district_event_allowed(event.get("title", ""))]
    return events


def fetch_school_feed(
    school: SchoolFeed,
    *,
    reference: date | None = None,
    return_refreshed_urls: bool = False,
):
    reference = reference or date.today()
    post_urls = _discover_post_urls(school)

    fresh_events: list[dict] = []
    refreshed_urls: set[str] = set()
    failures: list[str] = []

    for url in post_urls:
        try:
            page = _fetch_html(url)
            parsed = _parse_post(page, school=school, source_url=url, reference=reference)
            fresh_events.extend(parsed)
            refreshed_urls.add(url)
        except Exception as exc:  # one broken post should not invalidate the other three
            failures.append(f"{type(exc).__name__}: {exc}")

    if not refreshed_urls:
        detail = "; ".join(failures[:2]) or "no post pages refreshed"
        raise RuntimeError(f"{school.name} ParentSquare refresh failed: {detail}")

    fresh_events = _dedupe(fresh_events)
    print(
        f"usd-feed-{school.id} detail: parsed {len(fresh_events)} calendar-worthy event(s) "
        f"from {len(refreshed_urls)} refreshed ParentSquare post(s)"
    )
    if failures:
        print(f"usd-feed-{school.id} detail: {len(failures)} ParentSquare post refresh(es) failed")

    if return_refreshed_urls:
        return fresh_events, refreshed_urls
    return fresh_events
