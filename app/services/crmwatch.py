"""Pending-calls escalation — inquiries nobody has called back.

Each company keeps its inquiries on a Google Sheet. A row whose sales stage
still says the first-call word (Discovery / Pending Discovery) and whose
inquiry time is more than 30 minutes ago is a call not made; at 60 minutes
it is later still. Once an hour, 9 am to 6 pm, the counts per tele-caller
for TODAY's inquiries go by email (and WhatsApp, once its template is
approved) to the first person, and the 60-minute ones to the second.

The sheet is only ever read, with the read-only Google permission.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import CrmSend, CrmWatch, Organization, User
from . import approvals, mail, sheets, whatsapp

READONLY = approvals.READONLY
WA_TEMPLATE = os.getenv("WA_TEMPLATE_PENDING", "pending_calls_escalation_v2").strip()
SIGN_OFF = "MIS Team."

DEFAULTS = [   # label, name col, stage col, pending word, date order, sunday off
    ("Bodyzone", "J", "L", "Discovery", "mdy", False),
    ("Spa Kora", "L", "J", "Discovery", "dmy", False),
    ("BIPS", "K", "L", "Pending Discovery", "dmy", True),
]


# What the background loop last did, kept in memory so the page can show it.
# If this never changes, the loop is not running; if it carries an error,
# that is the reason nothing is going out — otherwise it lives only in a log
# nobody opens.
HEARTBEAT: dict = {"at": None, "sent": 0, "error": None, "passes": 0}


def settings(org: Organization) -> dict:
    return {
        "on": bool(org.crm_on),
        "l1": org.crm_l1_user_id, "l2": org.crm_l2_user_id,
        "m1": org.crm_l1_min or 30, "m2": org.crm_l2_min or 60,
        "h_from": 9 if org.crm_from_hour is None else org.crm_from_hour,
        "h_to": 18 if org.crm_to_hour is None else org.crm_to_hour,
        "email": True if org.crm_email_on is None else bool(org.crm_email_on),
        "wa": bool(org.crm_wa_on),
        # How often each level is sent: the first person every 30 minutes,
        # the second every hour, unless changed.
        "e1": org.crm_l1_every or 30, "e2": org.crm_l2_every or 60,
    }


def ensure_watches(db: Session, org_id: int) -> list[CrmWatch]:
    have = list(db.scalars(select(CrmWatch).where(CrmWatch.org_id == org_id)
                           .order_by(CrmWatch.id)).all())
    if not have:
        for label, nc, sc, word, order, sun in DEFAULTS:
            have.append(CrmWatch(org_id=org_id, label=label, name_col=nc,
                                 stage_col=sc, pending_word=word, tab="Inquiry",
                                 date_order=order, sunday_off=sun,
                                 active=False))
        db.add_all(have)
        db.commit()
    return have


# ----------------------------------------------------------------- dates ---
_WHEN = re.compile(
    r"^\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*$")


def _make(y, mo, d, h=0, mi=0, s=0):
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def moment(text: str, order: str, now: datetime) -> tuple[datetime | None, bool]:
    """(when, has_time). The sheet's own order is tried first; the other
    order only when that one is impossible or in the future. An inquiry that
    has not happened yet is not an inquiry."""
    m = _WHEN.match(text or "")
    if not m:
        return None, False
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    has_time = m.group(4) is not None
    h, mi, s = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
    first = (a, b) if order == "dmy" else (b, a)       # (day, month)
    second = (b, a) if order == "dmy" else (a, b)
    for d, mo in (first, second):
        got = _make(y, mo, d, h, mi, s)
        if got and got <= now:
            return got, has_time
    return None, has_time


def col_index(letters: str) -> int:
    n = 0
    for ch in (letters or "").strip().upper():
        if not "A" <= ch <= "Z":
            raise ValueError(f"“{letters}” is not a column letter")
        n = n * 26 + ord(ch) - 64
    if n < 1:
        raise ValueError("A column letter is needed")
    return n - 1


def alias_map(text: str) -> dict[str, str]:
    out = {}
    for line in (text or "").splitlines():
        if "=" in line:
            raw, shown = line.split("=", 1)
            if raw.strip() and shown.strip():
                out[raw.strip().lower()] = shown.strip()
    return out


# --------------------------------------------------------------- reading ---
def fetch(sheet_id: str, tab: str, cols: list[str]) -> list[list[str]]:
    """The three columns, rows 2 down, aligned row for row. Replaced in tests."""
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(sheets.SERVICE_ACCOUNT), scopes=READONLY)
    ws = gspread.authorize(creds).open_by_key(sheet_id).worksheet(tab or "Inquiry")
    got = ws.batch_get([f"{c}2:{c}" for c in cols])
    columns = [[(r[0] if r else "") for r in block] for block in got]
    n = max((len(c) for c in columns), default=0)
    columns = [c + [""] * (n - len(c)) for c in columns]
    return [list(row) for row in zip(*columns)]


def count_pending(rows: list[list[str]], w: CrmWatch, now: datetime,
                  minutes: int) -> tuple[dict[str, int], int]:
    """Pending calls per caller for today's inquiries older than `minutes`.
    Returns (counts, rows_today_without_a_time)."""
    today = now.date()
    word = (w.pending_word or "").strip().lower()
    names = alias_map(w.aliases)
    out: dict[str, int] = {}
    no_time = 0
    for when, who, stage in rows:
        at, has_time = moment(when, w.date_order, now)
        if at is None or at.date() != today:
            continue
        if (stage or "").strip().lower() != word:
            continue
        if not has_time:
            no_time += 1
            continue
        if (now - at).total_seconds() < minutes * 60:
            continue
        who = (who or "").strip()
        who = names.get(who.lower(), who) or "(no name)"
        out[who] = out.get(who, 0) + 1
    return out, no_time


# -------------------------------------------------------------- messages ---
def lines(counts: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def compose(label: str, day: date, counts: dict[str, int], level_min: int
            ) -> tuple[str, str, str, list[str]]:
    """(subject, text, html, whatsapp variables) — the office's own wording."""
    total = sum(counts.values())
    when = day.strftime("%d/%m/%Y")
    rows = lines(counts)
    text = "\n".join([
        "Hi,", "",
        f"This is about pending calls escalation for {label}:", "",
        f"1. Inquiry date: {when}",
        f"2. Total Pending calls as per CRM: {total}", "",
        "Summary of Pending Calls Tele callers wise:",
        *[f"{i}. {n} : {c}" for i, (n, c) in enumerate(rows, 1)], "",
        "Kindly look into this .", "",
        "Thanks & Regards,", "", SIGN_OFF])
    esc = lambda s: (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    html = (
        "<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#1f2937\">"
        f"<p>Hi,</p><p>This is about pending calls escalation for <b>{esc(label)}</b>:</p>"
        f"<p>1. Inquiry date: {when}<br>2. Total Pending calls as per CRM: <b>{total}</b></p>"
        "<p>Summary of Pending Calls Tele callers wise:</p><p>"
        + "<br>".join(f"{i}. {esc(n)} : {c}" for i, (n, c) in enumerate(rows, 1))
        + f"</p><p>Kindly look into this .</p><p>Thanks &amp; Regards,<br><br>{SIGN_OFF}</p></div>")
    subject = f"Pending calls escalation – {label} – {when} ({total} pending {level_min}+ min)"
    summary = ", ".join(f"{n} : {c}" for n, c in rows)
    # Four blanks, each used once, in the order they appear in the message.
    # (A first template used the company twice, and the provider and Meta
    # disagreed about whether that is four values or five. Every blank
    # appearing once leaves nothing to count differently.)
    return subject, text, html, [label, when, str(total), summary]


# ------------------------------------------------------------------ run ----
def _slot(now: datetime, every: int) -> str:
    """The `every`-minute stretch of the day this moment falls in."""
    start = (now.hour * 60 + now.minute) // every * every
    return f"{now:%Y-%m-%d} {start:04d}"


def _done(db: Session, w: CrmWatch, slot: str, level: int, channel: str) -> bool:
    return db.scalar(select(CrmSend.id).where(
        CrmSend.watch_id == w.id, CrmSend.slot == slot,
        CrmSend.level == level, CrmSend.channel == channel)) is not None


def in_hours(org: Organization, now: datetime, w: CrmWatch, every: int = 60) -> bool:
    """Is this stretch one that starts between the first and last hour?

    With the last hour 18, the stretch that begins at 18:00 is sent and the
    one that begins at 18:30 is not — "9 am to 6 pm" ends at six.
    """
    s = settings(org)
    if w.sunday_off and now.weekday() == 6:
        return False
    start = (now.hour * 60 + now.minute) // every * every
    return s["h_from"] * 60 <= start <= s["h_to"] * 60


def read_watch(db: Session, w: CrmWatch, now: datetime | None = None
               ) -> dict:
    """Read one sheet. Never raises. Returns the rows or the reason not."""
    now = now or clock.now()
    if not approvals.enabled():
        return {"ok": False, "why": "No Google credential is set on the server."}
    if not w.sheet_id:
        return {"ok": False, "why": "No sheet link has been saved for this company."}
    try:
        idx = [col_index(w.when_col), col_index(w.name_col), col_index(w.stage_col)]
        order = sorted(set(idx))
        letters = [chr(65 + i) if i < 26 else None for i in order]
        if None in letters:
            raise ValueError("Columns beyond Z are not supported here.")
        got = fetch(w.sheet_id, w.tab or "Inquiry", letters)
        pos = {c: k for k, c in enumerate(order)}
        rows = [[r[pos[i]] for i in idx] for r in got]
    except Exception as exc:                              # noqa: BLE001
        db.rollback()
        w.last_error = approvals._why(exc)
        db.commit()
        return {"ok": False, "why": w.last_error}
    w.last_read_at, w.last_error = now, None
    db.commit()
    return {"ok": True, "rows": rows}


def run_watch(db: Session, org: Organization, w: CrmWatch, now: datetime,
              send: bool = True, force: bool = False) -> dict:
    """One company, one moment. Reads once, then tells each level what it owes.

    Each level has its own rhythm — the first person every 30 minutes, the
    second every hour — and each rhythm is cut into stretches of the day
    (09:00-09:29, 09:30-09:59 ...). A message the log says went (or failed)
    in a level's current stretch is not repeated, so a restart at ten past
    nine does not send nine o'clock's message twice.
    `force` is the "Send now" button; it ignores the hours and the log.
    """
    s = settings(org)
    every = {1: s["e1"], 2: s["e2"]}
    slot = {lv: _slot(now, every[lv]) for lv in (1, 2)}
    out = {"label": w.label, "slot": slot[1], "levels": [], "ok": True}
    if force:
        due = {1, 2}
    else:
        due = {lv for lv in (1, 2) if w.active and in_hours(org, now, w, every[lv])
               and not _done(db, w, slot[lv], lv, "read")}
        if not due:
            out["skipped"] = True
            return out
    got = read_watch(db, w, now)
    if not got["ok"]:
        out.update(ok=False, why=got["why"])
        if not force:
            for lv in due:
                db.add(CrmSend(org_id=org.id, watch_id=w.id, slot=slot[lv], level=lv,
                               channel="read", status="failed", error=got["why"][:400]))
            db.commit()
        return out
    if not force:
        for lv in due:
            db.add(CrmSend(org_id=org.id, watch_id=w.id, slot=slot[lv], level=lv,
                           channel="read", status="sent"))
        db.commit()

    users = {u.id: u for u in db.scalars(select(User).where(
        User.id.in_([i for i in (s["l1"], s["l2"]) if i]))).all()} if (
        s["l1"] or s["l2"]) else {}
    for level, uid, minutes in ((1, s["l1"], s["m1"]), (2, s["l2"], s["m2"])):
        if level not in due:
            continue
        counts, no_time = count_pending(got["rows"], w, now, minutes)
        who = users.get(uid)
        info = {"level": level, "minutes": minutes, "counts": counts,
                "total": sum(counts.values()), "to": who.name if who else None,
                "no_time": no_time, "sent": []}
        out["levels"].append(info)
        w.last_note = (f"{info['total']} pending over {minutes} min"
                       + (f"; {no_time} today without a time" if no_time else ""))[:300]
        if not counts or not send:
            continue
        if who is None:
            info["why"] = "No person is chosen for this level."
            continue
        subject, text, html, wa_vars = compose(w.label, now.date(), counts, minutes)
        for channel in ("email", "whatsapp"):
            if (channel == "email" and not s["email"]) or \
               (channel == "whatsapp" and not s["wa"]):
                continue
            if not force and _done(db, w, slot[level], level, channel):
                continue
            if channel == "email":
                to = (who.email or "").strip()
                ok, why = (mail.send([to], subject, text, html=html)
                           if mail.valid(to) else (False, "No valid email address."))
            else:
                to = whatsapp.number(who.phone or "")
                ok, why = (whatsapp.send(to, wa_vars, template=WA_TEMPLATE)
                           if to else (False, "No valid mobile number."))
            db.add(CrmSend(org_id=org.id, watch_id=w.id, slot=slot[level], level=level,
                           channel=channel, to_name=who.name, to_addr=to,
                           pending=info["total"], status="sent" if ok else "failed",
                           error=None if ok else (why or "")[:400], body=text))
            db.commit()
            info["sent"].append((channel, ok, why))
    db.commit()
    return out


def run_all(db: Session, now: datetime | None = None) -> int:
    """For the background loop. Cheap when it is not the time."""
    now = now or clock.now()
    n = 0
    for org in db.scalars(select(Organization)).all():
        if not org.crm_on:
            continue
        for w in ensure_watches(db, org.id):
            res = run_watch(db, org, w, now)
            n += sum(1 for lv in res.get("levels", []) for _ in lv["sent"])
    return n
