from __future__ import annotations

import json
import re
import time
from typing import Any

SID = "1465843288260"
MENU_URL = f"https://champaignschoolsfoodservices.org/index.php?sid={SID}&page=menus"

TARGETS = [
    ("Elementary Schools", "Barkstall Elementary", ["Elementary Lunch Menu"]),
    ("Middle School Menus", "Edison Middle", ["Middle Lunch"]),
    (
        "High School Menus",
        "Central High",
        [
            "HS - Around the World Menu",
            "HS - Grill Zone & Garden Marke...",
            "HS - Pizzeria & Taste of Home ...",
        ],
    ),
]


def _compact(text: str, limit: int = 900) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _shape(value: Any, depth: int = 0) -> str:
    if depth >= 3:
        if isinstance(value, dict):
            return "{…}"
        if isinstance(value, list):
            return f"[{len(value)} items…]"
        return repr(value)[:80]
    if isinstance(value, dict):
        parts = []
        for k, v in list(value.items())[:14]:
            parts.append(f"{k}: {_shape(v, depth + 1)}")
        if len(value) > 14:
            parts.append("…")
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        return f"[{len(value)} items; first={_shape(value[0], depth + 1)}]"
    return repr(value)[:100]


def _graphql_records(driver):
    requests = {}
    responses = []

    for entry in driver.get_log("performance"):
        try:
            msg = json.loads(entry["message"])["message"]
        except Exception:
            continue

        method = msg.get("method")
        params = msg.get("params", {})

        if method == "Network.requestWillBeSent":
            req = params.get("request", {})
            url = req.get("url", "")
            if "api.schoolnutritionandfitness.com/graphql" not in url:
                continue
            request_id = params.get("requestId")
            post_data = req.get("postData", "")
            parsed = None
            if post_data:
                try:
                    parsed = json.loads(post_data)
                except Exception:
                    parsed = post_data
            requests[request_id] = {
                "url": url,
                "post": parsed,
            }

        elif method == "Network.responseReceived":
            resp = params.get("response", {})
            url = resp.get("url", "")
            if "api.schoolnutritionandfitness.com/graphql" not in url:
                continue
            request_id = params.get("requestId")
            body = None
            try:
                raw = driver.execute_cdp_cmd(
                    "Network.getResponseBody", {"requestId": request_id}
                ).get("body", "")
                if raw:
                    try:
                        body = json.loads(raw)
                    except Exception:
                        body = raw
            except Exception:
                pass
            responses.append(
                {
                    "request_id": request_id,
                    "status": resp.get("status"),
                    "request": requests.get(request_id),
                    "body": body,
                }
            )
    return responses


def discover_unit4_menu(*, wait_seconds: float = 5.0) -> list[dict]:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import Select
    except ImportError as exc:
        raise RuntimeError("Selenium is required for Unit 4 menu discovery") from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1600")
    options.add_argument("--lang=en-US")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    try:
        driver.execute_cdp_cmd("Network.enable", {})

        for group, school, menus in TARGETS:
            for menu in menus:
                print(f"unit4-menus target: {group} -> {school} -> {menu}")

                driver.get(MENU_URL)
                time.sleep(2)

                selects = driver.find_elements(By.TAG_NAME, "select")
                Select(selects[0]).select_by_visible_text(group)
                driver.execute_script(
                    """
                    const el = arguments[0];
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    """,
                    selects[0],
                )
                time.sleep(2)

                selects = driver.find_elements(By.TAG_NAME, "select")
                Select(selects[1]).select_by_visible_text(school)
                driver.execute_script(
                    """
                    const el = arguments[0];
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    """,
                    selects[1],
                )
                time.sleep(2)

                selects = driver.find_elements(By.TAG_NAME, "select")
                if len(selects) < 3:
                    raise RuntimeError(f"menu dropdown not found for {school}")

                menu_select = Select(selects[2])
                available = [o.text.strip() for o in menu_select.options if o.text.strip()]
                print(f"unit4-menus detail: available menus for {school}: {available}")

                if menu not in available:
                    raise RuntimeError(
                        f"{menu!r} not found for {school}; options={available!r}"
                    )

                # Clear all old network events immediately before the menu selection.
                try:
                    driver.get_log("performance")
                except Exception:
                    pass

                menu_select.select_by_visible_text(menu)
                driver.execute_script(
                    """
                    const el = arguments[0];
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    """,
                    selects[2],
                )
                time.sleep(wait_seconds)

                records = _graphql_records(driver)
                print(f"unit4-menus GraphQL calls for {school} / {menu}: {len(records)}")

                for i, record in enumerate(records[:8], 1):
                    req = record.get("request") or {}
                    post = req.get("post")
                    print(f"unit4-menus GraphQL call {i}: status={record.get('status')}")
                    if isinstance(post, dict):
                        if "operationName" in post:
                            print(
                                "  operationName:",
                                post.get("operationName"),
                            )
                        if "variables" in post:
                            print(
                                "  variables:",
                                json.dumps(post.get("variables"), ensure_ascii=False)[:1200],
                            )
                        query = post.get("query")
                        if query:
                            print(
                                "  query sample:",
                                _compact(query, 1000),
                            )
                    elif post:
                        print("  request body:", _compact(str(post), 1200))

                    body = record.get("body")
                    if isinstance(body, (dict, list)):
                        print("  response shape:", _shape(body))
                    elif body:
                        print("  response sample:", _compact(str(body), 1200))

                # Rendered text after selecting the actual menu can be useful too.
                try:
                    body_text = _compact(
                        driver.find_element(By.TAG_NAME, "body").text, 2600
                    )
                except Exception:
                    body_text = ""
                if body_text:
                    print(
                        f"unit4-menus rendered menu sample for {school} / {menu}:"
                    )
                    print(body_text)

        raise RuntimeError(
            "Unit 4 GraphQL discovery completed; live parser not configured yet"
        )
    finally:
        driver.quit()
