from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from collectors.quest import SOURCE_NAME as QUEST_SOURCE, fetch_yankee_k5
from collectors.snap import fetch_uni_snap

ROOT = Path(__file__).resolve().parent
STATIC_PATH = ROOT / "data" / "static-events.json"
OUTPUT_PATH = ROOT / "schoolboard-data.json"

UNI_SNAP_FEED = "https://manage.snap.app/ical/link/Uni/2026-2027/ALL/CDT/athletic%2Cactivities/0/0"
UNI_SOURCE = "Uni High Athletics — Snap!"


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


def previous_source_meals(source: str) -> list[dict]:
    old = load_json(OUTPUT_PATH, {})
    return [m for m in old.get("meals", []) if m.get("source") == source]


def build(*, offline: bool = False) -> dict:
    static = load_json(STATIC_PATH, {"events": [], "meals": []})
    static_events = list(static.get("events", []))
    static_meals = list(static.get("meals", []))
    source_status = []

    # Uni High athletics: direct public iCalendar feed.
    if offline:
        snap_events = previous_source_events(UNI_SOURCE)
        source_status.append({"id": "uni-athletics", "status": "cached", "count": len(snap_events), "unit": "events"})
    else:
        try:
            snap_events = fetch_uni_snap(UNI_SNAP_FEED)
            if not snap_events:
                raise RuntimeError("Snap feed returned zero VEVENT records")
            source_status.append({"id": "uni-athletics", "status": "live", "count": len(snap_events), "unit": "events"})
        except Exception as exc:
            snap_events = previous_source_events(UNI_SOURCE)
            source_status.append({
                "id": "uni-athletics",
                "status": "cached" if snap_events else "failed",
                "count": len(snap_events),
                "unit": "events",
                "error": f"{type(exc).__name__}: {exc}",
            })

    # USD 116 K–5 lunches: load the public MySchoolQuest page in headless Chrome
    # and inspect the same JSON responses used by the browser.
    if offline:
        quest_meals = previous_source_meals(QUEST_SOURCE)
        source_status.append({"id": "quest-k5", "status": "cached", "count": len(quest_meals), "unit": "menu days"})
    else:
        try:
            quest_meals = fetch_yankee_k5()
            if not quest_meals:
                raise RuntimeError("Quest collector returned zero dated menu records")
            source_status.append({"id": "quest-k5", "status": "live", "count": len(quest_meals), "unit": "menu days"})
        except Exception as exc:
            quest_meals = previous_source_meals(QUEST_SOURCE)
            source_status.append({
                "id": "quest-k5",
                "status": "cached" if quest_meals else "failed",
                "count": len(quest_meals),
                "unit": "menu days",
                "error": f"{type(exc).__name__}: {exc}",
            })

    events = merge_unique(static_events + snap_events)
    meals = merge_meals(static_meals + quest_meals)
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
