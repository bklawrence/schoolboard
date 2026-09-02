from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlparse


FOOD_SERVICE_URL = "https://www.champaignschools.org/page/food-service"
MOBILE_MENU_URL = "https://www.schoolnutritionandfitness.com/mobile/"


def _compact(text: str, limit: int = 1400) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _json_shape(value: Any, depth: int = 0) -> str:
    if depth >= 2:
        if isinstance(value, dict):
            return "{…}"
        if isinstance(value, list):
            return f"[{len(value)} items…]"
        return repr(value)[:80]
    if isinstance(value, dict):
        parts = []
        for key, child in list(value.items())[:10]:
            parts.append(f"{key}: {_json_shape(child, depth + 1)}")
        return "{" + ", ".join(parts) + (", …" if len(value) > 10 else "") + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        return f"[{len(value)} items; first={_json_shape(value[0], depth + 1)}]"
    return repr(value)[:80]


def _interesting(url: str) -> bool:
    low = (url or "").casefold()
    if not low:
        return False
    if any(noise in low for noise in (
        "google-analytics", "googletagmanager", "fonts.googleapis",
        "fonts.gstatic", "doubleclick", "facebook", "cloudflareinsights",
    )):
        return False
    return any(token in low for token in (
        "schoolnutrition", "webmenus", "/menu", "menus", "meal",
        "nutrition", "district.", "api", "champaignschools",
    ))


def _collect_network(driver) -> tuple[list[str], list[tuple[str, Any]]]:
    urls: list[str] = []
    payloads: list[tuple[str, Any]] = []
    for entry in driver.get_log("performance"):
        try:
            message = json.loads(entry["message"])["message"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if message.get("method") != "Network.responseReceived":
            continue
        params = message.get("params", {})
        response = params.get("response", {})
        url = response.get("url", "")
        request_id = params.get("requestId")
        mime = (response.get("mimeType") or "").casefold()
        rtype = (params.get("type") or "").casefold()
        if not _interesting(url):
            continue
        if url not in urls:
            urls.append(url)
        if not request_id:
            continue
        if rtype not in {"xhr", "fetch"} and "json" not in mime and "api" not in url.casefold():
            continue
        try:
            body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id}).get("body", "")
        except Exception:
            continue
        text = (body or "").strip()
        if not text or text[0] not in "[{":
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        payloads.append((url, payload))
    return urls, payloads


def discover_unit4_menu(*, wait_seconds: float = 6.0) -> list[dict]:
    """Discovery collector. It deliberately returns no meals until Unit 4's live menu identifiers are known."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
    except ImportError as exc:
        raise RuntimeError("Selenium is required for the Unit 4 menu discovery collector") from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--lang=en-US")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        print("unit4-menus detail: opening Unit 4 Food Service page")
        driver.get(FOOD_SERVICE_URL)
        time.sleep(wait_seconds)

        links = []
        for element in driver.find_elements(By.TAG_NAME, "a"):
            try:
                href = element.get_attribute("href") or ""
                label = _compact(element.text, 120)
            except Exception:
                continue
            if _interesting(href) or "menu" in label.casefold():
                links.append((label, href))

        iframes = []
        for frame in driver.find_elements(By.TAG_NAME, "iframe"):
            try:
                src = frame.get_attribute("src") or ""
            except Exception:
                continue
            if src:
                iframes.append(src)

        urls1, payloads1 = _collect_network(driver)

        print("unit4-menus detail: relevant links from district page:")
        for label, href in links[:12]:
            print(f"  - {label!r}: {href}")
        if iframes:
            print("unit4-menus detail: iframe sources:")
            for src in iframes[:12]:
                print(f"  - {src}")
        if urls1:
            print("unit4-menus detail: relevant district-page requests:")
            for url in urls1[:15]:
                print(f"  - {url}")
        if payloads1:
            print("unit4-menus detail: district-page JSON shapes:")
            for i, (url, payload) in enumerate(payloads1[:5], 1):
                print(f"  payload {i} {url}: {_json_shape(payload)}")

        target = None
        for label, href in links:
            low = href.casefold()
            if "schoolnutritionandfitness.com/mobile" in low:
                target = href
                break
        if not target:
            for label, href in links:
                if "schoolnutritionandfitness.com" in href.casefold():
                    target = href
                    break
        target = target or MOBILE_MENU_URL

        # Clear old performance records before opening the menu application.
        try:
            driver.get_log("performance")
        except Exception:
            pass

        print(f"unit4-menus detail: opening menu application {target}")
        driver.get(target)
        time.sleep(wait_seconds)

        print(f"unit4-menus detail: menu application final URL: {driver.current_url}")
        title = driver.title or ""
        if title:
            print(f"unit4-menus detail: menu application title: {title}")

        body = _compact(driver.find_element(By.TAG_NAME, "body").text, 2200)
        if body:
            print("unit4-menus rendered-text sample:")
            print(body)

        menu_links = []
        for element in driver.find_elements(By.TAG_NAME, "a"):
            try:
                href = element.get_attribute("href") or ""
                label = _compact(element.text, 100)
            except Exception:
                continue
            if _interesting(href) or "menu" in label.casefold():
                if (label, href) not in menu_links:
                    menu_links.append((label, href))
        if menu_links:
            print("unit4-menus detail: menu-app links:")
            for label, href in menu_links[:15]:
                print(f"  - {label!r}: {href}")

        selects = []
        for element in driver.find_elements(By.TAG_NAME, "select"):
            try:
                options_text = [_compact(opt.text, 80) for opt in element.find_elements(By.TAG_NAME, "option")]
            except Exception:
                continue
            if options_text:
                selects.append(options_text)
        if selects:
            print("unit4-menus detail: select options:")
            for idx, opts in enumerate(selects[:8], 1):
                print(f"  select {idx}: {opts[:20]}")

        urls2, payloads2 = _collect_network(driver)
        if urls2:
            print("unit4-menus observed requests:")
            for url in urls2[:20]:
                print(f"  - {url}")
        if payloads2:
            print("unit4-menus JSON shapes:")
            for i, (url, payload) in enumerate(payloads2[:8], 1):
                print(f"  payload {i} {url}: {_json_shape(payload)}")

        # Helpful identifiers from any discovered URL.
        ids = set()
        for url in [target, driver.current_url, *urls1, *urls2, *iframes]:
            for match in re.findall(r"(?:id=|/)([0-9a-f]{12,32})(?:[&#/?]|$)", url, re.I):
                ids.add(match)
        if ids:
            print("unit4-menus detail: possible menu/district identifiers:")
            for value in sorted(ids):
                print(f"  - {value}")

        raise RuntimeError("Unit 4 discovery completed; live menu parser not configured yet")
    finally:
        driver.quit()
