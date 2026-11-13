"""The Pending Approval Tracker — one page, read off CMD Sir's sheet.

Nothing here writes to the sheet. The page shows the copy last read, says
when that was, and has a button to read again.
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock, flash
from ..db import get_db
from ..deps import manager_up, require_right
from ..models import Organization, Right, User
from ..services import approvals, xlsx
from ..templating import templates

router = APIRouter()

STATUSES = [("", "All"), (approvals.PENDING, "Pending approval"),
            (approvals.APPROVED, "Approved"),
            (approvals.REVISION, "Needs revision"),
            (approvals.OTHER, "Other colour")]


def _org(db: Session, user: User) -> Organization:
    return db.scalar(select(Organization).where(Organization.id == user.org_id))


def _day(raw: str) -> date | None:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def _ranges(today: date) -> list[tuple[str, str, str]]:
    """The date ranges asked for most, one click each."""
    monday = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    return [("Today", today.isoformat(), today.isoformat()),
            ("This week", monday.isoformat(), today.isoformat()),
            ("This month", first.isoformat(), today.isoformat())]


@router.get("/approvals", response_class=HTMLResponse)
def tracker(request: Request, status: str = "", person: str = "", q: str = "",
            date_from: str = "", date_to: str = "", export: str = "",
            user: User = Depends(manager_up), db: Session = Depends(get_db)):
    """The tracker. Two layers of filter, and the order matters.

    The dates, the person and the search decide WHICH requests are being
    looked at — and the summary is of exactly those, so choosing a week
    gives that week's four figures. The category then picks one of the
    four out of them for the list. Done the other way round, choosing
    "Approved" would turn the summary into one number and three zeros.
    """
    org = _org(db, user)
    everything = approvals.items(db, org.id)

    if status not in dict(STATUSES):
        status = ""
    start, end = _day(date_from), _day(date_to)
    if start and end and end < start:
        start, end = end, start
    want = " ".join((q or "").lower().split())

    scope = everything
    if person:
        scope = [r for r in scope if (r.person or "") == person]
    if want:
        scope = [r for r in scope if want in " ".join(filter(None, [
            r.task_name, r.message, r.person, r.feedback, r.next_action,
        ])).lower()]
    # Rows whose date could not be read cannot be placed in a range.
    # Counted and said, rather than silently dropped from a week's total.
    undated = (sum(1 for r in scope if r.sent_on is None)
               if (start or end) else 0)
    if start:
        scope = [r for r in scope if r.sent_on and r.sent_on >= start]
    if end:
        scope = [r for r in scope if r.sent_on and r.sent_on <= end]
    rows = [r for r in scope if r.status == status] if status else scope

    if xlsx.wants(export):
        return xlsx.one("pending-approvals", "Pending approvals", [
            ("Sheet row", lambda r: r.row_no),
            ("Sent on", lambda r: r.sent_on or r.sent_text or ""),
            ("Status", lambda r: approvals.LABELS.get(r.status, r.status)),
            ("Person", lambda r: r.person or ""),
            ("Task", lambda r: r.task_name or ""),
            ("Message", lambda r: r.message or ""),
            ("Document", lambda r: r.doc_link or ""),
            ("Feedback / approval", lambda r: r.feedback or ""),
            ("Next action", lambda r: r.next_action or ""),
            ("Approved at", lambda r: r.approved_at or r.approved_text or ""),
        ], rows,
            f"Read from the sheet {org.appr_read_at:%d/%m/%Y %I:%M %p}"
            if org.appr_read_at else "Not read yet")

    s_iso = start.isoformat() if start else ""
    e_iso = end.isoformat() if end else ""

    def link(st: str = "", **over) -> str:
        """This page with the filters as they are, and one thing changed.

        Every figure and every tab is built with it, so opening
        "Approved" from a week's summary keeps the week.
        """
        keep = {"status": st, "person": person, "q": q,
                "date_from": s_iso, "date_to": e_iso}
        keep.update(over)
        return "/approvals?" + urlencode({k: v for k, v in keep.items() if v})

    dated = [r.sent_on for r in everything if r.sent_on]
    people = sorted({r.person for r in everything if r.person}, key=str.lower)
    return templates.TemplateResponse(request, "approvals.html", {
        "user": user, "org": org, "rows": rows,
        "total": approvals.summary(everything),
        "shown": approvals.summary(scope),
        "narrowed": bool(person or start or end or want),
        "statuses": STATUSES, "status": status, "person": person, "q": q,
        "date_from": s_iso, "date_to": e_iso, "start": start, "end": end,
        "first_day": min(dated) if dated else None,
        "last_day": max(dated) if dated else None,
        "undated": undated, "link": link,
        "ranges": _ranges(clock.today()),
        "people": people, "labels": approvals.LABELS,
        "weeks": approvals.by_week(scope), "week_len": timedelta(days=6),
        "days": approvals.by_day(scope),
        "enabled": approvals.enabled(), "robot": approvals.robot(),
        "configured": approvals.configured(org),
        "default_tab": approvals.DEFAULT_TAB,
        "default_row": approvals.DEFAULT_START_ROW,
        "today": clock.today(),
        "can_set": user.has(Right.MANAGE_USER),
    })


@router.post("/approvals/refresh")
def refresh(request: Request, user: User = Depends(manager_up),
            db: Session = Depends(get_db)):
    """Read the sheet again, now."""
    got = approvals.sync(db, _org(db, user))
    if got.get("ok"):
        flash.set(request, "saved",
                  f"Read {got['rows']} request(s) from the sheet just now.")
    else:
        flash.set(request, "error", got.get("why") or "Could not read it.")
    return RedirectResponse("/approvals", status_code=303)


@router.post("/approvals/settings")
def settings(request: Request, sheet: str = Form(""), tab: str = Form(""),
             start_row: str = Form(""),
             user: User = Depends(require_right(Right.MANAGE_USER)),
             db: Session = Depends(get_db)):
    """Which sheet, which tab, and which row counting starts from."""
    org = _org(db, user)
    sheet_id = approvals.sheet_id_from(sheet)
    if not sheet_id:
        raise HTTPException(
            400, "That does not look like a Google Sheet link. Open the "
                 "sheet, copy the whole address from the top of the browser, "
                 "and paste it here.")
    try:
        row = int(str(start_row).strip() or approvals.DEFAULT_START_ROW)
    except ValueError:
        raise HTTPException(400, "The starting row has to be a number.")
    if row < 2:
        raise HTTPException(
            400, "The starting row has to be 2 or more — row 1 is the "
                 "headings.")
    org.appr_sheet_id = sheet_id
    org.appr_tab = (tab or "").strip() or approvals.DEFAULT_TAB
    org.appr_start_row = row
    db.commit()

    got = approvals.sync(db, org)
    if got.get("ok"):
        flash.set(request, "saved",
                  f"Connected. Read {got['rows']} request(s) from row {row} on.")
    else:
        flash.set(request, "error",
                  "Saved, but the first read failed. " + (got.get("why") or ""))
    return RedirectResponse("/approvals", status_code=303)
