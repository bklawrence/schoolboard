from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from typing import Iterable
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")


def _unfold_ical(text: str) -> list[str]:
    """RFC 5545 line unfolding: continuation lines begin with space/tab."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for line in raw:
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    if ":" not in line:
        return None
    left, value = line.split(":", 1)
    bits = left.split(";")
    name = bits[0].upper()
    params: dict[str, str] = {}
    for bit in bits[1:]:
        if "=" in bit:
            k, v = bit.split("=", 1)
            params[k.upper()] = v.strip('"')
    return name, params, _unescape(value)


def _parse_dt(value: str, params: dict[str, str]) -> date | datetime:
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date()

    # RFC timestamps can be local-with-TZID or UTC with trailing Z.
    if value.endswith("Z"):
        dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(CHICAGO)

    fmt = "%Y%m%dT%H%M%S" if len(value) >= 15 else "%Y%m%dT%H%M"
    dt = datetime.strptime(value[:15] if fmt.endswith("%S") else value[:13], fmt)
    tzid = params.get("TZID")
    try:
        tz = ZoneInfo(tzid) if tzid else CHICAGO
    except Exception:
        tz = CHICAGO
    return dt.replace(tzinfo=tz).astimezone(CHICAGO)


def parse_ics(
    text: str,
    *,
    school_ids: list[str] | None = None,
    source: str = "Uni High Athletics — Snap!",
    id_prefix: str = "uni-snap",
) -> list[dict]:
    events: list[dict] = []
    current: dict[str, tuple[dict[str, str], str]] | None = None

    for line in _unfold_ical(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(
                    _normalize_raw_event(
                        current,
                        school_ids=school_ids,
                        source=source,
                        id_prefix=id_prefix,
                    )
                )
            current = None
            continue
        if current is None:
            continue
        prop = _split_property(line)
        if not prop:
            continue
        name, params, value = prop
        # Snap's useful fields are single-valued for our purposes.
        if name not in current:
            current[name] = (params, value)

    return [e for e in events if e]


def _normalize_raw_event(
    raw: dict[str, tuple[dict[str, str], str]],
    *,
    school_ids: list[str] | None = None,
    source: str = "Uni High Athletics — Snap!",
    id_prefix: str = "uni-snap",
) -> dict:
    def val(name: str) -> str:
        return raw.get(name, ({}, ""))[1]

    uid = val("UID") or f"{val('SUMMARY')}|{val('DTSTART')}|{val('LOCATION')}"
    summary = val("SUMMARY") or "Athletics event"
    location = val("LOCATION")
    description = val("DESCRIPTION")
    explicit_url = val("URL")

    start_prop = raw.get("DTSTART")
    if not start_prop:
        return {}
    start_obj = _parse_dt(start_prop[1], start_prop[0])

    end_obj = None
    if raw.get("DTEND"):
        end_obj = _parse_dt(raw["DTEND"][1], raw["DTEND"][0])

    digest = hashlib.sha1(uid.encode("utf-8")).hexdigest()[:18]
    out: dict = {
        "id": f"{id_prefix}-{digest}",
        "title": summary,
        "date": start_obj.isoformat() if isinstance(start_obj, date) and not isinstance(start_obj, datetime) else start_obj.date().isoformat(),
        "schools": list(school_ids or ["uni"]),
        "scope": "school",
        "category": "sport",
        "source": source,
    }

    if isinstance(start_obj, datetime):
        out["start"] = start_obj.strftime("%H:%M")
        if isinstance(end_obj, datetime) and end_obj.date() == start_obj.date():
            out["end"] = end_obj.strftime("%H:%M")
    else:
        out["allDay"] = True
        # Multi-day all-day DTEND is exclusive in iCalendar. SchoolBoard's
        # endDate is inclusive, so leave it out until we need multi-day sports.

    if location:
        out["location"] = location

    source_url = explicit_url or _first_url(description)
    if source_url:
        out["sourceUrl"] = source_url

    return out


def _first_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,);]")


def fetch_snap(
    feed_url: str,
    *,
    school_ids: list[str],
    source: str,
    id_prefix: str,
    timeout: int = 30,
    opener=urlopen,
) -> list[dict]:
    request = Request(
        feed_url,
        headers={
            "User-Agent": "ChambanaSchoolboard/1.0 (+public school calendar aggregator)",
            "Accept": "text/calendar,text/plain;q=0.9,*/*;q=0.8",
        },
    )
    with opener(request, timeout=timeout) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"

    text = body.decode(charset, errors="replace")

    # A valid Snap calendar may legitimately contain zero VEVENT records
    # (for example, between seasons). Distinguish that from an HTML error
    # page or other malformed response.
    if "BEGIN:VCALENDAR" not in text.upper():
        sample = " ".join(text.split())[:180]
        raise RuntimeError(
            "Snap response was not a valid iCalendar feed"
            + (f": {sample}" if sample else "")
        )

    return parse_ics(
        text,
        school_ids=school_ids,
        source=source,
        id_prefix=id_prefix,
    )


def fetch_uni_snap(
    feed_url: str,
    *,
    timeout: int = 30,
    opener=urlopen,
) -> list[dict]:
    """Backward-compatible wrapper retained for older build scripts/tests."""
    return fetch_snap(
        feed_url,
        school_ids=["uni"],
        source="Uni High Athletics — Snap!",
        id_prefix="uni-snap",
        timeout=timeout,
        opener=opener,
    )
