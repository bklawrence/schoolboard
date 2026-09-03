from __future__ import annotations

import hashlib
import re
import time
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")

CALENDAR_URL = "https://www.stmatt.net/school-news/calendar"
HOME_URL = "https://www.stmatt.net/school/"
SOURCE_NAME = "St. Matthew School Calendar"
SCHOOL_ID = "stmatthew"

SPORT_WORDS = (
    "baseball", "basketball", "cross country", "golf", "softball",
    "track", "volleyball", "soccer", "wrestling", "cheer",
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


class _SrcParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() not in {"iframe", "a"}:
            return
        for key, value in attrs:
            if key.casefold() in {"src", "href"} and value:
                self.urls.append(value)


def _request_text(url: str, *, timeout: int = 30, opener=urlopen, accept: str = "*/*") -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "ChambanaSchoolboard/1.0 (+public school calendar aggregator)",
            "Accept": accept,
        },
    )
    with opener(request, timeout=timeout) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return body.decode(charset, errors="replace")


def _candidate_urls_from_html(html: str, base_url: str) -> list[tuple[str, str]]:
    parser = _SrcParser()
    parser.feed(html)

    found: list[tuple[str, str]] = []
    for raw in parser.urls:
        full = urljoin(base_url, unescape(raw))
        low = full.casefold()
        if (
            "calendar.google.com" in low
            or low.endswith(".ics")
            or "ical" in low
            or low.startswith("webcal:")
        ):
            found.append((full, ""))

    # Catch URLs serialized in script/config data.
    decoded = unescape(html).replace("\\/", "/")
    for match in re.findall(r'https?://[^"\'<>\s]+', decoded, flags=re.I):
        low = match.casefold()
        if "calendar.google.com" in low or ".ics" in low:
            found.append((match.rstrip("\\,);"), ""))

    unique: list[tuple[str, str]] = []
    seen = set()
    for url, context in found:
        if url not in seen:
            seen.add(url)
            unique.append((url, context))
    return unique


def _rendered_candidates(
    url: str,
    *,
    wait_seconds: float = 2.5,
    page_load_timeout: int = 15,
) -> list[tuple[str, str]]:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:
        raise RuntimeError("Selenium is required for St. Matthew rendered calendar discovery") from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1500,1400")
    options.add_argument("--lang=en-US")
    options.page_load_strategy = "eager"

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(page_load_timeout)
    try:
        try:
            driver.get(url)
        except TimeoutException:
            # St. Matthew's site sometimes leaves subresources hanging.
            # The calendar iframes are often already present in the DOM, so
            # stop further loading and inspect what arrived instead of
            # allowing ChromeDriver to sit until its ~120 s transport timeout.
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        time.sleep(wait_seconds)
        records = driver.execute_script(
            """
            const frames = Array.from(document.querySelectorAll('iframe'));
            return frames.map((f, i) => {
              let node = f;
              let context = '';
              for (let depth = 0; depth < 4 && node; depth++, node = node.parentElement) {
                const txt = (node.innerText || '').trim();
                if (txt.length > context.length && txt.length < 1200) context = txt;
              }
              return {
                src: f.src || f.getAttribute('src') || '',
                title: f.title || '',
                context
              };
            });
            """
        ) or []

        found: list[tuple[str, str]] = []
        for record in records:
            src = str(record.get("src") or "")
            context = " ".join([
                str(record.get("title") or ""),
                str(record.get("context") or ""),
            ]).strip()
            low = src.casefold()
            if "calendar.google.com" in low or ".ics" in low or "ical" in low:
                found.append((src, context))

        if not found:
            found.extend(_candidate_urls_from_html(driver.page_source, driver.current_url))
        return found
    finally:
        driver.quit()


def _google_ics_urls(embed_url: str) -> list[str]:
    parsed = urlparse(embed_url)
    qs = parse_qs(parsed.query)
    ids: list[str] = []

    for key in ("src", "cid"):
        for value in qs.get(key, []):
            decoded = unquote(value).strip()
            if decoded and decoded not in ids:
                ids.append(decoded)

    urls = []
    for calendar_id in ids:
        urls.append(
            "https://calendar.google.com/calendar/ical/"
            + quote(calendar_id, safe="")
            + "/public/basic.ics"
        )
    return urls


def _feed_candidates(candidates: list[tuple[str, str]]) -> list[tuple[str, str]]:
    feeds: list[tuple[str, str]] = []
    seen = set()

    # Exclude parish-only embeds; retain school and athletics candidates.
    useful = []
    for url, context in candidates:
        low_ctx = context.casefold()
        if "parish calendar" in low_ctx and "school" not in low_ctx and "athletic" not in low_ctx:
            continue
        useful.append((url, context))

    # If the page gave no useful context, use at most the first two calendar
    # embeds; St. Matthew's calendar page presents School/Athletics before Parish.
    if useful and not any(context.strip() for _, context in useful):
        useful = useful[:2]

    for url, context in useful:
        low = url.casefold()
        if "calendar.google.com" in low:
            for ics_url in _google_ics_urls(url):
                key = (ics_url, context)
                if key not in seen:
                    seen.add(key)
                    feeds.append(key)
        elif low.startswith("webcal:"):
            ics_url = "https:" + url[len("webcal:"):]
            key = (ics_url, context)
            if key not in seen:
                seen.add(key)
                feeds.append(key)
        elif ".ics" in low or "ical" in low:
            key = (url, context)
            if key not in seen:
                seen.add(key)
                feeds.append(key)

    return feeds


def _unfold_ical(text: str) -> list[str]:
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for line in raw:
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _unescape_ical(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _split_property(line: str):
    if ":" not in line:
        return None
    left, value = line.split(":", 1)
    bits = left.split(";")
    name = bits[0].upper()
    params: dict[str, str] = {}
    for bit in bits[1:]:
        if "=" in bit:
            key, val = bit.split("=", 1)
            params[key.upper()] = val.strip('"')
    return name, params, _unescape_ical(value)


def _parse_dt(value: str, params: dict[str, str]) -> date | datetime:
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date()

    if value.endswith("Z"):
        dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(CHICAGO)

    fmt = "%Y%m%dT%H%M%S" if len(value) >= 15 else "%Y%m%dT%H%M"
    raw = value[:15] if fmt.endswith("%S") else value[:13]
    dt = datetime.strptime(raw, fmt)

    tzid = params.get("TZID")
    try:
        tz = ZoneInfo(tzid) if tzid else CHICAGO
    except Exception:
        tz = CHICAGO
    return dt.replace(tzinfo=tz).astimezone(CHICAGO)


def _category(title: str, context: str = "") -> str:
    low = f"{title} {context}".casefold()

    if "athletic" in low or any(word in low for word in SPORT_WORDS) or " vs " in low or " @ " in low:
        return "sport"
    if any(token in low for token in (
        "no school", "early dismissal", "dismissal", "school resumes",
        "first day", "last day", "conference", "spring break", "winter break",
    )):
        return "schedule"
    return "general"


def _normalize_ics_event(raw: dict, *, context: str, source_url: str) -> dict | None:
    def val(name: str) -> str:
        return raw.get(name, ({}, ""))[1]

    start_prop = raw.get("DTSTART")
    if not start_prop:
        return None

    start_obj = _parse_dt(start_prop[1], start_prop[0])
    end_obj = None
    if raw.get("DTEND"):
        end_obj = _parse_dt(raw["DTEND"][1], raw["DTEND"][0])

    title = val("SUMMARY").strip() or "School event"
    uid = val("UID") or f"{title}|{val('DTSTART')}|{val('LOCATION')}"
    digest = hashlib.sha1(uid.encode("utf-8")).hexdigest()[:18]

    event_day = start_obj.date() if isinstance(start_obj, datetime) else start_obj
    event: dict[str, Any] = {
        "id": f"stmatthew-{digest}",
        "title": title,
        "date": event_day.isoformat(),
        "schools": [SCHOOL_ID],
        "scope": "school",
        "category": _category(title, context),
        "source": SOURCE_NAME,
        "sourceUrl": CALENDAR_URL,
    }

    if isinstance(start_obj, datetime):
        event["start"] = start_obj.strftime("%H:%M")
        if isinstance(end_obj, datetime) and end_obj.date() == start_obj.date():
            event["end"] = end_obj.strftime("%H:%M")
    else:
        event["allDay"] = True
        if isinstance(end_obj, date) and not isinstance(end_obj, datetime):
            inclusive_end = end_obj - timedelta(days=1)
            if inclusive_end > start_obj:
                event["endDate"] = inclusive_end.isoformat()
                event["weekdaysOnly"] = False

    location = val("LOCATION").strip()
    if location:
        event["location"] = location

    return event


def _parse_ics(text: str, *, context: str, source_url: str) -> list[dict]:
    events: list[dict] = []
    current = None

    for line in _unfold_ical(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current:
                event = _normalize_ics_event(current, context=context, source_url=source_url)
                if event:
                    events.append(event)
            current = None
            continue
        if current is None:
            continue

        prop = _split_property(line)
        if not prop:
            continue
        name, params, value = prop
        if name not in current:
            current[name] = (params, value)

    return events


class _VisibleTextParser(HTMLParser):
    _BREAK_TAGS = {
        "article", "aside", "br", "dd", "div", "dt", "footer", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "li", "main", "nav", "p",
        "section", "td", "th", "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        low = tag.casefold()
        if low in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if not self._skip_depth and low in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        low = tag.casefold()
        if low in {"script", "style", "noscript", "svg"}:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if not self._skip_depth and low in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        clean = re.sub(r"[ \t\f\v]+", " ", data)
        if clean.strip():
            self.parts.append(clean)

    def text(self) -> str:
        joined = "".join(self.parts)
        lines = [re.sub(r"\s+", " ", line).strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line)


def _home_text_from_html(
    *,
    timeout: int,
    opener=urlopen,
) -> str:
    html = _request_text(
        HOME_URL,
        timeout=timeout,
        opener=opener,
        accept="text/html,application/xhtml+xml",
    )
    parser = _VisibleTextParser()
    parser.feed(html)
    return parser.text()


def _infer_year(month: int, day: int, reference: date) -> int:
    candidate = date(reference.year, month, day)
    if candidate < reference - timedelta(days=120):
        return reference.year + 1
    if candidate > reference + timedelta(days=300):
        return reference.year - 1
    return reference.year


def _parse_upcoming_text(text: str, *, reference: date) -> list[dict]:
    marker = re.search(r"\bUpcoming Events\b", text, flags=re.I)
    if not marker:
        return []

    chunk = text[marker.end():]
    end_marker = re.search(r"\bView Full Calendar\b", chunk, flags=re.I)
    if end_marker:
        chunk = chunk[:end_marker.start()]

    lines = [re.sub(r"\s+", " ", line).strip() for line in chunk.splitlines()]
    lines = [line for line in lines if line]

    date_re = re.compile(
        r"^(\d{1,2})\s+"
        r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)$",
        re.I,
    )
    range_re = re.compile(r"^\d{1,2}\s+[A-Za-z]{3,9}\s*[-–]\s*\d{1,2}\s+[A-Za-z]{3,9}$")
    time_re = re.compile(
        r"\s+(\d{1,2}:\d{2}\s*[AP]M)\s+to\s+(\d{1,2}:\d{2}\s*[AP]M)$",
        re.I,
    )

    events: list[dict] = []
    current_day: date | None = None

    for line in lines:
        dm = date_re.match(line)
        if dm:
            month = MONTHS[dm.group(2).casefold()]
            day_num = int(dm.group(1))
            current_day = date(_infer_year(month, day_num, reference), month, day_num)
            continue
        if range_re.match(line) or current_day is None:
            continue

        all_day = False
        start = end = None
        title = line

        if re.search(r"\s+All Day$", title, flags=re.I):
            title = re.sub(r"\s+All Day$", "", title, flags=re.I).strip()
            all_day = True
        else:
            tm = time_re.search(title)
            if tm:
                title = title[:tm.start()].strip()
                start = datetime.strptime(tm.group(1).upper(), "%I:%M %p").strftime("%H:%M")
                end = datetime.strptime(tm.group(2).upper(), "%I:%M %p").strftime("%H:%M")
                if start == "00:00" and end == "00:00":
                    start = end = None
                    all_day = True

        if not title or title in {"‹", "›"}:
            continue

        digest = hashlib.sha1(
            f"{current_day.isoformat()}|{start or ''}|{title}".encode("utf-8")
        ).hexdigest()[:18]
        event = {
            "id": f"stmatthew-upcoming-{digest}",
            "title": title,
            "date": current_day.isoformat(),
            "schools": [SCHOOL_ID],
            "scope": "school",
            "category": _category(title),
            "source": SOURCE_NAME,
            "sourceUrl": HOME_URL,
        }
        if start:
            event["start"] = start
            event["end"] = end
        else:
            event["allDay"] = True
        events.append(event)

    return events


def fetch_stmatthew_calendar(
    *,
    timeout: int = 12,
    opener=urlopen,
    reference: date | None = None,
) -> list[dict]:
    reference = reference or datetime.now(CHICAGO).date()

    candidates: list[tuple[str, str]] = []
    try:
        html = _request_text(
            CALENDAR_URL,
            timeout=timeout,
            opener=opener,
            accept="text/html,application/xhtml+xml",
        )
        candidates.extend(_candidate_urls_from_html(html, CALENDAR_URL))
    except Exception:
        pass

    if not candidates:
        try:
            candidates.extend(
                _rendered_candidates(
                    CALENDAR_URL,
                    page_load_timeout=min(15, max(8, timeout + 3)),
                )
            )
        except Exception as exc:
            print(
                "stmatthew-calendar detail: rendered calendar discovery failed fast; "
                f"{type(exc).__name__}: {exc}"
            )

    feeds = _feed_candidates(candidates)
    all_events: list[dict] = []
    feed_successes = 0

    for feed_url, context in feeds:
        try:
            text = _request_text(
                feed_url,
                timeout=timeout,
                opener=opener,
                accept="text/calendar,text/plain;q=0.9,*/*;q=0.8",
            )
            if "BEGIN:VCALENDAR" not in text.upper():
                continue
            events = _parse_ics(text, context=context, source_url=feed_url)
            all_events.extend(events)
            feed_successes += 1
        except Exception:
            continue

    if feed_successes:
        by_id = {event["id"]: event for event in all_events}
        events = sorted(
            by_id.values(),
            key=lambda e: (e.get("date", ""), e.get("start", ""), e.get("title", "")),
        )
        print(
            f"stmatthew-calendar detail: discovered {feed_successes} public calendar "
            f"feed(s); parsed {len(events)} raw events"
        )
        return events

    # Conservative fallback: use the school's server-rendered Upcoming Events
    # HTML without launching a second browser. If the site is unavailable,
    # fail quickly so build_data.py can preserve the previous good cache.
    try:
        visible_text = _home_text_from_html(
            timeout=timeout,
            opener=opener,
        )
    except Exception as exc:
        raise RuntimeError(
            "St. Matthew calendar discovery produced no usable feed and "
            f"homepage fallback failed quickly: {type(exc).__name__}: {exc}"
        ) from exc

    events = _parse_upcoming_text(visible_text, reference=reference)
    if not events:
        raise RuntimeError(
            "St. Matthew calendar page exposed no usable public ICS/Google feed "
            "and the server-rendered school homepage contained no parseable "
            "Upcoming Events"
        )

    print(
        f"stmatthew-calendar detail: no usable calendar feed found; "
        f"used server-rendered Upcoming Events fallback ({len(events)} events)"
    )
    return events
