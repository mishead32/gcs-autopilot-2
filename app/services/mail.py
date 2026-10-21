"""Sending email, over plain SMTP.

Until now this software sent nothing by email at all. WhatsApp and SMS were
written into a queue for a provider to drain later, and that was the whole
notification channel. Escalation is the first thing here that genuinely has
to arrive in somebody's inbox: the HOD, the EA and the CMD are not sitting
in front of the dashboard, and a report that the CMD never reads is the same
as no report.

So this is deliberately the smallest thing that works, with no account
anywhere to sign up for:

    MAIL_HOST       smtp.gmail.com
    MAIL_PORT       587      (587 = STARTTLS, 465 = SSL from the first byte)
    MAIL_USER       the mailbox it signs in as
    MAIL_PASS       that mailbox's app password
    MAIL_FROM       what the recipient sees it came from; defaults to USER
    MAIL_FROM_NAME  the name beside the address
    MAIL_REPLY_TO   where a reply should go, if not the sending mailbox

Two rules about the password, and they are the same rules as every other
secret in this app:

  * It is read from the environment. It is never written to the database,
    never into a template, never into a log line and never into an error
    message shown on a page — `_safe` below exists only to strip it out of
    whatever the mail server says back.
  * It is set in Render's own Environment tab and nowhere else. Not in the
    repository, not in a message, not in a chat.

With nothing configured `enabled()` is False and `send()` returns a plain
"not configured" rather than raising. That matters more than it looks: the
escalation that could not be sent is still written down, with the reason on
it, so switching the mail account on later shows exactly what the morning
would have sent.
"""
from __future__ import annotations

import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate

HOST = os.getenv("MAIL_HOST", "").strip()
PORT = int(os.getenv("MAIL_PORT", "587") or 587)
USER = os.getenv("MAIL_USER", "").strip()
PASS = os.getenv("MAIL_PASS", "")
FROM = os.getenv("MAIL_FROM", "").strip() or USER
FROM_NAME = os.getenv("MAIL_FROM_NAME", "").strip() or "GCS Autopilot"
REPLY_TO = os.getenv("MAIL_REPLY_TO", "").strip()
TIMEOUT = float(os.getenv("MAIL_TIMEOUT", "20") or 20)

# Good enough to catch a typo and a blank, and nothing more. A stricter
# pattern rejects addresses that are perfectly valid and the person on the
# other end of it cannot tell why.
ADDRESS = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def enabled() -> bool:
    """True when there is somewhere to send from."""
    return bool(HOST and FROM)


def status() -> str:
    """One line for the Setup page — never the password, not even its length."""
    if not HOST:
        return "No mail server set. Escalations will be written down but not sent."
    if not FROM:
        return f"{HOST} is set, but MAIL_FROM is empty, so nothing can be sent."
    who = f"{FROM} via {HOST}:{PORT}"
    return who if USER else f"{who} (no sign-in)"


def valid(address: str) -> bool:
    return bool(ADDRESS.match((address or "").strip()))


def clean(addresses) -> list[str]:
    """Keep the ones that are addresses, drop blanks and duplicates."""
    out: list[str] = []
    for raw in addresses or []:
        a = (raw or "").strip()
        if valid(a) and a.lower() not in {x.lower() for x in out}:
            out.append(a)
    return out


def _safe(text: str) -> str:
    """An error message with no secret left in it.

    A mail server that refuses a sign-in often quotes the attempt back, and
    that message ends up stored on the escalation row and shown on a page.
    """
    msg = str(text)
    for secret in (PASS, os.getenv("MAIL_PASS", "")):
        if secret and len(secret) > 3:
            msg = msg.replace(secret, "********")
    return msg[:400]


def send(to: list[str], subject: str, body: str,
         cc: list[str] | None = None, html: str = "") -> tuple[bool, str]:
    """Send one message. Returns (sent, reason-when-not).

    With `html` the message goes out as multipart/alternative: the HTML
    version for the mail clients that can show it, the plain text for the
    ones that cannot. Both say the same thing, which is the point — a
    recipient whose client strips HTML still gets the whole list rather
    than an empty email, and a spam filter scoring a message with an HTML
    part and no text part treats it worse.

    Never raises. The caller is a background job running at nine in the
    morning: a refused connection must leave a record and move on to the
    next person, not end the run.
    """
    to = clean(to)
    cc = clean(cc)
    # Somebody on both lines gets one copy, on the To line where it belongs.
    cc = [a for a in cc if a.lower() not in {t.lower() for t in to}]

    if not to:
        return False, "No valid address to send to."
    if not enabled():
        return False, "No mail server is set up yet."

    msg = EmailMessage()
    msg["From"] = formataddr((FROM_NAME, FROM))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if REPLY_TO:
        msg["Reply-To"] = REPLY_TO
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    # Marks it as a machine's message, so a mail server does not answer the
    # auto-reply of an out-of-office with another escalation email.
    msg["Auto-Submitted"] = "auto-generated"
    # Text first, then HTML as the alternative. The order matters: a mail
    # client shows the LAST part it understands, so text-then-HTML means an
    # HTML client shows the table and a plain one shows the text.
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        if PORT == 465:
            server = smtplib.SMTP_SSL(HOST, PORT, timeout=TIMEOUT,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(HOST, PORT, timeout=TIMEOUT)
        with server:
            if PORT != 465:
                try:
                    server.starttls(context=ssl.create_default_context())
                except smtplib.SMTPNotSupportedError:
                    # A mail server on the same machine with no TLS at all.
                    # Allowed, because the message never leaves the host.
                    pass
            if USER:
                server.login(USER, PASS)
            server.send_message(msg, to_addrs=to + cc)
        return True, ""
    except Exception as exc:                      # noqa: BLE001 — see docstring
        return False, _safe(f"{type(exc).__name__}: {exc}")
