from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from collectors.quest import QUEST_GROUPS, fetch_quest_group
from collectors.snap import fetch_snap
from collectors.unit4 import UNIT4_GROUPS, fetch_unit4_menus
from collectors.usd116_calendar import SOURCE_NAME as USD116_CALENDAR_SOURCE, fetch_usd116_calendar
from collectors.usd116_schoolfeeds import SCHOOL_FEEDS, SOURCE_PREFIX as USD116_SCHOOLFEED_PREFIX, fetch_school_feed

ROOT = Path(__file__).resolve().parent
STATIC_PATH = ROOT / "data" / "static-events.json"
OUTPUT_PATH = ROOT / "schoolboard-data.json"

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


def event_key(event: dict) -> tuple:
    return (
        event.get("date", ""),
        event.get("start", ""),
        event.get("title", "").strip().casefold(),
        tuple(sorted(event.get("schools") or [])),
    )


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
    seen: set[tuple] = set()
    result: list[dict] = []
    for event in merged:
        key = event_key(event)
        if key in seen:
            continue
        seen.add(key)
        result.append(event)
    return sorted(result, key=lambda e: (e.get("date", ""), e.get("start", ""), e.get("title", "")))


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
    today = datetime.now().date()

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
                fresh = fetch_school_feed(school, reference=today)
                source_events = merge_unique(cached_future + fresh)
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

    events = merge_unique(static_events + snap_events + usd116_calendar_events + usd116_school_events)
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
