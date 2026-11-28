"""To Do List sheet — settings, a "check now" button, and what was written."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import flash
from ..db import get_db
from ..deps import manager_up, require_right
from ..models import Organization, Right, TodoMark, User
from ..services import todosync
from ..templating import templates

router = APIRouter()


def _org(db: Session, user: User) -> Organization:
    return db.scalar(select(Organization).where(Organization.id == user.org_id))


@router.get("/todo-sync", response_class=HTMLResponse)
def page(request: Request, user: User = Depends(manager_up),
         db: Session = Depends(get_db)):
    org = _org(db, user)
    marks = list(db.scalars(select(TodoMark).where(TodoMark.org_id == org.id)
                            .order_by(TodoMark.marked_at.desc()).limit(60)).all())
    return templates.TemplateResponse(request, "todo_sync.html", {
        "user": user, "org": org, "cfg": todosync.settings(org),
        "marks": marks, "note": todosync.note(org),
        "times_label": todosync.times_text(todosync.settings(org)["times"]),
        "enabled": todosync.enabled(), "robot": todosync.robot(),
        "can_set": user.has(Right.MANAGE_USER),
    })


@router.post("/todo-sync/settings")
def save(request: Request, sheet: str = Form(""), tab: str = Form(""),
         from_row: str = Form(""), times: str = Form(""), on: str = Form(""),
         user: User = Depends(require_right(Right.MANAGE_USER)),
         db: Session = Depends(get_db)):
    org = _org(db, user)
    sheet_id = todosync.sheet_id_from(sheet) if sheet.strip() else todosync.DEFAULT_SHEET
    if not sheet_id:
        raise HTTPException(400, "That does not look like a Google Sheet link. "
                                 "Copy the whole address from the top of the browser.")
    try:
        row = int(str(from_row).strip() or todosync.DEFAULT_FROM)
    except ValueError:
        raise HTTPException(400, "The first row must be a number.")
    slots = todosync.read_times(times or todosync.DEFAULT_TIMES)
    if not slots:
        raise HTTPException(400, "Give the times like 12:00, 17:00 (24-hour).")
    if row < 2:
        raise HTTPException(400, "The first row must be 2 or more.")
    org.todo_sheet_id = sheet_id
    org.todo_tab = (tab or "").strip() or todosync.DEFAULT_TAB
    org.todo_from_row = row
    org.todo_times = todosync.times_text(slots)
    org.todo_on = bool(on)
    org.todo_read_at = None          # look again at the next chance
    db.commit()
    flash.set(request, "saved", "Saved. " + (
        f"It will check the sheet at {org.todo_times} every day." if org.todo_on
        else "It is switched off — press “Check now” to try it once."))
    return RedirectResponse("/todo-sync", status_code=303)


@router.post("/todo-sync/check")
def check(request: Request, user: User = Depends(manager_up),
          db: Session = Depends(get_db)):
    got = todosync.run(db, _org(db, user))
    if got["ok"]:
        flash.set(request, "saved",
                  f"Checked the sheet. {got['marked']} row(s) turned green, "
                  f"{got['waiting']} still waiting.")
    else:
        flash.set(request, "error", got["why"] or "Could not read the sheet.")
    return RedirectResponse("/todo-sync", status_code=303)
