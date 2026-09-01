# ===============================================================================
# Module:   app/mail.py
# Purpose:  Plain-text email sending via SMTP. Two callers: the
#           password-reset flow (mail TO a member) and notify_admin
#           (mail ABOUT a member, to the operator). Reads SMTP settings
#           from app.config — those come from /etc/challenge.env on prod
#           and from os.environ defaults locally.
#
#           When SMTP_HOST isn't configured (e.g. local dev with no creds)
#           the wrapper falls back to logging the message instead of
#           attempting a connection, so the reset flow can be exercised
#           end-to-end without ever sending mail.
# Author:   Jamie F. Weleber
# Created:  May 19, 2026
# ===============================================================================
"""SMTP wrapper for transactional email."""

import smtplib                              # stdlib SMTP client — no third-party mail library needed
import ssl                                  # TLS context for the STARTTLS handshake
from email.message import EmailMessage      # stdlib RFC 5322 message builder

from flask import current_app


def send_email(to, subject, body):
    """Send a plain-text email. Returns True on success, False on
    failure. Failures are logged but never raised.

    The "swallow exceptions" behavior is deliberate: the password
    reset route always shows the same generic "if your email is
    registered, a link is on its way" success message regardless of
    whether SMTP actually worked. That uniformity matters for UX
    (consistent feedback) AND for security (a downed SMTP server
    shouldn't leak existence of an account via response timing).

    Local-dev fallback: when SMTP_HOST isn't set, the message is
    logged to Flask's logger instead of being sent. That lets the
    reset flow be exercised on a laptop without real SMTP creds.
    """
    host = current_app.config.get("SMTP_HOST")
    if not host:
        # No SMTP configured — print to gunicorn / flask console so we
        # can verify the email content during local development.
        current_app.logger.info(
            "MAIL (SMTP_HOST not configured — logging instead of sending)\n"
            "  To: %s\n"
            "  Subject: %s\n"
            "  Body:\n%s",
            to, subject, body,
        )
        return True

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = current_app.config.get("MAIL_FROM")
    msg["To"] = to
    msg.set_content(body)

    try:
        # SMTP submission on port 587 uses STARTTLS. The with-block
        # cleans up the connection even if any step raises. We EHLO
        # twice — once before STARTTLS to advertise capabilities,
        # once after to refresh them post-handshake (some servers
        # advertise different capabilities once the channel is
        # encrypted, e.g. AUTH PLAIN/LOGIN).
        #
        # Explicit 10-second timeout matters more than it sounds:
        # smtplib's default is socket._GLOBAL_DEFAULT_TIMEOUT (None =
        # no timeout), which means a slow / firewalled SMTP server
        # blocks the gunicorn worker indefinitely. Gunicorn's own
        # 30-second request timeout would then kill the worker mid-
        # connect — visible to the user as a 500 error. With a finite
        # timeout, smtplib raises socket.timeout, the except below
        # catches it, send_email returns False, and the route carries
        # on to flash the generic success message to the user.
        with smtplib.SMTP(host, current_app.config["SMTP_PORT"], timeout=10) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(
                current_app.config["SMTP_USER"],
                current_app.config["SMTP_PASSWORD"],
            )
            server.send_message(msg)
        return True
    except Exception as exc:
        # Log enough to debug from the journal without leaking the
        # message body (it can contain reset tokens etc.).
        current_app.logger.error("Mail send to %r failed: %s", to, exc)
        return False


def notify_admin(subject, body):
    """Send an operational notification to NOTIFY_EMAIL.

    Returns True if the message was handed to send_email, False if
    notifications aren't configured or anything went wrong.

    This function never raises. That is the entire point of it existing
    rather than callers reaching for send_email directly, and it matters
    more than it looks: these notifications fire in the middle of a
    member registering an account and a member submitting a completion
    they just spent four hours earning. An SMTP outage, a typo in the
    configured address, a DNS hiccup — none of those are allowed to cost
    somebody their submission. send_email already swallows connection
    failures, but the config lookup and the handoff live out here, so
    the belt goes with the braces.

    A missing NOTIFY_EMAIL is treated as "notifications are off" rather
    than as an error, so local dev and any pre-configuration production
    window are both silent instead of noisy.

    Subjects get a "[Challenge]" prefix so they're filterable in a mail
    client without having to match on wording that might change.
    """
    try:
        to = current_app.config.get("NOTIFY_EMAIL")
        if not to:
            return False
        return send_email(to, f"[Challenge] {subject}", body)
    except Exception as exc:
        # Belt and braces. If we get here something is wrong with the
        # config rather than the network, so it's worth a log line — but
        # still not worth failing the member's request over.
        current_app.logger.error("Admin notification failed: %s", exc)
        return False
