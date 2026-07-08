# ─────────────────────────────────────────────────────────────
#  config.py  —  Jira Daily Report Configuration
#
#  Copy this file to config.py and fill in your values.
#  config.py is gitignored — never commit it with real credentials.
# ─────────────────────────────────────────────────────────────

# ── Jira ──────────────────────────────────────────────────────
JIRA_BASE_URL  = "https://your-org.atlassian.net"   # No trailing slash
JIRA_EMAIL     = "you@yourcompany.com"
JIRA_API_TOKEN = "your-jira-api-token"
# Get your token at: https://id.atlassian.com/manage-profile/security/api-tokens
JIRA_PROJECT   = "ABC"   # Your Jira project key

# ── Email (SMTP) ───────────────────────────────────────────────
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587
SMTP_USER     = "you@yourcompany.com"
SMTP_PASSWORD = "your-app-password"
# Gmail: use an App Password (not your login password)
# Create one at: https://myaccount.google.com/apppasswords

EMAIL_FROM = "you@yourcompany.com"
EMAIL_TO   = ["recipient@yourcompany.com"]  # list of recipients
