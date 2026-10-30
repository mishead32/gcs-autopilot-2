"""Sending one WhatsApp message through the office's existing provider.

The provider is template-based, which is not a detail — it is the whole
shape of this file. WhatsApp will not let a business send arbitrary text to
somebody who has not messaged first; it sends an APPROVED TEMPLATE with
blanks filled in. So nothing here writes the message. The words live in the
template, approved once in the provider's console, and this fills the
blanks and attaches the picture.

    WA_URL        https://wa.redlava.in/api/v1/whatsapp/sendMessage
    WA_API_KEY    the provider's key          -> x-api-key
    WA_PHONE_ID   which number it sends from  -> x-phone-id
    WA_TEMPLATE   the approved template's name
    WA_LANG       its language, "en" unless it was approved otherwise

The key is read from the environment and never written to the database, a
template, a log line or an error message — the same rule as every other
secret here.

The template this expects has four blanks, in this order:

    1  the person's name
    2  how many delegation tasks are overdue
    3  how many checklist tasks
    4  how many FMS steps

which is the message the office asked for:

    Overdue tasks

    Delegation - {{2}}
    Checklist - {{3}}
    FMS - {{4}}

    Kindly find the task list attached.
    Kindly complete the pending tasks.

    Thanks & Regards,
    GCS Core Team

Change the wording in the console whenever you like. Change the NUMBER of
blanks and WA_VARS below has to change with it, which is why that is one
line and not spread through the code.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

URL = os.getenv("WA_URL", "").strip()
API_KEY = os.getenv("WA_API_KEY", "").strip()
PHONE_ID = os.getenv("WA_PHONE_ID", "").strip()
TEMPLATE = os.getenv("WA_TEMPLATE", "").strip()
LANG = os.getenv("WA_LANG", "en").strip() or "en"
TIMEOUT = float(os.getenv("WA_TIMEOUT", "20") or 20)

# India, because every number in this office is one. Written down rather
# than assumed in three places.
COUNTRY = os.getenv("WA_COUNTRY", "91").strip() or "91"
NATIONAL_LEN = 10


def enabled() -> bool:
    """True when there is a provider, a key and a template to send."""
    return bool(URL and API_KEY and PHONE_ID and TEMPLATE)


def status() -> str:
    """One line for the settings page. Never the key, not even its length."""
    if not URL:
        return "No WhatsApp provider set. Nothing will be sent."
    for name, value in (("WA_API_KEY", API_KEY), ("WA_PHONE_ID", PHONE_ID),
                        ("WA_TEMPLATE", TEMPLATE)):
        if not value:
            return f"{URL} is set, but {name} is empty."
    return f"{TEMPLATE} via {URL}"


def number(raw) -> str:
    """A typed phone number as the provider wants it, or "" if it is not one.

    People write their own number six ways — with spaces, with +91, with a
    leading 0, with a hyphen in the middle. All of those are the same
    number and all of them should work; anything that is still not twelve
    digits afterwards is not a mobile and is refused rather than sent to
    whoever those digits happen to belong to.
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == NATIONAL_LEN:
        digits = COUNTRY + digits
    elif digits.startswith("0") and len(digits) == NATIONAL_LEN + 1:
        digits = COUNTRY + digits[1:]
    if digits.startswith(COUNTRY) and len(digits) == len(COUNTRY) + NATIONAL_LEN:
        return digits
    return ""


def send(to: str, variables: list[str], file_url: str = "") -> tuple[bool, str]:
    """One message. Returns (sent, reason-when-not). Never raises.

    The caller is a background job: a provider that is down must leave a
    record and move on to the next person, not end the morning.
    """
    mobile = number(to)
    if not mobile:
        return False, f"“{to}” is not a mobile number this can send to."
    if not enabled():
        return False, status()

    payload = {
        "templateName": TEMPLATE,
        "language": LANG,
        "templateVariables": [str(v) for v in variables],
        "to": mobile,
    }
    if file_url:
        payload["fileUrl"] = file_url

    req = urllib.request.Request(
        URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "x-api-key": API_KEY, "x-phone-id": PHONE_ID},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as answer:
            body = answer.read().decode("utf-8", "replace")[:300]
            if 200 <= answer.status < 300:
                # A 200 from a gateway is not always a yes — several answer
                # 200 with {"success": false} and the reason inside. Taking
                # the status code alone would record a message as sent that
                # nobody received.
                low = body.lower()
                if '"success":false' in low.replace(" ", "") or '"error"' in low:
                    return False, _safe(body)
                return True, ""
            return False, _safe(f"{answer.status}: {body}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:                                   # noqa: BLE001
            pass
        return False, _safe(f"{exc.code}: {detail}")
    except Exception as exc:                                # noqa: BLE001
        return False, _safe(f"{type(exc).__name__}: {exc}")


def _safe(text: str) -> str:
    """Whatever the provider said, with the key taken out of it.

    A gateway that echoes the request back — several do, in an error — would
    otherwise write the key into the log, where it is read by everybody who
    can open the escalation page.
    """
    out = str(text or "")
    for secret in (API_KEY, PHONE_ID):
        if secret and len(secret) > 4:
            out = out.replace(secret, "…")
    return out[:400]


def preview(name: str, counts: list[tuple[str, int]]) -> str:
    """The message as it will read, for the page that shows it.

    The real words come from the approved template, so this cannot be
    exact — it is what the blanks are being filled with, in the order the
    template takes them, so somebody can see that the numbers are right
    before anybody's phone buzzes.
    """
    lines = [f"Hi {name},", "", "Overdue tasks", ""]
    lines += [f"{label} - {n} task" + ("s" if n != 1 else "")
              for label, n in counts]
    lines += ["", "Kindly find the task list attached.",
              "Kindly complete the pending tasks.", "",
              "Thanks & Regards,", "GCS Core Team"]
    return "\n".join(lines)


def variables(name: str, counts: list[tuple[str, int]]) -> list[str]:
    """What goes into the template's blanks: the name, then each count.

    In the template's own order, and that order is the one thing here that
    has to match the console exactly — which is why it is in one place.
    """
    return [name] + [str(n) for _label, n in counts]
