"""Reads secrets from environment variables and writes config.py."""
import os

# EMAIL_TO secret should be comma-separated: email1@domain.com,email2@domain.com
email_to = [e.strip() for e in os.environ['EMAIL_TO'].split(',')]

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

print("config.py written successfully.")
