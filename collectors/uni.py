from __future__ import annotations

import hashlib
import re
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SOURCE_NAME = "Uni High Events — Google Calendar"
CALENDAR_ID = "uni-asstdirector@illinois.edu"
CALENDAR_EMBED_URL = (
    "https://calendar.google.com/calendar/u/0/embed"
    "?src=uni-asstdirector@illinois.edu&ctz=America/Chicago"
)
ICAL_URL = (
    "https://calendar.google.com/calendar/ical/"
    + quote(CALENDAR_ID, safe="")
    + "/public/basic.ics"
)

_LOCAL_TZ = ZoneInfo("America/Chicago")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
    ),
    "Accept": "text/calendar,text/plain;q=0.9,*/*;q=0.8",
}
_DAY_CODES = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}


def _fetch_text(url: str, *, timeout: int = 25) -> str:
    req = Request(url, headers=_HEADERS)
    with urlopen(req, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _unfold_ical(text: str) -> list[str]:
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for line in raw_lines:
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    if ":" not in line:
        return None
    left, value = line.split(":", 1)
    parts = left.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, param_value = part.split("=", 1)
        params[key.upper()] = param_value.strip('"')
    return name, params, value


def _unescape_text(value: str) -> str:
    value = value.replace(r"\N", "\n").replace(r"\n", "\n")
    value = value.replace(r"\,", ",").replace(r"\;", ";")
    value = value.replace(r"\\", "\\")
    return re.sub(r"\s+", " ", value).strip()


def _parse_ical_events(text: str) -> list[dict]:
    events: list[dict] = []
    current: dict[str, list[tuple[dict[str, str], str]]] | None = None

    for line in _unfold_ical(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None:
            continue

        prop = _split_property(line)
        if not prop:
            continue
        name, params, value = prop
        current.setdefault(name, []).append((params, value))

    return events


def _first(component: dict, name: str) -> tuple[dict[str, str], str] | None:
    values = component.get(name.upper()) or []
    return values[0] if values else None


def _parse_temporal(params: dict[str, str], raw: str):
    value_type = params.get("VALUE", "").upper()
    tzid = params.get("TZID")

    if value_type == "DATE" or re.fullmatch(r"\d{8}", raw):
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))

    match = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", raw)
    if not match:
        raise ValueError(f"Unsupported iCalendar date/time: {raw}")

    day_part, time_part, zulu = match.groups()
    dt = datetime(
        int(day_part[:4]),
        int(day_part[4:6]),
        int(day_part[6:8]),
        int(time_part[:2]),
        int(time_part[2:4]),
        int(time_part[4:6]),
    )

    if zulu:
        return dt.replace(tzinfo=timezone.utc).astimezone(_LOCAL_TZ)

    if tzid:
        try:
            return dt.replace(tzinfo=ZoneInfo(tzid)).astimezone(_LOCAL_TZ)
        except Exception:
            pass

    return dt.replace(tzinfo=_LOCAL_TZ)


def _temporal_key(value) -> str:
    if isinstance(value, datetime):
        local = value.astimezone(_LOCAL_TZ)
        return local.strftime("%Y%m%dT%H%M%S")
    return value.strftime("%Y%m%d")


def _event_duration(start, end):
    if end is None:
        return None
    if isinstance(start, datetime) and isinstance(end, datetime):
        return end - start
    if isinstance(start, date) and not isinstance(start, datetime):
        if isinstance(end, date) and not isinstance(end, datetime):
            return end - start
    return None


def _parse_until(raw: str, start):
    if not raw:
        return None
    try:
        if re.fullmatch(r"\d{8}", raw):
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        return _parse_temporal({}, raw)
    except Exception:
        return None


def _rrule_dict(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        result[key.upper()] = value
    return result


def _same_kind(day: date, prototype):
    if isinstance(prototype, datetime):
        return datetime(
            day.year,
            day.month,
            day.day,
            prototype.hour,
            prototype.minute,
            prototype.second,
            tzinfo=prototype.tzinfo or _LOCAL_TZ,
        )
    return day


def _add_months(value, months: int):
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    if isinstance(value, datetime):
        return value.replace(year=year, month=month, day=day)
    return date(year, month, day)


def _add_years(value, years: int):
    year = value.year + years
    day = min(value.day, monthrange(year, value.month)[1])
    if isinstance(value, datetime):
        return value.replace(year=year, day=day)
    return date(year, value.month, day)


def _within_until(value, until) -> bool:
    if until is None:
        return True
    if isinstance(value, datetime) and isinstance(until, date) and not isinstance(until, datetime):
        return value.date() <= until
    if isinstance(value, date) and not isinstance(value, datetime) and isinstance(until, datetime):
        return value <= until.date()
    return value <= until


def _expand_recurrence(
    start,
    rrule_raw: str | None,
    *,
    window_start: date,
    window_end: date,
) -> list:
    if not rrule_raw:
        day = start.date() if isinstance(start, datetime) else start
        return [start] if window_start <= day <= window_end else []

    rule = _rrule_dict(rrule_raw)
    freq = rule.get("FREQ", "").upper()
    interval = max(1, int(rule.get("INTERVAL", "1") or "1"))
    count_limit = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = _parse_until(rule.get("UNTIL", ""), start)

    occurrences: list = []
    generated = 0
    safety = 0

    def consider(value) -> bool:
        nonlocal generated
        if not _within_until(value, until):
            return False
        generated += 1
        day = value.date() if isinstance(value, datetime) else value
        if window_start <= day <= window_end:
            occurrences.append(value)
        if count_limit is not None and generated >= count_limit:
            return False
        return day <= window_end

    if freq == "DAILY":
        current = start
        while safety < 5000:
            safety += 1
            keep_going = consider(current)
            if not keep_going:
                break
            current = current + timedelta(days=interval)
        return occurrences

    if freq == "WEEKLY":
        byday = [
            _DAY_CODES[token[-2:]]
            for token in rule.get("BYDAY", "").split(",")
            if token[-2:] in _DAY_CODES
        ]
        if not byday:
            byday = [start.weekday()]

        start_day = start.date() if isinstance(start, datetime) else start
        week0 = start_day - timedelta(days=start_day.weekday())
        week_index = 0

        while safety < 1500:
            safety += 1
            week_start = week0 + timedelta(weeks=week_index * interval)
            for weekday in sorted(set(byday)):
                day = week_start + timedelta(days=weekday)
                value = _same_kind(day, start)
                if value < start:
                    continue
                if not _within_until(value, until):
                    return occurrences
                generated += 1
                day_value = value.date() if isinstance(value, datetime) else value
                if window_start <= day_value <= window_end:
                    occurrences.append(value)
                if count_limit is not None and generated >= count_limit:
                    return occurrences
            if week_start > window_end + timedelta(days=7):
                break
            week_index += 1
        return occurrences

    if freq == "MONTHLY":
        current = start
        while safety < 500:
            safety += 1
            keep_going = consider(current)
            if not keep_going:
                break
            current = _add_months(current, interval)
        return occurrences

    if freq == "YEARLY":
        current = start
        while safety < 100:
            safety += 1
            keep_going = consider(current)
            if not keep_going:
                break
            current = _add_years(current, interval)
        return occurrences

    # Unsupported recurrence types are safer as a single DTSTART than as
    # invented dates.
    day = start.date() if isinstance(start, datetime) else start
    return [start] if window_start <= day <= window_end else []


def _event_id(uid: str, occurrence) -> str:
    digest = hashlib.sha1(
        f"{uid}|{_temporal_key(occurrence)}".encode("utf-8")
    ).hexdigest()[:16]
    return f"uni-google-{digest}"


def _to_record(
    *,
    uid: str,
    title: str,
    occurrence,
    duration,
    location: str,
) -> dict:
    day = occurrence.date() if isinstance(occurrence, datetime) else occurrence
    record = {
        "id": _event_id(uid, occurrence),
        "title": title,
        "date": day.isoformat(),
        "schools": ["uni"],
        "scope": "school",
        "category": "general",
        "source": SOURCE_NAME,
        "sourceUrl": CALENDAR_EMBED_URL,
    }

    if isinstance(occurrence, datetime):
        local = occurrence.astimezone(_LOCAL_TZ)
        record["start"] = local.strftime("%H:%M")
        if duration:
            ending = local + duration
            record["end"] = ending.strftime("%H:%M")
            if ending.date() != local.date():
                record["endDate"] = ending.date().isoformat()
    else:
        record["allDay"] = True
        if duration and duration.days > 1:
            # DATE-valued DTEND is exclusive in iCalendar.
            record["endDate"] = (occurrence + duration - timedelta(days=1)).isoformat()

    if location:
        record["location"] = location
    return record


def parse_uni_ical(
    text: str,
    *,
    reference: date | None = None,
    history_days: int = 45,
    horizon_days: int = 120,
) -> list[dict]:
    reference = reference or date.today()
    window_start = reference - timedelta(days=history_days)
    window_end = reference + timedelta(days=horizon_days)

    components = _parse_ical_events(text)

    # Explicit recurrence overrides/cancellations suppress the generated base
    # occurrence with the same UID + RECURRENCE-ID.
    override_keys: set[tuple[str, str]] = set()
    for component in components:
        uid_prop = _first(component, "UID")
        recurrence_prop = _first(component, "RECURRENCE-ID")
        if not uid_prop or not recurrence_prop:
            continue
        try:
            recurrence_value = _parse_temporal(*recurrence_prop)
        except Exception:
            continue
        override_keys.add((uid_prop[1], _temporal_key(recurrence_value)))

    records: list[dict] = []

    for component in components:
        status_prop = _first(component, "STATUS")
        status = status_prop[1].upper() if status_prop else ""
        if status == "CANCELLED":
            continue

        uid_prop = _first(component, "UID")
        start_prop = _first(component, "DTSTART")
        summary_prop = _first(component, "SUMMARY")
        if not uid_prop or not start_prop or not summary_prop:
            continue

        uid = uid_prop[1]
        title = _unescape_text(summary_prop[1])
        if not title:
            continue

        try:
            start = _parse_temporal(*start_prop)
        except Exception:
            continue

        end_prop = _first(component, "DTEND")
        end = None
        if end_prop:
            try:
                end = _parse_temporal(*end_prop)
            except Exception:
                end = None
        duration = _event_duration(start, end)

        location_prop = _first(component, "LOCATION")
        location = _unescape_text(location_prop[1]) if location_prop else ""

        recurrence_prop = _first(component, "RECURRENCE-ID")
        if recurrence_prop:
            occurrence_day = start.date() if isinstance(start, datetime) else start
            if window_start <= occurrence_day <= window_end:
                records.append(
                    _to_record(
                        uid=uid,
                        title=title,
                        occurrence=start,
                        duration=duration,
                        location=location,
                    )
                )
            continue

        rrule_prop = _first(component, "RRULE")
        rrule_raw = rrule_prop[1] if rrule_prop else None

        exclusions: set[str] = set()
        for params, raw in component.get("EXDATE", []):
            for token in raw.split(","):
                try:
                    exclusions.add(_temporal_key(_parse_temporal(params, token)))
                except Exception:
                    pass

        for occurrence in _expand_recurrence(
            start,
            rrule_raw,
            window_start=window_start,
            window_end=window_end,
        ):
            key = _temporal_key(occurrence)
            if key in exclusions or (uid, key) in override_keys:
                continue
            records.append(
                _to_record(
                    uid=uid,
                    title=title,
                    occurrence=occurrence,
                    duration=duration,
                    location=location,
                )
            )

    # Exact duplicates occasionally occur when Google exports a moved instance
    # in addition to its recurring parent.
    chosen: dict[tuple, dict] = {}
    for record in records:
        key = (
            record.get("date", ""),
            record.get("start", ""),
            record.get("title", "").casefold(),
            record.get("location", "").casefold(),
        )
        chosen[key] = record

    return sorted(
        chosen.values(),
        key=lambda event: (
            event.get("date", ""),
            event.get("start", "23:59"),
            event.get("title", "").casefold(),
        ),
    )


def fetch_uni_calendar(
    *,
    reference: date | None = None,
) -> list[dict]:
    text = _fetch_text(ICAL_URL)
    events = parse_uni_ical(text, reference=reference)
    if not events:
        raise RuntimeError("Uni public Google Calendar returned zero parseable events")

    print(
        f"uni-calendar detail: public Google calendar parsed {len(events)} event(s); "
        f"calendar id {CALENDAR_ID}"
    )
    if events:
        sample = "; ".join(
            f"{event.get('date')} {event.get('title')}"
            for event in events[:8]
        )
        print(f"uni-calendar detail: first events: {sample}")
    return events
