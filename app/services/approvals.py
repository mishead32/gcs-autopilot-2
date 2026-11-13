"""The Pending Approvals tracker — reading CMD Sir's sheet, and only reading.

Every task that needs approval is a row on one Google Sheet, and what has
happened to it is said by the COLOUR of the row:

    no colour, with text   waiting for approval
    green                  approved
    orange                 needs doing again

Nobody could say how many of each there were without scrolling eleven
thousand rows and counting. This reads the rows, reads the colours, and
counts.

Three rules it keeps, because the sheet belongs to somebody else:

  * It asks Google for READ-ONLY access. Not "is careful not to write" —
    the permission it requests cannot write, so no bug in here can.
  * The copy it keeps is replaced whole on each read. The sheet is the
    record; this is a photograph of it.
  * A colour it does not recognise is counted as exactly that, on its own
    line, rather than guessed into one of the three. A tracker that
    quietly files a yellow row under "pending" is wrong in a way nobody
    can see.
"""
from __future__ import annotations

import colorsys
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import clock
from ..models import ApprovalItem, Organization
from . import sheets

# Deliberately narrower than the mirror's scope, which has to write.
READONLY = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

DEFAULT_TAB = "Pending Approvals"
# Row 11085 is the first request of November 2026 — where counting starts.
DEFAULT_START_ROW = 11085

# Which column holds what, counted from A = 0.
COL_SENT, COL_MESSAGE, COL_TASK, COL_PERSON = 0, 2, 3, 4
COL_LINK, COL_FEEDBACK, COL_NEXT, COL_APPROVED = 5, 6, 8, 10
LAST_COL = "K"

PENDING, APPROVED, REVISION, OTHER = "pending", "approved", "revision", "other"
LABELS = {PENDING: "Pending approval", APPROVED: "Approved",
          REVISION: "Needs revision", OTHER: "Other colour"}

# Enough rows for years of requests, and a stop on a sheet that has had a
# colour dragged down to row one million.
MAX_ROWS = 20000

# Exactly what is asked of Google for each cell, and nothing else. Kept
# as one named value because it is the one part of this that cannot be
# tested without Google: a bracket out of place and every read is
# refused. The suite counts the brackets.
FIELDS = ("sheets(properties(title),data(startRow,rowData(values("
          "formattedValue,hyperlink,effectiveValue(numberValue),"
          "effectiveFormat(backgroundColor)))))")

_URL = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
_LINK = re.compile(r"https?://\S+")


def enabled() -> bool:
    """Is there a Google credential at all? The same one the mirror uses."""
    return bool(sheets.SERVICE_ACCOUNT)


def robot() -> str:
    """The address the sheet has to be shared with."""
    try:
        return json.loads(sheets.SERVICE_ACCOUNT or "{}").get("client_email", "")
    except ValueError:
        return ""


def sheet_id_from(raw: str) -> str:
    """The id out of a pasted link — or the id itself, pasted bare."""
    raw = (raw or "").strip()
    found = _URL.search(raw)
    if found:
        return found.group(1)
    return raw if re.fullmatch(r"[A-Za-z0-9_-]{20,}", raw) else ""


def configured(org: Organization) -> bool:
    return bool(org and org.appr_sheet_id)


# ---------------------------------------------------------------- colour ---
def _rgb(cell: dict) -> tuple[float, float, float] | None:
    """A cell's fill as Google reports it, or None when it has none.

    A component Google leaves out is ZERO, not "unset": pure green arrives
    as {"green": 1} with no red or blue at all. Reading a missing key as 1
    would turn that into white.
    """
    fmt = (cell or {}).get("effectiveFormat") or {}
    c = fmt.get("backgroundColor")
    if c is None:
        return None
    return (float(c.get("red", 0)), float(c.get("green", 0)),
            float(c.get("blue", 0)))


def shade(rgb) -> str:
    """Which of the colours that mean something this is: green, orange,
    none, or something else.

    By HUE rather than by matching particular colour codes, because "green"
    on a sheet is whichever of a dozen greens was nearest the cursor that
    day — the pale one, the bright one, the one from the custom picker.
    White, black and every grey are "none": they have no hue at all, and a
    banded sheet's grey stripes are not a status.
    """
    if rgb is None:
        return "none"
    h, s, v = colorsys.rgb_to_hsv(*rgb)
    # The palest green on the standard palette has a saturation just under
    # a tenth, so the line between "tinted" and "not" has to sit below it.
    if s < 0.06 or v < 0.2:
        return "none"
    deg = h * 360
    if 70 <= deg <= 170:
        return "green"
    if 12 <= deg <= 42:
        return "orange"
    return "else"


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(round(max(0, min(1, x)) * 255) for x in rgb)


def status_of(cells: list) -> tuple[str, str | None]:
    """What a row's colour says about it, and the colour it was read from.

    Decided by the cells that are coloured at all, whichever they are:
    some rows are filled from end to end and some have only the remark
    cell touched. If a row somehow carries both green and orange, the one
    covering more of it wins — and a dead heat reads as "needs revision",
    because showing somebody a row they did not need to look at costs a
    glance, and telling them it was approved when it was not costs the
    work.
    """
    tally = {"green": 0, "orange": 0, "else": 0}
    seen: dict[str, str] = {}
    for cell in cells:
        rgb = _rgb(cell)
        kind = shade(rgb)
        if kind == "none":
            continue
        tally[kind] += 1
        seen.setdefault(kind, _hex(rgb))
    if tally["green"] > tally["orange"]:
        return APPROVED, seen["green"]
    if tally["orange"]:
        return REVISION, seen["orange"]
    if tally["else"]:
        return OTHER, seen["else"]
    return PENDING, None


# ----------------------------------------------------------------- values ---
_EPOCH = date(1899, 12, 30)       # day zero of a spreadsheet's calendar

# Day first, because this is an Indian office. Numeric ones are listed
# apart from the ones that spell the month, because only a numeric date
# can be read two ways.
_NUMERIC = ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
            "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
            "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y")
_WORDED = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%d %b %Y", "%d %B %Y",
           "%d-%b-%y", "%d %b %y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
# Month first — only ever reached by text that is impossible day-first,
# such as 11/13/2026, so it cannot steal a date that had a day-first
# reading.
_MONTH_FIRST = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
                "%m-%d-%Y", "%m/%d/%y")


def _text(cell: dict) -> str:
    return str((cell or {}).get("formattedValue") or "").strip()


def _other_way(when: datetime) -> datetime | None:
    """The same date with day and month changed over, if that is a date.

    03/11 can be the third of November or the eleventh of March; 13/11
    can only be one thing. Only the first kind has another reading.
    """
    if when.day > 12 or when.day == when.month:
        return None
    return when.replace(day=when.month, month=when.day)


def _moment(cell: dict) -> tuple[datetime | None, datetime | None]:
    """A date out of a cell — and its other possible reading, if it has one.

    A real spreadsheet date is a NUMBER underneath, and that is read
    first. But the number is only as right as the day it was typed: on a
    sheet set to American dates, somebody typing 12/11/2026 for the
    twelfth of November has stored the eleventh of December, and the
    sheet goes on SHOWING 12/11/2026, so nobody ever sees it. That is not
    something one cell can settle. Both readings are handed back, and
    the column is judged as a whole — see _put_right.
    """
    raw = (cell or {}).get("effectiveValue") or {}
    n = raw.get("numberValue")
    if isinstance(n, (int, float)) and 20000 < n < 80000:
        when = datetime.combine(_EPOCH, datetime.min.time()) + timedelta(days=n)
        return when, _other_way(when)
    text = _text(cell)
    if not text:
        return None, None
    for fmt in _NUMERIC:
        try:
            when = datetime.strptime(text, fmt)
            return when, _other_way(when)
        except ValueError:
            continue
    for fmt in _WORDED + _MONTH_FIRST:
        try:
            return datetime.strptime(text, fmt), None
        except ValueError:
            continue
    return None, None


def _badness(seq: list, today: date, floors: list | None = None) -> tuple:
    """How implausible a column of dates is, read one particular way.

    Worst things first: a request cannot have been sent tomorrow; an
    approval cannot come before its request; rows are added to the
    bottom of the sheet, so dates should not jump backwards as they go
    down; and a few weeks of requests do not span a year.
    """
    days = [w.date() for w in seq if w is not None]
    if not days:
        return (0, 0, 0, 0)
    future = sum(1 for d in days if d > today)
    early = 0
    if floors:
        early = sum(1 for w, f in zip(seq, floors)
                    if w is not None and f is not None and w.date() < f)
    back = sum(1 for a, b in zip(days, days[1:]) if b < a)
    return (future, early, back, (max(days) - min(days)).days)


def _put_right(pairs: list, today: date, floors: list | None = None
               ) -> tuple[list, bool]:
    """One column of dates, read whichever way round makes sense.

    `pairs` is (as read, the other reading or None) for each row, top to
    bottom. Every date that could go either way is read the SAME way —
    a sheet has one habit, not a different one per row — and the way is
    whichever leaves the column less absurd. Dates that can only be read
    one way are in both versions, which is what anchors the choice: a
    13th or a 25th says which month this really is.
    """
    as_read = [a for a, _ in pairs]
    if not any(alt is not None for _, alt in pairs):
        return as_read, False
    turned = [alt if alt is not None else a for a, alt in pairs]
    if _badness(turned, today, floors) < _badness(as_read, today, floors):
        return turned, True
    return as_read, False


def _link(cell: dict) -> str:
    """The document a row points at, whether it is a link or typed out."""
    cell = cell or {}
    if cell.get("hyperlink"):
        return str(cell["hyperlink"])
    found = _LINK.search(_text(cell))
    return found.group(0) if found else ""


# ---------------------------------------------------------------- reading ---
def fetch(sheet_id: str, tab: str, start_row: int) -> dict:
    """Ask Google for the rows AND their colours. Replaced in tests.

    The ordinary "give me the values" call does not return colours at all,
    which is the one thing this needs, so it asks for the grid — narrowed
    to the columns and the handful of properties it reads, or eleven
    thousand rows of every formatting detail come back with them.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(sheets.SERVICE_ACCOUNT), scopes=READONLY)
    client = gspread.authorize(creds)
    safe_tab = (tab or DEFAULT_TAB).replace("'", "''")
    return client.http_client.fetch_sheet_metadata(sheet_id, params={
        "ranges": f"'{safe_tab}'!A{int(start_row)}:{LAST_COL}",
        "includeGridData": "true",
        "fields": FIELDS,
    })


def parse(payload: dict, start_row: int, today: date | None = None
          ) -> tuple[list[dict], dict]:
    """Google's answer, turned into one plain record per request.

    A row with nothing in the message, the task or the name is not a
    request — it is the empty part of the sheet, or a row somebody
    coloured and never filled — and is left out whatever colour it is.

    Returns the records and a note of which date columns were found with
    day and month the wrong way round and put right.
    """
    today = today or clock.today()
    out, sent, done = [], [], []
    for sheet in (payload or {}).get("sheets") or []:
        for block in sheet.get("data") or []:
            first = int(block.get("startRow", start_row - 1)) + 1
            for n, row in enumerate((block.get("rowData") or [])[:MAX_ROWS]):
                cells = list(row.get("values") or [])
                cells += [{}] * (COL_APPROVED + 1 - len(cells))
                message, task = _text(cells[COL_MESSAGE]), _text(cells[COL_TASK])
                person = _text(cells[COL_PERSON])
                if not (message or task or person):
                    continue
                status, colour = status_of(cells[:COL_APPROVED + 1])
                sent.append(_moment(cells[COL_SENT]))
                done.append(_moment(cells[COL_APPROVED]))
                out.append({
                    "row_no": first + n,
                    "sent_text": _text(cells[COL_SENT])[:80],
                    "message": message, "task_name": task,
                    "person": person[:160],
                    "doc_link": _link(cells[COL_LINK]),
                    "feedback": _text(cells[COL_FEEDBACK]),
                    "next_action": _text(cells[COL_NEXT]),
                    "approved_text": _text(cells[COL_APPROVED])[:80],
                    "status": status, "colour": colour,
                })

    sent_days, sent_fixed = _put_right(sent, today)
    floors = [w.date() if w else None for w in sent_days]
    done_days, done_fixed = _put_right(done, today, floors)
    for rec, s_at, d_at in zip(out, sent_days, done_days):
        rec["sent_on"] = s_at.date() if s_at else None
        rec["approved_at"] = d_at
    return out, {"sent": sent_fixed, "approved": done_fixed}


def _why(exc: Exception) -> str:
    """A failure, in words somebody can act on."""
    text = str(exc)
    low = text.lower()
    if "403" in text or "permission" in low:
        who = robot() or "the service account"
        return (f"Google refused: the sheet is not shared with {who}. Open "
                "the sheet, press Share, add that address as a Viewer, and "
                "read again.")
    if "404" in text or "not found" in low:
        return ("Google cannot find that sheet. Check the link was copied "
                "whole.")
    if "unable to parse range" in low:
        return ("The sheet was found but not the tab. Check the tab name is "
                "exactly as it is written at the bottom of the sheet.")
    # Anything else is said in Google's own words. Guessing at a cause
    # here sends somebody to fix a tab name that was never wrong.
    return f"Could not read the sheet: {text[:300]}"


def sync(db: Session, org: Organization) -> dict:
    """Read the sheet and replace the copy. Never raises.

    On failure the previous copy is LEFT ALONE and the reason is written
    down beside it: yesterday's numbers with a notice saying they are
    yesterday's are worth more than an empty page.
    """
    if not enabled():
        return {"ok": False, "why": "No Google credential is set on the server."}
    if not configured(org):
        return {"ok": False, "why": "No sheet has been connected yet."}
    start = org.appr_start_row or DEFAULT_START_ROW
    try:
        rows, fixed = parse(
            fetch(org.appr_sheet_id, org.appr_tab or DEFAULT_TAB, start), start)
    except Exception as exc:                              # noqa: BLE001
        db.rollback()
        org.appr_error = _why(exc)
        db.commit()
        return {"ok": False, "why": org.appr_error}

    db.execute(delete(ApprovalItem).where(ApprovalItem.org_id == org.id))
    db.add_all([ApprovalItem(org_id=org.id, **r) for r in rows])
    org.appr_read_at = clock.now()
    org.appr_error = None
    org.appr_swapped = bool(fixed["sent"] or fixed["approved"])
    db.commit()
    return {"ok": True, "rows": len(rows), "fixed": fixed}


def sync_all(db: Session) -> int:
    """Every organisation with a sheet connected. For the background tick."""
    done = 0
    for org in db.scalars(select(Organization).where(
            Organization.appr_sheet_id.isnot(None))).all():
        if sync(db, org).get("ok"):
            done += 1
    return done


# --------------------------------------------------------------- counting ---
@dataclass
class Count:
    sent: int = 0
    approved: int = 0
    pending: int = 0
    revision: int = 0
    other: int = 0
    # Waiting, but with a remark already written: answered and not yet
    # coloured, most likely, and worth a look before it is chased.
    remarked: int = 0

    def add(self, item) -> None:
        self.sent += 1
        setattr(self, item.status, getattr(self, item.status) + 1)
        if item.status == PENDING and (item.feedback or "").strip():
            self.remarked += 1


def items(db: Session, org_id: int) -> list[ApprovalItem]:
    return list(db.scalars(
        select(ApprovalItem).where(ApprovalItem.org_id == org_id)
        .order_by(ApprovalItem.row_no.desc())).all())


def summary(rows) -> Count:
    c = Count()
    for r in rows:
        c.add(r)
    return c


def by_day(rows, days: int = 31) -> list[tuple[date, Count]]:
    """Requests grouped by the day they were SENT, newest first.

    How they stand NOW, not how they stood that evening: a request sent on
    Monday and approved on Wednesday is in Monday's line as approved. The
    sheet keeps no history of its colours, so that is the only honest
    reading there is.
    """
    out: dict[date, Count] = {}
    for r in rows:
        if r.sent_on:
            out.setdefault(r.sent_on, Count()).add(r)
    return sorted(out.items(), key=lambda kv: kv[0], reverse=True)[:days]


def by_week(rows) -> list[tuple[date, Count]]:
    """The same, by week — Monday to Sunday, labelled by its Monday."""
    out: dict[date, Count] = {}
    for r in rows:
        if r.sent_on:
            monday = r.sent_on - timedelta(days=r.sent_on.weekday())
            out.setdefault(monday, Count()).add(r)
    return sorted(out.items(), key=lambda kv: kv[0], reverse=True)
