import json
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from sqlalchemy import select, or_, update, delete as sa_delete
from sqlalchemy.orm import Session

from ..config import UPLOAD_DIR
from ..db import get_db
from ..deps import current_user, manager_up, can_view_task, require_right
from ..models import (
    Task, TaskStatus, TaskSource, TaskComment, Attachment, User, Role, Priority,
    Branch, Right, OutboundMessage, AuditState, AUDIT_LABELS, HelpTicket, HelpStatus
)
from ..services import notify, flows as flow_svc, storage, holidays
from ..templating import templates

router = APIRouter()

OPEN_STATES = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS,
               TaskStatus.REJECTED, TaskStatus.REOPENED)


def _visible_tasks_query(user: User):
    q = select(Task).where(Task.org_id == user.org_id)
    if user.role == Role.DOER:
        q = q.where(or_(Task.doer_id == user.id, Task.assigner_id == user.id))
    elif user.role == Role.MANAGER:
        q = q.where(or_(
            Task.branch_id == user.branch_id,
            Task.doer_id == user.id,
            Task.assigner_id == user.id,
        ))
    return q


# ---------------------------------------------------------------- list -----
# Which date column a view is really about. Asking "what did we finish last
# week" means the date it was CLOSED; asking "what is open in this period"
# means the date it was PLANNED for. Filtering both on the same column is the
# usual way these reports end up quietly wrong.
DATE_BASIS = {
    "done": ("closed_at", "Completion date"),
    "audit": ("submitted_at", "Submission date"),
    "audit_pending": ("due_at", "Planned date"),
    "open": ("due_at", "Planned date"),
    "overdue": ("due_at", "Planned date"),
    "upcoming": ("due_at", "Planned date"),
    "all": ("due_at", "Planned date"),
}


def _parse_day(raw: str):
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date() if raw.strip() else None
    except ValueError:
        return None


@router.get("/tasks", response_class=HTMLResponse)
def task_list(request: Request, status: str = "open", scope: str = "mine",
              date_from: str = "", date_to: str = "",
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = _visible_tasks_query(user)
    now = datetime.utcnow()

    if scope == "mine":
        q = q.where(Task.doer_id == user.id)
    elif scope == "assigned":
        q = q.where(Task.assigner_id == user.id)

    if status == "open":
        q = q.where(Task.status.in_(OPEN_STATES))
    elif status == "overdue":
        q = q.where(Task.status.in_(OPEN_STATES), Task.due_at < now)
    elif status == "upcoming":
        q = q.where(Task.status.in_(OPEN_STATES), Task.due_at >= now)
    elif status == "audit":
        q = q.where(Task.status == TaskStatus.SUBMITTED)
    elif status == "audit_pending":
        q = q.where(Task.audit_state == AuditState.PENDING)
    elif status == "done":
        q = q.where(Task.status == TaskStatus.COMPLETED)

    col_name, basis_label = DATE_BASIS.get(status, ("due_at", "Planned date"))
    col = getattr(Task, col_name)
    start, end = _parse_day(date_from), _parse_day(date_to)
    if start and end and start > end:
        start, end = end, start          # someone typed them the wrong way round
    if start:
        q = q.where(col >= datetime.combine(start, datetime.min.time()))
    if end:
        # inclusive of the whole end day, not up to midnight of it
        q = q.where(col <= datetime.combine(end, datetime.max.time()))
    if start or end:
        q = q.where(col.is_not(None))

    order = col.desc() if col_name == "closed_at" else Task.due_at.asc()
    tasks = db.scalars(q.order_by(order).limit(500)).all()
    return templates.TemplateResponse(request, "tasks.html", {
        "user": user, "tasks": tasks, "status": status, "scope": scope,
        "date_from": start.isoformat() if start else "",
        "date_to": end.isoformat() if end else "",
        "basis_label": basis_label,
        "filtered": bool(start or end),
    })


# ------------------------------------------------------------ delegate -----
@router.get("/tasks/new", response_class=HTMLResponse)
def new_task_form(request: Request,
                  user: User = Depends(require_right(Right.CREATE_TASK)),
                  db: Session = Depends(get_db)):
    doers = db.scalars(
        select(User).where(User.org_id == user.org_id, User.active.is_(True))
        .order_by(User.name)
    ).all()
    branches = db.scalars(select(Branch).where(Branch.org_id == user.org_id)).all()
    return templates.TemplateResponse(request, "task_new.html", {
        "user": user, "doers": doers, "branches": branches,
        "priorities": list(Priority),
        "default_due": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
    })


@router.post("/tasks/new")
async def create_task(
    request: Request,
    title: str = Form(...),
    details: str = Form(""),
    doer_id: int = Form(...),
    branch_id: str = Form(""),
    priority: str = Form("normal"),
    due_at: str = Form(...),
    requires_audit: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(require_right(Right.CREATE_TASK)),
    db: Session = Depends(get_db),
):
    doer = db.get(User, doer_id)
    if not doer or doer.org_id != user.org_id:
        raise HTTPException(400, "Unknown doer")

    # Proof is required unless the form explicitly says otherwise. The form
    # pairs the checkbox with a hidden "0", so an unticked box still posts a
    # value; a task created any other way (bulk import, API) sends nothing at
    # all and gets the safe default.
    form = await request.form()
    _proof = form.getlist("requires_attachment")
    proof_required = True if not _proof else ("1" in _proof)

    # A deadline on a holiday would mark the doer late for a day nobody was
    # in, so it moves to the next working day and the task says why.
    branch_for_holiday = int(branch_id) if branch_id else doer.branch_id
    due_dt, moved_from = holidays.shift_due(
        db, user.org_id, datetime.fromisoformat(due_at), branch_for_holiday)

    task = Task(
        org_id=user.org_id,
        branch_id=int(branch_id) if branch_id else doer.branch_id,
        title=title.strip(),
        details=details.strip() or None,
        assigner_id=user.id,
        doer_id=doer.id,
        priority=Priority(priority),
        source=TaskSource.DELEGATION,
        due_at=due_dt,
        requires_audit=bool(requires_audit),
        requires_attachment=proof_required,
    )
    db.add(task)
    db.flush()

    # Files chosen on the create form are only handled here in local mode.
    # With a bucket configured the browser uploads them directly afterwards.
    if storage.mode() == "local":
        for f in files or []:
            if not f.filename:
                continue
            data = await f.read()
            if storage.check(f.filename, f.content_type or "", len(data)):
                continue
            key = storage.save_local(task.id, f.filename, data)
            db.add(Attachment(task_id=task.id, uploaded_by_id=user.id,
                              filename=f.filename[:250], stored_name=key,
                              size=len(data), content_type=f.content_type or "",
                              storage="local"))

    if moved_from:
        db.add(TaskComment(
            task_id=task.id, author_id=user.id,
            body=f"Deadline moved to {due_dt:%d %b %Y, %I:%M %p} — "
                 f"{moved_from} is a holiday."))

    notify.queue_task_assigned(db, task)
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


# ------------------------------------------------------------- detail ------
@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(task_id: int, request: Request,
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.org_id != user.org_id or not can_view_task(user, task):
        raise HTTPException(404, "Task not found")

    capture_fields = []
    if task.flow_step and task.flow_step.capture_fields:
        capture_fields = [f.strip() for f in task.flow_step.capture_fields.split(",") if f.strip()]

    doers = []
    if user.has(Right.EDIT_TASK):
        doers = db.scalars(
            select(User).where(User.org_id == user.org_id, User.active.is_(True))
            .order_by(User.name)
        ).all()

    return templates.TemplateResponse(request, "task_detail.html", {
        "user": user, "task": task, "capture_fields": capture_fields,
        "captured": json.loads(task.captured_data or "{}"),
        "is_doer": task.doer_id == user.id,
        "doers": doers,
        "priorities": list(Priority),
        "can_audit": user.has(Right.AUDIT_TASK) and task.status == TaskStatus.SUBMITTED,
        "can_edit": user.has(Right.EDIT_TASK),
        "can_delete": user.has(Right.DELETE_TASK),
        "can_reopen": user.has(Right.REOPEN_TASK)
                      and task.status == TaskStatus.COMPLETED,
        "can_set_audit": user.has(Right.AUDIT_TASK),
        "audit_states": list(AuditState),
        "audit_labels": AUDIT_LABELS,
        "can_false_mark": user.has(Right.FALSE_MARK)
                          and task.status in (TaskStatus.SUBMITTED, TaskStatus.COMPLETED)
                          and not task.false_marked,
    })


# ------------------------------------------------------- edit / delete -----
@router.post("/tasks/{task_id}/edit")
def edit_task(task_id: int, title: str = Form(...), details: str = Form(""),
              doer_id: int = Form(...), priority: str = Form("normal"),
              due_at: str = Form(...),
              user: User = Depends(require_right(Right.EDIT_TASK)),
              db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.org_id != user.org_id or not can_view_task(user, task):
        raise HTTPException(404, "Task not found")

    new_doer = db.get(User, doer_id)
    if not new_doer or new_doer.org_id != user.org_id:
        raise HTTPException(400, "Unknown doer")

    changes = []
    if task.title != title.strip():
        changes.append(f"title → {title.strip()}")
    if task.doer_id != new_doer.id:
        changes.append(f"doer → {new_doer.name}")
        task.branch_id = new_doer.branch_id or task.branch_id
    new_due = datetime.fromisoformat(due_at)
    if task.due_at != new_due:
        changes.append(f"due → {new_due.strftime('%d %b %Y, %I:%M %p')}")
    if task.priority != Priority(priority):
        changes.append(f"priority → {priority}")

    task.title = title.strip()
    task.details = details.strip() or None
    task.doer_id = new_doer.id
    task.priority = Priority(priority)
    task.due_at = new_due

    if changes:
        db.add(TaskComment(task_id=task.id, author_id=user.id,
                           body="Edited: " + "; ".join(changes)))
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/delete")
def delete_task(task_id: int, user: User = Depends(require_right(Right.DELETE_TASK)),
                db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.org_id != user.org_id:
        raise HTTPException(404, "Task not found")
    if task.flow_instance_id:
        raise HTTPException(
            400, "This task is a step inside a running FMS flow. "
                 "Deleting it would break the chain — cancel the flow run instead."
        )
    # the notification log outlives the task, so unhook it rather than
    # cascading — Postgres enforces this foreign key even though SQLite doesn't
    db.execute(update(OutboundMessage)
               .where(OutboundMessage.task_id == task.id)
               .values(task_id=None))
    # a help ticket is only a record of who asked for this task — it goes with it
    db.execute(sa_delete(HelpTicket).where(HelpTicket.task_id == task.id))
    db.delete(task)
    db.commit()
    return RedirectResponse("/tasks?scope=assigned&status=open", status_code=303)


# ------------------------------------------------------------- reopen ------
@router.post("/tasks/{task_id}/reopen")
def reopen_task(task_id: int, reason: str = Form(""),
                user: User = Depends(require_right(Right.REOPEN_TASK)),
                db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.org_id != user.org_id:
        raise HTTPException(404, "Task not found")
    if task.status != TaskStatus.COMPLETED:
        raise HTTPException(400, "Only a completed task can be reopened")

    task.status = TaskStatus.REOPENED
    task.closed_at = None
    task.submitted_at = None
    task.reopen_count += 1
    task.reopened_by_id = user.id
    task.reopened_at = datetime.utcnow()
    # the work has to be checked again once it comes back
    task.requires_audit = True
    task.audit_state = AuditState.PENDING

    db.add(TaskComment(task_id=task.id, author_id=user.id,
                       body="Reopened by auditor." + (f" Reason: {reason.strip()}"
                                                      if reason.strip() else "")))
    notify.queue(db, task.doer, "task_rejected", task,
                 title=task.title, remark=reason.strip() or "Reopened for rework")
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


# -------------------------------------------------------- false marking ----
@router.post("/tasks/{task_id}/false-mark")
def false_mark(task_id: int, reason: str = Form(""), confirm: str = Form(""),
               user: User = Depends(require_right(Right.FALSE_MARK)),
               db: Session = Depends(get_db)):
    """Auditor asserts the doer closed this without actually doing it: −10."""
    task = db.get(Task, task_id)
    if not task or task.org_id != user.org_id:
        raise HTTPException(404, "Task not found")
    if confirm != "yes":
        raise HTTPException(400, "False marking must be confirmed")
    if task.false_marked:
        raise HTTPException(400, "Already flagged as false marking")
    if task.status not in (TaskStatus.SUBMITTED, TaskStatus.COMPLETED):
        raise HTTPException(400, "Only a submitted or completed task can be flagged")

    task.false_marked = True
    task.false_marked_by_id = user.id
    task.false_marked_at = datetime.utcnow()
    task.false_mark_reason = reason.strip() or None

    # a false mark always sends the work back
    task.status = TaskStatus.REOPENED
    task.closed_at = None
    task.submitted_at = None
    task.reopen_count += 1
    task.reopened_by_id = user.id
    task.reopened_at = task.false_marked_at
    task.requires_audit = True
    task.audit_state = AuditState.PENDING

    db.add(TaskComment(
        task_id=task.id, author_id=user.id,
        body="⚠ Flagged as FALSE MARKING (−10 to score)."
             + (f" Reason: {reason.strip()}" if reason.strip() else "")))
    notify.queue(db, task.doer, "task_rejected", task, title=task.title,
                 remark="Flagged as false marking (−10). "
                        + (reason.strip() or "Please redo and resubmit."))
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/start")
def start_task(task_id: int, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    """Kept only for tasks created before work started automatically.

    New tasks are already in progress the moment they are assigned, so this
    does nothing for them.
    """
    task = db.get(Task, task_id)
    if not task or task.doer_id != user.id:
        raise HTTPException(403, "Only the doer can start this task")
    if task.status == TaskStatus.PENDING:
        task.status = TaskStatus.IN_PROGRESS
        task.started_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/submit")
async def submit_task(task_id: int, request: Request,
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.doer_id != user.id:
        raise HTTPException(403, "Only the doer can submit this task")
    if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
        raise HTTPException(400, "Task is already closed")

    if task.requires_attachment and not task.attachments:
        raise HTTPException(
            400,
            "This task needs proof attached before it can be submitted. "
            "Attach a photo, screenshot or file above — you can paste a "
            "screenshot with Ctrl+V — then submit again.")

    form = await request.form()
    task.completion_note = (form.get("completion_note") or "").strip() or None

    captured = {k[6:]: v for k, v in form.items() if k.startswith("field_") and v}
    if captured:
        task.captured_data = json.dumps(captured)

    task.submitted_at = datetime.utcnow()
    if task.requires_audit:
        task.status = TaskStatus.SUBMITTED
        task.audit_state = AuditState.PENDING
        db.commit()
    else:
        task.status = TaskStatus.COMPLETED
        task.closed_at = task.submitted_at
        close_help_ticket(db, task)
        db.commit()
        if task.flow_instance_id:
            flow_svc.advance_flow(db, task)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


def close_help_ticket(db: Session, task: Task) -> None:
    """A help request is finished the moment its delegation task is."""
    ticket = db.scalar(select(HelpTicket).where(HelpTicket.task_id == task.id,
                                                HelpTicket.status == HelpStatus.OPEN))
    if ticket:
        ticket.status = HelpStatus.CLOSED
        ticket.closed_at = datetime.utcnow()


# --------------------------------------------------------- audit status ----
@router.post("/tasks/{task_id}/audit-state")
def set_audit_state(task_id: int, state: str = Form(...), remark: str = Form(""),
                    user: User = Depends(require_right(Right.AUDIT_TASK)),
                    db: Session = Depends(get_db)):
    """Set Audit pending / completed / not required on any task, with a remark.

    Separate from the approve-or-send-back flow above: that one decides the
    *task*, this one records the *audit* — including on tasks that were never
    flagged for audit when they were created.
    """
    task = db.get(Task, task_id)
    if not task or task.org_id != user.org_id or not can_view_task(user, task):
        raise HTTPException(404, "Task not found")
    try:
        new_state = AuditState(state)
    except ValueError:
        raise HTTPException(400, "Unknown audit status")

    if new_state == AuditState.COMPLETED and not remark.strip():
        raise HTTPException(400, "An audit remark is required to mark the audit complete.")

    was = task.audit_state
    task.audit_state = new_state
    task.requires_audit = new_state != AuditState.NOT_REQUIRED

    if remark.strip():
        task.audit_remark = remark.strip()
    if new_state == AuditState.COMPLETED:
        task.auditor_id = user.id
        task.audited_at = datetime.utcnow()
    elif new_state == AuditState.NOT_REQUIRED:
        task.auditor_id = None
        task.audited_at = None

    if was != new_state or remark.strip():
        db.add(TaskComment(
            task_id=task.id, author_id=user.id,
            body=f"Audit: {AUDIT_LABELS[was]} → {AUDIT_LABELS[new_state]}."
                 + (f" Remark: {remark.strip()}" if remark.strip() else "")))
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/audit")
def audit_task(task_id: int, decision: str = Form(...), score: float = Form(8.0),
               remark: str = Form(""),
               user: User = Depends(require_right(Right.AUDIT_TASK)),
               db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.org_id != user.org_id:
        raise HTTPException(404, "Task not found")
    if task.status != TaskStatus.SUBMITTED:
        raise HTTPException(400, "Task is not awaiting audit")

    task.auditor_id = user.id
    task.audit_remark = remark.strip() or None
    task.audited_at = datetime.utcnow()

    if decision == "approve":
        task.audit_score = max(0.0, min(10.0, score))
        task.status = TaskStatus.COMPLETED
        task.audit_state = AuditState.COMPLETED
        task.closed_at = task.audited_at
        close_help_ticket(db, task)
        db.commit()
        if task.flow_instance_id:
            flow_svc.advance_flow(db, task)
    else:
        task.status = TaskStatus.REJECTED
        task.audit_state = AuditState.PENDING     # it comes back for audit again
        task.submitted_at = None
        notify.queue(db, task.doer, "task_rejected", task,
                     title=task.title, remark=task.audit_remark or "—")
        db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/comment")
def add_comment(task_id: int, body: str = Form(...),
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or not can_view_task(user, task):
        raise HTTPException(404, "Task not found")
    if body.strip():
        db.add(TaskComment(task_id=task.id, author_id=user.id, body=body.strip()))
        db.commit()
    return RedirectResponse(f"/tasks/{task_id}#notes", status_code=303)


