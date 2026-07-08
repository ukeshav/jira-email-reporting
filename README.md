# Jira Email Reporting

Automated daily Jira sprint reports delivered by email. Runs on a schedule via GitHub Actions — no server required.

Each report includes:
- Sprint metadata (name, dates, progress bar)
- KPI dashboard: total tickets, completion rate, bugs, carryover count
- Status breakdown table (To Do / In Progress / Done / Blocked)
- Burndown chart (PNG, embedded inline)
- Epic tracker with health classification (on-track / at-risk / complete)
- Release-wise ticket bifurcation
- Bug list with assignee and priority
- PDF attachment alongside the HTML email

---

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/ukeshav/jira-email-reporting.git
cd jira-email-reporting
pip install -r requirements.txt
```

### 2. Configure

```bash
cp config.example.py config.py
# Edit config.py with your Jira and SMTP credentials
```

| Variable | Description |
|---|---|
| `JIRA_BASE_URL` | Your Jira instance URL, e.g. `https://your-org.atlassian.net` |
| `JIRA_EMAIL` | Your Atlassian account email |
| `JIRA_API_TOKEN` | API token from [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `JIRA_PROJECT` | Jira project key, e.g. `ABC` |
| `SMTP_HOST` | SMTP server (e.g. `smtp.gmail.com`) |
| `SMTP_PORT` | SMTP port (usually `587` for TLS) |
| `SMTP_USER` | SMTP login email |
| `SMTP_PASSWORD` | SMTP password or Gmail App Password |
| `EMAIL_FROM` | Sender address |
| `EMAIL_TO` | List of recipient addresses |

### 3. Run

```bash
python jira_daily_report.py
```

Generates `report_<date>.html` and `report_<date>.pdf`, then emails them to `EMAIL_TO`.

---

## Automated scheduling with GitHub Actions

The included workflow (`.github/workflows/daily_sprint_report.yml`) runs the report automatically every weekday.

**Setup:**

1. Fork or push this repo to GitHub
2. Go to **Settings → Secrets and variables → Actions** and add these repository secrets:

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

3. Push to `main` — the workflow runs daily at the scheduled time (default: 7:45 PM IST / 14:15 UTC, Mon–Fri). Adjust the `cron` line in the workflow file for your timezone.

You can also trigger a run manually from **Actions → Daily Jira Sprint Report → Run workflow**.

---

## Requirements

- Python 3.11+
- `requests`, `matplotlib`, `reportlab` (see `requirements.txt`)
- A Jira account with API access
- An SMTP server (Gmail App Passwords work well)

---

## License

MIT — see [LICENSE](LICENSE).
