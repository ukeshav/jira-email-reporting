# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Added
- `config.example.py` — template for local credentials setup
- `CONTRIBUTING.md` — developer guide, project structure, PR process
- `CODE_OF_CONDUCT.md` — Contributor Covenant v2.1
- `CHANGELOG.md` — this file
- `LICENSE` — MIT license
- Expanded `.gitignore` to cover `venv/`, `.env`, IDE directories
- Full `README.md` with setup guide and GitHub Actions secrets reference

### Changed
- Scrubbed all internal company/domain references from source files

---

## [1.0.0] — Initial release

### Added
- Sprint metadata section (name, dates, duration, state)
- KPI dashboard banner (total tickets, completion %, bugs, carryover)
- Overall status summary with pie chart
- Issue type breakdown with pie chart
- Epics & Stories combined table
- Bug status breakdown with pie chart
- Release-wise bifurcation bar chart and table
- App version bifurcation pie chart (Android / iOS / Web / Backend / Admin)
- Sprint task allocation per-person table with totals
- Sprint burndown chart (issues remaining by day) with data table
- Bug sheet with full details (all columns)
- PDF attachment generated via ReportLab
- HTML email with all charts base64-encoded inline (no external assets)
- SMTP delivery with `MIMEMultipart`
- GitHub Actions workflow for scheduled weekday runs
- `write_config.py` for injecting secrets safely in CI
- Automatic pagination supporting both Jira `nextPageToken` and legacy `startAt` APIs
