from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

CALENDAR_URL = "https://www.countrysideschool.org/calendar"
SOURCE_NAME = "Countryside School Calendar"
SCHOOL_ID = "countryside"

SPORT_CATEGORIES = {
    "athletics",
    "boys basketball",
    "girls basketball",
    "scholastic bowl",
    "soccer",
    "volleyball",
}
KNOWN_CATEGORIES = SPORT_CATEGORIES | {"admissions", "alumni"}

DAY_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2})$",
    re.I,
)
MONTH_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})$",
    re.I,
)
TIME_RANGE_RE = re.compile(
    r"^(\d{1,2}:\d{2}\s*[AP]M)\s*[-–]\s*(\d{1,2}:\d{2}\s*[AP]M)$",
    re.I,
)
ONE_TIME_RE = re.compile(r"^(\d{1,2}:\d{2}\s*[AP]M)$", re.I)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _compact(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _to_24h(value: str) -> str:
    return datetime.strptime(_compact(value).upper(), "%I:%M %p").strftime("%H:%M")


def _infer_year(month: int, visible_month: int, visible_year: int) -> int:
    # Month grids include a few days from the prior/next month.
    if visible_month == 1 and month == 12:
        return visible_year - 1
    if visible_month == 12 and month == 1:
        return visible_year + 1
    return visible_year


def _category_for(title: str, category_label: str | None) -> str:
    low_title = title.casefold()
    low_cat = (category_label or "").casefold()

    if low_cat in SPORT_CATEGORIES:
        return "athletics"
    if any(token in low_title for token in (
        "no school", "dismissal", "inservice", "first day", "no extended day",
    )):
        return "schedule"
    return "general"


def _slug(day: date, title: str, start: str | None) -> str:
    core = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:52]
    stamp = (start or "all-day").replace(":", "")
    return f"countryside-{day.isoformat()}-{stamp}-{core}"


def _parse_event_text(
    text: str,
    *,
    event_day: date,
    event_url: str | None = None,
) -> dict | None:
    lines = [_compact(line) for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None

    category_label = None
    if lines and lines[0].casefold() in KNOWN_CATEGORIES:
        category_label = lines.pop(0)
    if not lines:
        return None

    # Find time metadata anywhere in the event block.
    start = end = None
    time_index = None
    for i, line in enumerate(lines):
        m = TIME_RANGE_RE.match(line)
        if m:
            start = _to_24h(m.group(1))
            end = _to_24h(m.group(2))
            time_index = i
            break
        m = ONE_TIME_RE.match(line)
        if m:
            start = _to_24h(m.group(1))
            time_index = i
            break
        if line.casefold() == "all day":
            time_index = i
            break

    title_lines = lines if time_index is None else lines[:time_index]
    tail = [] if time_index is None else lines[time_index + 1:]

    # Finalsite event cards normally put title first. If a category label was
    # present, it has already been removed.
    if not title_lines:
        return None
    title = title_lines[0].strip(" -–")
    if not title:
        return None

    # The remaining text after the time is generally the location.
    location = " · ".join(tail[:2]) if tail else None

    event = {
        "id": _slug(event_day, title, start),
        "title": title,
        "date": event_day.isoformat(),
        "schools": [SCHOOL_ID],
        "scope": "school",
        "category": _category_for(title, category_label),
        "source": SOURCE_NAME,
        "sourceUrl": event_url or CALENDAR_URL,
    }
    if start:
        event["start"] = start
        if end:
            event["end"] = end
    else:
        event["allDay"] = True
    if location:
        event["location"] = location
    return event


def _month_label(driver) -> tuple[int, int]:
    # Prefer a compact visible heading matching "September 2026".
    candidates = driver.execute_script(
        """
        return Array.from(document.querySelectorAll('button,h1,h2,h3,h4,div,span'))
          .map(el => (el.innerText || '').trim())
          .filter(t => /^(January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{4}$/.test(t))
          .slice(0, 20);
        """
    ) or []

    for value in candidates:
        m = MONTH_RE.match(_compact(value))
        if m:
            return MONTHS[m.group(1).casefold()], int(m.group(2))
    raise RuntimeError("Countryside calendar month heading was not found")


def _extract_month(driver) -> list[dict]:
    visible_month, visible_year = _month_label(driver)

    raw = driver.execute_script(
        """
        const boxes = Array.from(document.querySelectorAll(
          '.fsCalendarDaybox, [class~="fsCalendarDaybox"]'
        ));

        return boxes.map(box => {
          const allText = (box.innerText || '').trim();
          const dateEl = box.querySelector(
            '.fsCalendarDayboxDate, [class~="fsCalendarDayboxDate"], time'
          );
          const dateText = dateEl ? (dateEl.innerText || '').trim() : '';

          let eventEls = Array.from(box.querySelectorAll('.fsCalendarEvent'));
          if (!eventEls.length) {
            eventEls = Array.from(box.querySelectorAll('article[class*="CalendarEvent"], li[class*="CalendarEvent"]'));
          }

          return {
            text: allText,
            dateText,
            events: eventEls.map(ev => ({
              text: (ev.innerText || '').trim(),
              href: (ev.querySelector('a[href]') || {}).href || ''
            }))
          };
        });
        """
    ) or []

    events: list[dict] = []
    for box in raw:
        text = str(box.get("text") or "")
        date_text = _compact(box.get("dateText"))

        if not DAY_RE.match(date_text):
            # Finalsite often keeps the complete weekday/date as the first
            # line of the day box even when its dedicated date element is terse.
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
            if DAY_RE.match(first_line):
                date_text = first_line

        m = DAY_RE.match(date_text)
        if not m:
            continue

        month = MONTHS[m.group(2).casefold()]
        year = _infer_year(month, visible_month, visible_year)
        event_day = date(year, month, int(m.group(3)))

        for raw_event in box.get("events") or []:
            event = _parse_event_text(
                raw_event.get("text") or "",
                event_day=event_day,
                event_url=raw_event.get("href") or CALENDAR_URL,
            )
            if event:
                events.append(event)

    if events or raw:
        return events

    # Diagnostic: if Finalsite changes class names, report enough information
    # to adjust without dumping the entire page.
    class_samples = driver.execute_script(
        """
        const values = new Set();
        for (const el of document.querySelectorAll('[class*="Calendar"],[class*="calendar"]')) {
          if (el.className && typeof el.className === 'string') values.add(el.className);
          if (values.size >= 25) break;
        }
        return Array.from(values);
        """
    ) or []
    raise RuntimeError(
        "Countryside calendar rendered but no calendar day boxes were found; "
        f"class samples={class_samples[:12]!r}"
    )


def _click_next_month(driver, old_label: tuple[int, int], *, timeout: float = 8.0) -> None:
    from selenium.webdriver.common.by import By

    buttons = driver.find_elements(By.TAG_NAME, "button")
    next_button = None
    for button in buttons:
        try:
            text = _compact(button.text)
            aria = _compact(button.get_attribute("aria-label"))
            title = _compact(button.get_attribute("title"))
        except Exception:
            continue
        if text == ">" or "next" in aria.casefold() or "next" in title.casefold():
            next_button = button
            break

    if next_button is None:
        raise RuntimeError("Countryside calendar next-month button was not found")

    driver.execute_script("arguments[0].click();", next_button)

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.35)
        try:
            if _month_label(driver) != old_label:
                return
        except Exception:
            pass
    raise RuntimeError("Countryside calendar did not advance to the next month")


def fetch_countryside_calendar(*, months: int = 12) -> list[dict]:
    """Collect the visible Countryside calendar month-by-month."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:
        raise RuntimeError("Selenium is required for Countryside calendar collection") from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1600,1400")
    options.add_argument("--lang=en-US")

    driver = webdriver.Chrome(options=options)
    try:
        driver.get(CALENDAR_URL)

        # Give Finalsite's calendar widget a moment to render.
        deadline = time.time() + 12
        while time.time() < deadline:
            time.sleep(0.4)
            try:
                _month_label(driver)
                break
            except Exception:
                continue
        else:
            raise RuntimeError("Countryside calendar page loaded but calendar widget did not render")

        collected: list[dict] = []
        labels_seen: list[str] = []

        for i in range(max(1, months)):
            month_no, year = _month_label(driver)
            labels_seen.append(f"{year:04d}-{month_no:02d}")
            collected.extend(_extract_month(driver))
            if i < months - 1:
                _click_next_month(driver, (month_no, year))

        # Month grids repeat trailing/leading days, so collapse by stable ID.
        by_id: dict[str, dict] = {}
        for event in collected:
            by_id[event["id"]] = event

        events = sorted(
            by_id.values(),
            key=lambda e: (e.get("date", ""), e.get("start", ""), e.get("title", "")),
        )

        print(
            f"countryside-calendar detail: collected {len(events)} events "
            f"across {len(labels_seen)} months; "
            f"months {', '.join(labels_seen[:3])}"
            + (f" ... {labels_seen[-1]}" if len(labels_seen) > 3 else "")
        )
        return events
    finally:
        driver.quit()
