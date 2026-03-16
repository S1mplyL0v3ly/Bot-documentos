"""Email channel integration via SMTP."""

import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from config import settings


def send_email(
    to: str,
    subject: str,
    body: str,
    attachment_path: Optional[Path] = None,
) -> bool:
    """Send an email with optional attachment via SMTP.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain text body.
        attachment_path: Optional path to file to attach.

    Returns:
        True if sent successfully.
    """
    if not settings.email_host or not settings.email_user:
        print("[Email] SMTP not configured — skipping send.")
        return False

    msg = MIMEMultipart()
    msg["From"] = settings.email_user
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachment_path and attachment_path.exists():
        with attachment_path.open("rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f"attachment; filename={attachment_path.name}",
        )
        msg.attach(part)

    try:
        with smtplib.SMTP(settings.email_host, settings.email_port) as server:
            server.starttls()
            server.login(settings.email_user, settings.email_password)
            server.sendmail(settings.email_user, to, msg.as_string())
        return True
    except smtplib.SMTPException as exc:
        print(f"[Email] Send failed: {exc}")
        return False
