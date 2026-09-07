# Chambana Schoolboard

[**Chambana Schoolboard**](https://www.chambanaschoolboard.com/) is a free, unofficial calendar and school-information aggregator for families in Champaign-Urbana, Illinois.

Local school and family information is often distributed across district calendars, individual school sites, athletics platforms, lunch systems, libraries, park districts, PDFs, and other public pages. Chambana Schoolboard brings that information into a single, family-oriented view.

## Current features

- Combined daily and weekly calendar views
- User-selected schools and community organizations
- School calendars and selected school events
- Athletics
- Lunch menus
- Youth and family library programming
- Free youth and family park district events
- English, Spanish, and French interface labels
- Home Screen installation on supported mobile devices
- Local persistence of school selections
- Links back to source pages and registration information when available

The site is designed to remain lightweight and usable without an account.

## Coverage

Chambana Schoolboard currently draws from public information published by organizations in the Champaign-Urbana area, including:

- Urbana School District 116
- Champaign Unit 4
- University High School
- selected local independent and private schools
- The Urbana Free Library
- Champaign Public Library
- Urbana Park District
- Champaign Park District
- public school lunch and menu systems

Coverage is necessarily uneven. Different organizations publish information in different formats, and some sources are easier to collect reliably than others. A source appearing on Chambana Schoolboard does not imply affiliation, endorsement, or participation by that organization.

## How it works

The project uses a collection-and-build pipeline rather than a conventional application server.

Python collectors retrieve publicly available calendar, athletics, menu, library, and park-district information. `build_data.py` normalizes those sources into `schoolboard-data.json`. A GitHub Actions workflow rebuilds the data on a schedule and publishes the static site through GitHub Pages.

The front end is contained primarily in `index.html`. School and community selections are stored locally in the user's browser. When the installed Home Screen version returns to the foreground, it checks for current SchoolBoard data while preserving those local selections.

### Repository structure

```text
.
├── .github/workflows/       # automated collection and GitHub Pages deployment
├── collectors/              # source-specific Python collectors
├── data/                    # supporting source data where needed
├── tests/                   # collector and data checks
├── build_data.py            # orchestrates collection and normalization
├── index.html               # public interface
├── schoolboard-data.json    # generated site data
├── manifest.webmanifest     # Home Screen / web-app metadata
└── README.md
```

## Data quality and source responsibility

Chambana Schoolboard is an aggregation service, not an official record. Public source pages remain authoritative.

Collectors are designed to prefer omission over confidently presenting information that cannot be tied to the correct event or source. Even so, websites change, calendars are revised, events are canceled, and automated parsing can fail.

For time-sensitive or consequential information, users should confirm details with the linked school, district, library, park district, or other original source.

## AI-assisted development

This project has been developed with substantial assistance from an **OpenAI ChatGPT agent**.

The agent has been used as part of the development workflow to research public data-source structures, write and revise collectors, troubleshoot parsing and deployment problems, develop interface behavior, identify edge cases, and prepare code revisions. The project owner directs the work, reviews proposed changes, uploads revisions, and tests the live site.

AI assistance is therefore part of the project's development process; it is not presented as a substitute for source verification or human review. The production site relies on deterministic code and public source data rather than an AI model generating calendar entries for visitors in real time.

## Privacy

Chambana Schoolboard does not require users to create an account. School selections are stored locally in the browser using web storage.

The site aggregates publicly available institutional information; it is not intended to collect or publish private student information.

## Status

This is an active, evolving community project. New sources are added as reliable collection methods become available, and existing collectors may need adjustment when source websites change.

Bug reports are especially useful when they identify:

- an incorrect date, time, location, or registration link
- a duplicated or missing event
- a source that has stopped updating
- a school or community source that would be useful to add

## Disclaimer

Chambana Schoolboard is an independent project and is not affiliated with, sponsored by, or endorsed by any school district, school, library, park district, municipality, or other organization whose public information may appear on the site.

Always consult the original source for official information.
