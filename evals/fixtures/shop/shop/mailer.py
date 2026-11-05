import os
import smtplib
from email.message import EmailMessage

OUTBOX = []  # messages "sent" while SHOP_MAIL is outbox, the default


def send(sender, to, subject, body):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(body)
    if os.environ.get("SHOP_MAIL", "outbox") == "outbox":
        OUTBOX.append(msg)
        return
    with smtplib.SMTP(os.environ.get("SMTP_HOST", "localhost")) as smtp:
        smtp.send_message(msg)
