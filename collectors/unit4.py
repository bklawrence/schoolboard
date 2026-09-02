from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

SID = "1465843288260"
MENU_URL = f"https://champaignschoolsfoodservices.org/index.php?sid={SID}&page=menus"
SOURCE_NAME = "Unit 4 School Nutrition & Fitness"
GRAPHQL_URL = "https://api.schoolnutritionandfitness.com/graphql"

# We deliberately use one representative school for each districtwide menu band.
# The public menu application exposes the same grade-band menu types at schools
# within those bands; this avoids fetching every school separately.
TARGETS = [
    ("u4elem", "Elementary Schools", "Barkstall Elementary", "Elementary Lunch Menu"),
    ("u4middle", "Middle School Menus", "Edison Middle", "Middle Lunch"),
    ("u4world", "High School Menus", "Central High", "HS - Around the World"),
    ("u4grill", "High School Menus", "Central High", "HS - Grill Zone & Garden"),
    ("u4combo", "High School Menus", "Central High", "HS - Pizzeria & Taste of Home"),
]

UNIT4_GROUPS = ("u4elem", "u4middle", "u4world", "u4pizza", "u4home", "u4grill")

EXCLUDE_CATEGORY_WORDS = {
    "condiment", "dressing", "beverage", "milk", "fruit", "vegetable",
    "veggie side", "side dish", "sauce", "topping",
}

OBVIOUS_NON_ENTREES = {
    "ketchup", "mustard", "mayonnaise", "mayo", "ranch dressing",
    "bbq sauce", "barbecue sauce", "hot sauce", "syrup",
}


def _compact(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _dispatch_change(driver, element) -> None:
    driver.execute_script(
        """
        const el = arguments[0];
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
        """,
        element,
    )


def _find_option(select_obj, wanted: str) -> str:
    """Return the real visible text for an option, tolerating UI ellipses."""
    wanted_norm = _compact(wanted).casefold()
    options = [(_compact(opt.text), opt) for opt in select_obj.options]

    for text, _ in options:
        if text.casefold() == wanted_norm:
            return text

    # The current SNAF UI truncates two HS labels with literal "...".
    # Match by the stable leading phrase instead of hard-coding the truncation.
    wanted_prefix = wanted_norm.rstrip(" .…")
    for text, _ in options:
        text_norm = text.casefold().rstrip(" .…")
        if text_norm.startswith(wanted_prefix) or wanted_prefix.startswith(text_norm):
            return text

    # Last chance: all meaningful words from the wanted label appear in option text.
    tokens = [t for t in re.findall(r"[a-z0-9]+", wanted_norm) if len(t) > 2]
    for text, _ in options:
        low = text.casefold()
        if tokens and all(t in low for t in tokens[:3]):
            return text

    raise RuntimeError(
        f"Unit 4 menu option {wanted!r} not found; "
        f"available={[text for text, _ in options if text]!r}"
    )


def _select_visible(select_obj, wanted: str) -> str:
    actual = _find_option(select_obj, wanted)
    select_obj.select_by_visible_text(actual)
    return actual


def _graphql_menu_payloads(driver) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []

    for entry in driver.get_log("performance"):
        try:
            msg = json.loads(entry["message"])["message"]
        except Exception:
            continue

        if msg.get("method") != "Network.responseReceived":
            continue

        params = msg.get("params", {})
        response = params.get("response", {})
        url = response.get("url", "")
        if GRAPHQL_URL not in url or response.get("status") != 200:
            continue

        request_id = params.get("requestId")
        if not request_id:
            continue

        try:
            raw = driver.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": request_id}
            ).get("body", "")
            payload = json.loads(raw)
        except Exception:
            continue

        menu = (payload.get("data") or {}).get("menu") if isinstance(payload, dict) else None
        if isinstance(menu, dict) and isinstance(menu.get("items"), list):
            payloads.append(menu)

    return payloads


def _first_present(*values: Any) -> Any:
    """Return the first value that is not None/blank."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _item_date(item: dict[str, Any], menu: dict[str, Any]) -> str | None:
    """Convert SNAF's item date into YYYY-MM-DD.

    SNAF's month is JavaScript-style zero-based at the menu level (September
    2026 is reported as month=8).  Some item records include month/year keys
    whose values are null/blank, so they must explicitly fall back to the
    enclosing menu rather than using dict.get(..., fallback).
    """
    raw_day = _first_present(item.get("day"), item.get("date"))

    # Be tolerant if the API ever emits an ISO-ish date string instead of a day number.
    if isinstance(raw_day, str):
        text = raw_day.strip()
        match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if match:
            try:
                return date(*(int(part) for part in match.groups())).isoformat()
            except ValueError:
                return None

    try:
        year = int(_first_present(item.get("year"), menu.get("year")))
        month_raw = int(_first_present(item.get("month"), menu.get("month")))
        day = int(raw_day)
    except (TypeError, ValueError):
        return None

    # The observed September 2026 menu reports month=8, so SNAF is zero-based.
    month = month_raw + 1 if 0 <= month_raw <= 11 else month_raw

    # Day is normally 1-based. Handle a possible zero for the first of the month
    # defensively without shifting ordinary day values.
    if day == 0:
        day = 1

    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _product_name(item: dict[str, Any]) -> str | None:
    product = item.get("product")
    if not isinstance(product, dict):
        return None
    name = _compact(product.get("name"))
    if not name:
        return None
    if name.casefold() in OBVIOUS_NON_ENTREES:
        return None
    return name


def _category_text(item: dict[str, Any]) -> str:
    product = item.get("product") if isinstance(item.get("product"), dict) else {}
    parts = []
    for source in (item, product):
        for key in ("category", "meal", "food_group", "type", "section", "station"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
            elif isinstance(value, dict):
                for subkey in ("name", "label", "title"):
                    subvalue = value.get(subkey)
                    if isinstance(subvalue, str) and subvalue.strip():
                        parts.append(subvalue)
    return " ".join(parts).casefold()


def _keep_item(item: dict[str, Any]) -> bool:
    product = item.get("product")
    if not isinstance(product, dict):
        return False

    # Respect the menu application's own hidden/disabled flags when present.
    if product.get("enabled") is False:
        return False
    if product.get("hide_on_calendars") is True or product.get("hide_on_mobile") is True:
        return False

    category = _category_text(item)
    if category and any(word in category for word in EXCLUDE_CATEGORY_WORDS):
        return False
    return True


def _menu_to_days(menu: dict[str, Any]) -> dict[str, list[str]]:
    items = [item for item in (menu.get("items") or []) if isinstance(item, dict)]

    def collect(*, apply_category_filter: bool) -> dict[str, list[str]]:
        found: dict[str, list[str]] = defaultdict(list)
        for item in items:
            if apply_category_filter and not _keep_item(item):
                continue
            day = _item_date(item, menu)
            name = _product_name(item)
            if not day or not name:
                continue
            if name not in found[day]:
                found[day].append(name)
        return dict(found)

    # Prefer the cleaner entrée-oriented view. If SNAF's category metadata is
    # inconsistent, retain the dated named products rather than failing the
    # whole source; obvious condiments are still removed by _product_name().
    found = collect(apply_category_filter=True)
    if found:
        return found

    fallback = collect(apply_category_filter=False)
    if fallback:
        print("unit4-menus detail: category filter produced zero items; using named-product fallback")
    return fallback


def _meal_records(group: str, days: dict[str, list[str]]) -> list[dict[str, Any]]:
    today = date.today()
    earliest = today - timedelta(days=45)
    latest = today + timedelta(days=120)
    out = []

    for day, items in sorted(days.items()):
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            continue
        if not (earliest <= parsed <= latest) or not items:
            continue
        out.append(
            {
                "date": day,
                "group": group,
                "items": items,
                "status": "live",
                "label": "Live Unit 4 menu",
                "source": SOURCE_NAME,
                "sourceUrl": MENU_URL,
            }
        )
    return out


def _split_pizza_home(days: dict[str, list[str]]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """The source combines Pizzeria + Taste of Home; preserve the site's two cards."""
    pizza: dict[str, list[str]] = {}
    home: dict[str, list[str]] = {}

    for day, items in days.items():
        pizza_items = []
        home_items = []
        for name in items:
            low = name.casefold()
            if "pizza" in low or "pizzeria" in low:
                pizza_items.append(name)
            else:
                home_items.append(name)
        if pizza_items:
            pizza[day] = pizza_items
        if home_items:
            home[day] = home_items

    return pizza, home


def _fetch_target(driver, group: str, grade_band: str, school: str, menu_name: str, *, wait_seconds: float) -> dict[str, list[str]]:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select

    driver.get(MENU_URL)
    time.sleep(2.0)

    selects = driver.find_elements(By.TAG_NAME, "select")
    if not selects:
        raise RuntimeError("Unit 4 grade-band dropdown not found")
    grade_select = Select(selects[0])
    actual_grade = _select_visible(grade_select, grade_band)
    _dispatch_change(driver, selects[0])
    time.sleep(2.0)

    selects = driver.find_elements(By.TAG_NAME, "select")
    if len(selects) < 2:
        raise RuntimeError(f"Unit 4 school dropdown did not appear for {grade_band}")
    school_select = Select(selects[1])
    actual_school = _select_visible(school_select, school)
    _dispatch_change(driver, selects[1])
    time.sleep(2.0)

    selects = driver.find_elements(By.TAG_NAME, "select")
    if len(selects) < 3:
        raise RuntimeError(f"Unit 4 menu dropdown did not appear for {school}")
    menu_select = Select(selects[2])
    actual_menu = _select_visible(menu_select, menu_name)

    # Clear prior GraphQL traffic so only this actual menu selection is parsed.
    try:
        driver.get_log("performance")
    except Exception:
        pass

    menu_select.select_by_visible_text(actual_menu)
    _dispatch_change(driver, selects[2])
    time.sleep(wait_seconds)

    menus = _graphql_menu_payloads(driver)
    if not menus:
        raise RuntimeError(
            f"Unit 4 GraphQL returned no menu payload for {actual_school} / {actual_menu}"
        )

    # The menu request is the payload with the largest items array.
    menu = max(menus, key=lambda m: len(m.get("items") or []))
    days = _menu_to_days(menu)
    if not days:
        sample = []
        for item in (menu.get("items") or [])[:8]:
            if not isinstance(item, dict):
                continue
            product = item.get("product") if isinstance(item.get("product"), dict) else {}
            sample.append({
                "day": item.get("day"),
                "month": item.get("month"),
                "year": item.get("year"),
                "product": product.get("name"),
                "category": product.get("category"),
                "meal": product.get("meal"),
            })
        print(
            f"unit4-menus detail: unparsed item sample for {actual_school} / {actual_menu}: "
            + json.dumps(sample, ensure_ascii=False)
        )
        raise RuntimeError(
            f"Unit 4 menu payload contained no dated food items for {actual_school} / {actual_menu}"
        )

    sample_days = ", ".join(sorted(days)[:6])
    item_count = sum(len(v) for v in days.values())
    print(
        f"unit4-menus detail: {group} live via {actual_school} / {actual_menu}; "
        f"{len(days)} dates, {item_count} food items; first dates {sample_days}"
    )
    return days


def fetch_unit4_menus(*, wait_seconds: float = 5.0) -> list[dict[str, Any]]:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:
        raise RuntimeError("Selenium is required for Unit 4 menus") from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1600")
    options.add_argument("--lang=en-US")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    all_records: list[dict[str, Any]] = []

    try:
        driver.execute_cdp_cmd("Network.enable", {})

        for group, grade_band, school, menu_name in TARGETS:
            days = _fetch_target(
                driver,
                group,
                grade_band,
                school,
                menu_name,
                wait_seconds=wait_seconds,
            )

            if group == "u4combo":
                pizza_days, home_days = _split_pizza_home(days)
                all_records.extend(_meal_records("u4pizza", pizza_days))
                all_records.extend(_meal_records("u4home", home_days))
                print(
                    f"unit4-menus detail: split combined HS feed into "
                    f"u4pizza={len(pizza_days)} dates and u4home={len(home_days)} dates"
                )
            else:
                all_records.extend(_meal_records(group, days))

    finally:
        driver.quit()

    if not all_records:
        raise RuntimeError("Unit 4 collector returned zero live menu records")

    groups_found = sorted({m["group"] for m in all_records})
    print(
        f"unit4-menus detail: live groups {', '.join(groups_found)}; "
        f"{len(all_records)} group-day records"
    )
    return all_records


# Backward-compatible name used by the discovery-era build_data.py.
def discover_unit4_menu(*, wait_seconds: float = 5.0) -> list[dict[str, Any]]:
    return fetch_unit4_menus(wait_seconds=wait_seconds)
