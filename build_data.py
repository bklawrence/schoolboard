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
from collectors.usd116_calendar import SOURCE_NAME as USD116_CALENDAR_SOURCE, fetch_usd116_calendar
from collectors.usd116_schoolfeeds import SCHOOL_FEEDS, SOURCE_PREFIX as USD116_SCHOOLFEED_PREFIX, fetch_school_feed
from collectors.unit4_events import UNIT4_CALENDARS, fetch_unit4_calendar

ROOT = Path(__file__).resolve().parent
STATIC_PATH = ROOT / "data" / "static-events.json"
OUTPUT_PATH = ROOT / "schoolboard-data.json"

# Keep a modest amount of recent history for debugging/context and a little
# more than one year ahead so newly published next-school-year calendars
# appear automatically. These bounds move forward on every build.
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
)


def normalized_event_title(title: str) -> str:
    clean = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
    clean = re.sub(r"\.pdf\s*$", "", clean).strip(" -–:,.")

    for phrase in SEMANTIC_EVENT_PHRASES:
        if phrase in clean:
            return phrase

    clean = re.sub(r"[^a-z0-9]+", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


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
        key = event_key(event)
        if key not in best_by_key:
            best_by_key[key] = event
            order.append(key)
            continue
        if _event_quality(event) < _event_quality(best_by_key[key]):
            best_by_key[key] = event

    result = [best_by_key[key] for key in order]
    return sorted(result, key=lambda e: (e.get("date", ""), e.get("start", ""), e.get("title", "")))


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

    events = merge_unique(
        static_events
        + snap_events
        + usd116_calendar_events
        + usd116_school_events
        + unit4_school_events
        + countryside_events
    )
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
