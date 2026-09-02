from __future__ import annotations

import json
import re
import time
from typing import Any

SID = "1465843288260"
CANDIDATE_URLS = [
    f"https://champaignschoolsfoodservices.org/index.php?sid={SID}&page=menus",
    f"https://www.schoolnutritionandfitness.com/index.php?sid={SID}&page=menus",
    f"https://www.schoolnutritionandfitness.com/mobile/?sid={SID}",
]


def _compact(text: str, limit: int = 2200) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _interesting(url: str) -> bool:
    low = (url or "").casefold()
    if not low:
        return False
    if any(x in low for x in (
        "google-analytics", "googletagmanager", "doubleclick",
        "fonts.googleapis", "fonts.gstatic", "facebook",
    )):
        return False
    return any(x in low for x in (
        "schoolnutrition", "champaignschoolsfoodservices",
        "menu", "meal", "nutrition", "webmenu", "sid=",
    ))


def _collect_urls(driver) -> list[str]:
    urls = []
    for entry in driver.get_log("performance"):
        try:
            msg = json.loads(entry["message"])["message"]
        except Exception:
            continue
        if msg.get("method") != "Network.responseReceived":
            continue
        url = msg.get("params", {}).get("response", {}).get("url", "")
        if _interesting(url) and url not in urls:
            urls.append(url)
    return urls


def _select_options(driver):
    from selenium.webdriver.common.by import By

    found = []
    for select in driver.find_elements(By.TAG_NAME, "select"):
        try:
            opts = [_compact(o.text, 90) for o in select.find_elements(By.TAG_NAME, "option")]
        except Exception:
            continue
        opts = [o for o in opts if o]
        if opts:
            found.append(opts)
    return found


def discover_unit4_menu(*, wait_seconds: float = 5.0) -> list[dict]:
    """Targeted Unit 4 discovery using the district's known SchoolNutritionAndFitness SID."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
    except ImportError as exc:
        raise RuntimeError("Selenium is required for Unit 4 menu discovery") from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--lang=en-US")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    try:
        best = None

        for target in CANDIDATE_URLS:
            try:
                driver.get_log("performance")
            except Exception:
                pass

            print(f"unit4-menus detail: trying {target}")
            try:
                driver.get(target)
                time.sleep(wait_seconds)
            except Exception as exc:
                print(f"unit4-menus detail: navigation failed: {type(exc).__name__}: {exc}")
                continue

            final_url = driver.current_url
            title = driver.title or ""
            try:
                body = _compact(driver.find_element(By.TAG_NAME, "body").text)
            except Exception:
                body = ""

            selects = _select_options(driver)
            urls = _collect_urls(driver)

            score = len(body) + 500 * len(selects)
            candidate = (score, target, final_url, title, body, selects, urls)
            if best is None or candidate[0] > best[0]:
                best = candidate

            # Stop early if this clearly looks like the Unit 4 menu application.
            low = body.casefold()
            if (
                ("breakfast" in low or "lunch" in low)
                and (
                    "barkstall" in low
                    or "centennial" in low
                    or "central" in low
                    or "champaign" in low
                )
            ):
                break

        if best is None:
            raise RuntimeError("none of the Unit 4 SID menu URLs could be opened")

        _, target, final_url, title, body, selects, urls = best

        print(f"unit4-menus detail: best target: {target}")
        print(f"unit4-menus detail: final URL: {final_url}")
        if title:
            print(f"unit4-menus detail: title: {title}")

        if selects:
            print("unit4-menus select options:")
            for i, opts in enumerate(selects[:6], 1):
                print(f"  select {i}: {opts[:30]}")
        else:
            print("unit4-menus select options: none found")

        if body:
            print("unit4-menus rendered-text sample:")
            print(body)

        focused = [
            u for u in urls
            if SID in u or any(x in u.casefold() for x in ("menu", "meal", "webmenu"))
        ]
        if focused:
            print("unit4-menus focused requests:")
            for u in focused[:12]:
                print(f"  - {u}")
        else:
            print("unit4-menus focused requests: none found")

        raise RuntimeError(
            f"Unit 4 SID discovery completed for sid={SID}; live parser not configured yet"
        )
    finally:
        driver.quit()
