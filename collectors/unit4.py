from __future__ import annotations

import json
import re
from typing import Any
from urllib.request import Request, urlopen

SID = "1465843288260"
API_URL = f"https://champaignschoolsfoodservices.org/ngApi/index.php/read?sid={SID}"


def _short(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _shape(value: Any, depth: int = 0) -> str:
    if depth >= 3:
        if isinstance(value, dict):
            return "{…}"
        if isinstance(value, list):
            return f"[{len(value)} items…]"
        return repr(value)[:80]

    if isinstance(value, dict):
        parts = []
        for key, child in list(value.items())[:14]:
            parts.append(f"{key}: {_shape(child, depth + 1)}")
        if len(value) > 14:
            parts.append("…")
        return "{" + ", ".join(parts) + "}"

    if isinstance(value, list):
        if not value:
            return "[]"
        return f"[{len(value)} items; first={_shape(value[0], depth + 1)}]"

    return repr(value)[:100]


def _walk(value: Any, path: str = "$"):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for i, child in enumerate(value[:50]):
            child_path = f"{path}[{i}]"
            yield child_path, child
            yield from _walk(child, child_path)


def _interesting_path(path: str) -> bool:
    low = path.casefold()
    return any(token in low for token in (
        "menu", "site", "group", "school", "location", "meal",
        "category", "calendar", "program",
    ))


def _looks_like_group_list(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    sample = value[:5]
    if not all(isinstance(x, dict) for x in sample):
        return False
    keys = {str(k).casefold() for item in sample for k in item.keys()}
    return bool(keys & {"id", "name", "title", "label", "siteid", "site_id", "groupid", "group_id"})


def _looks_like_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def discover_unit4_menu() -> list[dict]:
    req = Request(
        API_URL,
        headers={
            "User-Agent": "Mozilla/5.0 SchoolBoard/1.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": f"https://champaignschoolsfoodservices.org/index.php?sid={SID}&page=menus",
        },
    )

    print(f"unit4-menus detail: requesting {API_URL}")
    with urlopen(req, timeout=30) as response:
        body = response.read()
        content_type = response.headers.get("content-type", "")
        status = getattr(response, "status", None)

    print(f"unit4-menus detail: HTTP {status}; content-type={content_type}; bytes={len(body)}")

    text = body.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        print("unit4-menus response sample:")
        print(_short(text, 1800))
        raise RuntimeError("Unit 4 ngApi/read endpoint did not return JSON")

    print("unit4-menus JSON shape:")
    print(_shape(payload))

    top = list(payload.keys()) if isinstance(payload, dict) else []
    if top:
        print("unit4-menus top-level keys:")
        print("  " + ", ".join(map(str, top[:40])))

    # Print only useful group-like arrays so we can identify elementary/middle/high IDs.
    seen_lists = 0
    for path, value in _walk(payload):
        if _interesting_path(path) and _looks_like_group_list(value):
            print(f"unit4-menus candidate group list at {path}:")
            for item in value[:20]:
                fields = []
                for key in (
                    "id", "site_id", "siteId", "group_id", "groupId",
                    "name", "title", "label", "description", "slug",
                ):
                    if key in item:
                        fields.append(f"{key}={_short(item[key], 100)!r}")
                if not fields:
                    fields = [f"{k}={_short(v, 80)!r}" for k, v in list(item.items())[:6]]
                print("  - " + ", ".join(fields))
            seen_lists += 1
            if seen_lists >= 8:
                break

    # Print menu/API URLs found inside the payload.
    urls = []
    for path, value in _walk(payload):
        if _looks_like_url(value) and (
            "menu" in value.casefold()
            or "api" in value.casefold()
            or "nutrition" in value.casefold()
        ):
            pair = (path, value)
            if pair not in urls:
                urls.append(pair)

    if urls:
        print("unit4-menus embedded menu/API URLs:")
        for path, value in urls[:20]:
            print(f"  - {path}: {value}")

    # Print scalar values at paths whose names strongly suggest IDs/configuration.
    scalars = []
    for path, value in _walk(payload):
        if isinstance(value, (str, int, float, bool)) and _interesting_path(path):
            low = path.casefold()
            if any(token in low for token in ("id", "endpoint", "url", "slug", "name", "title")):
                entry = (path, value)
                if entry not in scalars:
                    scalars.append(entry)

    if scalars:
        print("unit4-menus useful scalar fields:")
        for path, value in scalars[:60]:
            print(f"  - {path} = {_short(value, 140)!r}")

    raise RuntimeError("Unit 4 ngApi structure discovered; live menu parser not configured yet")
