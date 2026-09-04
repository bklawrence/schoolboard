from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen


BASE_URL = "https://www.arbiterlive.com/"

# These are the Arbiter entities supplied for SchoolBoard. Uni High, Urbana
# High, and Urbana Middle are retained for opponent recognition but are not
# fetched here because SchoolBoard already has live Snap! athletics for them.
ARBITER_SOURCES = [
    {
        "id": "arbiter-central",
        "school_id": "central",
        "entity_id": "3871",
        "name": "Champaign Central Athletics — ArbiterLive",
        "url": "https://www.arbiterlive.com/Teams?entityId=3871",
        "fetch": True,
    },
    {
        "id": "arbiter-edison",
        "school_id": "edison",
        "entity_id": "3872",
        "name": "Edison Middle Athletics — ArbiterLive",
        "url": "https://www.arbiterlive.com/Teams?entityId=3872",
        "fetch": True,
    },
    {
        "id": "arbiter-franklin",
        "school_id": "franklin",
        "entity_id": "3873",
        "name": "Franklin Middle Athletics — ArbiterLive",
        "url": "https://www.arbiterlive.com/Teams?entityId=3873",
        "fetch": True,
    },
    {
        "id": "arbiter-holycross",
        "school_id": "holycross",
        "entity_id": "3874",
        "name": "Holy Cross Athletics — ArbiterLive",
        "url": "https://www.arbiterlive.com/Teams?entityId=3874",
        "fetch": True,
    },
    {
        "id": "arbiter-jefferson",
        "school_id": "jefferson",
        "entity_id": "3875",
        "name": "Jefferson Middle Athletics — ArbiterLive",
        "url": "https://www.arbiterlive.com/Teams?entityId=3875",
        "fetch": True,
    },
    {
        "id": "arbiter-nextgen",
        "school_id": "nextgen",
        "entity_id": "15990",
        "name": "Next Generation Athletics — ArbiterLive",
        "url": "https://www.arbiterlive.com/Teams?entityId=15990",
        "fetch": True,
    },
    {
        "id": "arbiter-uni",
        "school_id": "uni",
        "entity_id": "24076",
        "name": "Uni High Athletics — ArbiterLive",
        "url": "https://www.arbiterlive.com/Teams?entityId=24076",
        "fetch": False,
    },
    {
        "id": "arbiter-uhs",
        "school_id": "uhs",
        "entity_id": "24155",
        "name": "Urbana High Athletics — ArbiterLive",
        "url": "https://www.arbiterlive.com/Teams?entityId=24155",
        "fetch": False,
    },
    {
        "id": "arbiter-ums",
        "school_id": "ums",
        "entity_id": "24159",
        "name": "Urbana Middle Athletics — ArbiterLive",
        "url": "https://www.arbiterlive.com/Teams?entityId=24159",
        "fetch": False,
    },
    {
        "id": "arbiter-stmatthew",
        "school_id": "stmatthew",
        "entity_id": "30001",
        "name": "St. Matthew Athletics — ArbiterLive",
        "url": "https://www.arbiterlive.com/Teams?entityId=30001",
        "fetch": True,
    },
    {
        "id": "arbiter-stjohn",
        "school_id": "stjohn",
        "entity_id": "30002",
        "name": "St. John Lutheran Athletics — ArbiterLive",
        "url": "https://www.arbiterlive.com/Teams?entityId=30002",
        "fetch": True,
    },
]

# Judah already has a working Arbiter collector, but it still belongs in the
# opponent dictionary so games discovered through another school's schedule
# can be attached to Judah too.
KNOWN_OPPONENTS = {
    "champaign central high school": "central",
    "champaign central": "central",
    "edison middle school": "edison",
    "champaign edison middle school": "edison",
    "franklin middle school": "franklin",
    "champaign franklin middle school": "franklin",
    "holy cross catholic school": "holycross",
    "holy cross school": "holycross",
    "jefferson middle school": "jefferson",
    "champaign jefferson middle school": "jefferson",
    "next generation": "nextgen",
    "next generation school": "nextgen",
    "champaign next generation": "nextgen",
    "university laboratory high school": "uni",
    "university high school": "uni",
    "uni high": "uni",
    "urbana high school": "uhs",
    "urbana middle school": "ums",
    "st matthew catholic school": "stmatthew",
    "st. matthew catholic school": "stmatthew",
    "st matthew school": "stmatthew",
    "st. matthew school": "stmatthew",
    "st john lutheran school": "stjohn",
    "st. john lutheran school": "stjohn",
    "judah christian school": "judah",
    "judah christian": "judah",
}


@dataclass(frozen=True)
class TeamLink:
    sport: str
    label: str
    href: str


class _TeamIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_sport = ""
        self._in_h3 = False
        self._h3_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []
        self.teams: list[TeamLink] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        attrs_d = dict(attrs)
        if tag == "h3":
            self._in_h3 = True
            self._h3_parts = []
        elif tag == "a":
            href = attrs_d.get("href") or ""
            if re.search(r"/Teams/Schedule/\d+", href, re.I):
                self._anchor_href = href
                self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "h3" and self._in_h3:
            self._in_h3 = False
            sport = re.sub(r"\s+", " ", "".join(self._h3_parts)).strip()
            if sport:
                self.current_sport = sport
        elif tag == "a" and self._anchor_href:
            label = re.sub(r"\s+", " ", "".join(self._anchor_parts)).strip()
            if label:
                self.teams.append(
                    TeamLink(self.current_sport, label, self._anchor_href)
                )
            self._anchor_href = None
            self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_h3:
            self._h3_parts.append(data)
        if self._anchor_href:
            self._anchor_parts.append(data)


class _TableRowsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _fetch_text(url: str, *, timeout: int = 25, opener=urlopen) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "ChambanaSchoolboard/1.0 (public calendar aggregator)",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
    )
    with opener(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _clean(value: str) -> str:
    value = html.unescape(str(value or ""))
    return re.sub(r"\s+", " ", value).strip()


def _normalize_name(value: str) -> str:
    value = _clean(value).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def opponent_school_id(name: str) -> str | None:
    normalized = _normalize_name(name)
    if normalized in KNOWN_OPPONENTS:
        return KNOWN_OPPONENTS[normalized]

    # Arbiter occasionally appends a mascot or grade designation. Use a
    # conservative longest-alias containment check only for reasonably
    # distinctive names.
    matches = [
        (alias, school_id)
        for alias, school_id in KNOWN_OPPONENTS.items()
        if len(alias) >= 12 and alias in normalized
    ]
    if not matches:
        return None
    matches.sort(key=lambda pair: len(pair[0]), reverse=True)
    return matches[0][1]


_DATE_TIME_RE = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(?P<day>\d{1,2})"
    r"(?:\s+(?P<time>\d{1,2}:\d{2}\s*(?:AM|PM)))?",
    re.I,
)


def _resolve_date(month: int, day: int, reference: date) -> date:
    candidates: list[date] = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            pass
    if not candidates:
        raise ValueError(f"Invalid Arbiter date: {month}/{day}")
    return min(candidates, key=lambda candidate: abs((candidate - reference).days))


def _parse_date_time(text: str, reference: date) -> tuple[date, str | None] | None:
    match = _DATE_TIME_RE.match(_clean(text))
    if not match:
        return None
    month = datetime.strptime(match.group("month").title(), "%b").month
    day = int(match.group("day"))
    event_date = _resolve_date(month, day, reference)
    raw_time = match.group("time")
    if not raw_time:
        return event_date, None
    parsed_time = datetime.strptime(
        re.sub(r"\s+", " ", raw_time.upper()).strip(),
        "%I:%M %p",
    ).strftime("%H:%M")
    return event_date, parsed_time


def _team_name(team: TeamLink) -> str:
    sport = _clean(team.sport)
    label = _clean(team.label)
    if sport and sport.casefold() not in label.casefold():
        return f"{label} {sport}".strip()
    return label or sport or "Athletics"


def _schedule_id(href: str) -> str:
    match = re.search(r"/Teams/Schedule/(\d+)", href, re.I)
    return match.group(1) if match else hashlib.sha1(href.encode()).hexdigest()[:12]


def _game_rows(page_html: str) -> list[list[str]]:
    parser = _TableRowsParser()
    parser.feed(page_html)
    return [
        row for row in parser.rows
        if row and _DATE_TIME_RE.match(_clean(row[0]))
    ]


def _row_to_event(
    row: list[str],
    *,
    team: TeamLink,
    cfg: dict,
    schedule_url: str,
    reference: date,
) -> dict | None:
    parsed = _parse_date_time(row[0], reference)
    if not parsed or len(row) < 3:
        return None

    event_date, start_time = parsed
    home_away = _clean(row[1]).casefold()

    # Competition schedule rows identify the opponent with "vs" or "@".
    # This deliberately excludes practice rows, which would otherwise swamp
    # a school-wide feed for large high schools.
    if home_away not in {"vs", "@"}:
        return None

    opponent = _clean(row[2])
    if not opponent:
        return None

    location = _clean(row[3]) if len(row) >= 4 else ""
    status_text = " ".join(_clean(cell) for cell in row[4:])
    canceled = re.search(r"\bcancel(?:ed|led)\b", status_text, re.I) is not None
    postponed = re.search(r"\bpostponed\b", status_text, re.I) is not None

    team_name = _team_name(team)
    connector = "vs" if home_away == "vs" else "@"
    title = f"{team_name} — {connector} {opponent}"
    if canceled:
        title = f"Canceled — {title}"
    elif postponed:
        title = f"Postponed — {title}"

    schools = [cfg["school_id"]]
    opponent_id = opponent_school_id(opponent)
    if opponent_id and opponent_id not in schools:
        schools.append(opponent_id)

    sid = _schedule_id(team.href)
    key = f"{sid}|{event_date.isoformat()}|{start_time or ''}|{opponent}|{home_away}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    event = {
        "id": f"arbiter-{cfg['school_id']}-{digest}",
        "title": title,
        "date": event_date.isoformat(),
        "schools": schools,
        "scope": "school",
        "category": "athletics",
        "source": cfg["name"],
        "sourceUrl": schedule_url,
        "sport": _clean(team.sport),
        "team": team_name,
        "opponent": opponent,
    }
    if start_time:
        event["start"] = start_time
    else:
        event["allDay"] = True
    if location and location.casefold() not in {"tba", "n/a", "none"}:
        event["location"] = location
    if canceled:
        event["detail"] = "Canceled"
    elif postponed:
        event["detail"] = "Postponed"
    return event


def parse_team_index(page_html: str) -> list[TeamLink]:
    parser = _TeamIndexParser()
    parser.feed(page_html)

    seen: set[str] = set()
    teams: list[TeamLink] = []
    for team in parser.teams:
        href = urljoin(BASE_URL, team.href)
        if href in seen:
            continue
        seen.add(href)
        teams.append(TeamLink(team.sport, team.label, href))
    return teams


def parse_team_schedule(
    page_html: str,
    *,
    team: TeamLink,
    cfg: dict,
    schedule_url: str,
    reference: date,
) -> list[dict]:
    events: list[dict] = []
    for row in _game_rows(page_html):
        event = _row_to_event(
            row,
            team=team,
            cfg=cfg,
            schedule_url=schedule_url,
            reference=reference,
        )
        if event:
            events.append(event)
    return events


def fetch_arbiter_source(
    cfg: dict,
    *,
    reference: date,
    timeout: int = 25,
    opener=urlopen,
) -> list[dict]:
    if not cfg.get("fetch", True):
        return []

    index_html = _fetch_text(cfg["url"], timeout=timeout, opener=opener)
    teams = parse_team_index(index_html)

    if not teams:
        # An empty Arbiter entity is a valid state (St. John currently has
        # one), but distinguish it from a completely unrelated/error page.
        if "Active Teams" not in index_html and "No Events Today" not in index_html:
            raise RuntimeError(
                f"Arbiter page for {cfg['school_id']} exposed no recognizable team listing"
            )
        print(
            f"{cfg['id']} detail: Arbiter entity {cfg['entity_id']} "
            "currently exposes 0 active team schedules"
        )
        return []

    events: list[dict] = []
    schedule_errors: list[str] = []

    for team in teams:
        try:
            schedule_html = _fetch_text(team.href, timeout=timeout, opener=opener)
            events.extend(
                parse_team_schedule(
                    schedule_html,
                    team=team,
                    cfg=cfg,
                    schedule_url=team.href,
                    reference=reference,
                )
            )
        except Exception as exc:
            schedule_errors.append(
                f"{_team_name(team)}: {type(exc).__name__}: {exc}"
            )

    if schedule_errors and len(schedule_errors) == len(teams):
        raise RuntimeError(
            f"All {len(teams)} Arbiter team schedules failed; "
            + schedule_errors[0]
        )

    unique: dict[tuple, dict] = {}
    for event in events:
        key = (
            event.get("date", ""),
            event.get("start", ""),
            _normalize_name(event.get("team", "")),
            _normalize_name(event.get("opponent", "")),
        )
        if key not in unique:
            unique[key] = event
        else:
            # Prefer the record that cross-tags a known SchoolBoard opponent.
            if len(event.get("schools", [])) > len(unique[key].get("schools", [])):
                unique[key] = event

    result = sorted(
        unique.values(),
        key=lambda e: (e.get("date", ""), e.get("start", ""), e.get("title", "")),
    )

    print(
        f"{cfg['id']} detail: Arbiter entity {cfg['entity_id']} exposed "
        f"{len(teams)} active teams; parsed {len(result)} competition events"
    )
    if schedule_errors:
        print(
            f"{cfg['id']} detail: {len(schedule_errors)} team schedule(s) "
            "failed while the remaining schedules were retained"
        )
    if result:
        sample = "; ".join(
            f"{event['date']} {event['title']}"
            for event in result[:8]
        )
        print(f"{cfg['id']} detail: first events: {sample}")

    return result
