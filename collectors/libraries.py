from __future__ import annotations

import concurrent.futures
import html
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class LibraryConfig:
    key: str
    name: str
    source: str
    log_id: str
    base_url: str
    early_id: str
    elementary_id: str
    teens_id: str
    discovery_ages: tuple[str, ...]


LIBRARIES: tuple[LibraryConfig, ...] = (
    LibraryConfig(
        key="urbana",
        name="Urbana Free Library",
        source="Urbana Free Library",
        log_id="urbana-library",
        base_url="https://urbanafreelibrary.libnet.info",
        early_id="urbana-lib-early",
        elementary_id="urbana-lib-elementary",
        teens_id="urbana-lib-teens",
        discovery_ages=(
            "Babies",
            "Toddlers",
            "Pre-Schoolers",
            "Preschoolers",
            "Elementary Students",
            "Middle School Students",
            "High School Students",
            "Families",
        ),
    ),
    LibraryConfig(
        key="champaign",
        name="Champaign Public Library",
        source="Champaign Public Library",
        log_id="champaign-library",
        base_url="https://champaign.libnet.info",
        early_id="champaign-lib-early",
        elementary_id="champaign-lib-elementary",
        teens_id="champaign-lib-teens",
        discovery_ages=(
            "Baby",
            "Babies",
            "Preschool",
            "School age",
            "School-age",
            "Teen",
            "Teens",
            "All ages",
            "Families",
        ),
    ),
)


USER_AGENT = (
    "Mozilla/5.0 (compatible; ChambanaSchoolBoard/1.0; "
    "+https://www.chambanaschoolboard.com/)"
)
TIMEOUT_SECONDS = 18
MAX_WORKERS = 8

_EVENT_LINK_RE = re.compile(
    r'''href=["']([^"']*/event/\d+(?:\?[^"']*)?)["']''',
    re.IGNORECASE,
)
_EVENT_ID_RE = re.compile(r"/event/(\d+)", re.IGNORECASE)
_ISO_DT_RE = re.compile(
    r"(?<!\d)(20\d{2}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2})?"
)
_LDJSON_RE = re.compile(
    r'''<script\b[^>]*type=["']application/ld\+json["'][^>]*>(.*?)</script>''',
    re.IGNORECASE | re.DOTALL,
)


class _VisibleTextParser(HTMLParser):
    BLOCK_TAGS = {
        "p", "div", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "li", "br",
        "dt", "dd", "tr", "td", "th",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if not self._skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if not self._skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = html.unescape("".join(self.parts))
        lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _fetch_text(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Cache-Control": "no-cache",
        },
    )
    with urlopen(req, timeout=TIMEOUT_SECONDS) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _visible_text(page_html: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(page_html)
    return parser.text()


def _event_links_from_listing(page_html: str, base_url: str) -> set[str]:
    links: set[str] = set()
    for href in _EVENT_LINK_RE.findall(page_html):
        full = urljoin(base_url.rstrip("/") + "/", html.unescape(href))
        parsed = urlparse(full)
        canonical = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if _EVENT_ID_RE.search(canonical):
            links.add(canonical)
    return links


def _listing_url(
    library: LibraryConfig,
    start_date: date,
    end_date: date,
    *,
    age: str | None = None,
) -> str:
    url = (
        f"{library.base_url.rstrip('/')}/events"
        f"?start={start_date.isoformat()}&end={end_date.isoformat()}&v=list"
    )
    if age:
        url += f"&a={quote(age)}"
    return url


def _discover_event_links(
    library: LibraryConfig,
    start_date: date,
    end_date: date,
) -> set[str]:
    links: set[str] = set()
    successful_filters = 0

    for age in library.discovery_ages:
        try:
            page = _fetch_text(
                _listing_url(library, start_date, end_date, age=age)
            )
        except Exception:
            continue
        successful_filters += 1
        links.update(_event_links_from_listing(page, library.base_url))

    if not links:
        page = _fetch_text(_listing_url(library, start_date, end_date))
        links.update(_event_links_from_listing(page, library.base_url))

    if not links:
        raise RuntimeError(
            f"{library.name} Communico listings returned zero event links"
        )

    print(
        f"{library.log_id} detail: discovered {len(links)} candidate event "
        f"link(s) from {successful_filters} youth/family Communico filter(s)"
    )
    return links


def _jsonld_event(page_html: str) -> dict[str, Any] | None:
    def walk(value: Any):
        if isinstance(value, dict):
            typ = value.get("@type")
            if typ == "Event" or (isinstance(typ, list) and "Event" in typ):
                yield value
            graph = value.get("@graph")
            if graph is not None:
                yield from walk(graph)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    for block in _LDJSON_RE.findall(page_html):
        try:
            value = json.loads(html.unescape(block).strip())
        except Exception:
            continue
        for event in walk(value):
            return event
    return None


def _plain_heading(page_html: str) -> str:
    for tag in ("h1", "h2", "h3"):
        for match in re.finditer(
            rf"<{tag}\b[^>]*>(.*?)</{tag}>",
            page_html,
            re.IGNORECASE | re.DOTALL,
        ):
            text = re.sub(r"<[^>]+>", " ", match.group(1))
            text = re.sub(r"\s+", " ", html.unescape(text)).strip()
            if not text:
                continue
            lowered = text.lower()
            if lowered in {"events", "event", "add to calendar"}:
                continue
            if re.search(
                r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
                lowered,
            ):
                continue
            return text
    return ""


def _title_from_page(page_html: str, jsonld: dict[str, Any] | None) -> str:
    if jsonld:
        name = str(jsonld.get("name") or "").strip()
        if name:
            return html.unescape(name)

    for pattern in (
        r'''<meta\b[^>]*property=["']og:title["'][^>]*content=["']([^"']+)["']''',
        r'''<meta\b[^>]*content=["']([^"']+)["'][^>]*property=["']og:title["']''',
    ):
        match = re.search(pattern, page_html, re.IGNORECASE)
        if match:
            title = html.unescape(match.group(1)).strip()
            title = re.sub(
                r"\s*[-|]\s*(?:Urbana Free Library|Champaign Public Library)\s*$",
                "",
                title,
                flags=re.IGNORECASE,
            ).strip()
            if title:
                return title

    return _plain_heading(page_html)


def _datetime_parts(
    page_html: str,
    jsonld: dict[str, Any] | None,
) -> tuple[str, str | None, str | None]:
    if jsonld:
        start = str(jsonld.get("startDate") or "").strip()
        end = str(jsonld.get("endDate") or "").strip()
        sm = re.search(
            r"(20\d{2}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?",
            start,
        )
        em = re.search(
            r"(20\d{2}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?",
            end,
        )
        if sm:
            event_date = sm.group(1)
            start_time = sm.group(2)
            end_time = em.group(2) if em and em.group(1) == event_date else None
            return event_date, start_time, end_time

    matches = _ISO_DT_RE.findall(html.unescape(page_html))
    if matches:
        event_date, start_time = matches[0]
        end_time: str | None = None
        for candidate_date, candidate_time in matches[1:]:
            if candidate_date == event_date and candidate_time != start_time:
                end_time = candidate_time
                break
        return event_date, start_time, end_time

    raise RuntimeError("event page had no parseable start date/time")


def _location_from_jsonld(jsonld: dict[str, Any] | None) -> str:
    if not jsonld:
        return ""
    location = jsonld.get("location")
    if isinstance(location, dict):
        name = str(location.get("name") or "").strip()
        if name:
            return html.unescape(name)
    if isinstance(location, str):
        return html.unescape(location).strip()
    return ""


def _location_from_visible_text(text: str) -> str:
    lines = text.splitlines()
    try:
        index = next(
            i for i, line in enumerate(lines)
            if line.strip().lower() == "add to calendar"
        )
    except StopIteration:
        return ""

    candidates: list[str] = []
    for line in lines[index + 1:index + 7]:
        line = line.strip()
        if not line:
            continue
        if re.search(
            r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            line,
            re.IGNORECASE,
        ):
            continue
        if re.fullmatch(
            r"\d{1,2}:\d{2}\s*(?:am|pm)\s*-\s*\d{1,2}:\d{2}\s*(?:am|pm)",
            line,
            re.IGNORECASE,
        ):
            continue
        if line.lower() in {"register", "registration now closed"}:
            continue
        candidates.append(line)

    if not candidates:
        return ""

    venue = candidates[0]
    if len(candidates) >= 2:
        room = candidates[1]
        if (
            len(room) <= 90
            and not re.search(r"[.!?]$", room)
            and room.lower() not in {"add to calendar", "register"}
        ):
            return f"{venue} — {room}"
    return venue


def _section_text(text: str, heading: str, stop_headings: tuple[str, ...]) -> str:
    upper = text.upper()
    start = upper.find(heading.upper())
    if start < 0:
        return ""
    start += len(heading)

    end = len(text)
    for stop in stop_headings:
        idx = upper.find(stop.upper(), start)
        if idx >= 0:
            end = min(end, idx)

    return text[start:end].strip()


def _audience_ids(
    library: LibraryConfig,
    title: str,
    text: str,
) -> list[str]:
    age_section = _section_text(
        text,
        "AGE GROUP:",
        ("EVENT TYPE:", "TAGS:", "ABOUT THE LIBRARY"),
    )
    event_type = _section_text(
        text,
        "EVENT TYPE:",
        ("TAGS:", "ABOUT THE LIBRARY"),
    )

    age_norm = re.sub(r"[^a-z0-9+]+", " ", age_section.lower()).strip()
    type_norm = re.sub(r"[^a-z0-9+]+", " ", event_type.lower()).strip()
    title_norm = title.lower()

    early = any(
        phrase in age_norm
        for phrase in (
            "baby", "babies", "toddler", "toddlers",
            "preschool", "pre school", "pre schoolers",
            "early childhood", "infant", "infants",
        )
    )
    elementary = any(
        phrase in age_norm
        for phrase in (
            "school age", "school aged",
            "elementary", "elementary students",
        )
    )
    teens = any(
        phrase in age_norm
        for phrase in (
            "teen", "teens",
            "middle school", "middle school students",
            "high school", "high school students",
        )
    )

    explicit_youth = early or elementary or teens
    family = (
        "families" in age_norm
        or "family" in age_norm
        or "all ages" in age_norm
    )

    if family and not explicit_youth:
        adult_only_type = (
            ("adult" in type_norm or "senior" in type_norm)
            and "children" not in type_norm
            and "teen" not in type_norm
            and "outreach" not in type_norm
        )
        adult_title = bool(
            re.search(
                r"\b(?:senior|seniors|adult|adults|retirement|medicare)\b",
                title_norm,
            )
        )
        if not adult_only_type and not adult_title:
            early = elementary = teens = True

    if not (early or elementary or teens):
        if re.search(r"\b(?:baby|toddler|preschool|pre-k)\b", title_norm):
            early = True
        elif re.search(r"\bteen(?:s|age|aged)?\b", title_norm):
            teens = True
        elif re.search(
            r"\b(?:elementary|school[- ]age|kids club|kids in|diy kids)\b",
            title_norm,
        ):
            elementary = True

    ids: list[str] = []
    if early:
        ids.append(library.early_id)
    if elementary:
        ids.append(library.elementary_id)
    if teens:
        ids.append(library.teens_id)
    return ids


def _parse_event_page(
    library: LibraryConfig,
    event_url: str,
) -> dict[str, Any] | None:
    page_html = _fetch_text(event_url)
    text = _visible_text(page_html)
    jsonld = _jsonld_event(page_html)

    title = _title_from_page(page_html, jsonld)
    if not title:
        return None

    schools = _audience_ids(library, title, text)
    if not schools:
        return None

    event_date, start_time, end_time = _datetime_parts(page_html, jsonld)
    event_id_match = _EVENT_ID_RE.search(event_url)
    event_id = (
        event_id_match.group(1)
        if event_id_match
        else str(abs(hash(event_url)))
    )

    location = (
        _location_from_jsonld(jsonld)
        or _location_from_visible_text(text)
        or library.name
    )

    event: dict[str, Any] = {
        "id": f"{library.log_id}-{event_id}",
        "title": title,
        "date": event_date,
        "schools": schools,
        "scope": "community",
        "category": "general",
        "source": library.source,
        "sourceUrl": event_url,
    }
    if start_time:
        event["start"] = start_time
    else:
        event["allDay"] = True
    if end_time:
        event["end"] = end_time
    if location:
        event["location"] = location

    return event


def fetch_library_calendar(
    library: LibraryConfig,
    *,
    reference: date | None = None,
) -> list[dict[str, Any]]:
    reference = reference or date.today()
    start_date = reference - timedelta(days=30)
    end_date = reference + timedelta(days=60)

    event_urls = sorted(
        _discover_event_links(library, start_date, end_date)
    )

    parsed: list[dict[str, Any]] = []
    fetch_failures = 0
    non_youth = 0

    def worker(url: str):
        return url, _parse_event_page(library, url)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:
        futures = [executor.submit(worker, url) for url in event_urls]
        for future in concurrent.futures.as_completed(futures):
            try:
                _url, event = future.result()
            except Exception:
                fetch_failures += 1
                continue
            if event is None:
                non_youth += 1
            else:
                parsed.append(event)

    if fetch_failures > max(8, len(event_urls) // 4):
        raise RuntimeError(
            f"{library.name} event detail fetches failed too often: "
            f"{fetch_failures}/{len(event_urls)}"
        )

    if not parsed:
        raise RuntimeError(
            f"{library.name} event pages produced zero youth events"
        )

    unique: dict[str, dict[str, Any]] = {}
    for event in parsed:
        unique[event["id"]] = event

    events = sorted(
        unique.values(),
        key=lambda event: (
            event.get("date", ""),
            event.get("start", ""),
            event.get("title", ""),
        ),
    )

    early_count = sum(
        library.early_id in event.get("schools", [])
        for event in events
    )
    elementary_count = sum(
        library.elementary_id in event.get("schools", [])
        for event in events
    )
    teens_count = sum(
        library.teens_id in event.get("schools", [])
        for event in events
    )

    print(
        f"{library.log_id} detail: event-page audience classification "
        f"early={early_count}, elementary={elementary_count}, "
        f"teens={teens_count}; kept {len(events)} unique youth/family "
        f"event(s); excluded {non_youth} non-youth page(s); "
        f"{fetch_failures} detail fetch failure(s)"
    )

    sample = "; ".join(
        f"{event['date']} {event['title']} "
        f"[{','.join(event.get('schools', []))}]"
        for event in events[:8]
    )
    if sample:
        print(f"{library.log_id} detail: first classified events: {sample}")

    return events
