from __future__ import annotations

import json
import re
import time

SID = "1465843288260"
MENU_URL = f"https://champaignschoolsfoodservices.org/index.php?sid={SID}&page=menus"

TARGETS = [
    ("Elementary Schools", "Barkstall Elementary"),
    ("Middle School Menus", "Edison Middle"),
    ("High School Menus", "Central High"),
]


def _compact(text: str, limit: int = 2200) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _interesting_href(href: str) -> bool:
    low = (href or "").casefold()
    if any(x in low for x in (
        "google-analytics", "googletagmanager", "doubleclick",
        "fonts.googleapis", "fonts.gstatic",
    )):
        return False
    return any(token in low for token in (
        ".pdf", "menu", "meal", "webmenu", "snaf-assets",
        "isitesoftware", "nutrition", "automenu",
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
        if url and _interesting_href(url) and url not in urls:
            urls.append(url)
    return urls


def _dump_selects(driver, label: str):
    from selenium.webdriver.common.by import By

    selects = driver.find_elements(By.TAG_NAME, "select")
    print(f"unit4-menus select state for {label}:")
    for i, select in enumerate(selects, 1):
        try:
            opts = [_compact(o.text, 90) for o in select.find_elements(By.TAG_NAME, "option")]
            value = select.get_attribute("value")
        except Exception:
            continue
        print(f"  select {i} value={value!r}: {opts[:30]}")
    return selects


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

        for group, school in TARGETS:
            print(f"unit4-menus target: {group} -> {school}")
            driver.get(MENU_URL)
            time.sleep(2)

            # Select grade band.
            selects = driver.find_elements(By.TAG_NAME, "select")
            if not selects:
                raise RuntimeError("grade-band dropdown not found")
            Select(selects[0]).select_by_visible_text(group)
            driver.execute_script(
                """
                const el = arguments[0];
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                """,
                selects[0],
            )
            time.sleep(3)

            selects = _dump_selects(driver, f"{group} after group selection")
            if len(selects) < 2:
                raise RuntimeError(f"school dropdown did not appear for {group}")

            school_select = Select(selects[1])
            available = [o.text.strip() for o in school_select.options if o.text.strip()]
            if school not in available:
                raise RuntimeError(
                    f"{school!r} not found for {group}; options were {available!r}"
                )

            # Clear network log right before selecting a school.
            try:
                driver.get_log("performance")
            except Exception:
                pass

            school_select.select_by_visible_text(school)
            driver.execute_script(
                """
                const el = arguments[0];
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                """,
                selects[1],
            )
            time.sleep(wait_seconds)

            print(f"unit4-menus detail: {school} final URL: {driver.current_url}")
            _dump_selects(driver, f"{school} after school selection")

            # Links that appear after the individual school is chosen.
            links = []
            for a in driver.find_elements(By.TAG_NAME, "a"):
                try:
                    href = a.get_attribute("href") or ""
                    text = _compact(a.text, 180)
                except Exception:
                    continue
                if href and _interesting_href(href):
                    pair = (text, href)
                    if pair not in links:
                        links.append(pair)

            if links:
                print(f"unit4-menus links after selecting {school}:")
                for text, href in links[:40]:
                    print(f"  - {text!r}: {href}")
            else:
                print(f"unit4-menus links after selecting {school}: none found")

            # Iframes/embedded documents.
            frames = []
            for frame in driver.find_elements(By.TAG_NAME, "iframe"):
                try:
                    src = frame.get_attribute("src") or ""
                except Exception:
                    continue
                if src and src not in frames:
                    frames.append(src)
            if frames:
                print(f"unit4-menus iframes after selecting {school}:")
                for src in frames[:20]:
                    print(f"  - {src}")

            # Buttons can sometimes launch a PDF/menu without an anchor href.
            buttons = []
            for selector in ("button", "input[type=button]", "input[type=submit]"):
                for el in driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        text = _compact(el.text or el.get_attribute("value") or "", 120)
                        onclick = el.get_attribute("onclick") or ""
                    except Exception:
                        continue
                    if text or onclick:
                        buttons.append((text, onclick))
            if buttons:
                print(f"unit4-menus buttons after selecting {school}:")
                for text, onclick in buttons[:25]:
                    print(f"  - text={text!r} onclick={onclick!r}")

            body = _compact(driver.find_element(By.TAG_NAME, "body").text, 2600)
            print(f"unit4-menus rendered sample after selecting {school}:")
            print(body)

            urls = _capture_network(driver)
            if urls:
                print(f"unit4-menus focused requests after selecting {school}:")
                for url in urls[:35]:
                    print(f"  - {url}")
            else:
                print(f"unit4-menus focused requests after selecting {school}: none found")

        raise RuntimeError(
            "Unit 4 representative-school discovery completed; live parser not configured yet"
        )
    finally:
        driver.quit()
