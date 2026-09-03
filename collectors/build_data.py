from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from collectors.quest import QUEST_GROUPS, fetch_quest_group
from collectors.snap import fetch_snap
from collectors.unit4 import UNIT4_GROUPS, fetch_unit4_menus
from collectors.countryside import SOURCE_NAME as COUNTRYSIDE_SOURCE, fetch_countryside_calendar
from collectors.libraries import LIBRARIES, fetch_library_calendar
from collectors.stmatthew import SOURCE_NAME as STMATTHEW_SOURCE, fetch_stmatthew_calendar
from collectors.stjohn import SOURCE_NAME as STJOHN_SOURCE, fetch_stjohn_calendar
from collectors.judah import (
    CALENDAR_SOURCE_NAME as JUDAH_CALENDAR_SOURCE,
    ATHLETICS_SOURCE_NAME as JUDAH_ATHLETICS_SOURCE,
    fetch_judah_calendar,
    fetch_judah_athletics,
)
from collectors.academyhigh import (
    CALENDAR_SOURCE_NAME as ACADEMY_CALENDAR_SOURCE,
    NEWSLETTER_SOURCE_NAME as ACADEMY_NEWSLETTER_SOURCE,
    fetch_academy_calendar,
    fetch_academy_newsletter,
)
from collectors.montessori import SOURCE_NAME as MONTESSORI_SOURCE, fetch_montessori_calendar
from collectors.usd116_calendar import SOURCE_NAME as USD116_CALENDAR_SOURCE, fetch_usd116_calendar
from collectors.usd116_schoolfeeds import SCHOOL_FEEDS, SOURCE_PREFIX as USD116_SCHOOLFEED_PREFIX, fetch_school_feed
from collectors.apptegy_calendars import (
    DISTRICT_SOURCE as UNIT4_DISTRICT_SOURCE,
    STM_SOURCE,
    UNIT4_CALENDARS,
    UNIT4_SCHOOL_IDS,
    fetch_stm_calendar,
    fetch_unit4_calendar,
    fetch_unit4_district_calendar,
)

ROOT = Path(__file__).resolve().parent
STATIC_PATH = ROOT / "data" / "static-events.json"
OUTPUT_PATH = ROOT / "schoolboard-data.json"

# Keep recent context plus a short forward planning window. These bounds
# move forward automatically on every build.
EVENT_HISTORY_DAYS = 30
EVENT_HORIZON_DAYS = 60

SNAP_SOURCES = [
    {
        "id": "uni-athletics",
        "feed": "https://manage.snap.app/ical/link/Uni/2026-2027/ALL/CDT/athletic%2Cactivities/0/0",
        "source": "Uni High Athletics — Snap!",
        "schools": ["uni"],
        "id_prefix": "uni-snap",
    },
    {
        "id": "uhs-athletics",
        "feed": "https://manage.snap.app/ical/link/Urbana/2026-2027/ALL/CDT/athletic%2Cactivities/0/0",
        "source": "Urbana High Athletics — Snap!",
        "schools": ["uhs"],
        "id_prefix": "uhs-snap",
    },
    {
        "id": "ums-sgc-athletics",
        "feed": "https://manage.snap.app/ical/link/urbanams/2026-2027/ALL/CDT/athletic%2Cactivities/0/0",
        "source": "Urbana Middle / SGC Athletics — Snap!",
        "schools": ["ums", "sgc"],
        "id_prefix": "ums-sgc-snap",
    },
]


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


SEMANTIC_EVENT_PHRASES = (
    "curriculum night",
    "picture day",
    "open house",
    "black family affinity parent group",
    "spanish parents dual language",
)


def normalized_event_title(title: str) -> str:
    clean = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
    clean = re.sub(r"\.pdf\s*$", "", clean).strip(" -–:,.")

    for phrase in SEMANTIC_EVENT_PHRASES:
        if phrase in clean:
            return phrase

    clean = re.sub(r"[^a-z0-9]+", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def canonicalize_known_event(event: dict) -> dict:
    """
    Normalize a few source-specific modeling mistakes before dedupe.
    BPAC is a Yankee Ridge parent-group event; when a source encodes SGC as a
    second school, SGC is actually the venue.
    """
    event = dict(event)
    title_key = normalized_event_title(event.get("title", ""))

    if title_key == "spanish parents dual language":
        schools = list(event.get("schools") or [])
        location_text = str(event.get("location") or "").casefold()
        title_text = str(event.get("title") or "").casefold()

        sgc_evidence = (
            "sgc" in schools
            or "sixth grade center" in location_text
            or "sixth grade center" in title_text
            or re.search(r"\bsgc\b", title_text) is not None
        )
        if sgc_evidence:
            event["schools"] = ["yankee"]
            event["location"] = "Urbana Sixth Grade Center — Multipurpose Room"

    return event


def event_key(event: dict) -> tuple:
    return (
        event.get("date", ""),
        event.get("start", ""),
        normalized_event_title(event.get("title", "")),
        tuple(sorted(event.get("schools") or [])),
    )


def _event_quality(event: dict) -> tuple:
    title = str(event.get("title", "")).strip()
    source = str(event.get("source", ""))
    noisy = int(".pdf" in title.casefold() or "hello " in title.casefold())
    schoolfeed = int(source.startswith("USD 116 School Feed"))
    return (noisy, schoolfeed, len(title))


def merge_unique(events: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    no_id: list[dict] = []
    for event in events:
        event_id = event.get("id")
        if event_id:
            by_id[event_id] = event
        else:
            no_id.append(event)
    merged = list(by_id.values()) + no_id

    best_by_key: dict[tuple, dict] = {}
    order: list[tuple] = []
    for event in merged:
        event = canonicalize_known_event(event)
        key = event_key(event)
        if key not in best_by_key:
            best_by_key[key] = event
            order.append(key)
            continue
        if _event_quality(event) < _event_quality(best_by_key[key]):
            best_by_key[key] = event

    result = [best_by_key[key] for key in order]
    return sorted(result, key=lambda e: (e.get("date", ""), e.get("start", ""), e.get("title", "")))


def _unit4_district_title_key(title: str) -> str:
    """Conservative normalization for district-vs-school duplicate detection."""
    clean = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
    clean = re.sub(r"[^a-z0-9]+", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def _unit4_district_signature(event: dict) -> tuple:
    return (
        event.get("date", ""),
        event.get("start", ""),
        event.get("end", ""),
        _unit4_district_title_key(event.get("title", "")),
    )


def collapse_unit4_district_duplicates(
    candidates: list[dict],
    district_events: list[dict],
) -> tuple[list[dict], int]:
    """
    Prefer the single district record only when a Unit 4 school/static record
    has the same date, start/end time, and normalized title.
    """
    district_signatures = {
        _unit4_district_signature(event)
        for event in district_events
    }
    unit4_ids = set(UNIT4_SCHOOL_IDS)

    kept: list[dict] = []
    removed = 0

    for event in candidates:
        schools = set(event.get("schools") or [])
        is_unit4_record = bool(schools) and schools.issubset(unit4_ids)

        if (
            is_unit4_record
            and _unit4_district_signature(event) in district_signatures
        ):
            removed += 1
            continue

        kept.append(event)

    return kept, removed


def rolling_event_window(reference: date) -> tuple[date, date]:
    return (
        reference - timedelta(days=EVENT_HISTORY_DAYS),
        reference + timedelta(days=EVENT_HORIZON_DAYS),
    )


def filter_events_to_rolling_window(
    events: list[dict],
    *,
    reference: date,
) -> tuple[list[dict], date, date]:
    window_start, window_end = rolling_event_window(reference)
    kept: list[dict] = []

    for event in events:
        raw_start = str(event.get("date") or "")
        try:
            event_start = date.fromisoformat(raw_start)
        except ValueError:
            continue

        raw_end = str(event.get("endDate") or "")
        try:
            event_end = date.fromisoformat(raw_end) if raw_end else event_start
        except ValueError:
            event_end = event_start

        # Keep events that overlap the rolling window, including multi-day
        # events that began shortly before it.
        if event_end < window_start or event_start > window_end:
            continue
        kept.append(event)

    kept = sorted(
        kept,
        key=lambda e: (e.get("date", ""), e.get("start", ""), e.get("title", "")),
    )
    return kept, window_start, window_end


def merge_meals(meals: list[dict]) -> list[dict]:
    by_key: dict[tuple[str, str], dict] = {}
    for meal in meals:
        key = (meal.get("group", ""), meal.get("date", ""))
        if all(key):
            by_key[key] = meal
    return sorted(by_key.values(), key=lambda m: (m.get("date", ""), m.get("group", "")))


def previous_source_events(source: str) -> list[dict]:
    old = load_json(OUTPUT_PATH, {})
    return [e for e in old.get("events", []) if e.get("source") == source]


def previous_group_meals(group: str) -> list[dict]:
    old = load_json(OUTPUT_PATH, {})
    return [m for m in old.get("meals", []) if m.get("group") == group]


def previous_schoolfeed_events(school_id: str) -> list[dict]:
    old = load_json(OUTPUT_PATH, {})
    prefix = f"{USD116_SCHOOLFEED_PREFIX} — "
    return [
        event for event in old.get("events", [])
        if str(event.get("source", "")).startswith(prefix)
        and school_id in (event.get("schools") or [])
    ]



def dedupe_academy_sources(
    calendar_events: list[dict],
    newsletter_events: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Prefer Academy High's annual academic calendar for structural dates that
    the current Smore newsletter repeats, while preserving newsletter-only
    reminders and athletics.
    """
    def words(title: str) -> set[str]:
        normalized = str(title or "").casefold().replace("½", " half ")
        normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
        return set(normalized.split())

    def covered_dates(event: dict) -> set[str]:
        start_text = str(event.get("date") or "")
        end_text = str(event.get("endDate") or start_text)
        try:
            start_day = date.fromisoformat(start_text)
            end_day = date.fromisoformat(end_text)
        except Exception:
            return {start_text} if start_text else set()

        if end_day < start_day:
            end_day = start_day

        days = set()
        current = start_day
        while current <= end_day:
            days.add(current.isoformat())
            current += timedelta(days=1)
        return days

    def structural_kind(title: str) -> str | None:
        w = words(title)

        if "fall" in w and "break" in w:
            return "fall-break"
        if "winter" in w and "break" in w:
            return "winter-break"
        if "spring" in w and "break" in w:
            return "spring-break"
        if "labor" in w and "day" in w:
            return "no-school"
        if "no" in w and "school" in w:
            return "no-school"
        if "quarter" in w:
            return "quarter"
        if "semester" in w:
            return "semester"
        if "conference" in w or "conferences" in w:
            return "conference"
        if "half" in w and (
            "day" in w or "dismissal" in w or "development" in w
        ):
            return "half-day"
        if "faculty" in w and "development" in w:
            return "faculty-development"
        return None

    calendar_by_day: dict[str, list[tuple[str, dict]]] = {}
    for event in calendar_events:
        kind = structural_kind(event.get("title", ""))
        if not kind:
            continue
        for day in covered_dates(event):
            calendar_by_day.setdefault(day, []).append((kind, event))

    kept_newsletter = []
    removed_newsletter = []

    for event in newsletter_events:
        day = str(event.get("date") or "")
        newsletter_kind = structural_kind(event.get("title", ""))
        candidates = calendar_by_day.get(day, [])

        duplicate = False
        if newsletter_kind and candidates:
            for calendar_kind, _calendar_event in candidates:
                if newsletter_kind == calendar_kind:
                    duplicate = True
                    break

                if newsletter_kind == "no-school" and calendar_kind in {
                    "no-school",
                    "fall-break",
                    "winter-break",
                    "spring-break",
                    "faculty-development",
                }:
                    duplicate = True
                    break

                if newsletter_kind == "half-day" and calendar_kind in {
                    "half-day",
                    "faculty-development",
                }:
                    duplicate = True
                    break

        if duplicate:
            removed_newsletter.append(event)
        else:
            kept_newsletter.append(event)

    if removed_newsletter:
        sample = "; ".join(
            f"{event.get('date')} {event.get('title')}"
            for event in removed_newsletter[:8]
        )
        print(
            f"academy-dedupe detail: removed {len(removed_newsletter)} "
            f"newsletter structural duplicates in favor of academic calendar"
            + (f"; sample: {sample}" if sample else "")
        )

    return calendar_events, kept_newsletter


def build(*, offline: bool = False) -> dict:
    static = load_json(STATIC_PATH, {"events": [], "meals": []})
    static_events = [
        event for event in static.get("events", [])
        if "USD 116" not in str(event.get("source", ""))
    ]
    static_meals = list(static.get("meals", []))
    source_status = []
    today = datetime.now(ZoneInfo("America/Chicago")).date()

    snap_events: list[dict] = []
    for cfg in SNAP_SOURCES:
        if offline:
            source_events = previous_source_events(cfg["source"])
            source_status.append({
                "id": cfg["id"],
                "status": "cached" if source_events else "failed",
                "count": len(source_events),
                "unit": "events",
            })
        else:
            try:
                source_events = fetch_snap(
                    cfg["feed"],
                    school_ids=cfg["schools"],
                    source=cfg["source"],
                    id_prefix=cfg["id_prefix"],
                )
                source_status.append({
                    "id": cfg["id"],
                    "status": "live",
                    "count": len(source_events),
                    "unit": "events",
                })
            except Exception as exc:
                source_events = previous_source_events(cfg["source"])
                source_status.append({
                    "id": cfg["id"],
                    "status": "cached" if source_events else "failed",
                    "count": len(source_events),
                    "unit": "events",
                    "error": f"{type(exc).__name__}: {exc}",
                })
        snap_events.extend(source_events)

    # USD 116 district school-year calendar. This replaces the hand-entered
    # USD 116 schedule records while leaving school-specific events untouched.
    if offline:
        usd116_calendar_events = previous_source_events(USD116_CALENDAR_SOURCE)
        source_status.append({
            "id": "usd-calendar",
            "status": "cached" if usd116_calendar_events else "failed",
            "count": len(usd116_calendar_events),
            "unit": "events",
        })
    else:
        try:
            usd116_calendar_events = fetch_usd116_calendar()
            source_status.append({
                "id": "usd-calendar",
                "status": "live",
                "count": len(usd116_calendar_events),
                "unit": "events",
            })
        except Exception as exc:
            usd116_calendar_events = previous_source_events(USD116_CALENDAR_SOURCE)
            source_status.append({
                "id": "usd-calendar",
                "status": "cached" if usd116_calendar_events else "failed",
                "count": len(usd116_calendar_events),
                "unit": "events",
                "error": f"{type(exc).__name__}: {exc}",
            })

    # Recent public ParentSquare posts mirrored on each USD 116 school site.
    # Only explicit date-led lines are parsed. Previously collected future
    # events remain cached even after an older post rolls off the homepage.
    usd116_school_events: list[dict] = []
    for school in SCHOOL_FEEDS:
        cached = previous_schoolfeed_events(school.id)
        cached_future = [
            event for event in cached
            if event.get("date") and event.get("date") >= today.isoformat()
        ]

        if offline:
            source_events = cached_future
            source_status.append({
                "id": f"usd-feed-{school.id}",
                "status": "cached" if source_events else "live",
                "count": len(source_events),
                "unit": "events",
            })
        else:
            try:
                fresh, refreshed_urls = fetch_school_feed(
                    school,
                    reference=today,
                    return_refreshed_urls=True,
                )

                # A successfully refreshed ParentSquare post is authoritative:
                # remove its prior cached events before adding the fresh parse.
                # Future events from older posts that have rolled off the
                # homepage remain cached.
                retained_cached = [
                    event for event in cached_future
                    if event.get("sourceUrl") not in refreshed_urls
                ]
                source_events = merge_unique(retained_cached + fresh)

                source_status.append({
                    "id": f"usd-feed-{school.id}",
                    "status": "live",
                    "count": len(source_events),
                    "unit": "events",
                })
            except Exception as exc:
                source_events = cached_future
                source_status.append({
                    "id": f"usd-feed-{school.id}",
                    "status": "cached" if source_events else "failed",
                    "count": len(source_events),
                    "unit": "events",
                    "error": f"{type(exc).__name__}: {exc}",
                })

        usd116_school_events.extend(source_events)

    # Unit 4 school-specific Apptegy calendars. Each school discovers its own
    # public iCalendar URL from the Events page and caches independently.
    unit4_school_events: list[dict] = []
    for calendar in UNIT4_CALENDARS:
        if offline:
            source_events = previous_source_events(calendar.source)
            source_events, _, _ = filter_events_to_rolling_window(
                source_events,
                reference=today,
            )
            source_status.append({
                "id": calendar.log_id,
                "status": "cached" if source_events else "live",
                "count": len(source_events),
                "unit": "events",
            })
        else:
            try:
                source_events = fetch_unit4_calendar(calendar)
                source_events, _, _ = filter_events_to_rolling_window(
                    source_events,
                    reference=today,
                )
                source_status.append({
                    "id": calendar.log_id,
                    "status": "live",
                    "count": len(source_events),
                    "unit": "events",
                })
            except Exception as exc:
                source_events = previous_source_events(calendar.source)
                source_events, _, _ = filter_events_to_rolling_window(
                    source_events,
                    reference=today,
                )
                source_status.append({
                    "id": calendar.log_id,
                    "status": "cached" if source_events else "failed",
                    "count": len(source_events),
                    "unit": "events",
                    "error": f"{type(exc).__name__}: {exc}",
                })

        unit4_school_events.extend(source_events)

    # Unit 4 district-wide Apptegy calendar. A successful live fetch replaces
    # the prior district snapshot; cached data is used only if the source fails.
    if offline:
        unit4_district_events = previous_source_events(UNIT4_DISTRICT_SOURCE)
        unit4_district_events, _, _ = filter_events_to_rolling_window(
            unit4_district_events,
            reference=today,
        )
        source_status.append({
            "id": "u4-district-calendar",
            "status": "cached" if unit4_district_events else "live",
            "count": len(unit4_district_events),
            "unit": "events",
        })
    else:
        try:
            unit4_district_events = fetch_unit4_district_calendar()
            unit4_district_events, _, _ = filter_events_to_rolling_window(
                unit4_district_events,
                reference=today,
            )
            source_status.append({
                "id": "u4-district-calendar",
                "status": "live",
                "count": len(unit4_district_events),
                "unit": "events",
            })
        except Exception as exc:
            unit4_district_events = previous_source_events(UNIT4_DISTRICT_SOURCE)
            unit4_district_events, _, _ = filter_events_to_rolling_window(
                unit4_district_events,
                reference=today,
            )
            source_status.append({
                "id": "u4-district-calendar",
                "status": "cached" if unit4_district_events else "failed",
                "count": len(unit4_district_events),
                "unit": "events",
                "error": f"{type(exc).__name__}: {exc}",
            })

    # The High School of Saint Thomas More public Apptegy calendar.
    if offline:
        stm_events = previous_source_events(STM_SOURCE)
        stm_events, _, _ = filter_events_to_rolling_window(
            stm_events,
            reference=today,
        )
        source_status.append({
            "id": "stm-calendar",
            "status": "cached" if stm_events else "live",
            "count": len(stm_events),
            "unit": "events",
        })
    else:
        try:
            stm_events = fetch_stm_calendar()
            stm_events, _, _ = filter_events_to_rolling_window(
                stm_events,
                reference=today,
            )
            source_status.append({
                "id": "stm-calendar",
                "status": "live",
                "count": len(stm_events),
                "unit": "events",
            })
        except Exception as exc:
            stm_events = previous_source_events(STM_SOURCE)
            stm_events, _, _ = filter_events_to_rolling_window(
                stm_events,
                reference=today,
            )
            source_status.append({
                "id": "stm-calendar",
                "status": "cached" if stm_events else "failed",
                "count": len(stm_events),
                "unit": "events",
                "error": f"{type(exc).__name__}: {exc}",
            })

    # St. Matthew Catholic School public School/Athletics calendars.
    if offline:
        stmatthew_events = previous_source_events(STMATTHEW_SOURCE)
        stmatthew_events, _, _ = filter_events_to_rolling_window(
            stmatthew_events,
            reference=today,
        )
        source_status.append({
            "id": "stmatthew-calendar",
            "status": "cached" if stmatthew_events else "live",
            "count": len(stmatthew_events),
            "unit": "events",
        })
    else:
        try:
            stmatthew_events = fetch_stmatthew_calendar(reference=today)
            stmatthew_events, _, _ = filter_events_to_rolling_window(
                stmatthew_events,
                reference=today,
            )
            source_status.append({
                "id": "stmatthew-calendar",
                "status": "live",
                "count": len(stmatthew_events),
                "unit": "events",
            })
        except Exception as exc:
            stmatthew_events = previous_source_events(STMATTHEW_SOURCE)
            stmatthew_events, _, _ = filter_events_to_rolling_window(
                stmatthew_events,
                reference=today,
            )
            source_status.append({
                "id": "stmatthew-calendar",
                "status": "cached" if stmatthew_events else "failed",
                "count": len(stmatthew_events),
                "unit": "events",
                "error": f"{type(exc).__name__}: {exc}",
            })

    # St. John Lutheran School / Little Lamb Preschool. The public school
    # calendar page links its current annual PDF through Beehively; the
    # collector rediscovers that document each run so yearly replacements do
    # not require a GitHub edit.
    if offline:
        stjohn_events = previous_source_events(STJOHN_SOURCE)
        stjohn_events, _, _ = filter_events_to_rolling_window(
            stjohn_events,
            reference=today,
        )
        source_status.append({
            "id": "stjohn-calendar",
            "status": "cached" if stjohn_events else "failed",
            "count": len(stjohn_events),
            "unit": "events",
        })
    else:
        try:
            stjohn_events = fetch_stjohn_calendar(reference=today)
            stjohn_events, _, _ = filter_events_to_rolling_window(
                stjohn_events,
                reference=today,
            )
            source_status.append({
                "id": "stjohn-calendar",
                "status": "live",
                "count": len(stjohn_events),
                "unit": "events",
            })
        except Exception as exc:
            stjohn_events = previous_source_events(STJOHN_SOURCE)
            stjohn_events, _, _ = filter_events_to_rolling_window(
                stjohn_events,
                reference=today,
            )
            source_status.append({
                "id": "stjohn-calendar",
                "status": "cached" if stjohn_events else "failed",
                "count": len(stjohn_events),
                "unit": "events",
                "error": f"{type(exc).__name__}: {exc}",
            })

    # Judah Christian School: current annual calendar PDF plus public Arbiter athletics.
    if offline:
        judah_calendar_events = previous_source_events(JUDAH_CALENDAR_SOURCE)
        judah_calendar_events, _, _ = filter_events_to_rolling_window(judah_calendar_events, reference=today)
        source_status.append({"id":"judah-calendar","status":"cached" if judah_calendar_events else "failed","count":len(judah_calendar_events),"unit":"events"})
    else:
        try:
            judah_calendar_events = fetch_judah_calendar(reference=today)
            judah_calendar_events, _, _ = filter_events_to_rolling_window(judah_calendar_events, reference=today)
            source_status.append({"id":"judah-calendar","status":"live","count":len(judah_calendar_events),"unit":"events"})
        except Exception as exc:
            judah_calendar_events = previous_source_events(JUDAH_CALENDAR_SOURCE)
            judah_calendar_events, _, _ = filter_events_to_rolling_window(judah_calendar_events, reference=today)
            source_status.append({"id":"judah-calendar","status":"cached" if judah_calendar_events else "failed","count":len(judah_calendar_events),"unit":"events","error":f"{type(exc).__name__}: {exc}"})

    if offline:
        judah_athletics_events = previous_source_events(JUDAH_ATHLETICS_SOURCE)
        judah_athletics_events, _, _ = filter_events_to_rolling_window(judah_athletics_events, reference=today)
        source_status.append({"id":"judah-athletics","status":"cached" if judah_athletics_events else "failed","count":len(judah_athletics_events),"unit":"events"})
    else:
        try:
            judah_athletics_events = fetch_judah_athletics()
            judah_athletics_events, _, _ = filter_events_to_rolling_window(judah_athletics_events, reference=today)
            source_status.append({"id":"judah-athletics","status":"live","count":len(judah_athletics_events),"unit":"events"})
        except Exception as exc:
            judah_athletics_events = previous_source_events(JUDAH_ATHLETICS_SOURCE)
            judah_athletics_events, _, _ = filter_events_to_rolling_window(judah_athletics_events, reference=today)
            source_status.append({"id":"judah-athletics","status":"cached" if judah_athletics_events else "failed","count":len(judah_athletics_events),"unit":"events","error":f"{type(exc).__name__}: {exc}"})

    # Academy High: annual academic calendar plus Important Dates from the
    # latest Smore newsletter linked on Academy High's own homepage.
    if offline:
        academy_calendar_events = previous_source_events(ACADEMY_CALENDAR_SOURCE)
        academy_calendar_events, _, _ = filter_events_to_rolling_window(
            academy_calendar_events,
            reference=today,
        )
        source_status.append({
            "id": "academy-calendar",
            "status": "cached" if academy_calendar_events else "failed",
            "count": len(academy_calendar_events),
            "unit": "events",
        })
    else:
        try:
            academy_calendar_events = fetch_academy_calendar(reference=today)
            academy_calendar_events, _, _ = filter_events_to_rolling_window(
                academy_calendar_events,
                reference=today,
            )
            source_status.append({
                "id": "academy-calendar",
                "status": "live",
                "count": len(academy_calendar_events),
                "unit": "events",
            })
        except Exception as exc:
            academy_calendar_events = previous_source_events(ACADEMY_CALENDAR_SOURCE)
            academy_calendar_events, _, _ = filter_events_to_rolling_window(
                academy_calendar_events,
                reference=today,
            )
            source_status.append({
                "id": "academy-calendar",
                "status": "cached" if academy_calendar_events else "failed",
                "count": len(academy_calendar_events),
                "unit": "events",
                "error": f"{type(exc).__name__}: {exc}",
            })

    if offline:
        academy_newsletter_events = previous_source_events(ACADEMY_NEWSLETTER_SOURCE)
        academy_newsletter_events, _, _ = filter_events_to_rolling_window(
            academy_newsletter_events,
            reference=today,
        )
        source_status.append({
            "id": "academy-newsletter",
            "status": "cached" if academy_newsletter_events else "failed",
            "count": len(academy_newsletter_events),
            "unit": "events",
        })
    else:
        try:
            academy_newsletter_events = fetch_academy_newsletter(reference=today)
            academy_newsletter_events, _, _ = filter_events_to_rolling_window(
                academy_newsletter_events,
                reference=today,
            )
            source_status.append({
                "id": "academy-newsletter",
                "status": "live",
                "count": len(academy_newsletter_events),
                "unit": "events",
            })
        except Exception as exc:
            academy_newsletter_events = previous_source_events(ACADEMY_NEWSLETTER_SOURCE)
            academy_newsletter_events, _, _ = filter_events_to_rolling_window(
                academy_newsletter_events,
                reference=today,
            )
            source_status.append({
                "id": "academy-newsletter",
                "status": "cached" if academy_newsletter_events else "failed",
                "count": len(academy_newsletter_events),
                "unit": "events",
                "error": f"{type(exc).__name__}: {exc}",
            })

    academy_calendar_events, academy_newsletter_events = dedupe_academy_sources(
        academy_calendar_events,
        academy_newsletter_events,
    )

    # Montessori School of Champaign-Urbana: public Google Calendar
    # discovered from the school's own Import Google Calendar control.
    if offline:
        montessori_events = previous_source_events(MONTESSORI_SOURCE)
        montessori_events, _, _ = filter_events_to_rolling_window(
            montessori_events,
            reference=today,
        )
        source_status.append({
            "id": "montessori-calendar",
            "status": "cached" if montessori_events else "failed",
            "count": len(montessori_events),
            "unit": "events",
        })
    else:
        try:
            montessori_events = fetch_montessori_calendar(reference=today)
            montessori_events, _, _ = filter_events_to_rolling_window(
                montessori_events,
                reference=today,
            )
            source_status.append({
                "id": "montessori-calendar",
                "status": "live",
                "count": len(montessori_events),
                "unit": "events",
            })
        except Exception as exc:
            montessori_events = previous_source_events(MONTESSORI_SOURCE)
            montessori_events, _, _ = filter_events_to_rolling_window(
                montessori_events,
                reference=today,
            )
            source_status.append({
                "id": "montessori-calendar",
                "status": "cached" if montessori_events else "failed",
                "count": len(montessori_events),
                "unit": "events",
                "error": f"{type(exc).__name__}: {exc}",
            })

    # Countryside School (independent K-8) public Finalsite calendar.
    if offline:
        countryside_events = previous_source_events(COUNTRYSIDE_SOURCE)
        source_status.append({
            "id": "countryside-calendar",
            "status": "cached" if countryside_events else "live",
            "count": len(countryside_events),
            "unit": "events",
        })
    else:
        try:
            countryside_events = fetch_countryside_calendar()
            source_status.append({
                "id": "countryside-calendar",
                "status": "live",
                "count": len(countryside_events),
                "unit": "events",
            })
        except Exception as exc:
            countryside_events = previous_source_events(COUNTRYSIDE_SOURCE)
            source_status.append({
                "id": "countryside-calendar",
                "status": "cached" if countryside_events else "failed",
                "count": len(countryside_events),
                "unit": "events",
                "error": f"{type(exc).__name__}: {exc}",
            })

    # Youth programming from the two public libraries. Communico's public
    # age filters are treated like selectable household audiences; one event
    # may carry several age-band IDs but remains a single event record.
    library_events: list[dict] = []
    for library in LIBRARIES:
        if offline:
            source_events = previous_source_events(library.source)
            source_events, _, _ = filter_events_to_rolling_window(
                source_events,
                reference=today,
            )
            source_status.append({
                "id": library.log_id,
                "status": "cached" if source_events else "failed",
                "count": len(source_events),
                "unit": "events",
            })
        else:
            try:
                source_events = fetch_library_calendar(
                    library,
                    reference=today,
                )
                source_events, _, _ = filter_events_to_rolling_window(
                    source_events,
                    reference=today,
                )
                source_status.append({
                    "id": library.log_id,
                    "status": "live",
                    "count": len(source_events),
                    "unit": "events",
                })
            except Exception as exc:
                source_events = previous_source_events(library.source)
                source_events, _, _ = filter_events_to_rolling_window(
                    source_events,
                    reference=today,
                )
                source_status.append({
                    "id": library.log_id,
                    "status": "cached" if source_events else "failed",
                    "count": len(source_events),
                    "unit": "events",
                    "error": f"{type(exc).__name__}: {exc}",
                })

        library_events.extend(source_events)

    quest_meals: list[dict] = []
    for group_id, cfg in QUEST_GROUPS.items():
        if offline:
            group_meals = previous_group_meals(group_id)
            source_status.append({"id": cfg.log_id, "status": "cached", "count": len(group_meals), "unit": "menu days"})
        else:
            try:
                group_meals = fetch_quest_group(group_id)
                if not group_meals:
                    raise RuntimeError(f"Quest collector returned zero {group_id} menu records")
                source_status.append({"id": cfg.log_id, "status": "live", "count": len(group_meals), "unit": "menu days"})
            except Exception as exc:
                group_meals = previous_group_meals(group_id)
                source_status.append({
                    "id": cfg.log_id,
                    "status": "cached" if group_meals else "failed",
                    "count": len(group_meals),
                    "unit": "menu days",
                    "error": f"{type(exc).__name__}: {exc}",
                })
        quest_meals.extend(group_meals)

    # Unit 4's public School Nutrition & Fitness application is collected
    # independently.  On a transient failure, preserve every previously live
    # Unit 4 group instead of falling back to predictions immediately.
    cached_unit4 = [
        meal
        for group_id in UNIT4_GROUPS
        for meal in previous_group_meals(group_id)
    ]
    if offline:
        unit4_meals = cached_unit4
        source_status.append({
            "id": "unit4-menus",
            "status": "cached" if unit4_meals else "failed",
            "count": len(unit4_meals),
            "unit": "menu days",
        })
    else:
        try:
            unit4_meals = fetch_unit4_menus()
            if not unit4_meals:
                raise RuntimeError("Unit 4 collector returned zero menu records")
            source_status.append({
                "id": "unit4-menus",
                "status": "live",
                "count": len(unit4_meals),
                "unit": "menu days",
            })
        except Exception as exc:
            unit4_meals = cached_unit4
            source_status.append({
                "id": "unit4-menus",
                "status": "cached" if unit4_meals else "failed",
                "count": len(unit4_meals),
                "unit": "menu days",
                "error": f"{type(exc).__name__}: {exc}",
            })

    event_candidates = (
        static_events
        + snap_events
        + usd116_calendar_events
        + usd116_school_events
        + unit4_school_events
        + stm_events
        + stmatthew_events
        + stjohn_events
        + judah_calendar_events
        + judah_athletics_events
        + academy_calendar_events
        + academy_newsletter_events
        + montessori_events
        + countryside_events
        + library_events
    )

    event_candidates, unit4_district_duplicates_removed = (
        collapse_unit4_district_duplicates(
            event_candidates,
            unit4_district_events,
        )
    )
    print(
        "u4-district-calendar detail: removed "
        f"{unit4_district_duplicates_removed} exact Unit 4 duplicate records "
        "in favor of district records"
    )

    events = merge_unique(event_candidates + unit4_district_events)
    unfiltered_event_count = len(events)
    events, window_start, window_end = filter_events_to_rolling_window(
        events,
        reference=today,
    )
    print(
        f"rolling-window detail: {window_start.isoformat()} through "
        f"{window_end.isoformat()}; kept {len(events)} of "
        f"{unfiltered_event_count} merged events"
    )

    meals = merge_meals(static_meals + quest_meals + unit4_meals)
    now = datetime.now(ZoneInfo("America/Chicago")).isoformat(timespec="seconds")
    return {
        "updated": now,
        "mode": "live-collector",
        "sources": source_status,
        "events": events,
        "meals": meals,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build schoolboard-data.json from public source collectors.")
    parser.add_argument("--offline", action="store_true", help="Do not request remote sources; retain cached live records.")
    args = parser.parse_args()
    payload = build(offline=args.offline)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for source in payload.get("sources", []):
        unit = source.get("unit", "records")
        print(f"{source['id']}: {source['status']} ({source['count']} {unit})")
        if source.get("error"):
            print(f"  {source['error']}")
    print(f"Wrote {len(payload['events'])} events and {len(payload['meals'])} live menu days to {OUTPUT_PATH.name}")


if __name__ == "__main__":
    main()
