from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36 "
    "SchoolBoard/1.0"
)
TIMEOUT = 25


@dataclass(frozen=True)
class ParkDistrict:
    key: str
    log_id: str
    source: str
    calendar_url: str
    group_ids: dict[str, str]


PARK_DISTRICTS = (
    ParkDistrict(
        key="champaign",
        log_id="champaign-parks",
        source="Champaign Park District Free Youth & Family Events",
        calendar_url="https://champaignparks.org/events/",
        group_ids={
            "early": "champaign-parks-early",
            "elementary": "champaign-parks-elementary",
            "teens": "champaign-parks-teens",
            "family": "champaign-parks-family",
        },
    ),
    ParkDistrict(
        key="urbana",
        log_id="urbana-parks",
        source="Urbana Park District Free Youth & Family Events",
        calendar_url="https://www.urbanaparks.org/calendar/",
        group_ids={
            "early": "urbana-parks-early",
            "elementary": "urbana-parks-elementary",
            "teens": "urbana-parks-teens",
            "family": "urbana-parks-family",
        },
    ),
)


def _fetch(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


class _PageParser(HTMLParser):
    """Small dependency-free HTML extractor for event pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._text: list[str] = []
        self._heading_level: str | None = None
        self._heading_text: list[str] = []
        self.headings: list[tuple[str, str]] = []
        self._script_type: str | None = None
        self._script_text: list[str] = []
        self.jsonld: list[object] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag == "a":
            self._href = attrs_dict.get("href") or None
            self._link_text = []
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading_level = tag
            self._heading_text = []
        elif tag == "script":
            self._script_type = attrs_dict.get("type", "").casefold()
            self._script_text = []

        if tag in {"br", "p", "div", "li", "section", "article", "tr"}:
            self._text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            text = _clean(" ".join(self._link_text))
            self.links.append((self._href, text))
            self._href = None
            self._link_text = []
        elif self._heading_level == tag:
            heading = _clean(" ".join(self._heading_text))
            if heading:
                self.headings.append((tag, heading))
            self._heading_level = None
            self._heading_text = []
        elif tag == "script":
            if self._script_type == "application/ld+json":
                blob = "".join(self._script_text).strip()
                if blob:
                    try:
                        self.jsonld.append(json.loads(blob))
                    except json.JSONDecodeError:
                        pass
            self._script_type = None
            self._script_text = []

        if tag in {"p", "div", "li", "section", "article", "tr"}:
            self._text.append("\n")

    def handle_data(self, data: str) -> None:
        if self._script_type is not None:
            self._script_text.append(data)
            return
        if not data:
            return
        self._text.append(data)
        if self._href:
            self._link_text.append(data)
        if self._heading_level:
            self._heading_text.append(data)

    @property
    def text(self) -> str:
        lines = [_clean(part) for part in "".join(self._text).splitlines()]
        return "\n".join(line for line in lines if line)


def _parse_page(page_html: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(page_html)
    return parser


def _clean(value: str) -> str:
    value = html.unescape(str(value or ""))
    return re.sub(r"\s+", " ", value).strip()


def _absolute(base: str, href: str) -> str:
    return urljoin(base, html.unescape(href or ""))


def _month_urls(base: str, reference: date, *, back: int = 1, forward: int = 2) -> list[str]:
    months: list[tuple[int, int]] = []
    for delta in range(-back, forward + 1):
        month_index = reference.year * 12 + (reference.month - 1) + delta
        year, zero_month = divmod(month_index, 12)
        months.append((year, zero_month + 1))

    if "champaignparks.org" in base:
        return [f"https://champaignparks.org/events/{year:04d}-{month:02d}/" for year, month in months]
    return [f"https://www.urbanaparks.org/calendar/{month}/{year}/" for year, month in months]


def _discover_champaign(reference: date) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for month_url in _month_urls("https://champaignparks.org", reference):
        parser = _parse_page(_fetch(month_url))
        for href, anchor_text in parser.links:
            url = _absolute(month_url, href)
            parsed = urlparse(url)
            if parsed.netloc.casefold() not in {"champaignparks.org", "www.champaignparks.org"}:
                continue
            if not parsed.path.startswith("/event/"):
                continue
            # Ignore generic series/category links; real event detail pages have
            # a slug immediately after /event/.
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) < 2:
                continue
            found.setdefault(url.split("#", 1)[0], {"anchor": anchor_text})
    return found


_URBANA_ANCHOR_TIME = re.compile(
    r"^(?P<start>\d{1,2}:\d{2}\s*[AP]M)"
    r"(?:\s*-\s*(?P<end>\d{1,2}:\d{2}\s*[AP]M))?\s*:\s*(?P<title>.+)$",
    re.I,
)


def _discover_urbana(reference: date) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for month_url in _month_urls("https://www.urbanaparks.org", reference):
        parser = _parse_page(_fetch(month_url))
        for href, anchor_text in parser.links:
            url = _absolute(month_url, href)
            parsed = urlparse(url)
            if parsed.netloc.casefold() not in {"urbanaparks.org", "www.urbanaparks.org"}:
                continue
            if not parsed.path.startswith("/calendar/events/"):
                continue
            info: dict[str, str] = {"anchor": anchor_text}
            match = _URBANA_ANCHOR_TIME.match(anchor_text)
            if match:
                info.update({key: _clean(value) for key, value in match.groupdict().items() if value})
            found.setdefault(url.split("#", 1)[0], info)
    return found


def _iter_jsonld_event_nodes(value: object):
    if isinstance(value, dict):
        kind = value.get("@type")
        kinds = {str(item).casefold() for item in kind} if isinstance(kind, list) else {str(kind).casefold()}
        if "event" in kinds:
            yield value
        graph = value.get("@graph")
        if graph is not None:
            yield from _iter_jsonld_event_nodes(graph)
        for key, child in value.items():
            if key != "@graph" and isinstance(child, (dict, list)):
                yield from _iter_jsonld_event_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_jsonld_event_nodes(child)


def _first_jsonld_event(parser: _PageParser) -> dict | None:
    for blob in parser.jsonld:
        for node in _iter_jsonld_event_nodes(blob):
            return node
    return None


def _clock(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    # ISO date-time from JSON-LD.
    iso = re.search(r"T(\d{2}):(\d{2})", value)
    if iso:
        return f"{iso.group(1)}:{iso.group(2)}"
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", value, re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3).casefold()
    if meridiem == "p" and hour != 12:
        hour += 12
    elif meridiem == "a" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        return match.group(0)
    match = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2,4})\b", value)
    if match:
        month, day, year = map(int, match.groups())
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def _jsonld_location(node: dict) -> str | None:
    location = node.get("location")
    if isinstance(location, list) and location:
        location = location[0]
    if isinstance(location, str):
        return _clean(location)
    if not isinstance(location, dict):
        return None
    name = _clean(location.get("name", ""))
    address = location.get("address")
    if isinstance(address, str):
        address_text = _clean(address)
    elif isinstance(address, dict):
        pieces = [
            address.get("streetAddress"),
            address.get("addressLocality"),
            address.get("addressRegion"),
        ]
        address_text = ", ".join(_clean(piece) for piece in pieces if _clean(piece or ""))
    else:
        address_text = ""
    if name and address_text and address_text.casefold() not in name.casefold():
        return f"{name}, {address_text}"
    return name or address_text or None


def _jsonld_is_free(node: dict) -> bool | None:
    offers = node.get("offers")
    if offers is None:
        return None
    if not isinstance(offers, list):
        offers = [offers]
    prices: list[float] = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        for key in ("price", "lowPrice", "highPrice"):
            raw = offer.get(key)
            if raw is None or raw == "":
                continue
            try:
                prices.append(float(str(raw).replace("$", "").replace(",", "")))
            except ValueError:
                pass
    if not prices:
        return None
    return max(prices) == 0


def _page_is_free(title: str, text: str, parser: _PageParser, node: dict | None) -> bool:
    if node:
        jsonld_free = _jsonld_is_free(node)
        if jsonld_free is not None:
            return jsonld_free

    title_key = title.casefold()
    if re.search(r"\bfree!?\b", title_key):
        return True

    # Urbana's event pages use clear phrases in the event body. Do not treat
    # incidental phrases like "free parking" or "free pizza" as admission.
    strong_free = (
        r"\bfree for all ages\b",
        r"\beverything is free\b",
        r"\bthis is a free event\b",
        r"\bthe event is free\b",
        r"\badmission is free\b",
        r"\bfree and all ages\b",
        r"\bfree family\b",
        r"\bfree teen\b",
        r"\bfree toddler\b",
        r"\bfree kids?\b",
    )
    lowered = text.casefold()
    if any(re.search(pattern, lowered) for pattern in strong_free):
        return True

    # Champaign event detail pages often print a standalone "Free" immediately
    # after the schedule. Newline boundaries make this safer than a global word.
    if re.search(r"(?:^|\n)free(?:\n|$)", text, re.I):
        return True
    return False


_AGE_RANGE_PATTERNS = (
    re.compile(r"\bages?\s*:?\s*(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\b", re.I),
    re.compile(r"\bage\s*:?\s*(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\b", re.I),
)
_GRADE_RANGE = re.compile(r"\bgrades?\s*(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\b", re.I)


def _add_range_buckets(buckets: set[str], low: int, high: int) -> None:
    if high < low:
        low, high = high, low
    # Early childhood is roughly birth through 5; elementary roughly 5-12;
    # teens roughly 12-18. Boundary ages may intentionally land in two groups.
    if low <= 5 and high >= 0:
        buckets.add("early")
    if low <= 12 and high >= 5:
        buckets.add("elementary")
    if low <= 18 and high >= 12:
        buckets.add("teens")


def _audience_buckets(title: str, text: str) -> set[str]:
    combined = f"{title}\n{text}"
    lowered = combined.casefold()

    # Explicit adult-only/senior programming is not part of this feed unless
    # the same event also clearly says family/all ages.
    family_signal = bool(re.search(r"\b(all ages|family|families|family-friendly|kids and adults)\b", lowered))
    adult_only = bool(
        re.search(r"\b(18\s*\+|21\s*\+|18 and older|21 and over|adults? only|senior club|for seniors)\b", lowered)
    )
    if adult_only and not family_signal:
        return set()

    buckets: set[str] = set()
    for pattern in _AGE_RANGE_PATTERNS:
        for match in pattern.finditer(combined):
            _add_range_buckets(buckets, int(match.group(1)), int(match.group(2)))

    for match in _GRADE_RANGE.finditer(combined):
        low_grade, high_grade = int(match.group(1)), int(match.group(2))
        if low_grade <= 5:
            buckets.add("elementary")
        if high_grade >= 6:
            buckets.add("teens")

    if re.search(r"\b(baby|babies|toddler|toddlers|preschool|preschooler|pre-k|early childhood)\b", lowered):
        buckets.add("early")
    if re.search(r"\b(elementary|school-aged|school age|children|child|kids|youth)\b", lowered):
        buckets.add("elementary")
    if re.search(r"\b(teens?|teenager|grades? 6-12|middle school|high school)\b", lowered):
        buckets.add("teens")
    if family_signal:
        buckets.add("family")

    # Avoid classifying generic adult/community events merely because their
    # boilerplate mentions children elsewhere on the page.
    if not buckets:
        return set()
    return buckets


_REG_REQUIRED = (
    r"\bregistration (?:is )?required\b",
    r"\bpre-?registration (?:is )?required\b",
    r"\bmust (?:pre-?)?register\b",
    r"\bplease register\b",
    r"\bplease sign up\b",
)
_REG_REQUESTED = (
    r"\bregistration (?:is )?requested\b",
    r"\bregistration (?:is )?recommended\b",
    r"\bpre-?registration preferred\b",
    r"\bsign up so we know\b",
)
_NO_REG = (
    r"\bno registration (?:is )?required\b",
    r"\bno advance registration\b",
    r"\byou don['’]?t need to register\b",
    r"\bwalk-?ins? welcome\b",
    r"\bdrop-?ins? welcome\b",
)


def _registration_status(text: str) -> str:
    lowered = text.casefold()
    if any(re.search(pattern, lowered) for pattern in _NO_REG):
        # A page can say "registration preferred; walk-ins welcome." That is
        # optional rather than required.
        if any(re.search(pattern, lowered) for pattern in _REG_REQUESTED):
            return "requested"
        return "none"
    if any(re.search(pattern, lowered) for pattern in _REG_REQUIRED):
        return "required"
    if any(re.search(pattern, lowered) for pattern in _REG_REQUESTED):
        return "requested"
    return "none"


def _looks_like_registration_link(href: str, label: str) -> bool:
    haystack = f"{href} {label}".casefold()
    return bool(
        re.search(r"\b(register|registration|sign\s*up|signup)\b", haystack)
        or "activecommunities" in haystack
        or "myactivecenter" in haystack
    )


def _registration_url(parser: _PageParser, base_url: str, event_day: str | None) -> str | None:
    candidates: list[tuple[str, str]] = []
    for href, label in parser.links:
        if not _looks_like_registration_link(href, label):
            continue
        absolute = _absolute(base_url, href)
        parsed = urlparse(absolute)
        # Ignore generic site-navigation links such as a top-level
        # "Registration" menu item; we want a link for this event.
        if label.strip().casefold() == "registration" and parsed.path.rstrip("/").endswith("/registration"):
            continue
        candidates.append((absolute, label))
    if not candidates:
        return None

    # Series pages can contain one registration link per month. Prefer the link
    # whose label names this event's month.
    if event_day:
        try:
            event_date = date.fromisoformat(event_day)
            month_name = event_date.strftime("%B").casefold()
            for href, label in candidates:
                if month_name in label.casefold():
                    return href
        except ValueError:
            pass

    # Prefer off-site or purpose-built registration endpoints over links back to
    # the generic event calendar.
    for href, _label in candidates:
        parsed = urlparse(href)
        if parsed.netloc and "champaignparks.org" not in parsed.netloc and "urbanaparks.org" not in parsed.netloc:
            return href
    return candidates[0][0]


def _strip_free_prefix(title: str) -> str:
    title = _clean(title)
    return re.sub(r"^free!?\s*(?:[-–—:]\s*)?", "", title, flags=re.I).strip()


def _event_id(prefix: str, url: str, day: str, title: str) -> str:
    digest = hashlib.sha1(f"{url}|{day}|{title}".encode("utf-8")).hexdigest()[:16]
    return f"parks-{prefix}-{digest}"


def _champaign_event(url: str, district: ParkDistrict) -> dict | None:
    parser = _parse_page(_fetch(url))
    text = parser.text
    node = _first_jsonld_event(parser)

    title = ""
    event_day = None
    start = None
    end = None
    location = None
    if node:
        title = _clean(node.get("name", ""))
        event_day = _iso_date(str(node.get("startDate", "")))
        start = _clock(str(node.get("startDate", "")))
        end = _clock(str(node.get("endDate", "")))
        location = _jsonld_location(node)

    if not title:
        for level, heading in parser.headings:
            if level == "h1":
                title = heading
                break
    if not title:
        return None

    if not event_day:
        # Fallback for pages where JSON-LD is absent.
        date_match = re.search(
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
            r"(\d{1,2})(?:,\s*(\d{4}))?\s*@\s*([^\n]+)",
            text,
            re.I,
        )
        if date_match:
            month_name, day_num, year_text, time_text = date_match.groups()
            year = int(year_text or date.today().year)
            month = {
                name: idx
                for idx, name in enumerate(
                    "January February March April May June July August September October November December".split(),
                    start=1,
                )
            }[month_name.title()]
            try:
                event_day = date(year, month, int(day_num)).isoformat()
            except ValueError:
                return None
            parsed_times = re.findall(r"\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?", time_text, re.I)
            if parsed_times:
                start = _clock(parsed_times[0])
                if len(parsed_times) > 1:
                    end = _clock(parsed_times[1])
    if not event_day:
        return None

    if not _page_is_free(title, text, parser, node):
        return None
    buckets = _audience_buckets(title, text)
    if not buckets:
        return None

    reg_status = _registration_status(text)
    reg_url = _registration_url(parser, url, event_day)
    if reg_url and reg_status == "none":
        reg_status = "requested"

    title = _strip_free_prefix(title)
    event = {
        "id": _event_id(district.key, url, event_day, title),
        "title": title,
        "date": event_day,
        "schools": [district.group_ids[key] for key in ("early", "elementary", "teens", "family") if key in buckets],
        "scope": "community",
        "category": "general",
        "source": district.source,
        "sourceUrl": url,
    }
    if start:
        event["start"] = start
    if end and end != start:
        event["end"] = end
    if location:
        event["location"] = location
    if reg_status != "none":
        event["registrationStatus"] = reg_status
    if reg_url:
        event["registrationUrl"] = reg_url
    return event


def _urbana_event(url: str, hint: dict, district: ParkDistrict) -> dict | None:
    parser = _parse_page(_fetch(url))
    text = parser.text

    title = hint.get("title", "")
    if not title:
        for level, heading in parser.headings:
            if level == "h1":
                title = heading
                break
    title = _clean(title)
    if not title:
        return None

    date_match = re.search(r"(?:^|\n)Date:\s*([^\n]+)", text, re.I)
    event_day = _iso_date(date_match.group(1)) if date_match else None
    if not event_day:
        return None

    time_match = re.search(r"(?:^|\n)Time:\s*([^\n]+)", text, re.I)
    start = _clock(hint.get("start") or (time_match.group(1) if time_match else None))
    end = _clock(hint.get("end"))

    location_match = re.search(r"(?:^|\n)Location:\s*([^\n]+)", text, re.I)
    location = _clean(location_match.group(1)) if location_match else None

    if not _page_is_free(title, text, parser, None):
        return None
    buckets = _audience_buckets(title, text)
    if not buckets:
        return None

    reg_status = _registration_status(text)
    reg_url = _registration_url(parser, url, event_day)
    if reg_url and reg_status == "none":
        reg_status = "requested"

    title = _strip_free_prefix(title)
    event = {
        "id": _event_id(district.key, url, event_day, title),
        "title": title,
        "date": event_day,
        "schools": [district.group_ids[key] for key in ("early", "elementary", "teens", "family") if key in buckets],
        "scope": "community",
        "category": "general",
        "source": district.source,
        "sourceUrl": url,
    }
    if start:
        event["start"] = start
    if end and end != start:
        event["end"] = end
    if location:
        event["location"] = location
    if reg_status != "none":
        event["registrationStatus"] = reg_status
    if reg_url:
        event["registrationUrl"] = reg_url
    return event


def fetch_park_calendar(key: str, *, reference: date | None = None) -> list[dict]:
    """Collect free child/teen/family park-district events.

    Events are tagged with one or more pseudo-school IDs so the existing
    SchoolBoard selection/filter machinery can treat age bands the same way it
    treats library age groups. Paid programs and adult-only programs are
    deliberately excluded.
    """
    district = next((item for item in PARK_DISTRICTS if item.key == key), None)
    if district is None:
        raise KeyError(f"Unknown park district: {key}")

    reference = reference or date.today()
    if key == "champaign":
        discovered = _discover_champaign(reference)
        detail_fn = lambda url, hint: _champaign_event(url, district)
    elif key == "urbana":
        discovered = _discover_urbana(reference)
        detail_fn = lambda url, hint: _urbana_event(url, hint, district)
    else:
        raise KeyError(key)

    events: list[dict] = []
    failures: list[str] = []
    for url, hint in discovered.items():
        try:
            event = detail_fn(url, hint)
        except Exception as exc:
            failures.append(f"{url}: {type(exc).__name__}: {exc}")
            continue
        if event:
            events.append(event)

    # URL/date/title dedupe in case monthly calendars repeat spillover events or
    # a recurring event appears through more than one calendar view.
    unique: dict[tuple[str, str, str], dict] = {}
    for event in events:
        key_tuple = (
            event.get("date", ""),
            event.get("start", ""),
            event.get("title", "").casefold(),
        )
        if key_tuple in unique:
            merged = unique[key_tuple]
            merged["schools"] = sorted(set(merged.get("schools", [])) | set(event.get("schools", [])))
            if event.get("registrationUrl") and not merged.get("registrationUrl"):
                merged["registrationUrl"] = event["registrationUrl"]
                merged["registrationStatus"] = event.get("registrationStatus", "required")
        else:
            unique[key_tuple] = event

    result = sorted(unique.values(), key=lambda e: (e.get("date", ""), e.get("start", ""), e.get("title", "")))

    registration_count = sum(bool(event.get("registrationUrl")) for event in result)
    print(
        f"{district.log_id} detail: discovered {len(discovered)} event page(s); "
        f"kept {len(result)} free youth/family event(s); "
        f"{registration_count} with registration link"
    )
    if result:
        sample = "; ".join(
            f"{event.get('date')} {event.get('title')} [{','.join(event.get('schools', []))}]"
            for event in result[:10]
        )
        print(f"{district.log_id} detail: first events: {sample}")
    if failures:
        print(
            f"{district.log_id} detail: {len(failures)} event detail page(s) failed; "
            + "; ".join(failures[:3])
        )
    return result
