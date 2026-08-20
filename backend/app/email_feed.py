"""Outbound transactional email (password reset only, today), using only
the standard library (urllib) so no extra dependency is required — same
philosophy as price_feed.py.

Sends via Resend's HTTP API (https://resend.com) rather than SMTP: one
JSON POST, no SMTP library/connection-handling needed, and a generous free
tier that needs no credit card to start. Requires a `RESEND_API_KEY`
environment variable — get one from the Resend dashboard after verifying a
sending domain (or use their shared `onboarding@resend.dev` sender for
testing, which works with no domain verification but only delivers to the
email address on your own Resend account).

Left unset, this feature silently does nothing beyond logging — same
pattern as ODDS_API_KEY (see README): forgot-password requests still
return a generic "if that account has an email on file..." response
either way, so an unconfigured deployment doesn't leak whether email
sending is on, and doesn't 500 for the visitor either.
"""
import json
import os
import urllib.request
import urllib.error

RESEND_API_URL = "https://api.resend.com/emails"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
# Must be an address on a domain verified with Resend, unless you're using
# their onboarding@resend.dev test sender (see module docstring).
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "UNICORN <onboarding@resend.dev>")
TIMEOUT_SECONDS = 10


class EmailFeedError(Exception):
    pass


def is_configured() -> bool:
    return bool(RESEND_API_KEY)


def send_password_reset_email(to_email: str, reset_url: str) -> None:
    if not RESEND_API_KEY:
        raise EmailFeedError("RESEND_API_KEY is not set — email sending is not configured")

    subject = "Reset your UNICORN password"
    text_body = (
        "Someone (hopefully you) asked to reset the password on your UNICORN account.\n\n"
        f"Reset it here: {reset_url}\n\n"
        "This link works once and expires in 60 minutes. If you didn't request this, "
        "you can safely ignore this email — your password hasn't been changed."
    )
    html_body = (
        f'<p>Someone (hopefully you) asked to reset the password on your UNICORN account.</p>'
        f'<p><a href="{reset_url}">Click here to reset it</a>.</p>'
        f'<p style="color:#666;font-size:13px;">This link works once and expires in 60 minutes. '
        f"If you didn't request this, you can safely ignore this email — your password hasn't "
        f'been changed.</p>'
    )
    payload = json.dumps({
        "from": RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "text": text_body,
        "html": html_body,
    }).encode("utf-8")

    req = urllib.request.Request(
        RESEND_API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            resp.read()  # drain, nothing in the response body is needed on success
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise EmailFeedError(f"Resend API returned {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise EmailFeedError(f"Network error sending email via Resend: {e}") from e
