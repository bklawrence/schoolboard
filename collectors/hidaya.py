from __future__ import annotations

import hashlib
import re
from datetime import date
from urllib.request import Request, urlopen

import pymupdf


SCHOOL_ID = "hidaya"
SOURCE_NAME = "Hidaya Academy 2026–27 School Calendar"
SOURCE_URL = (
    "https://img1.wsimg.com/blobby/go/"
    "b4d8649e-57f1-45c0-8624-6e7936af069a/"
    "HACU-%202026-2027-%20Formatted%20Calendar%20V_FINAL_4-.pdf"
)

EXPECTED_SCHOOL_YEAR = "2026-2027"
EXPECTED_UPDATE = "4-20-2026"

# This calendar is a designed 2-page PDF whose colored month-grid semantics
# do not map cleanly to ordinary text extraction. We therefore transcribe the
# dated entries from the current published revision, while fetching and
# validating the public PDF every run. If Hidaya replaces or revises the PDF,
# the revision guard fails visibly rather than silently serving stale dates.
#
# Tuple shape:
# (start_date, end_date, title, category, start_time, end_time, detail)
_EVENT_SPECS = [
    (
        "2026-08-03", "2026-08-11",
        "Teacher Institute Week", "schedule", "", "",
        "No school for students."
    ),
    (
        "2026-08-08", "",
        "Meet the Teachers / Parent Orientation / Supply Drop-off",
        "general", "16:00", "19:00", ""
    ),
    (
        "2026-08-12", "",
        "First Day of School", "schedule", "", "",
        "Half day for PK & KG."
    ),
    (
        "2026-09-01", "2026-09-04",
        "MAP Testing", "general", "", "",
        "Bi-annual student assessments; regular school-day schedule."
    ),
    (
        "2026-09-07", "",
        "Labor Day — No School", "schedule", "", "",
        "School closed; no school for students or teachers."
    ),
    (
        "2026-09-25", "",
        "Career Day", "general", "", "", ""
    ),
    (
        "2026-10-12", "",
        "End of Quarter 1", "general", "", "",
        "The academic quarter comes to a close; no impact on the school day."
    ),
    (
        "2026-10-16", "",
        "Early Dismissal", "schedule", "", "",
        "Student pick-up at 1 PM before Jummah prayer."
    ),
    (
        "2026-10-22", "2026-10-23",
        "Teacher Institute Days", "schedule", "", "",
        "No school for students."
    ),
    (
        "2026-10-26", "",
        "Parent Teacher Conferences", "schedule", "", "",
        "No school for students; parent meetings with teachers by appointment."
    ),
    (
        "2026-11-12", "",
        "Picture Day", "general", "", "", ""
    ),
    (
        "2026-11-24", "",
        "Teacher Appreciation Event by PTO", "general", "", "", ""
    ),
    (
        "2026-11-25", "2026-11-27",
        "Fall Break", "schedule", "", "",
        "School closed."
    ),
    (
        "2026-12-07", "2026-12-11",
        "Spirit Week", "general", "", "", ""
    ),
    (
        "2026-12-18", "",
        "End of Quarter 2 / Early Dismissal", "schedule", "", "",
        "Student pick-up at 1 PM before Jummah prayer."
    ),
    (
        "2026-12-21", "2026-12-31",
        "Winter Break", "schedule", "", "",
        "School closed."
    ),
    (
        "2027-01-01", "",
        "Winter Break", "schedule", "", "",
        "School closed."
    ),
    (
        "2027-01-04", "",
        "Teacher Institute Day", "schedule", "", "",
        "No school for students."
    ),
    (
        "2027-01-18", "",
        "Dr. MLK Jr Day / Muhammad Ali Observance", "schedule", "", "",
        "School closed."
    ),
    (
        "2027-01-29", "",
        "Hidaya Reading Day", "general", "", "", ""
    ),
    (
        "2027-02-08", "",
        "First Day of Ramadan Schedule", "schedule", "", "",
        "Reduced school hours; notice will be sent in advance of change."
    ),
    (
        "2027-02-19", "",
        "Black History Fair", "general", "", "", ""
    ),
    (
        "2027-02-26", "",
        "Early Dismissal", "schedule", "", "",
        "Student pick-up at 1 PM before Jummah prayer."
    ),
    (
        "2027-02-27", "",
        "Ramadan Around the World (Family Iftar)", "general", "", "", ""
    ),
    (
        "2027-03-01", "2027-03-12",
        "Ramadan and Eid Break", "schedule", "", "",
        "School closed."
    ),
    (
        "2027-03-09", "",
        "Eid Al-Fitr", "schedule", "", "",
        "School closed."
    ),
    (
        "2027-03-15", "",
        "Teacher Institute Day", "schedule", "", "",
        "No school for students."
    ),
    (
        "2027-03-19", "",
        "Eid Celebration at School", "general", "", "", ""
    ),
    (
        "2027-03-22", "",
        "End of Quarter 3", "general", "", "",
        "The academic quarter comes to a close; no impact on the school day."
    ),
    (
        "2027-04-05", "",
        "Parent Teacher Conferences", "schedule", "", "",
        "No school for students; parent meetings with teachers by appointment."
    ),
    (
        "2027-04-16", "",
        "Art Gallery at School", "general", "", "", ""
    ),
    (
        "2027-04-30", "",
        "Science Fair", "general", "17:00", "19:00", ""
    ),
    (
        "2027-05-03", "",
        "Teacher Appreciation Event by PTO", "general", "", "", ""
    ),
    (
        "2027-05-04", "2027-05-07",
        "MAP Testing", "general", "", "",
        "Bi-annual student assessments; regular school-day schedule."
    ),
    (
        "2027-05-09", "",
        "MAS–Hidaya Hajj Event", "general", "", "", ""
    ),
    (
        "2027-05-17", "2027-05-19",
        "Eid Al Adha Break", "schedule", "", "",
        "School closed."
    ),
    (
        "2027-05-27", "",
        "End of Year Ceremony", "general", "17:00", "19:30",
        "Time is tentative."
    ),
    (
        "2027-05-28", "",
        "Last Day of School / Early Dismissal", "schedule", "", "",
        "Student pick-up at 1 PM before Jummah prayer."
    ),
    (
        "2027-05-31", "",
        "Memorial Day — No School", "schedule", "", "",
        "School closed."
    ),
    (
        "2027-06-01", "2027-06-04",
        "Snow Make-up Days / Teacher Institute Week", "schedule", "", "",
        "Reserved as make-up days due to weather or unforeseen closures."
    ),
]


def _request_pdf(
    *,
    timeout: int = 30,
    opener=urlopen,
) -> bytes:
    request = Request(
        SOURCE_URL,
        headers={
            "User-Agent": (
                "ChambanaSchoolboard/1.0 "
                "(+public school calendar aggregator)"
            ),
            "Accept": "application/pdf,*/*;q=0.8",
        },
    )
    with opener(request, timeout=timeout) as response:
        return response.read()


def _normalized_pdf_text(pdf_bytes: bytes) -> str:
    document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    text = "\n".join(page.get_text("text") for page in document)
    text = text.replace("–", "-").replace("—", "-").replace("’", "'")
    return re.sub(r"\s+", " ", text).strip()


def _validate_current_revision(text: str) -> None:
    checks = [
        "Hidaya Academy of Champaign-Urbana",
        "2026- 2027 School Calendar",
        "Updated 4-20-2026",
        "Labor Day",
        "Ramadan",
        "Last Day of School",
    ]
    missing = [item for item in checks if item not in text]
    if missing:
        raise RuntimeError(
            "Hidaya calendar PDF changed or no longer matches the "
            "transcribed 2026-27 revision; missing markers: "
            + ", ".join(missing)
        )


def _event_id(start_date: str, title: str) -> str:
    key = f"{start_date}|{title.casefold()}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"hidaya-calendar-{digest}"


def _events_from_specs() -> list[dict]:
    events: list[dict] = []

    for (
        start_date,
        end_date,
        title,
        category,
        start_time,
        end_time,
        detail,
    ) in _EVENT_SPECS:
        # Validate the transcription as real calendar dates at import/run time.
        date.fromisoformat(start_date)
        if end_date:
            date.fromisoformat(end_date)

        event = {
            "id": _event_id(start_date, title),
            "title": title,
            "date": start_date,
            "schools": [SCHOOL_ID],
            "scope": "school",
            "category": category,
            "source": SOURCE_NAME,
            "sourceUrl": SOURCE_URL,
        }

        if end_date:
            event["endDate"] = end_date
            event["allDay"] = True
        elif start_time:
            event["start"] = start_time
            if end_time:
                event["end"] = end_time
        else:
            event["allDay"] = True

        if detail:
            event["detail"] = detail

        events.append(event)

    return sorted(
        events,
        key=lambda event: (
            event["date"],
            event.get("start", ""),
            event["title"],
        ),
    )




def fetch_hidaya_calendar(
    *,
    reference: date,
    timeout: int = 30,
    opener=urlopen,
) -> list[dict]:
    pdf_bytes = _request_pdf(timeout=timeout, opener=opener)
    text = _normalized_pdf_text(pdf_bytes)
    _validate_current_revision(text)

    events = _events_from_specs()

    print(
        "hidaya-calendar detail: validated public 2026-27 PDF revision "
        f"{EXPECTED_UPDATE}; loaded {len(events)} dated events"
    )
    sample = "; ".join(
        f"{event['date']} {event['title']}"
        for event in events[:10]
    )
    print(f"hidaya-calendar detail: first events: {sample}")

    return events
