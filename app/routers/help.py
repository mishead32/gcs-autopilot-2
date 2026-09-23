"""Help Desk — one employee asking another for help.

Deliberately thin. Raising a ticket creates the Delegation task there and then,
so the request lands on the helper's dashboard like any other work, counts in
their Delegation score, and can carry attachments and notes without a second
system to learn. The ticket itself only exists to hold the "who asked whom, and
did they say no" story that a bare task can't.

The helper can decline, which cancels the task and tells the raiser why. That
is the check on people quietly dumping their work on a colleague — a declined
ticket is visible to both, and to anyone reading the list.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import (
    HelpTicket, HelpStatus, Task, TaskStatus, TaskSource, TaskComment,
    User, Role, Priority,
)
from ..services import notify
from ..templating import templates

router = APIRouter()


def _visible(user: User):
    q = select(HelpTicket).where(HelpTicket.org_id == user.org_id)
    if user.role not in (Role.OWNER, Role.ADMIN):
        q = q.where(or_(HelpTicket.raiser_id == user.id,
                        HelpTicket.helper_id == user.id))
    return q


@router.get("/help", response_class=HTMLResponse)
def help_list(request: Request, user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    tickets = db.scalars(
        _visible(user).order_by(HelpTicket.created_at.desc()).limit(200)
    ).all()
    return templates.TemplateResponse(request, "help.html", {
        "user": user,
        "asked_of_me": [t for t in tickets if t.helper_id == user.id],
        "i_asked": [t for t in tickets if t.raiser_id == user.id],
        "others": [t for t in tickets
                   if t.helper_id != user.id and t.raiser_id != user.id],
    })


@router.get("/help/new", response_class=HTMLResponse)
def help_form(request: Request, user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    colleagues = db.scalars(
        select(User).where(User.org_id == user.org_id, User.active.is_(True),
                           User.id != user.id).order_by(User.name)
    ).all()
    return templates.TemplateResponse(request, "help_new.html", {
        "user": user, "colleagues": colleagues, "priorities": list(Priority),
        "default_due": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
    })


@router.post("/help/new")
def raise_ticket(subject: str = Form(...), details: str = Form(""),
                 helper_id: int = Form(...), priority: str = Form("normal"),
                 needed_by: str = Form(...),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    helper = db.get(User, helper_id)
    if not helper or helper.org_id != user.org_id or not helper.active:
        raise HTTPException(400, "Pick a colleague from your organisation")
    if helper.id == user.id:
        raise HTTPException(400, "You can't raise a help ticket with yourself")
    if not subject.strip():
        raise HTTPException(400, "Say briefly what you need help with")

    due = datetime.fromisoformat(needed_by)

    task = Task(
        org_id=user.org_id,
        branch_id=helper.branch_id or user.branch_id,
        title=f"Help: {subject.strip()}"[:250],
        details=(f"Help requested by {user.name}.\n\n{details.strip()}"
                 if details.strip() else f"Help requested by {user.name}."),
        assigner_id=user.id,
        doer_id=helper.id,
        priority=Priority(priority),
        source=TaskSource.DELEGATION,
        due_at=due,
        requires_audit=False,
    )
    db.add(task)
    db.flush()

    ticket = HelpTicket(
        org_id=user.org_id, raiser_id=user.id, helper_id=helper.id,
        task_id=task.id, subject=subject.strip()[:250],
        details=details.strip() or None, priority=Priority(priority),
        needed_by=due, status=HelpStatus.OPEN,
    )
    db.add(ticket)
    notify.queue_task_assigned(db, task)
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


@router.post("/help/{ticket_id}/decline")
def decline(ticket_id: int, reason: str = Form(""),
            user: User = Depends(current_user), db: Session = Depends(get_db)):
    ticket = db.get(HelpTicket, ticket_id)
    if not ticket or ticket.org_id != user.org_id:
        raise HTTPException(404, "Help ticket not found")
    if ticket.helper_id != user.id:
        raise HTTPException(403, "Only the person asked can decline this")
    if ticket.status != HelpStatus.OPEN:
        raise HTTPException(400, "This ticket is already closed")

    ticket.status = HelpStatus.DECLINED
    ticket.decline_reason = reason.strip() or None
    ticket.closed_at = datetime.utcnow()

    task = ticket.task
    if task and task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
        task.status = TaskStatus.CANCELLED
        task.closed_at = ticket.closed_at
        db.add(TaskComment(
            task_id=task.id, author_id=user.id,
            body="Help request declined."
                 + (f" Reason: {reason.strip()}" if reason.strip() else "")))
    db.commit()
    return RedirectResponse("/help", status_code=303)
