# Chambana Schoolboard

A local school-calendar aggregator for Champaign–Urbana families.

## What this first live version does

The visible site remains `index.html`. It reads `schoolboard-data.json` when that file is available and retains the old embedded demo data as a browser-side fallback.

`build_data.py` creates `schoolboard-data.json` by combining:

- the existing static event baseline in `data/static-events.json`, and
- a live server-side fetch of the Uni High Snap! athletics iCalendar feed.

The GitHub Action in `.github/workflows/update-data.yml` runs the build every four hours and commits `schoolboard-data.json` only when the data changes.

## Run locally

```bash
python build_data.py
python -m http.server 8000
```

Then open `http://localhost:8000` in a browser.

`python build_data.py --offline` rebuilds without contacting Snap and retains any cached Uni Snap events already present in `schoolboard-data.json`.

## Why the collector is separate from the webpage

Browser JavaScript is subject to CORS and other browser security rules. The collector runs outside the browser, reads public source feeds, normalizes them, and gives the webpage one same-origin JSON file to consume.

## Next source

After confirming the Snap collector works in GitHub Actions, add lunch-menu collection as a separate module under `collectors/` rather than changing the frontend.
