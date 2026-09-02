from __future__ import annotations

import json
import re
import time

SID = "1465843288260"
MENU_URL = f"https://champaignschoolsfoodservices.org/index.php?sid={SID}&page=menus"
TARGET_GROUPS = [
    "Elementary Schools",
    "Middle School Menus",
    "High School Menus",
]


def _compact(text: str, limit: int = 1500) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _interesting_href(href: str) -> bool:
    low = (href or "").casefold()
    return any(token in low for token in (
        ".pdf", "menu", "greenmenu", "isitesoftware", "snaf-assets",
        "webmenus", "schoolnutritionandfitness",
    ))


def _capture_network(driver) -> list[str]:
    urls = []
    for entry in driver.get_log("performance"):
        try:
            msg = json.loads(entry["message"])["message"]
        except Exception:
            continue
        if msg.get("method") != "Network.responseReceived":
            continue
        url = msg.get("params", {}).get("response", {}).get("url", "")
        low = url.casefold()
        if (
            SID in url
            or ".pdf" in low
            or "greenmenu" in low
            or "snaf-assets" in low
            or "menu" in low
        ):
            if url not in urls:
                urls.append(url)
    return urls


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
    options.add_argument("--window-size=1440,1400")
    options.add_argument("--lang=en-US")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.get(MENU_URL)
        time.sleep(wait_seconds)

        selects = driver.find_elements(By.TAG_NAME, "select")
        if not selects:
            raise RuntimeError("Unit 4 menu group dropdown was not found")

        select_el = selects[0]
        selector = Select(select_el)
        available = [o.text.strip() for o in selector.options if o.text.strip()]
        print("unit4-menus detail: available groups:")
        for value in available:
            print(f"  - {value}")

        for group in TARGET_GROUPS:
            # Reload for every group so stale DOM/network state cannot contaminate results.
            driver.get(MENU_URL)
            time.sleep(2)
            try:
                driver.get_log("performance")
            except Exception:
                pass

            select_el = driver.find_elements(By.TAG_NAME, "select")[0]
            selector = Select(select_el)

            print(f"unit4-menus group: {group}")
            selector.select_by_visible_text(group)

            # Fire both change/input to accommodate old Angular/jQuery handlers.
            driver.execute_script(
                """
                const el = arguments[0];
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                """,
                select_el,
            )
            time.sleep(wait_seconds)

            print(f"unit4-menus detail: {group} final URL: {driver.current_url}")

            links = []
            for a in driver.find_elements(By.TAG_NAME, "a"):
                try:
                    href = a.get_attribute("href") or ""
                    text = _compact(a.text, 160)
                except Exception:
                    continue
                if href and _interesting_href(href):
                    pair = (text, href)
                    if pair not in links:
                        links.append(pair)

            if links:
                print(f"unit4-menus links for {group}:")
                for text, href in links[:30]:
                    print(f"  - {text!r}: {href}")
            else:
                print(f"unit4-menus links for {group}: none found")

            iframe_srcs = []
            for frame in driver.find_elements(By.TAG_NAME, "iframe"):
                try:
                    src = frame.get_attribute("src") or ""
                except Exception:
                    continue
                if src and src not in iframe_srcs:
                    iframe_srcs.append(src)
            if iframe_srcs:
                print(f"unit4-menus iframes for {group}:")
                for src in iframe_srcs[:15]:
                    print(f"  - {src}")

            body = _compact(driver.find_element(By.TAG_NAME, "body").text, 1700)
            print(f"unit4-menus rendered sample for {group}:")
            print(body)

            urls = _capture_network(driver)
            if urls:
                print(f"unit4-menus focused requests for {group}:")
                for url in urls[:25]:
                    print(f"  - {url}")

        raise RuntimeError(
            "Unit 4 grade-band link discovery completed; live parser not configured yet"
        )
    finally:
        driver.quit()
