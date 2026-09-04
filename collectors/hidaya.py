from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from urllib.request import Request, urlopen

import pymupdf


SCHOOL_ID = "hidaya"
SOURCE_NAME = "Hidaya Academy 2026–27 School Calendar"
SOURCE_URL = (
    "https://img1.wsimg.com/blobby/go/"
    "b4d8649e-57f1-45c0-8624-6e7936af069a/"
    "HACU-%202026-2027-%20Formatted%20Calendar%20V_FINAL_4-.pdf"
)

BASKETBALL_SOURCE_NAME = "Hidaya Basketball Clinic"
BASKETBALL_SOURCE_URL = "https://hidayaacademycu.com/hidaya-basketball"

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



_MONTHS = {
    name.casefold(): number
    for number, name in enumerate(
        (
            "",
            "January", "February", "March", "April",
            "May", "June", "July", "August",
            "September", "October", "November", "December",
        )
    )
    if name
}

_BASKETBALL_DATE_RE = re.compile(
    r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    r"(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|"
    r"Sep|Sept|Oct|Nov|Dec)"
    r"\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(?P<year>20\d{2}))?",
    re.I,
)

_BASKETBALL_NUMERIC_DATE_RE = re.compile(
    r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>20\d{2})\b"
)

_BASKETBALL_TIME_RE = re.compile(
    r"(?P<start>\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm))"
    r"(?:\s*(?:-|–|—|to)\s*"
    r"(?P<end>\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)))?"
)


def _clinic_clock(raw: str) -> str:
    value = re.sub(r"\s+", "", raw).upper()
    if ":" not in value:
        value = re.sub(r"(\d{1,2})(AM|PM)$", r"\1:00\2", value)
    value = re.sub(r"(AM|PM)$", r" \1", value)
    return datetime.strptime(value, "%I:%M %p").strftime("%H:%M")


def _clinic_year(
    *,
    month: int,
    explicit_year: str | None,
    reference: date,
) -> int:
    if explicit_year:
        return int(explicit_year)

    # Choose the nearest plausible school/community-program occurrence.
    candidates = [
        date(reference.year - 1, month, 1),
        date(reference.year, month, 1),
        date(reference.year + 1, month, 1),
    ]
    best = min(
        candidates,
        key=lambda candidate: abs((candidate - reference).days),
    )
    return best.year


def _parse_basketball_visible_text(
    text: str,
    *,
    reference: date,
) -> list[dict]:
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.replace("\r", "\n").split("\n")
        if re.sub(r"\s+", " ", line).strip()
    ]

    events: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for idx, line in enumerate(lines):
        date_match = _BASKETBALL_DATE_RE.search(line)
        numeric_match = _BASKETBALL_NUMERIC_DATE_RE.search(line)

        if not date_match and not numeric_match:
            continue

        if date_match:
            month_token = date_match.group("month").rstrip(".").casefold()
            month_map = {
                "jan": 1, "january": 1,
                "feb": 2, "february": 2,
                "mar": 3, "march": 3,
                "apr": 4, "april": 4,
                "may": 5,
                "jun": 6, "june": 6,
                "jul": 7, "july": 7,
                "aug": 8, "august": 8,
                "sep": 9, "sept": 9, "september": 9,
                "oct": 10, "october": 10,
                "nov": 11, "november": 11,
                "dec": 12, "december": 12,
            }
            month = month_map[month_token]
            day = int(date_match.group("day"))
            year = _clinic_year(
                month=month,
                explicit_year=date_match.group("year"),
                reference=reference,
            )
            matched_text = date_match.group(0)
        else:
            month = int(numeric_match.group("month"))
            day = int(numeric_match.group("day"))
            year = int(numeric_match.group("year"))
            matched_text = numeric_match.group(0)

        try:
            event_day = date(year, month, day)
        except ValueError:
            continue

        # Ignore dates very far away; this guards against footer/copyright or
        # unrelated historical content on the rendered page.
        if abs((event_day - reference).days) > 400:
            continue

        # Prefer descriptive text on the same line, otherwise inspect the
        # nearest non-navigation neighboring line. The page is specifically
        # a Hidaya basketball page, so a generic clinic title is safer than
        # guessing if no useful descriptor survives.
        remainder = re.sub(
            re.escape(matched_text),
            " ",
            line,
            count=1,
            flags=re.I,
        )
        remainder = _BASKETBALL_TIME_RE.sub(" ", remainder)
        remainder = re.sub(r"\s+", " ", remainder).strip(" -–—:|")

        boilerplate = {
            "applications for the 2026-2027 year are closed",
            "hidaya basketball",
            "basketball",
            "register",
            "registration",
            "sign up",
        }

        title = ""
        if remainder and remainder.casefold() not in boilerplate:
            title = remainder

        if not title:
            for neighbor_idx in (idx - 1, idx + 1):
                if not (0 <= neighbor_idx < len(lines)):
                    continue
                neighbor = lines[neighbor_idx]
                lower = neighbor.casefold()
                if (
                    lower in boilerplate
                    or "application" in lower
                    or "privacy" in lower
                    or "copyright" in lower
                    or _BASKETBALL_DATE_RE.search(neighbor)
                    or _BASKETBALL_NUMERIC_DATE_RE.search(neighbor)
                ):
                    continue
                if len(neighbor) <= 100:
                    title = neighbor
                    break

        if not title:
            title = "Hidaya Basketball Clinic"
        elif "basketball" not in title.casefold():
            title = f"Hidaya Basketball Clinic — {title}"

        time_match = _BASKETBALL_TIME_RE.search(line)
        if not time_match:
            for neighbor_idx in (idx + 1, idx - 1):
                if 0 <= neighbor_idx < len(lines):
                    time_match = _BASKETBALL_TIME_RE.search(lines[neighbor_idx])
                    if time_match:
                        break

        start = ""
        end = ""
        if time_match:
            start = _clinic_clock(time_match.group("start"))
            if time_match.group("end"):
                end = _clinic_clock(time_match.group("end"))

        key = (event_day.isoformat(), start)
        if key in seen:
            continue
        seen.add(key)

        event = {
            "id": _event_id(
                event_day.isoformat(),
                f"{title}|{start}",
            ).replace("hidaya-calendar-", "hidaya-basketball-"),
            "title": title,
            "date": event_day.isoformat(),
            "schools": [SCHOOL_ID],
            "scope": "school",
            "category": "athletics",
            "source": BASKETBALL_SOURCE_NAME,
            "sourceUrl": BASKETBALL_SOURCE_URL,
        }

        if start:
            event["start"] = start
            if end:
                event["end"] = end
        else:
            event["allDay"] = True

        events.append(event)

    return sorted(
        events,
        key=lambda event: (
            event["date"],
            event.get("start", ""),
            event["title"],
        ),
    )


def fetch_hidaya_basketball(
    *,
    reference: date,
    timeout: int = 30,
) -> list[dict]:
    # GoDaddy renders the substantive clinic page client-side, so ordinary
    # urllib/HTML parsing sees almost none of the visible content.
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,2000")

    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(timeout)
        driver.get(BASKETBALL_SOURCE_URL)

        WebDriverWait(driver, timeout).until(
            lambda browser: len(
                browser.find_element(By.TAG_NAME, "body").text.strip()
            ) > 80
        )

        body_text = driver.find_element(By.TAG_NAME, "body").text
    finally:
        driver.quit()

    events = _parse_basketball_visible_text(
        body_text,
        reference=reference,
    )

    if not events:
        raise RuntimeError(
            "Hidaya basketball page loaded but no dated clinic entries "
            "were parsed from the visible page text"
        )

    print(
        "hidaya-basketball detail: rendered public clinic page; parsed "
        f"{len(events)} dated event(s)"
    )
    sample = "; ".join(
        f"{event['date']} {event['title']}"
        for event in events[:10]
    )
    print(f"hidaya-basketball detail: first events: {sample}")
    return events

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
