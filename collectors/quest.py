from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

YANKEE_RIDGE_MENU_URL = "https://www.myschoolquest.com/yankee-ridge-elementary/menus"
SOURCE_NAME = "MySchoolQuest — USD 116 K–5"

_DATE_KEYS = {
    "date", "menudate", "service_date", "servicedate", "servingdate",
    "calendar_date", "calendardate", "daydate", "startdate",
}
_STRONG_ITEM_KEYS = {
    "itemname", "menuitemname", "recipename", "productname", "foodname",
    "dishname", "displayname", "display_name", "item_name", "menu_item_name", "recipe_name",
}
_GENERIC_ITEM_KEYS = {"name", "title", "description", "label"}
_MEAL_HINT_KEYS = {
    "meal", "mealname", "mealtype", "mealperiod", "mealperiodname",
    "service", "servicename", "period", "periodname",
}
_PATH_FOOD_WORDS = ("item", "recipe", "food", "product", "dish", "entree", "offering")
_STOP_VALUES = {
    "lunch", "breakfast", "dinner", "menu", "menus", "main", "main line",
    "daily menu", "view menus", "nutrition", "allergens", "allergen",
    "calories", "serving size", "station", "stations", "category", "categories",
    "yankee ridge elementary", "yankee ridge multilingual school",
}


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", value.casefold())


def _parse_date(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        # Accept millisecond/second Unix timestamps in a plausible school-year range.
        try:
            seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
            d = datetime.fromtimestamp(seconds).date()
            if 2025 <= d.year <= 2028:
                return d.isoformat()
        except (OverflowError, OSError, ValueError):
            return None
        return None

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    # ISO dates/timestamps.
    m = re.match(r"^(20\d{2})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None

    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _clean_item(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip(" \t\r\n·•-|:")
    if len(text) < 3 or len(text) > 140:
        return None
    lower = text.casefold()
    if lower in _STOP_VALUES:
        return None
    if lower.startswith(("http://", "https://")):
        return None
    if re.fullmatch(r"[\d\s.,/%$-]+", text):
        return None
    if "@" in text and "." in text:
        return None
    return text


def _meal_hint_from_dict(obj: dict[str, Any], inherited: str | None) -> str | None:
    hint = inherited
    for key, value in obj.items():
        nk = _norm_key(str(key))
        if nk not in {_norm_key(x) for x in _MEAL_HINT_KEYS}:
            continue
        if isinstance(value, str):
            lower = value.casefold()
            if "lunch" in lower:
                return "lunch"
            if "breakfast" in lower:
                hint = "breakfast"
    return hint




def _strings_in(obj: Any) -> list[str]:
    values: list[str] = []
    if isinstance(obj, dict):
        for value in obj.values():
            values.extend(_strings_in(value))
    elif isinstance(obj, list):
        for value in obj:
            values.extend(_strings_in(value))
    elif isinstance(obj, str):
        values.append(obj)
    return values


def _select_lunch_node(day_menu: Any, meal_periods: Any) -> Any:
    """Pick the Lunch portion of one Quest day menu when possible."""
    if isinstance(day_menu, list) and isinstance(meal_periods, list) and len(day_menu) == len(meal_periods):
        for idx, period in enumerate(meal_periods):
            if isinstance(period, str) and "lunch" in period.casefold():
                return day_menu[idx]

    # Fallback: search the day's children for an object whose own strings say Lunch.
    if isinstance(day_menu, list):
        for child in day_menu:
            own = [x.casefold() for x in _strings_in(child)[:40]]
            if any(x == "lunch" or x.endswith(" lunch") for x in own):
                return child
    return day_menu


def _extract_main_entrees(node: Any) -> list[str]:
    """Extract Quest's Main Entree choices from a Lunch menu subtree.

    Quest's week endpoint groups the response as parallel ``dates`` and ``menus``
    arrays. Inside a Lunch subtree, category objects label the entree section as
    ``Main Entree``. We propagate that category context down to item objects and
    collect their human-readable names.
    """
    out: list[str] = []

    def add(value: Any) -> None:
        item = _clean_item(value)
        if not item:
            return
        lower = item.casefold()
        if lower in {"main entree", "main entrée", "entrée 1", "entrée 2", "entrée 3", "entree 4"}:
            return
        if re.fullmatch(r"(?:entr[eé]e|entree)\s*\d+", lower):
            return
        if item not in out:
            out.append(item)

    def walk(value: Any, in_main: bool = False) -> None:
        if isinstance(value, dict):
            direct_strings = [v for v in value.values() if isinstance(v, str)]
            direct_lower = [re.sub(r"\s+", " ", v).strip().casefold() for v in direct_strings]
            category_labels: list[str] = []
            for key, child in value.items():
                if "category" not in _norm_key(str(key)):
                    continue
                if isinstance(child, str):
                    category_labels.append(child)
                elif isinstance(child, dict):
                    category_labels.extend(v for v in child.values() if isinstance(v, str))
            category_lower = [re.sub(r"\s+", " ", v).strip().casefold() for v in category_labels]
            local_main = in_main or any(v in {"main entree", "main entrée"} for v in direct_lower + category_lower)

            if local_main:
                for key, child in value.items():
                    nk = _norm_key(str(key))
                    if nk in {_norm_key(x) for x in _STRONG_ITEM_KEYS | _GENERIC_ITEM_KEYS}:
                        # Avoid re-adding the category label itself.
                        if isinstance(child, str) and child.strip().casefold() not in {"main entree", "main entrée", "lunch"}:
                            add(child)

            for child in value.values():
                walk(child, local_main)
        elif isinstance(value, list):
            for child in value:
                walk(child, in_main)

    walk(node)
    return out


def _extract_parallel_quest_days(payload: Any) -> dict[str, list[str]]:
    """Parse Quest's current /v1/menus/week response shape."""
    if not isinstance(payload, dict):
        return {}
    dates = payload.get("dates")
    menus = payload.get("menus")
    meal_periods = payload.get("meal_periods")
    if not isinstance(dates, list) or not isinstance(menus, list):
        return {}

    parsed: dict[str, list[str]] = {}
    for idx, raw_date in enumerate(dates):
        if idx >= len(menus):
            break
        day = _parse_date(raw_date)
        if not day:
            continue
        lunch_node = _select_lunch_node(menus[idx], meal_periods)
        items = _extract_main_entrees(lunch_node)
        if items:
            parsed[day] = items
    return parsed

def extract_menu_days(payloads: list[Any], *, source_url: str = YANKEE_RIDGE_MENU_URL) -> list[dict]:
    """Best-effort extraction from MySchoolQuest JSON responses.

    The site is a JavaScript application. Rather than hard-code a private API endpoint,
    the collector observes its public browser requests and walks JSON responses for
    dated menu-item structures. This makes the first version tolerant of endpoint changes.
    """
    found: dict[str, list[str]] = defaultdict(list)

    # Quest's current API returns parallel dates/menus arrays. Parse that
    # explicitly first; retain the generic walker below as a compatibility
    # fallback for future response changes.
    for payload in payloads:
        for day, items in _extract_parallel_quest_days(payload).items():
            for item in items:
                if item not in found[day]:
                    found[day].append(item)

    def walk(node: Any, current_date: str | None = None, meal_hint: str | None = None, path: tuple[str, ...] = ()) -> None:
        if isinstance(node, dict):
            local_date = current_date
            for key, value in node.items():
                if _norm_key(str(key)) in {_norm_key(x) for x in _DATE_KEYS}:
                    parsed = _parse_date(value)
                    if parsed:
                        local_date = parsed
                        break

            local_meal = _meal_hint_from_dict(node, meal_hint)
            path_lower = "/".join(p.casefold() for p in path)

            if local_date and local_meal != "breakfast":
                for key, value in node.items():
                    nk = _norm_key(str(key))
                    strong = nk in {_norm_key(x) for x in _STRONG_ITEM_KEYS}
                    generic = nk in {_norm_key(x) for x in _GENERIC_ITEM_KEYS}
                    food_path = any(word in path_lower for word in _PATH_FOOD_WORDS)
                    if strong or (generic and food_path):
                        item = _clean_item(value)
                        if item and item not in found[local_date]:
                            found[local_date].append(item)

            for key, value in node.items():
                walk(value, local_date, local_meal, path + (str(key),))

        elif isinstance(node, list):
            for idx, value in enumerate(node):
                walk(value, current_date, meal_hint, path + (str(idx),))

    for payload in payloads:
        walk(payload)

    today = date.today()
    earliest = today - timedelta(days=45)
    latest = today + timedelta(days=120)
    meals = []
    for day, items in sorted(found.items()):
        try:
            d = date.fromisoformat(day)
        except ValueError:
            continue
        if not (earliest <= d <= latest):
            continue
        if not items:
            continue
        meals.append({
            "date": day,
            "group": "k5",
            "items": items,
            "status": "live",
            "label": "Live Quest menu",
            "source": SOURCE_NAME,
            "sourceUrl": source_url,
        })
    return meals


def _json_from_body(body: str) -> Any | None:
    text = body.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None



def _switch_page_to_lunch(driver) -> bool:
    """Try to switch MySchoolQuest's visible program/menu selector to Lunch.

    The public page currently opens Yankee Ridge on a Snack program.  We prefer
    the site's own controls instead of guessing undocumented query parameters.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select

    # First handle ordinary <select> controls, if Quest uses one.
    for element in driver.find_elements(By.TAG_NAME, "select"):
        try:
            choices = Select(element)
            for option in choices.options:
                label = (option.text or "").strip()
                if "lunch" in label.casefold():
                    choices.select_by_visible_text(label)
                    time.sleep(2.0)
                    return True
        except Exception:
            continue

    # Quest's current UI appears to use a custom dropdown.  Open likely
    # program controls, then look for a short visible option containing Lunch.
    likely_controls = []
    for element in driver.find_elements(By.XPATH, "//button | //*[@role='button']"):
        try:
            if not element.is_displayed():
                continue
            text = re.sub(r"\s+", " ", element.text or "").strip()
            lower = text.casefold()
            if text and len(text) <= 90 and any(token in lower for token in ("program", "asccp", "esser", "snack")):
                likely_controls.append(element)
        except Exception:
            continue

    for control in likely_controls[:8]:
        try:
            try:
                control.click()
            except Exception:
                driver.execute_script("arguments[0].click();", control)
            time.sleep(0.6)

            candidates = driver.find_elements(
                By.XPATH,
                "//*[contains(translate(normalize-space(.), 'LUNCH', 'lunch'), 'lunch')]",
            )
            short_candidates = []
            for candidate in candidates:
                try:
                    if not candidate.is_displayed():
                        continue
                    text = re.sub(r"\s+", " ", candidate.text or "").strip()
                    if text and len(text) <= 80 and "lunch" in text.casefold():
                        short_candidates.append((len(text), candidate, text))
                except Exception:
                    continue
            short_candidates.sort(key=lambda row: row[0])
            if short_candidates:
                _, candidate, label = short_candidates[0]
                try:
                    candidate.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", candidate)
                time.sleep(3.0)
                body = re.sub(r"\s+", " ", driver.find_element(By.TAG_NAME, "body").text).strip()
                if "lunch" in body.casefold():
                    print(f"quest-k5 detail: selected menu option {label!r}")
                    return True
        except Exception:
            continue
    return False


def _payload_shape(payload: Any, *, depth: int = 0, max_depth: int = 3) -> str:
    """Return a compact, non-exhaustive description for workflow diagnostics."""
    if depth >= max_depth:
        if isinstance(payload, dict):
            return "{…}"
        if isinstance(payload, list):
            return f"[{len(payload)} items…]"
        if isinstance(payload, str):
            text = re.sub(r"\s+", " ", payload).strip()
            return repr(text[:70] + ("…" if len(text) > 70 else ""))
        return repr(payload)
    if isinstance(payload, dict):
        parts = []
        for key, value in list(payload.items())[:12]:
            parts.append(f"{key}: {_payload_shape(value, depth=depth+1, max_depth=max_depth)}")
        suffix = ", …" if len(payload) > 12 else ""
        return "{" + ", ".join(parts) + suffix + "}"
    if isinstance(payload, list):
        if not payload:
            return "[]"
        return f"[{len(payload)} items; first={_payload_shape(payload[0], depth=depth+1, max_depth=max_depth)}]"
    if isinstance(payload, str):
        text = re.sub(r"\s+", " ", payload).strip()
        return repr(text[:70] + ("…" if len(text) > 70 else ""))
    return repr(payload)


def fetch_yankee_k5(*, url: str = YANKEE_RIDGE_MENU_URL, wait_seconds: float = 8.0) -> list[dict]:
    """Load MySchoolQuest in headless Chrome and inspect public JSON responses."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:  # pragma: no cover - GitHub Actions installs Selenium.
        raise RuntimeError("Selenium is required for the Quest collector") from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--lang=en-US")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    response_meta: dict[str, dict[str, Any]] = {}
    payloads: list[Any] = []
    diagnostic_urls: list[str] = []

    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.get(url)
        time.sleep(wait_seconds)
        switched_to_lunch = _switch_page_to_lunch(driver)
        if switched_to_lunch:
            print("quest-k5 detail: page switch to Lunch succeeded")
        else:
            print("quest-k5 detail: could not confirm a page switch to Lunch")

        for entry in driver.get_log("performance"):
            try:
                message = json.loads(entry["message"])["message"]
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if message.get("method") != "Network.responseReceived":
                continue
            params = message.get("params", {})
            response = params.get("response", {})
            request_id = params.get("requestId")
            response_url = response.get("url", "")
            mime = (response.get("mimeType") or "").casefold()
            rtype = (params.get("type") or "").casefold()
            if not request_id or not response_url:
                continue
            if rtype in {"xhr", "fetch"} or "json" in mime or any(token in response_url.casefold() for token in ("api", "menu", "meal")):
                response_meta[request_id] = {"url": response_url, "mime": mime, "type": rtype}

        for request_id, meta in response_meta.items():
            if len(diagnostic_urls) < 12:
                diagnostic_urls.append(meta["url"])
            try:
                result = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
            except Exception:
                continue
            parsed = _json_from_body(result.get("body", ""))
            if parsed is not None:
                payloads.append(parsed)

        # Some frameworks embed initial JSON state directly in script tags.
        source = driver.page_source
        for match in re.finditer(r"<script[^>]*>(.*?)</script>", source, flags=re.I | re.S):
            script_text = re.sub(r"^\s+|\s+$", "", match.group(1))
            parsed = _json_from_body(script_text)
            if parsed is not None:
                payloads.append(parsed)

        meals = extract_menu_days(payloads, source_url=url)
        if meals:
            dates = ", ".join(m["date"] for m in meals[:6])
            suffix = "…" if len(meals) > 6 else ""
            print(f"quest-k5 detail: captured {len(payloads)} JSON payloads; menu dates {dates}{suffix}")
            return meals

        # Give the workflow log enough information for the next parser refinement,
        # without committing raw third-party payloads into the repository.
        print(f"quest-k5 detail: captured {len(payloads)} JSON payloads but extracted no dated menu items")
        if diagnostic_urls:
            print("quest-k5 observed requests:")
            for observed in diagnostic_urls:
                print(f"  - {observed}")
        if payloads:
            print("quest-k5 JSON shapes:")
            for idx, payload in enumerate(payloads[:4], start=1):
                print(f"  payload {idx}: {_payload_shape(payload)}")
        body_text = re.sub(r"\s+", " ", driver.find_element("tag name", "body").text).strip()
        if body_text:
            print("quest-k5 rendered-text sample:")
            print(body_text[:1200].replace("\n", " | "))
        raise RuntimeError("MySchoolQuest loaded, but its menu JSON structure was not recognized")
    finally:
        driver.quit()
