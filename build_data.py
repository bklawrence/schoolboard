from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
    # Useful secondary dedupe across static/live sources.
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


def previous_source_events(source: str) -> list[dict]:
    old = load_json(OUTPUT_PATH, {})
    return [e for e in old.get("events", []) if e.get("source") == source]


def build(*, offline: bool = False) -> dict:
    static = load_json(STATIC_PATH, {"events": [], "meals": []})
    static_events = list(static.get("events", []))
    source_status = []

    live_events: list[dict] = []
    if offline:
        live_events = previous_source_events(UNI_SOURCE)
        source_status.append({"id": "uni-athletics", "status": "cached", "count": len(live_events)})
    else:
        try:
            live_events = fetch_uni_snap(UNI_SNAP_FEED)
            if not live_events:
                raise RuntimeError("Snap feed returned zero VEVENT records")
            source_status.append({"id": "uni-athletics", "status": "live", "count": len(live_events)})
        except Exception as exc:
            live_events = previous_source_events(UNI_SOURCE)
            source_status.append({
                "id": "uni-athletics",
                "status": "cached" if live_events else "failed",
                "count": len(live_events),
                "error": f"{type(exc).__name__}: {exc}",
            })

    events = merge_unique(static_events + live_events)
    now = datetime.now(ZoneInfo("America/Chicago")).isoformat(timespec="seconds")
    return {
        "updated": now,
        "mode": "live-collector",
        "sources": source_status,
        "events": events,
        "meals": static.get("meals", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build schoolboard-data.json from public source collectors.")
    parser.add_argument("--offline", action="store_true", help="Do not request remote sources; retain cached live records.")
    args = parser.parse_args()

    payload = build(offline=args.offline)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for source in payload.get("sources", []):
        print(f"{source['id']}: {source['status']} ({source['count']} events)")
    print(f"Wrote {len(payload['events'])} events to {OUTPUT_PATH.name}")


if __name__ == "__main__":
    main()
