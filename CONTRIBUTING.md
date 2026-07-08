# Contributing to Jira Email Reporting

Thank you for your interest in contributing! This guide covers everything you need to get started.

---

## Table of contents

- [Code of conduct](#code-of-conduct)
- [How to contribute](#how-to-contribute)
- [Development setup](#development-setup)
- [Project structure](#project-structure)
- [Submitting a pull request](#submitting-a-pull-request)
- [Reporting bugs](#reporting-bugs)
- [Requesting features](#requesting-features)

---

## Code of conduct

This project follows the [Contributor Covenant](https://www.contributor-covenant.org/version/2/1/code_of_conduct/) code of conduct. By participating you agree to abide by its terms. Please be respectful and welcoming to all contributors.

---

## How to contribute

- **Bug fixes** — open a PR with a clear description of the problem and what you changed
- **New features** — open an issue first to discuss the approach before writing code
- **Documentation** — improvements to README, CONTRIBUTING, or inline docs are always welcome
- **Tests** — the project has no automated tests yet; adding them would be a great first contribution

---

## Development setup

### Prerequisites

- Python 3.11+
- A Jira Cloud account with API access
- An SMTP server (Gmail App Passwords work well for testing)

### 1. Fork and clone

```bash
git clone https://github.com/your-username/jira-email-reporting.git
cd jira-email-reporting
```

### 2. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure credentials

```bash
cp config.example.py config.py
# Edit config.py — never commit this file
```

### 4. Run locally

```bash
python jira_daily_report.py
```

This generates `report_<date>.html` and `report_<date>.pdf` in the current directory and sends the email. Check your inbox (or comment out the `send_email()` call to skip sending during development).

---

## Project structure

```
jira_daily_report.py    Main script — fetches Jira data, builds HTML/PDF, sends email
write_config.py         Used by GitHub Actions to write config.py from secrets
config.example.py       Template — copy to config.py and fill in your values
config.py               Your local credentials — gitignored, never committed
requirements.txt        Python dependencies
.github/workflows/      GitHub Actions workflow for scheduled runs
```

### How the report is built

1. **`JiraClient`** — thin wrapper around Jira REST API v3 and Agile API v1, handles pagination automatically (supports both new `nextPageToken` and legacy `startAt` endpoints)
2. **Data fetching** — pulls the active sprint, all issues in the sprint, epics, burndown data, and release versions
3. **Chart generation** — uses Matplotlib (Agg backend, no display required) to produce PNG charts that are base64-encoded and embedded inline in the HTML
4. **HTML email** — single self-contained HTML string with inline styles and embedded images
5. **PDF attachment** — generated via ReportLab alongside the HTML email
6. **Email delivery** — sent via SMTP with `MIMEMultipart` (HTML body + PDF attachment)

---

## Submitting a pull request

1. Create a branch from `main`:
   ```bash
   git checkout -b feat/your-feature-name
   ```
2. Make your changes and commit with a clear message:
   ```bash
   git commit -m "feat: add weekly summary section to report"
   ```
3. Push and open a PR against `main`
4. Fill in the PR description — what changed and why
5. One approval is enough to merge

### Commit message style

Use [Conventional Commits](https://www.conventionalcommits.org/):

| Prefix | When to use |
|---|---|
| `feat:` | New feature |
| `fix:` | Bug fix |
| `docs:` | Documentation only |
| `chore:` | Maintenance, deps, config |
| `refactor:` | Code change with no behaviour change |

---

## Reporting bugs

Open a [GitHub issue](https://github.com/ukeshav/jira-email-reporting/issues/new) and include:

- Python version (`python --version`)
- A description of what happened vs. what you expected
- The full error traceback (redact any credentials)
- Your Jira Cloud plan if relevant (some API endpoints differ between plans)

---

## Requesting features

Open a [GitHub issue](https://github.com/ukeshav/jira-email-reporting/issues/new) and describe:

- The use case — what problem does this solve?
- What the output or behaviour should look like
- Any Jira API endpoints or data fields involved

---

## Questions?

Open a [discussion](https://github.com/ukeshav/jira-email-reporting/discussions) or a GitHub issue — happy to help.
