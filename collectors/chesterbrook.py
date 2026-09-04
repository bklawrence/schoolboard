from __future__ import annotations

import calendar as calendar_mod
import hashlib
import html as html_lib
import io
import re
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import fitz
from pypdf import PdfReader


SCHOOL_ID = "chesterbrook"
MENU_GROUP = "chesterbrook"

PARENTS_PAGE = (
    "https://www.chesterbrookacademy.com/preschools/il/champaign/"
    "uiuc/parents/calendars-menu/"
)

CALENDAR_SOURCE_NAME = "Chesterbrook Academy School-Year Calendar"
EVENTS_SOURCE_NAME = "Chesterbrook Academy Website Calendar"
MENU_SOURCE_NAME = "Chesterbrook Academy Monthly Menu"

_MONTHS = {
    name.casefold(): number
    for number, name in enumerate(calendar_mod.month_name)
    if name
}

_FULL_DATE_RE = re.compile(
    r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+"
    r"(?P<day>\d{1,2}),\s+(?P<year>\d{4})$",
    re.I,
)

_TIME_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}\s*(?:am|pm))"
    r"\s*(?:-|–|—|to)\s*"
    r"(?P<end>\d{1,2}:\d{2}\s*(?:am|pm))",
    re.I,
)

_ANNUAL_ROW_RE = re.compile(
    r"^(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+"
    r"(?P<day>\d{1,2})(?P<suffix>st|nd|rd|th)?"
    r"(?:-(?P<endday>\d{1,2}))?\s*\|\s*(?P<title>.+)$",
    re.I,
)

_NUMERIC_RANGE_RE = re.compile(
    r"^(?P<m1>\d{1,2})/(?P<d1>\d{1,2})/(?P<y1>\d{2,4})"
    r"-(?P<m2>\d{1,2})/(?P<d2>\d{1,2})/(?P<y2>\d{2,4})"
    r"\s*\|\s*(?P<title>.+)$"
)

_YEAR_RANGE_RE = re.compile(r"(20\d{2})\s*[–-]\s*(20\d{2})")


class _PageParser(HTMLParser):
    BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "div", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "main", "nav",
        "p", "section", "td", "th", "tr",
    }
    SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip_depth = 0
        self._href: str | None = None
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        attrs_d = dict(attrs)
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

        if tag == "a":
            self._href = attrs_d.get("href")
            self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return

        if tag == "a" and self._href:
            label = re.sub(r"\s+", " ", "".join(self._anchor_parts)).strip()
            self.links.append((label, self._href))
            self._href = None
            self._anchor_parts = []

        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.parts.append(data)
        if self._href:
            self._anchor_parts.append(data)

    def visible_lines(self) -> list[str]:
        text = html_lib.unescape("".join(self.parts)).replace("\xa0", " ")
        lines = []
        for raw in text.splitlines():
            line = re.sub(r"\s+", " ", raw).strip(" \t•")
            if line:
                lines.append(line)
        return lines


def _request_bytes(
    url: str,
    *,
    timeout: int = 30,
    opener=urlopen,
    accept: str = "*/*",
) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "ChambanaSchoolboard/1.0 "
                "(+public school calendar aggregator)"
            ),
            "Accept": accept,
        },
    )
    with opener(request, timeout=timeout) as response:
        return response.read()


def _request_text(
    url: str,
    *,
    timeout: int = 30,
    opener=urlopen,
) -> str:
    raw = _request_bytes(
        url,
        timeout=timeout,
        opener=opener,
        accept="text/html,application/xhtml+xml,*/*;q=0.8",
    )
    return raw.decode("utf-8", errors="replace")


def _parse_parent_page(page_html: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(page_html)
    return parser


def _discover_links(
    page_html: str,
    *,
    reference: date,
) -> tuple[str, str]:
    parser = _parse_parent_page(page_html)

    calendar_candidates: list[tuple[int, str]] = []
    menu_candidates: list[tuple[int, str]] = []

    for label, href in parser.links:
        absolute = urljoin(PARENTS_PAGE, href)
        combined = f"{label} {href}".casefold()

        if ".pdf" not in combined:
            continue

        if "calendar" in combined and (
            "school year" in combined
            or "school-calendar" in combined
            or "school_calendar" in combined
        ):
            score = 0
            if str(reference.year)[2:] in combined:
                score += 2
            if str(reference.year + 1)[2:] in combined:
                score += 1
            calendar_candidates.append((score, absolute))

        if "menu" in combined:
            score = 0
            month_name = calendar_mod.month_name[reference.month].casefold()
            month_abbr = calendar_mod.month_abbr[reference.month].casefold()
            if month_name in combined or month_abbr in combined:
                score += 4
            if str(reference.year) in combined:
                score += 3

            for month_num in range(1, 13):
                name = calendar_mod.month_name[month_num]
                match = re.search(
                    rf"\b{name}\b.*?\b(20\d{{2}})\b",
                    label,
                    re.I,
                )
                if match:
                    candidate_year = int(match.group(1))
                    delta = abs(
                        (candidate_year * 12 + month_num)
                        - (reference.year * 12 + reference.month)
                    )
                    score += max(0, 6 - delta)
                    break
            menu_candidates.append((score, absolute))

    if not calendar_candidates:
        raise RuntimeError(
            "Chesterbrook parents page exposed no school-year calendar PDF"
        )
    if not menu_candidates:
        raise RuntimeError(
            "Chesterbrook parents page exposed no menu PDF"
        )

    calendar_url = max(calendar_candidates, key=lambda item: item[0])[1]
    menu_url = max(menu_candidates, key=lambda item: item[0])[1]
    return calendar_url, menu_url


def _category(title: str) -> str:
    text = str(title or "").casefold()
    schedule_terms = (
        "school closed",
        "closed",
        "staff training",
        "early closure",
        "first day",
        "last day",
        "holiday",
        "break",
    )
    return "schedule" if any(term in text for term in schedule_terms) else "general"


def _event_id(prefix: str, day: date, title: str, start: str = "") -> str:
    key = f"{day.isoformat()}|{start}|{title.casefold()}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _parse_clock(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip().upper()
    normalized = re.sub(r"\s*(AM|PM)$", r" \1", normalized)
    return datetime.strptime(
        normalized,
        "%I:%M %p",
    ).strftime("%H:%M")


def _school_year_for_month(
    *,
    month: int,
    start_year: int,
    end_year: int,
) -> int:
    return start_year if month >= 7 else end_year


def _canonical_annual_title(section: str, raw_title: str) -> str:
    title = re.sub(r"\s+", " ", raw_title).strip()

    if section == "closed":
        return f"{title} — School Closed"

    if section == "early":
        return "Early Closure"

    return title


def parse_school_year_calendar_text(
    text: str,
    *,
    source_url: str,
) -> list[dict]:
    year_match = _YEAR_RANGE_RE.search(text)
    if not year_match:
        raise RuntimeError(
            "Chesterbrook annual calendar did not expose a school-year range"
        )
    start_year = int(year_match.group(1))
    end_year = int(year_match.group(2))

    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.replace("\r", "\n").split("\n")
        if re.sub(r"\s+", " ", line).strip()
    ]

    section = ""
    events: list[dict] = []

    for line in lines:
        lower = line.casefold()
        if lower == "school closed":
            section = "closed"
            continue
        if lower == "early release days":
            section = "early"
            continue
        if lower == "special school events":
            section = "special"
            continue
        if lower == "break camps":
            section = "camps"
            continue
        if lower == "parent teacher conferences":
            section = "conferences"
            continue

        if section not in {"closed", "early", "special", "camps"}:
            continue

        numeric = _NUMERIC_RANGE_RE.match(line)
        if numeric and section == "camps":
            y1 = int(numeric.group("y1"))
            y2 = int(numeric.group("y2"))
            if y1 < 100:
                y1 += 2000
            if y2 < 100:
                y2 += 2000
            start_day = date(
                y1,
                int(numeric.group("m1")),
                int(numeric.group("d1")),
            )
            end_day = date(
                y2,
                int(numeric.group("m2")),
                int(numeric.group("d2")),
            )
            title = numeric.group("title").strip()
            events.append({
                "id": _event_id("chesterbrook-calendar", start_day, title),
                "title": title,
                "date": start_day.isoformat(),
                "endDate": end_day.isoformat(),
                "schools": [SCHOOL_ID],
                "scope": "school",
                "category": "general",
                "allDay": True,
                "source": CALENDAR_SOURCE_NAME,
                "sourceUrl": source_url,
            })
            continue

        match = _ANNUAL_ROW_RE.match(line)
        if not match:
            continue

        month = _MONTHS[match.group("month").casefold()]
        year = _school_year_for_month(
            month=month,
            start_year=start_year,
            end_year=end_year,
        )
        day = int(match.group("day"))
        endday = (
            int(match.group("endday"))
            if match.group("endday")
            else None
        )
        raw_title = match.group("title").strip()
        start_day = date(year, month, day)

        title = _canonical_annual_title(section, raw_title)
        event = {
            "id": _event_id("chesterbrook-calendar", start_day, title),
            "title": title,
            "date": start_day.isoformat(),
            "schools": [SCHOOL_ID],
            "scope": "school",
            "category": _category(title),
            "source": CALENDAR_SOURCE_NAME,
            "sourceUrl": source_url,
        }

        if endday:
            event["allDay"] = True
            event["endDate"] = date(year, month, endday).isoformat()
        elif section == "early":
            clock_match = re.search(
                r"Closes at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm))",
                raw_title,
                re.I,
            )
            if clock_match:
                raw_clock = clock_match.group(1)
                if ":" not in raw_clock:
                    raw_clock = re.sub(
                        r"(\d{1,2})(\s*(?:am|pm))",
                        r"\1:00\2",
                        raw_clock,
                        flags=re.I,
                    )
                event["start"] = _parse_clock(raw_clock)
                event["detail"] = raw_title
            else:
                event["allDay"] = True
        else:
            event["allDay"] = True

        events.append(event)

    if len(events) < 20:
        raise RuntimeError(
            "Chesterbrook annual calendar text was readable, but only "
            f"{len(events)} dated events were parsed"
        )

    unique: dict[tuple, dict] = {}
    for event in events:
        key = (
            event["date"],
            event.get("start", ""),
            re.sub(
                r"[^a-z0-9]+",
                " ",
                event["title"].casefold(),
            ).strip(),
        )
        unique[key] = event

    return sorted(
        unique.values(),
        key=lambda event: (
            event["date"],
            event.get("start", ""),
            event["title"],
        ),
    )


def fetch_chesterbrook_calendar(
    *,
    reference: date,
    timeout: int = 30,
    opener=urlopen,
) -> list[dict]:
    page_html = _request_text(
        PARENTS_PAGE,
        timeout=timeout,
        opener=opener,
    )
    calendar_url, _ = _discover_links(
        page_html,
        reference=reference,
    )

    pdf_bytes = _request_bytes(
        calendar_url,
        timeout=timeout,
        opener=opener,
        accept="application/pdf,*/*;q=0.8",
    )
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join(
        page.extract_text() or ""
        for page in reader.pages
    )

    events = parse_school_year_calendar_text(
        text,
        source_url=calendar_url,
    )
    print(
        "chesterbrook-calendar detail: annual PDF parsed "
        f"{len(events)} dated events"
    )
    if events:
        sample = "; ".join(
            f"{event['date']} {event['title']}"
            for event in events[:10]
        )
        print(
            "chesterbrook-calendar detail: first events: "
            + sample
        )
    return events


def _normalize_site_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip(" -–—")
    if re.search(
        r"labor day.*school closed",
        title,
        re.I,
    ):
        return "Labor Day — School Closed"
    return title


def parse_website_calendar_html(
    page_html: str,
) -> list[dict]:
    parser = _parse_parent_page(page_html)
    lines = parser.visible_lines()

    ignore = {
        "select",
        "sunday",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "calendars & menu",
    }

    events: list[dict] = []
    date_indices = [
        idx
        for idx, line in enumerate(lines)
        if _FULL_DATE_RE.match(line)
    ]

    for pos, idx in enumerate(date_indices):
        match = _FULL_DATE_RE.match(lines[idx])
        if not match:
            continue

        event_day = date(
            int(match.group("year")),
            _MONTHS[match.group("month").casefold()],
            int(match.group("day")),
        )

        next_idx = (
            date_indices[pos + 1]
            if pos + 1 < len(date_indices)
            else len(lines)
        )

        segment = []
        for line in lines[idx + 1:next_idx]:
            clean = re.sub(r"\s+", " ", line).strip()
            lower = clean.casefold()
            if not clean:
                continue
            if clean.isdigit():
                continue
            if lower in ignore:
                continue
            if lower.startswith("chesterbrook academy"):
                break
            if (
                "hours of operation" in lower
                or "license #" in lower
            ):
                break
            segment.append(clean)

        used: set[int] = set()

        for line_idx, line in enumerate(segment):
            time_match = _TIME_RANGE_RE.search(line)
            if not time_match:
                continue

            title_idx = line_idx - 1
            while (
                title_idx >= 0
                and title_idx in used
            ):
                title_idx -= 1
            if title_idx < 0:
                continue

            title = _normalize_site_title(
                segment[title_idx]
            )
            if _TIME_RANGE_RE.search(title):
                continue

            start = _parse_clock(
                time_match.group("start")
            )
            end = _parse_clock(
                time_match.group("end")
            )

            events.append({
                "id": _event_id(
                    "chesterbrook-web",
                    event_day,
                    title,
                    start,
                ),
                "title": title,
                "date": event_day.isoformat(),
                "start": start,
                "end": end,
                "schools": [SCHOOL_ID],
                "scope": "school",
                "category": _category(title),
                "source": EVENTS_SOURCE_NAME,
                "sourceUrl": PARENTS_PAGE,
            })
            used.add(line_idx)
            used.add(title_idx)

        for line_idx, line in enumerate(segment):
            if line_idx in used:
                continue
            if _TIME_RANGE_RE.search(line):
                continue
            if re.fullmatch(r"\d{4}", line):
                continue

            title = _normalize_site_title(line)
            if not re.search(r"[A-Za-z]", title):
                continue

            events.append({
                "id": _event_id(
                    "chesterbrook-web",
                    event_day,
                    title,
                ),
                "title": title,
                "date": event_day.isoformat(),
                "schools": [SCHOOL_ID],
                "scope": "school",
                "category": _category(title),
                "allDay": True,
                "source": EVENTS_SOURCE_NAME,
                "sourceUrl": PARENTS_PAGE,
            })

    unique: dict[tuple, dict] = {}
    for event in events:
        key = (
            event["date"],
            event.get("start", ""),
            re.sub(
                r"[^a-z0-9]+",
                " ",
                event["title"].casefold(),
            ).strip(),
        )
        unique[key] = event

    result = sorted(
        unique.values(),
        key=lambda event: (
            event["date"],
            event.get("start", ""),
            event["title"],
        ),
    )

    if len(date_indices) < 20:
        raise RuntimeError(
            "Chesterbrook website calendar exposed fewer than "
            "20 dated cells; page structure may have changed"
        )

    return result


def fetch_chesterbrook_website_events(
    *,
    reference: date,
    timeout: int = 30,
    opener=urlopen,
) -> list[dict]:
    page_html = _request_text(
        PARENTS_PAGE,
        timeout=timeout,
        opener=opener,
    )
    events = parse_website_calendar_html(
        page_html
    )

    print(
        "chesterbrook-events detail: current website month "
        f"parsed {len(events)} public events"
    )
    if events:
        sample = "; ".join(
            f"{event['date']} {event['title']}"
            for event in events[:10]
        )
        print(
            "chesterbrook-events detail: first events: "
            + sample
        )
    return events


def _group_day_headers(
    words: list[tuple],
    page_width: float,
) -> list[list[tuple]]:
    numeric = []

    for word in words:
        text = str(word[4]).strip()
        if not re.fullmatch(r"\d{1,2}", text):
            continue

        value = int(text)
        if not (1 <= value <= 31):
            continue

        x0, y0, x1, y1 = word[:4]
        center_x = (x0 + x1) / 2
        center_y = (y0 + y1) / 2

        if center_x < page_width * 0.05:
            continue

        numeric.append(
            (center_y, center_x, word)
        )

    groups: list[list[tuple]] = []

    for center_y, center_x, word in sorted(numeric):
        placed = False

        for group in groups:
            group_y = (
                sum(item[0] for item in group)
                / len(group)
            )
            if abs(center_y - group_y) <= 4.5:
                group.append(
                    (center_y, center_x, word)
                )
                placed = True
                break

        if not placed:
            groups.append(
                [(center_y, center_x, word)]
            )

    header_rows = []

    for group in groups:
        x_values = sorted(
            item[1]
            for item in group
        )
        if len(group) < 4:
            continue
        if (
            x_values[-1] - x_values[0]
            < page_width * 0.55
        ):
            continue

        header_rows.append([
            item[2]
            for item in sorted(
                group,
                key=lambda item: item[1],
            )[:5]
        ])

    return sorted(
        header_rows,
        key=lambda group: min(
            word[1]
            for word in group
        ),
    )


def _row_label_y(
    words: list[tuple],
    *,
    top: float,
    bottom: float,
    page_width: float,
    target: str,
) -> float | None:
    candidates = []

    for word in words:
        x0, y0, x1, y1, text = word[:5]
        if x0 > page_width * 0.11:
            continue

        center_y = (y0 + y1) / 2
        if not (top <= center_y <= bottom):
            continue

        if (
            str(text).strip().casefold()
            == target.casefold()
        ):
            candidates.append(center_y)

    return (
        min(candidates)
        if candidates
        else None
    )


def _clean_cell_text(
    words: list[tuple],
) -> str:
    if not words:
        return ""

    rows: list[list[tuple]] = []

    for word in sorted(
        words,
        key=lambda w: (
            (w[1] + w[3]) / 2,
            w[0],
        ),
    ):
        center_y = (word[1] + word[3]) / 2
        placed = False

        for row in rows:
            row_y = (
                sum(
                    (item[1] + item[3]) / 2
                    for item in row
                )
                / len(row)
            )

            if abs(center_y - row_y) <= 2.5:
                row.append(word)
                placed = True
                break

        if not placed:
            rows.append([word])

    text_rows = []

    for row in rows:
        line = " ".join(
            str(word[4]).strip()
            for word in sorted(
                row,
                key=lambda w: w[0],
            )
            if str(word[4]).strip()
        )
        if line:
            text_rows.append(line)

    text = " ".join(text_rows)
    text = re.sub(
        r"\s+([,.;:)])",
        r"\1",
        text,
    )
    text = re.sub(
        r"([(])\s+",
        r"\1",
        text,
    )
    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def parse_menu_words(
    words: list[tuple],
    *,
    page_width: float,
    page_height: float,
    month: int,
    year: int,
    source_url: str,
) -> list[dict]:
    header_rows = _group_day_headers(
        words,
        page_width,
    )

    if len(header_rows) < 4:
        raise RuntimeError(
            "Chesterbrook menu PDF did not expose four "
            "recognizable week rows"
        )

    records: list[dict] = []
    days_in_month = calendar_mod.monthrange(
        year,
        month,
    )[1]

    for row_idx, header_words in enumerate(
        header_rows
    ):
        centers = [
            (word[0] + word[2]) / 2
            for word in header_words
        ]

        if len(centers) != 5:
            continue

        header_bottom = max(
            word[3]
            for word in header_words
        )
        block_top = header_bottom + 1

        block_bottom = (
            min(
                word[1]
                for word
                in header_rows[row_idx + 1]
            ) - 2
            if row_idx + 1 < len(header_rows)
            else page_height * 0.90
        )

        breakfast_y = _row_label_y(
            words,
            top=block_top,
            bottom=block_bottom,
            page_width=page_width,
            target="Breakfast",
        )
        lunch_y = _row_label_y(
            words,
            top=block_top,
            bottom=block_bottom,
            page_width=page_width,
            target="Lunch",
        )
        snack_y = _row_label_y(
            words,
            top=block_top,
            bottom=block_bottom,
            page_width=page_width,
            target="Snack",
        )

        if (
            breakfast_y is None
            or lunch_y is None
            or snack_y is None
        ):
            continue

        row_bounds = {
            "Breakfast": (
                block_top,
                (breakfast_y + lunch_y) / 2,
            ),
            "Lunch": (
                (breakfast_y + lunch_y) / 2,
                (lunch_y + snack_y) / 2,
            ),
            "PM Snack": (
                (lunch_y + snack_y) / 2,
                block_bottom,
            ),
        }

        col_bounds = [
            page_width * 0.08
        ]
        col_bounds.extend(
            (
                centers[idx]
                + centers[idx + 1]
            ) / 2
            for idx in range(4)
        )
        col_bounds.append(
            page_width * 0.995
        )

        for col_idx, header_word in enumerate(
            header_words
        ):
            day_text = str(
                header_word[4]
            ).strip()

            if not day_text.isdigit():
                continue

            day = int(day_text)
            if not (
                1 <= day <= days_in_month
            ):
                continue

            candidate_day = date(
                year,
                month,
                day,
            )

            if (
                candidate_day.weekday()
                != col_idx
            ):
                continue

            x_left = col_bounds[col_idx]
            x_right = col_bounds[col_idx + 1]

            meal_parts = []
            closed = False

            for meal_label, (
                y_top,
                y_bottom,
            ) in row_bounds.items():
                cell_words = []

                for word in words:
                    x0, y0, x1, y1, text = word[:5]
                    center_x = (x0 + x1) / 2
                    center_y = (y0 + y1) / 2

                    if not (
                        x_left
                        <= center_x
                        < x_right
                    ):
                        continue

                    if not (
                        y_top
                        <= center_y
                        < y_bottom
                    ):
                        continue

                    if word is header_word:
                        continue

                    cell_words.append(word)

                cell_text = _clean_cell_text(
                    cell_words
                )

                if not cell_text:
                    continue

                if (
                    "school closed"
                    in cell_text.casefold()
                ):
                    closed = True
                    continue

                meal_parts.append(
                    f"{meal_label}: {cell_text}"
                )

            items = (
                ["School closed"]
                if closed and not meal_parts
                else meal_parts
            )

            if not items:
                continue

            records.append({
                "date": candidate_day.isoformat(),
                "group": MENU_GROUP,
                "items": items,
                "status": "live",
                "label": (
                    "Live Chesterbrook "
                    "meals & snacks"
                ),
                "source": MENU_SOURCE_NAME,
                "sourceUrl": source_url,
            })

    unique = {
        (
            record["group"],
            record["date"],
        ): record
        for record in records
    }

    result = sorted(
        unique.values(),
        key=lambda record: record["date"],
    )

    if len(result) < 15:
        raise RuntimeError(
            "Chesterbrook menu PDF positional parser "
            f"produced only {len(result)} weekday records"
        )

    return result


def parse_menu_pdf_bytes(
    pdf_bytes: bytes,
    *,
    source_url: str,
) -> list[dict]:
    document = fitz.open(
        stream=pdf_bytes,
        filetype="pdf",
    )

    if not document.page_count:
        raise RuntimeError(
            "Chesterbrook menu PDF had no pages"
        )

    page = document[0]
    text = page.get_text("text")

    title_match = re.search(
        r"(January|February|March|April|May|June|"
        r"July|August|September|October|November|"
        r"December)\s+Menu\s+(20\d{2})",
        text,
        re.I,
    )

    if not title_match:
        title_match = re.search(
            r"(January|February|March|April|May|June|"
            r"July|August|September|October|November|"
            r"December).*?(20\d{2})",
            text,
            re.I | re.S,
        )

    if not title_match:
        raise RuntimeError(
            "Chesterbrook menu PDF did not expose "
            "a month/year title"
        )

    month = _MONTHS[
        title_match.group(1).casefold()
    ]
    year = int(title_match.group(2))

    return parse_menu_words(
        page.get_text(
            "words",
            sort=False,
        ),
        page_width=page.rect.width,
        page_height=page.rect.height,
        month=month,
        year=year,
        source_url=source_url,
    )


def fetch_chesterbrook_menu(
    *,
    reference: date,
    timeout: int = 30,
    opener=urlopen,
) -> list[dict]:
    page_html = _request_text(
        PARENTS_PAGE,
        timeout=timeout,
        opener=opener,
    )
    _, menu_url = _discover_links(
        page_html,
        reference=reference,
    )

    pdf_bytes = _request_bytes(
        menu_url,
        timeout=timeout,
        opener=opener,
        accept="application/pdf,*/*;q=0.8",
    )

    records = parse_menu_pdf_bytes(
        pdf_bytes,
        source_url=menu_url,
    )

    print(
        "chesterbrook-menu detail: monthly PDF parsed "
        f"{len(records)} menu days"
    )
    if records:
        sample = "; ".join(
            f"{record['date']} "
            f"({len(record['items'])} meal rows)"
            for record in records[:8]
        )
        print(
            "chesterbrook-menu detail: first records: "
            + sample
        )

    return records
