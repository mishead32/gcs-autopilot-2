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

# ----------------------------------------------- sending over HTTPS instead --
# Render's free web services block outbound traffic to the SMTP ports — 25,
# 465 and 587 — so smtplib cannot open the connection at all and every send
# comes back "[Errno 101] Network is unreachable". Nothing about the mailbox
# or the password is wrong; the port is simply not reachable from there.
#
# The way out that does not cost anything is to hand the message to a mail
# provider over ordinary HTTPS, which is not blocked. One extra environment
# variable each, and the rest of this module does not change:
#
#     MAIL_API       brevo | resend
#     MAIL_API_KEY   that provider's key
#     MAIL_FROM      still the address it comes from
#
# Set, it is used instead of SMTP. Unset, nothing changes for anybody.
API = os.getenv("MAIL_API", "").strip().lower()
API_KEY = os.getenv("MAIL_API_KEY", "").strip()

APIS = {
    "brevo": ("https://api.brevo.com/v3/smtp/email", "api-key"),
    "resend": ("https://api.resend.com/emails", "bearer"),
}

# Good enough to catch a typo and a blank, and nothing more. A stricter
# pattern rejects addresses that are perfectly valid and the person on the
# other end of it cannot tell why.
ADDRESS = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def api_ready() -> bool:
    """True when a provider's HTTPS interface is set up and usable."""
    return bool(API in APIS and API_KEY and FROM)


def enabled() -> bool:
    """True when there is somewhere to send from — either way."""
    return bool(FROM and (api_ready() or HOST))


def status() -> str:
    """One line for the Setup page — never the password, not even its length."""
    if api_ready():
        return f"{FROM} via {API} over HTTPS"
    if API and not API_KEY:
        return f"MAIL_API is {API}, but MAIL_API_KEY is empty."
    if API and API not in APIS:
        return (f"MAIL_API is “{API}”, which is not one this app knows. "
                f"Use one of: {', '.join(sorted(APIS))}.")
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


def advice(why: str) -> str:
    """What to do about a failure whose own words do not say.

    One case so far, and it is the one that wastes the most time: nothing
    is wrong with the mailbox or the password, the SMTP port is simply not
    reachable from this host. Separate from send() so that whoever SHOWS a
    failure can add it too — a message stored before this existed, or one
    handed back by something other than smtplib, still gets the hint.
    """
    if "Network is unreachable" in (why or "") or "Errno 101" in (why or ""):
        return (" — this host blocks the SMTP ports. Send over HTTPS "
                "instead: set MAIL_API and MAIL_API_KEY.")
    return ""


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

    # Over HTTPS when a provider is configured, because the SMTP ports are
    # blocked wherever this is likely to be hosted. Tried first rather than
    # as a fallback: falling back would mean every send waiting out an SMTP
    # timeout before doing the thing that works.
    if api_ready():
        return _send_api(to, subject, body, cc, html)

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
        why = _safe(f"{type(exc).__name__}: {exc}")
        return False, why + advice(why)


def _send_api(to: list[str], subject: str, body: str,
              cc: list[str], html: str) -> tuple[bool, str]:
    """Hand the message to a provider over HTTPS. Never raises.

    Two providers, because two is enough to not be stuck with one and few
    enough to keep this readable. Both take the same five things; only the
    shape of the request differs.
    """
    import json
    import urllib.error
    import urllib.request

    url, auth = APIS[API]
    if API == "brevo":
        payload = {
            "sender": {"email": FROM, "name": FROM_NAME},
            "to": [{"email": a} for a in to],
            "subject": subject,
            "textContent": body,
        }
        if html:
            payload["htmlContent"] = html
        if cc:
            payload["cc"] = [{"email": a} for a in cc]
        if REPLY_TO:
            payload["replyTo"] = {"email": REPLY_TO}
        headers = {"api-key": API_KEY}
    else:                                                     # resend
        payload = {"from": f"{FROM_NAME} <{FROM}>", "to": to,
                   "subject": subject, "text": body}
        if html:
            payload["html"] = html
        if cc:
            payload["cc"] = cc
        if REPLY_TO:
            payload["reply_to"] = REPLY_TO
        headers = {"Authorization": f"Bearer {API_KEY}"}

    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as answer:
            if 200 <= answer.status < 300:
                return True, ""
            return False, _safe(f"{API} answered {answer.status}")
    except urllib.error.HTTPError as exc:
        # The provider's own words, which say what is wrong far better than
        # a status code: an unverified sender, a key that has been revoked.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:                                     # noqa: BLE001
            pass
        return False, _safe(f"{API} refused it ({exc.code}): {detail}")
    except Exception as exc:                                  # noqa: BLE001
        return False, _safe(f"{type(exc).__name__}: {exc}")
