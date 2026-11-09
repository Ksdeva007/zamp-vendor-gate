"""Sends the message to the vendor by email, when an SMTP server is configured."""
import os
import smtplib
from email.message import EmailMessage


def available():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def send(to, subject, body):
    """Raises smtplib.SMTPException, OSError or ValueError if the message can't be sent."""
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "587"))
    secure_from_start = port == 465
    with (smtplib.SMTP_SSL if secure_from_start else smtplib.SMTP)(os.environ["SMTP_HOST"], port, timeout=20) as server:
        if not secure_from_start:
            server.starttls()
        if os.environ.get("SMTP_USER"):
            server.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        server.send_message(msg)
