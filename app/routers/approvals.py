"""The Pending Approval Tracker — one page, read off CMD Sir's sheet.

Nothing here writes to the sheet. The page shows the copy last read, says
when that was, and has a button to read again.
"""
from __future__ import annotations

from datetime import date

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


@router.get("/approvals", response_class=HTMLResponse)
def tracker(request: Request, status: str = "", person: str = "", q: str = "",
            date_from: str = "", date_to: str = "", export: str = "",
            user: User = Depends(manager_up), db: Session = Depends(get_db)):
    org = _org(db, user)
    everything = approvals.items(db, org.id)
    total = approvals.summary(everything)

    if status not in dict(STATUSES):
        status = ""
    start, end = _day(date_from), _day(date_to)
    want = " ".join((q or "").lower().split())
    rows = everything
    if status:
        rows = [r for r in rows if r.status == status]
    if person:
        rows = [r for r in rows if (r.person or "") == person]
    if start:
        rows = [r for r in rows if r.sent_on and r.sent_on >= start]
    if end:
        rows = [r for r in rows if r.sent_on and r.sent_on <= end]
    if want:
        rows = [r for r in rows if want in " ".join(filter(None, [
            r.task_name, r.message, r.person, r.feedback, r.next_action,
        ])).lower()]

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

    people = sorted({r.person for r in everything if r.person},
                    key=str.lower)
    return templates.TemplateResponse(request, "approvals.html", {
        "user": user, "org": org, "rows": rows, "total": total,
        "shown": approvals.summary(rows),
        "filtered": bool(status or person or start or end or want),
        "statuses": STATUSES, "status": status, "person": person, "q": q,
        "date_from": date_from if start else "",
        "date_to": date_to if end else "",
        "people": people, "labels": approvals.LABELS,
        "weeks": approvals.by_week(everything),
        "days": approvals.by_day(everything),
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
