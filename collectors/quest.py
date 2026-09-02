from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class QuestGroup:
    group: str
    log_id: str
    url: str
    source: str
    preferred_programs: tuple[str, ...]


QUEST_GROUPS: dict[str, QuestGroup] = {
    "k5": QuestGroup(
        group="k5",
        log_id="quest-k5",
        url="https://www.myschoolquest.com/yankee-ridge-elementary/menus",
        source="MySchoolQuest — USD 116 K–5",
        preferred_programs=("K-5 Lunch", "K–5 Lunch", "Lunch"),
    ),
    "middle": QuestGroup(
        group="middle",
        log_id="quest-middle",
        url="https://www.myschoolquest.com/urbana-middle-school/menus",
        source="MySchoolQuest — USD 116 6–8",
        preferred_programs=("6-8 Lunch", "6–8 Lunch", "Lunch"),
    ),
    "high": QuestGroup(
        group="high",
        log_id="quest-high",
        url="https://www.myschoolquest.com/urbana-high-school/menus",
        source="MySchoolQuest — USD 116 9–12",
        preferred_programs=("9-12 Lunch", "9–12 Lunch", "Lunch"),
    ),
}

_NUTRITION_LABEL_NORMALIZED = {
    "calories", "servingsize", "totalfat", "saturatedfat", "transfat",
    "cholesterol", "sodium", "totalcarbohydrate", "dietaryfiber",
    "totalsugars", "addedsugars", "protein", "vitamind",
    "vitamindd2d3", "calcium", "iron", "potassium",
}


def _parse_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
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
    if len(text) < 3 or len(text) > 160:
        return None
    norm = re.sub(r"[^a-z0-9]", "", text.casefold())
    if norm in _NUTRITION_LABEL_NORMALIZED:
        return None
    if text.casefold() in {
        "lunch", "breakfast", "main entree", "main entrée", "shared items",
        "vegetable", "fruit", "milk", "condiment", "nutrition", "allergens",
    }:
        return None
    return text


def _sanitize_items(items: list[str]) -> list[str]:
    out: list[str] = []
    for raw in items:
        item = _clean_item(raw)
        if item and item not in out:
            out.append(item)
    return out


def _extract_rendered_lunch(body_text: str, dates: list[str]) -> dict[str, list[str]]:
    """Extract only visible Entrée/Main Entree food names from Quest's week view."""
    text = re.sub(r"\s+", " ", body_text or "").strip()
    if not text or not dates:
        return {}

    day_re = re.compile(
        r"(?:Today|Monday|Tuesday|Wednesday|Thursday|Friday)\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?\s+",
        re.I,
    )
    matches = list(day_re.finditer(text))
    if not matches:
        return {}

    by_date: dict[str, list[str]] = {}
    for idx, match in enumerate(matches):
        if idx >= len(dates):
            break
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        segment = text[start:end]
        for stop in (" Home View Menus ", " CONSUMING RAW OR UNDERCOOKED "):
            pos = segment.find(stop)
            if pos >= 0:
                segment = segment[:pos]

        entree_re = re.compile(
            r"(?:Entr[ée]e|Entree)\s*\d+\s+Main Entree\s+"
            r"(.+?)"
            r"(?=(?:Entr[ée]e|Entree)\s*\d+\s+Main Entree|Shared Items|Vegetable|Fruit|Milk|Condiment|$)",
            re.I,
        )
        items = _sanitize_items([m.group(1).strip() for m in entree_re.finditer(segment)])
        if items:
            by_date[dates[idx]] = items
    return by_date


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
    if isinstance(day_menu, list) and isinstance(meal_periods, list) and len(day_menu) == len(meal_periods):
        for idx, period in enumerate(meal_periods):
            if isinstance(period, str) and "lunch" in period.casefold():
                return day_menu[idx]
    if isinstance(day_menu, list):
        for child in day_menu:
            own = [x.casefold() for x in _strings_in(child)[:40]]
            if any(x == "lunch" or x.endswith(" lunch") for x in own):
                return child
    return day_menu


def _extract_main_entrees(node: Any) -> list[str]:
    """Conservative JSON fallback: ignore nested nutrition branches."""
    out: list[str] = []
    strong_keys = {
        "itemname", "menuitemname", "recipename", "productname", "foodname",
        "dishname", "displayname", "display_name", "item_name", "menu_item_name", "recipe_name",
    }
    generic_keys = {"name", "title", "description", "label"}

    def norm_key(value: str) -> str:
        return re.sub(r"[^a-z0-9_]", "", value.casefold())

    def is_main(obj: dict[str, Any]) -> bool:
        vals: list[str] = []
        for key, value in obj.items():
            if isinstance(value, str):
                vals.append(value)
            if "category" in norm_key(str(key)) and isinstance(value, dict):
                vals.extend(v for v in value.values() if isinstance(v, str))
        return any(re.sub(r"\s+", " ", v).strip().casefold() in {"main entree", "main entrée"} for v in vals)

    def nutrition_branch(key: str, child: Any) -> bool:
        nk = norm_key(key)
        words = (
            "nutrition", "nutrient", "allergen", "totalfat", "saturatedfat", "transfat",
            "cholesterol", "sodium", "carbohydrate", "fiber", "sugars", "protein",
            "vitamind", "calcium", "iron", "potassium", "calories",
        )
        if any(word in nk for word in words):
            return True
        if isinstance(child, dict):
            keys = [norm_key(str(k)) for k in child]
            if sum(any(word in k for word in words) for k in keys) >= 2:
                return True
        return False

    def add_name(obj: dict[str, Any]) -> None:
        for keys in (strong_keys, generic_keys):
            for key, value in obj.items():
                if norm_key(str(key)) in keys and isinstance(value, str):
                    item = _clean_item(value)
                    if item and not re.fullmatch(r"(?:entr[eé]e|entree)\s*\d+", item.casefold()) and item not in out:
                        out.append(item)
                        return

    def walk(value: Any, parent_main: bool = False) -> None:
        if isinstance(value, list):
            for child in value:
                walk(child, parent_main)
            return
        if not isinstance(value, dict):
            return
        this_main = is_main(value)
        if this_main or parent_main:
            add_name(value)
        for key, child in value.items():
            if nutrition_branch(str(key), child):
                continue
            walk(child, this_main)

    walk(node)
    return _sanitize_items(out)


def _extract_parallel_days(payload: Any) -> dict[str, list[str]]:
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


def _json_from_body(body: str) -> Any | None:
    text = body.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _program_rank(label: str, preferred: tuple[str, ...]) -> tuple[int, int]:
    lower = re.sub(r"\s+", " ", label).strip().casefold()
    preferred_lower = [p.casefold() for p in preferred]
    for idx, target in enumerate(preferred_lower):
        if lower == target:
            return (idx, len(label))
    for idx, target in enumerate(preferred_lower):
        if target in lower or lower in target:
            return (10 + idx, len(label))
    return (50 if "lunch" in lower else 100, len(label))


def _switch_page_to_program(driver, cfg: QuestGroup) -> str | None:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select

    body = re.sub(r"\s+", " ", driver.find_element(By.TAG_NAME, "body").text).strip()
    # If the correct program is already visible, don't disturb the page.
    if "Lunch for Week" in body:
        for label in cfg.preferred_programs:
            if re.search(rf"\bProgram\s+{re.escape(label)}\b", body, re.I):
                print(f"{cfg.log_id} detail: menu option {label!r} already active")
                return label

    for element in driver.find_elements(By.TAG_NAME, "select"):
        try:
            select = Select(element)
            options = [(option.text or "").strip() for option in select.options]
            candidates = sorted(((_program_rank(label, cfg.preferred_programs), label) for label in options if "lunch" in label.casefold()))
            if candidates:
                label = candidates[0][1]
                select.select_by_visible_text(label)
                time.sleep(2.0)
                print(f"{cfg.log_id} detail: selected menu option {label!r}")
                return label
        except Exception:
            continue

    controls = []
    tokens = ("program", "snack", "lunch", "asccp", "esser", "k-5", "6-8", "9-12")
    for element in driver.find_elements(By.XPATH, "//button | //*[@role='button']"):
        try:
            if not element.is_displayed():
                continue
            text = re.sub(r"\s+", " ", element.text or "").strip()
            if text and len(text) <= 100 and any(token in text.casefold() for token in tokens):
                controls.append(element)
        except Exception:
            continue

    for control in controls[:10]:
        try:
            try:
                control.click()
            except Exception:
                driver.execute_script("arguments[0].click();", control)
            time.sleep(0.6)
            candidates = []
            for candidate in driver.find_elements(By.XPATH, "//*[contains(translate(normalize-space(.), 'LUNCH', 'lunch'), 'lunch')]"):
                try:
                    if not candidate.is_displayed():
                        continue
                    label = re.sub(r"\s+", " ", candidate.text or "").strip()
                    if label and len(label) <= 90 and "lunch" in label.casefold():
                        candidates.append((_program_rank(label, cfg.preferred_programs), candidate, label))
                except Exception:
                    continue
            candidates.sort(key=lambda row: row[0])
            if candidates:
                _, candidate, label = candidates[0]
                try:
                    candidate.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", candidate)
                time.sleep(3.0)
                body = re.sub(r"\s+", " ", driver.find_element(By.TAG_NAME, "body").text).strip()
                if "Lunch for Week" in body:
                    print(f"{cfg.log_id} detail: selected menu option {label!r}")
                    return label
        except Exception:
            continue
    return None


def _payload_shape(payload: Any, *, depth: int = 0, max_depth: int = 2) -> str:
    if depth >= max_depth:
        if isinstance(payload, dict):
            return "{…}"
        if isinstance(payload, list):
            return f"[{len(payload)} items…]"
        return repr(payload)[:80]
    if isinstance(payload, dict):
        parts = []
        for key, value in list(payload.items())[:8]:
            parts.append(f"{key}: {_payload_shape(value, depth=depth+1, max_depth=max_depth)}")
        return "{" + ", ".join(parts) + (", …" if len(payload) > 8 else "") + "}"
    if isinstance(payload, list):
        if not payload:
            return "[]"
        return f"[{len(payload)} items; first={_payload_shape(payload[0], depth=depth+1, max_depth=max_depth)}]"
    return repr(payload)[:80]


def fetch_quest_group(group_id: str, *, wait_seconds: float = 7.0) -> list[dict]:
    cfg = QUEST_GROUPS[group_id]
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Selenium is required for the Quest collector") from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--lang=en-US")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    payload_records: list[tuple[str, Any]] = []
    diagnostic_urls: list[str] = []
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.get(cfg.url)
        time.sleep(wait_seconds)
        selected = _switch_page_to_program(driver, cfg)
        if selected:
            print(f"{cfg.log_id} detail: page switch/confirmation to Lunch succeeded")
        else:
            print(f"{cfg.log_id} detail: could not confirm a Lunch program")

        response_meta: list[tuple[str, str]] = []
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
                response_meta.append((request_id, response_url))

        for request_id, response_url in response_meta:
            if len(diagnostic_urls) < 10:
                diagnostic_urls.append(response_url)
            try:
                result = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
            except Exception:
                continue
            parsed = _json_from_body(result.get("body", ""))
            if parsed is not None:
                payload_records.append((response_url, parsed))

        # Quest can emit more than one /menus/week response while a page is loading
        # or switching programs.  Do not blindly take the last one: calendar UI
        # interactions sometimes trigger a narrower one-day response.  Prefer the
        # response with the most dated menu entries, breaking ties in favor of the
        # later response.
        menu_payloads = [p for url, p in payload_records if "/menus/week" in url and isinstance(p, dict)]
        if not menu_payloads:
            menu_payloads = [p for _, p in payload_records if isinstance(p, dict) and isinstance(p.get("dates"), list) and isinstance(p.get("menus"), list)]

        def payload_date_count(payload: Any) -> int:
            if not isinstance(payload, dict) or not isinstance(payload.get("dates"), list):
                return 0
            return sum(1 for raw in payload.get("dates", []) if _parse_date(raw))

        menu_payload = None
        if menu_payloads:
            _, menu_payload = max(enumerate(menu_payloads), key=lambda row: (payload_date_count(row[1]), row[0]))

        response_dates: list[str] = []
        if isinstance(menu_payload, dict):
            response_dates = [d for d in (_parse_date(x) for x in menu_payload.get("dates", [])) if d]

        body_text = driver.find_element("tag name", "body").text
        rendered_days = _extract_rendered_lunch(body_text, response_dates)
        json_days = _extract_parallel_days(menu_payload) if menu_payload is not None else {}

        # The elementary page exposes numbered Entrée slots cleanly in rendered
        # text, while secondary pages often split choices across pizza, deli,
        # salad, grill, etc.  The JSON retains all of those Main Entree items.
        # Choose the richer clean set for each date rather than applying one
        # extraction method to every grade band.
        day_map: dict[str, list[str]] = {}
        methods_used: set[str] = set()
        for day in sorted(set(rendered_days) | set(json_days)):
            rendered_items = _sanitize_items(rendered_days.get(day, []))
            json_items = _sanitize_items(json_days.get(day, []))
            if len(json_items) > len(rendered_items):
                day_map[day] = json_items
                methods_used.add("Quest JSON")
            elif rendered_items:
                day_map[day] = rendered_items
                methods_used.add("rendered Lunch page")
            elif json_items:
                day_map[day] = json_items
                methods_used.add("Quest JSON")

        if methods_used == {"Quest JSON"}:
            method = "Quest JSON"
        elif methods_used == {"rendered Lunch page"}:
            method = "rendered Lunch page"
        elif methods_used:
            method = "rendered page + Quest JSON"
        else:
            method = "none"

        today = date.today()
        earliest = today - timedelta(days=45)
        latest = today + timedelta(days=120)
        meals: list[dict] = []
        for day, items in sorted(day_map.items()):
            try:
                d = date.fromisoformat(day)
            except ValueError:
                continue
            clean = _sanitize_items(items)
            if not clean or not (earliest <= d <= latest):
                continue
            meals.append({
                "date": day,
                "group": cfg.group,
                "items": clean,
                "status": "live",
                "label": "Live Quest menu",
                "source": cfg.source,
                "sourceUrl": cfg.url,
            })

        if meals:
            dates = ", ".join(m["date"] for m in meals[:6])
            suffix = "…" if len(meals) > 6 else ""
            print(f"{cfg.log_id} detail: used {method}; menu dates {dates}{suffix}")
            return meals

        print(f"{cfg.log_id} detail: extracted no dated lunch items")
        if diagnostic_urls:
            print(f"{cfg.log_id} observed requests:")
            for observed in diagnostic_urls:
                print(f"  - {observed}")
        if payload_records:
            print(f"{cfg.log_id} JSON shapes:")
            for idx, (_, payload) in enumerate(payload_records[:4], start=1):
                print(f"  payload {idx}: {_payload_shape(payload)}")
        sample = re.sub(r"\s+", " ", body_text).strip()
        if sample:
            print(f"{cfg.log_id} rendered-text sample:")
            print(sample[:1400])
        raise RuntimeError(f"MySchoolQuest loaded for {group_id}, but no lunch menu was extracted")
    finally:
        driver.quit()


def fetch_yankee_k5(*, wait_seconds: float = 7.0) -> list[dict]:
    """Backward-compatible wrapper used by earlier SchoolBoard builds."""
    return fetch_quest_group("k5", wait_seconds=wait_seconds)
