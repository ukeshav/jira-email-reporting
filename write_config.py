"""Reads secrets from environment variables and writes config.py."""
import os

# Parse EMAIL_TO — handles any format:
#   user@example.com
#   ["user@example.com"]
#   user@a.com,other@b.com
raw = os.environ['EMAIL_TO'].strip().strip('[]')
email_to = [e.strip().strip('"\'') for e in raw.split(',') if e.strip().strip('"\'')]

with open("config.py", "w") as f:
    f.write(f"""JIRA_BASE_URL  = {os.environ['JIRA_BASE_URL']!r}
JIRA_EMAIL     = {os.environ['JIRA_EMAIL']!r}
JIRA_API_TOKEN = {os.environ['JIRA_API_TOKEN']!r}
JIRA_PROJECT   = {os.environ['JIRA_PROJECT']!r}
SMTP_HOST      = {os.environ['SMTP_HOST']!r}
SMTP_PORT      = {int(os.environ['SMTP_PORT'])}
SMTP_USER      = {os.environ['SMTP_USER']!r}
SMTP_PASSWORD  = {os.environ['SMTP_PASSWORD']!r}
EMAIL_FROM     = {os.environ['EMAIL_FROM']!r}
EMAIL_TO       = {email_to!r}
""")

print(f"config.py written successfully. EMAIL_TO={email_to}")
