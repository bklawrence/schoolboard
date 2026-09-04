from __future__ import annotations

from datetime import date


EARLY_SCHOOL_ID = "nextgen-early"

EARLY_SOURCE_NAME = "Next Generation 2026 Early Education / Preschool / TK Closures"
EARLY_SOURCE_URL = (
    "https://static1.squarespace.com/static/"
    "693edc3939d5b136adbb1a2b/t/6944534618065465ccf37221/"
    "1766085446399/Closed+Days+2026.jpg"
)

# This source explicitly applies to Next Generation Early Education,
# Next Generation Preschool, and Next Generation Transitional Kindergarten,
# which operate on a year-round calendar.
_CLOSURES = [
    ("2026-01-01", "Winter Holiday"),
    ("2026-01-02", "Winter Holiday"),
    ("2026-02-16", "Teacher In-Service"),
    ("2026-04-03", "Spring Holiday"),
    ("2026-05-22", "Memorial Day Holiday"),
    ("2026-05-25", "Memorial Day Holiday"),
    ("2026-06-19", "Juneteenth"),
    ("2026-07-03", "Fourth of July"),
    ("2026-09-04", "Teacher In-Service"),
    ("2026-09-07", "Labor Day"),
    ("2026-11-26", "Thanksgiving Day"),
    ("2026-11-27", "Friday after Thanksgiving Day"),
    ("2026-12-24", "Winter Holiday"),
    ("2026-12-25", "Winter Holiday"),
    ("2026-12-31", "Winter Holiday"),
    ("2027-01-01", "Winter Holiday"),
]


def fetch_nextgen_early_closures(*, reference: date | None = None) -> list[dict]:
    # The dates are transcribed from Next Generation's public 2026 closure
    # notice rather than inferred from its older 2025-26 primary/middle
    # calendar. build_data.py applies the normal rolling window afterward.
    events = []
    for day, reason in _CLOSURES:
        events.append({
            "id": f"nextgen-early-closure-{day}",
            "title": f"No School / Closed — {reason}",
            "date": day,
            "schools": [EARLY_SCHOOL_ID],
            "scope": "school",
            "category": "schedule",
            "allDay": True,
            "source": EARLY_SOURCE_NAME,
            "sourceUrl": EARLY_SOURCE_URL,
        })

    print(
        "nextgen-early-closures detail: loaded "
        f"{len(events)} dates from Next Generation's public 2026 "
        "Early Education / Preschool / TK closure schedule"
    )
    return events
