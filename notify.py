"""Alerting for strong-fit postings.

Primary channel is email (SMTP, e.g. Gmail). Configure these in
.streamlit/secrets.toml (or as environment variables):

    EMAIL_ADDRESS      = "martin.strell@gmail.com"
    EMAIL_APP_PASSWORD = "abcd efgh ijkl mnop"   # Gmail App Password, not your login password
    EMAIL_TO           = "martin.strell@gmail.com"  # optional; defaults to EMAIL_ADDRESS

Gmail requires an App Password (Google Account -> Security -> 2-Step Verification
-> App passwords), since normal passwords are rejected for SMTP.

If email isn't configured, falls back to a macOS desktop notification. Either
way the alert is always printed to stdout.
"""
import shutil
import smtplib
import subprocess
from email.message import EmailMessage

from evaluator import get_keys


def _send_email(subject: str, body: str, cfg: dict) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["EMAIL_ADDRESS"]
    msg["To"] = cfg["EMAIL_TO"] or cfg["EMAIL_ADDRESS"]
    msg.set_content(body)
    host = cfg["SMTP_HOST"] or "smtp.gmail.com"
    port = int(cfg["SMTP_PORT"] or 465)
    with smtplib.SMTP_SSL(host, port) as server:
        server.login(cfg["EMAIL_ADDRESS"], cfg["EMAIL_APP_PASSWORD"])
        server.send_message(msg)


def _desktop(title: str, message: str) -> None:
    if shutil.which("osascript"):
        t = title.replace('"', "'")
        m = message.replace('"', "'")
        try:
            subprocess.run(
                ["osascript", "-e", f'display notification "{m}" with title "{t}"'],
                check=False,
                capture_output=True,
            )
        except Exception:
            pass


def notify(title: str, message: str, url: str | None = None) -> None:
    print(f"[ALERT] {title} — {message}" + (f"  {url}" if url else ""))
    cfg = get_keys()
    if cfg["EMAIL_ADDRESS"] and cfg["EMAIL_APP_PASSWORD"]:
        body = message + (f"\n\n{url}" if url else "")
        try:
            _send_email(title, body, cfg)
            return
        except Exception as e:
            print(f"  ! email alert failed ({e}); falling back to desktop notification")
    _desktop(title, message)
