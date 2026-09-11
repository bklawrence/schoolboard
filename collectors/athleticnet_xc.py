from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class AthleticNetXCSource:
    id: str
    athletic_id: int
    source: str
    schools: tuple[str, ...]
    season: int
    title_prefix: str

    @property
    def team_url(self) -> str:
        return f"https://www.athletic.net/team/{self.athletic_id}/cross-country/{self.season}"

    @property
    def print_url(self) -> str:
        return (
            "https://www.athletic.net/CrossCountry/Print/Calendar.aspx"
            f"?SchoolID={self.athletic_id}&S={self.season}"
        )

    @property
    def embed_help_url(self) -> str:
        return (
            "https://www.athletic.net/Help/EmbedHelp.aspx"
            f"?CrossCountry=&Report=XCCalendar1&S={self.season}&SchoolID={self.athletic_id}"
        )


ATHLETICNET_XC_SOURCES = (
    AthleticNetXCSource(
        id="uni-8th-xc",
        athletic_id=22694,
        source="Uni 8th Grade Cross Country — Athletic.net",
        schools=("uni",),
        season=2026,
        title_prefix="8th Grade Cross Country",
    ),
    AthleticNetXCSource(
        id="ums-sgc-xc",
        athletic_id=30924,
        source="Urbana Middle / SGC Cross Country — Athletic.net",
        schools=("ums", "sgc"),
        season=2026,
        title_prefix="Cross Country",
    ),
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/javascript,*/*;q=0.8",
}

_MONTHS = {
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
_MONTH_RE = re.compile(
    r"(?:(?:Mon|Tue|Tues|Wed|Thu|Thur|Fri|Sat|Sun)\w*,?\s+)?"
    r"(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\.?\s+(?P<day>\d{1,2})(?:,?\s*(?P<year>\d{4}))?",
    re.I,
)
_NUMERIC_DATE_RE = re.compile(
    r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{2,4}))?\b"
)
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", re.I)


class _CalendarHTMLParser(HTMLParser):
    """Collect table rows and calendar-item blocks from Athletic.net HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self.blocks: list[dict] = []
        self._row_depth = 0
        self._row_text: list[str] = []
        self._row_links: list[tuple[str, str]] = []
        self._block_depth = 0
        self._block_text: list[str] = []
        self._block_links: list[tuple[str, str]] = []
        self._active_href: str | None = None
        self._active_anchor_text: list[str] = []

    @staticmethod
    def _classes(attrs) -> set[str]:
        for key, value in attrs:
            if key.lower() == "class" and value:
                return set(str(value).split())
        return set()

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        classes = self._classes(attrs)

        if tag == "tr":
            self._row_depth += 1
            if self._row_depth == 1:
                self._row_text = []
                self._row_links = []

        if "ANETxcCAL_CalItem" in classes:
            self._block_depth += 1
            if self._block_depth == 1:
                self._block_text = []
                self._block_links = []
        elif self._block_depth:
            self._block_depth += 1

        if tag == "a":
            href = ""
            for key, value in attrs:
                if key.lower() == "href" and value:
                    href = str(value)
                    break
            self._active_href = href
            self._active_anchor_text = []

    def handle_data(self, data: str) -> None:
        clean = re.sub(r"\s+", " ", data).strip()
        if not clean:
            return
        if self._row_depth:
            self._row_text.append(clean)
        if self._block_depth:
            self._block_text.append(clean)
        if self._active_href is not None:
            self._active_anchor_text.append(clean)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag == "a" and self._active_href is not None:
            anchor_text = re.sub(r"\s+", " ", " ".join(self._active_anchor_text)).strip()
            item = (self._active_href, anchor_text)
            if self._row_depth:
                self._row_links.append(item)
            if self._block_depth:
                self._block_links.append(item)
            self._active_href = None
            self._active_anchor_text = []

        if tag == "tr" and self._row_depth:
            self._row_depth -= 1
            if self._row_depth == 0:
                text = re.sub(r"\s+", " ", " ".join(self._row_text)).strip()
                if text:
                    self.rows.append({"text": text, "links": list(self._row_links)})

        if self._block_depth:
            self._block_depth -= 1
            if self._block_depth == 0:
                text = re.sub(r"\s+", " ", " ".join(self._block_text)).strip()
                if text:
                    self.blocks.append({"text": text, "links": list(self._block_links)})


def _fetch_text(url: str, *, timeout: int = 25) -> str:
    req = Request(url, headers=_HEADERS)
    with urlopen(req, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _decode_remote_payload(text: str) -> str:
    """RemoteHTML.ashx usually returns JavaScript document.write calls."""
    if "<" in text and "ANETxcCAL" in text:
        return text

    pieces: list[str] = []
    for match in re.finditer(
        r"document\.write\(\s*(?P<q>[\"'])(?P<body>.*?)(?P=q)\s*\)\s*;?",
        text,
        re.I | re.S,
    ):
        q = match.group("q")
        body = match.group("body")
        try:
            if q == '"':
                pieces.append(json.loads(f'"{body}"'))
            else:
                body = body.replace(r"\'", "'").replace(r"\\", "\\")
                body = body.replace(r"\n", "\n").replace(r"\r", "\r").replace(r"\t", "\t")
                pieces.append(body)
        except Exception:
            pieces.append(body)

    combined = "".join(pieces).strip()
    return html.unescape(combined or text)


def _discover_remote_calendar_url(source: AthleticNetXCSource, helper_html: str) -> str | None:
    decoded = html.unescape(helper_html)
    match = re.search(
        r"""https://www\.athletic\.net/api/1/RemoteHTML\.ashx\?[^"'<> ]*Report=XCCalendar1[^"'<> ]*""",
        decoded,
        re.I,
    )
    if not match:
        match = re.search(
            r"""(?:src\s*=\s*["'])([^"']*RemoteHTML\.ashx\?[^"']*Report=XCCalendar1[^"']*)""",
            decoded,
            re.I,
        )
        if match:
            return urljoin(source.embed_help_url, html.unescape(match.group(1)))
        return None
    return html.unescape(match.group(0))


def _date_matches(text: str, season: int) -> list[tuple[date, tuple[int, int]]]:
    matches: list[tuple[date, tuple[int, int]]] = []

    for match in _MONTH_RE.finditer(text):
        month_key = re.sub(r"[^a-z]", "", match.group("month").lower())
        month = _MONTHS.get(month_key)
        if not month:
            continue
        year = int(match.group("year")) if match.group("year") else season
        try:
            parsed = date(year, month, int(match.group("day")))
        except ValueError:
            continue
        matches.append((parsed, match.span()))

    for match in _NUMERIC_DATE_RE.finditer(text):
        year_raw = match.group("year")
        year = season if not year_raw else int(year_raw)
        if year < 100:
            year += 2000
        try:
            parsed = date(year, int(match.group("month")), int(match.group("day")))
        except ValueError:
            continue
        matches.append((parsed, match.span()))

    matches.sort(key=lambda item: item[1][0])
    return matches


def _meet_link(links: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    for href, label in links:
        href_lower = href.lower()
        if (
            "/crosscountry/meet/" in href_lower
            or "/crosscountry/results/meet" in href_lower
            or ("meet=" in href_lower and "crosscountry" in href_lower)
        ):
            clean_label = re.sub(r"\s+", " ", html.unescape(label)).strip()
            return clean_label or None, urljoin("https://www.athletic.net/", href)
    return None, None


def _clean_fallback_title(text: str, spans: list[tuple[int, int]]) -> str:
    if spans:
        start = spans[-1][1]
        text = text[start:]
    text = re.sub(
        r"\b(?:View Results|View Meet|Meet Info|Results|Simulate|Print Calendar|Download Calendar)\b",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"^[\s,;:|/\\\-–—]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_start_time(text: str) -> str | None:
    match = _TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    ap = match.group(3).lower()
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    if hour == 12:
        hour = 0
    if ap == "p":
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def _slug(source_id: str, event_date: date, title: str) -> str:
    digest = hashlib.sha1(
        f"{source_id}|{event_date.isoformat()}|{title.casefold()}".encode("utf-8")
    ).hexdigest()[:14]
    return f"athleticnet-xc-{source_id}-{digest}"


def _parse_calendar_html(
    page_html: str,
    *,
    source: AthleticNetXCSource,
) -> list[dict]:
    parser = _CalendarHTMLParser()
    parser.feed(page_html)
    parser.close()

    candidates = parser.blocks + parser.rows
    events: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for item in candidates:
        text = item["text"]
        date_info = _date_matches(text, source.season)
        if not date_info:
            continue

        meet_title, meet_url = _meet_link(item["links"])
        if not meet_title:
            meet_title = _clean_fallback_title(
                text,
                [span for _parsed, span in date_info],
            )

        meet_title = re.sub(r"\s+", " ", html.unescape(meet_title or "")).strip(" -–—:|")
        if not meet_title:
            continue
        if len(meet_title) > 180:
            continue
        if re.fullmatch(r"\d{4}", meet_title):
            continue

        start_date = date_info[0][0]
        end_date = date_info[1][0] if len(date_info) > 1 else None
        if end_date and (end_date < start_date or (end_date - start_date).days > 7):
            end_date = None

        event_title = f"{source.title_prefix} — {meet_title}"
        key = (start_date.isoformat(), event_title.casefold())
        if key in seen:
            continue
        seen.add(key)

        record = {
            "id": _slug(source.id, start_date, event_title),
            "title": event_title,
            "date": start_date.isoformat(),
            "schools": list(source.schools),
            "scope": "school",
            "category": "sport",
            "source": source.source,
            "sourceUrl": meet_url or source.team_url,
        }

        start_time = _parse_start_time(text)
        if start_time:
            record["start"] = start_time
        else:
            record["allDay"] = True

        if end_date and end_date != start_date:
            record["endDate"] = end_date.isoformat()

        events.append(record)

    return sorted(
        events,
        key=lambda event: (
            event.get("date", ""),
            event.get("start", "23:59"),
            event.get("title", ""),
        ),
    )


def fetch_athleticnet_xc(
    source: AthleticNetXCSource,
    *,
    reference: date | None = None,
) -> list[dict]:
    """Fetch one Athletic.net XC calendar.

    Primary source is the old printable calendar URL supplied by the user.
    If Athletic.net stops rendering that endpoint, fall back to the site's
    documented embeddable XCCalendar1 report, then to the team page.
    """
    attempts: list[str] = []

    try:
        print_html = _fetch_text(source.print_url)
        events = _parse_calendar_html(print_html, source=source)
        if events:
            print(
                f"{source.id} detail: parsed {len(events)} Athletic.net XC event(s) "
                "from printable calendar"
            )
            return events
        attempts.append("print calendar returned no parseable events")
    except Exception as exc:
        attempts.append(f"print calendar: {type(exc).__name__}: {exc}")

    try:
        helper_html = _fetch_text(source.embed_help_url)
        remote_url = _discover_remote_calendar_url(source, helper_html)
        if remote_url:
            remote_text = _fetch_text(remote_url)
            remote_html = _decode_remote_payload(remote_text)
            events = _parse_calendar_html(remote_html, source=source)
            if events:
                print(
                    f"{source.id} detail: parsed {len(events)} Athletic.net XC event(s) "
                    "from XCCalendar1 embed feed"
                )
                return events
            attempts.append("XCCalendar1 returned no parseable events")
        else:
            attempts.append("embed helper exposed no XCCalendar1 URL")
    except Exception as exc:
        attempts.append(f"embed calendar: {type(exc).__name__}: {exc}")

    try:
        team_html = _fetch_text(source.team_url)
        events = _parse_calendar_html(team_html, source=source)
        if events:
            print(
                f"{source.id} detail: parsed {len(events)} Athletic.net XC event(s) "
                "from team page fallback"
            )
            return events
        attempts.append("team page returned no parseable events")
    except Exception as exc:
        attempts.append(f"team page: {type(exc).__name__}: {exc}")

    raise RuntimeError("; ".join(attempts[:3]) or "Athletic.net XC calendar refresh failed")
