# Jira Email Reporting

Automated daily Jira sprint reports delivered straight to your inbox. Runs on a cron schedule via GitHub Actions — no server required.

![Python](https://img.shields.io/badge/python-3.11+-blue) ![License](https://img.shields.io/badge/license-MIT-blue) ![GitHub Actions](https://img.shields.io/badge/CI-GitHub%20Actions-2088FF?logo=github-actions&logoColor=white)

---

## What's in each report

| Section | Description |
|---|---|
| Sprint meta | Name, dates, duration, days remaining, state |
| KPI banner | Total tickets, % completion, bugs, carryover count |
| Status summary | Pie chart + table of all Jira statuses |
| Issue type breakdown | Pie chart by Bug / Story / Task / Epic |
| Epics & Stories | Combined tracker table |
| Bug status breakdown | Pie chart + table |
| Release-wise bifurcation | Bar chart + table grouped by fix version |
| App version bifurcation | Pie chart (Android / iOS / Web / Backend / Admin) |
| Per-person allocation | Full table with individual and team totals |
| Sprint burndown | Line chart (issues remaining by day) + data table |
| Bug sheet | Complete bug list with assignee, priority, status |

All charts are embedded inline as base64 PNGs — no external assets, works in any email client. A PDF copy is attached to every email.

---

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/ukeshav/jira-email-reporting.git
cd jira-email-reporting
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp config.example.py config.py
# Edit config.py — this file is gitignored and never committed
```

| Variable | Description |
|---|---|
| `JIRA_BASE_URL` | Your Jira instance URL, e.g. `https://your-org.atlassian.net` |
| `JIRA_EMAIL` | Your Atlassian account email |
| `JIRA_API_TOKEN` | API token — create at [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `JIRA_PROJECT` | Jira project key, e.g. `PROJ` |
| `SMTP_HOST` | SMTP server, e.g. `smtp.gmail.com` |
| `SMTP_PORT` | Usually `587` for STARTTLS |
| `SMTP_USER` | SMTP login email |
| `SMTP_PASSWORD` | SMTP password or [Gmail App Password](https://myaccount.google.com/apppasswords) |
| `EMAIL_FROM` | Sender address shown in the email |
| `EMAIL_TO` | List of recipient addresses |

### 3. Run

```bash
python jira_daily_report.py
```

Generates `report_<date>.html` and `report_<date>.pdf` in the current directory, then emails them to `EMAIL_TO`.

---

## Automated scheduling with GitHub Actions

The included workflow (`.github/workflows/daily_sprint_report.yml`) runs every weekday automatically — no server needed.

**Setup:**

1. Fork or push this repo to GitHub
2. Go to **Settings → Secrets and variables → Actions** and add:

   | Secret | Value |
   |---|---|
   | `JIRA_BASE_URL` | Your Jira URL |
   | `JIRA_EMAIL` | Your Atlassian email |
   | `JIRA_API_TOKEN` | Your Jira API token |
   | `JIRA_PROJECT` | Project key |
   | `SMTP_HOST` | SMTP server |
   | `SMTP_PORT` | SMTP port |
   | `SMTP_USER` | SMTP login |
   | `SMTP_PASSWORD` | SMTP password |
   | `EMAIL_FROM` | Sender address |
   | `EMAIL_TO` | Recipient address(es), comma-separated |

3. Push to `main` — the workflow fires daily at **14:15 UTC (7:45 PM IST), Mon–Fri**

To change the schedule, edit the `cron` line in `.github/workflows/daily_sprint_report.yml`:

```yaml
# Common timezone conversions:
#   IST (India)      → "15 14 * * 1-5"   (7:45 PM IST = 14:15 UTC)
#   SGT (Singapore)  → "0 0 * * 1-5"     (8:00 AM SGT = 00:00 UTC)
#   GMT (London)     → "0 8 * * 1-5"
#   EST (New York)   → "0 13 * * 1-5"
```

You can also trigger a run manually: **Actions → Daily Jira Sprint Report → Run workflow**.

Generated HTML and PDF are uploaded as build artifacts (retained for 30 days) so you can download them without email access.

---

## How it works

```
JiraClient  ──►  fetch active sprint + all issues
                 fetch epics, burndown, fix versions
                     │
                     ▼
             build charts (Matplotlib, Agg backend)
             encode charts as base64 PNG
                     │
                     ▼
             render HTML email  +  generate PDF (ReportLab)
                     │
                     ▼
             send via SMTP (HTML body + PDF attachment)
```

The script supports both the modern Jira `/search/jql` (nextPageToken) and the legacy `/search` (startAt) pagination APIs — it detects which one your instance uses automatically.

---

## Requirements

- Python 3.11+
- `requests` `matplotlib` `reportlab` (see `requirements.txt`)
- A Jira Cloud account with API access (`read:jira-work` scope)
- An SMTP server

---

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, project structure, and PR guidelines.

Please read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before participating.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## License

MIT — see [LICENSE](LICENSE).
