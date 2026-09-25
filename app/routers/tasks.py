import json
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from sqlalchemy import select, or_, update, case, func, delete as sa_delete
from sqlalchemy.orm import Session

from ..config import UPLOAD_DIR
from .. import flash
from .. import clock
from ..db import get_db
from ..deps import current_user, manager_up, can_view_task, require_right
from ..models import (
    Task, TaskStatus, TaskSource, TaskComment, Attachment, User, Role, Priority,
    Branch, Right, OutboundMessage, AuditState, AUDIT_LABELS, HelpTicket, HelpStatus,
    Followup
)
from ..services import notify, flows as flow_svc, storage, holidays, xlsx
from ..templating import templates

router = APIRouter()

OPEN_STATES = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS,
               TaskStatus.REJECTED, TaskStatus.REOPENED)

# What "Completed" means on a task list: the person doing it has finished it.
#
# It used to mean only CLOSED, which left anything waiting on an auditor in a
# limbo of its own — the doer had done the work, the tab said nothing was
# there, and the only place it appeared was Audit pending. From where they
# sit the job IS done; whether the auditor has looked at it yet is the
# auditor's business, and the Audit column says so on every row.
#
# The score is deliberately not changed by this. A task still earns its marks
# when the audit closes, because an audit can send it back.
FINISHED_STATES = (TaskStatus.SUBMITTED, TaskStatus.COMPLETED)


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
    "done": ("finished_at", "Completion date"),
    "audit": ("submitted_at", "Submission date"),
    "audit_pending": ("due_at", "Planned date"),
    "audit_done": ("audited_at", "Audit date"),
    "false_mark": ("false_marked_at", "Flagged on"),
    "open": ("due_at", "Planned date"),
    "overdue": ("due_at", "Planned date"),
    "upcoming": ("due_at", "Planned date"),
    "all": ("due_at", "Planned date"),
}


def _basis_col(name: str):
    """The date column a view is filtered and sorted on.

    "finished_at" is not a column. A task closed outright has closed_at; one
    sitting with the auditor has only submitted_at. Both are finished as far
    as the doer is concerned, so the Completed view filters on whichever of
    the two exists — otherwise picking a date range would silently drop every
    task still awaiting audit.
    """
    if name == "finished_at":
        return func.coalesce(Task.closed_at, Task.submitted_at)
    return getattr(Task, name)


# High first, then medium, then low. The database stores the name, so an
# ordinary sort would give HIGH, LOW, MEDIUM alphabetically — hence the
# explicit ranking.
#
# Written as separate comparisons rather than case({...}, value=Task.priority):
# that shorter form leaves the enum values untyped, every row falls through to
# the else, and the list quietly sorts by date alone. It looks right until you
# check, which is exactly the kind of bug worth pinning down in a test.
PRIORITY_RANK = case(
    (Task.priority == Priority.HIGH, 0),
    (Task.priority == Priority.MEDIUM, 1),
    else_=2,
)

# The three kinds of work a person can be given. Everyone has some of each,
# and "what do I owe on the checklist today" is a different question from
# "what has somebody delegated to me", so the list has to separate them.
SOURCE_TABS = {
    "delegation": {"src": TaskSource.DELEGATION, "label": "Delegation",
                   "blurb": "one-off work somebody assigned you"},
    "checklist": {"src": TaskSource.RECURRING, "label": "Checklist",
                  "blurb": "your repeating daily and weekly jobs"},
    "fms": {"src": TaskSource.FLOW, "label": "FMS",
            "blurb": "steps inside a running flow"},
}
SOURCE_KEY = {v["src"]: k for k, v in SOURCE_TABS.items()}

# What each status tab is called, for the line written at the top of an export
# so a downloaded file says what it is months later.
STATUS_LABELS = {
    "open": "Open", "overdue": "Overdue", "upcoming": "Coming up",
    "done": "Completed", "audit": "With auditor",
    "audit_pending": "Audit pending", "audit_done": "Audit completed",
    "false_mark": "False marking", "all": "All",
}


def _parse_day(raw: str):
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date() if raw.strip() else None
    except ValueError:
        return None


@router.get("/tasks", response_class=HTMLResponse)
def task_list(request: Request, status: str = "open", scope: str = "mine",
              date_from: str = "", date_to: str = "", source: str = "",
              export: str = "",
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = _visible_tasks_query(user)
    now = clock.now()

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
    elif status == "audit_done":
        q = q.where(Task.audit_state == AuditState.COMPLETED)
    elif status == "false_mark":
        q = q.where(Task.false_marked.is_(True))
    elif status == "done":
        q = q.where(Task.status.in_(FINISHED_STATES))

    col_name, basis_label = DATE_BASIS.get(status, ("due_at", "Planned date"))
    col = _basis_col(col_name)
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

    # Count each kind of work BEFORE narrowing to one, so the tabs can show
    # how much is waiting under each — otherwise you have to click all three
    # to find out where your day has gone.
    counts = {k: 0 for k in SOURCE_TABS}
    _sub = q.subquery()
    for src, n in db.execute(select(_sub.c.source, func.count())
                             .select_from(_sub).group_by(_sub.c.source)).all():
        # A subquery column hands back the raw stored value on some backends
        # and the enum on others, so accept either rather than silently
        # counting nothing.
        key = SOURCE_KEY.get(src) or SOURCE_KEY.get(
            next((m for m in TaskSource if m.value == str(src).lower()
                  or m.name == str(src).upper()), None))
        if key:
            counts[key] = n
    counts["all"] = sum(counts[k] for k in SOURCE_TABS)

    # Which kind of work. A doer's three kinds land in one list, and until now
    # the only way to tell them apart was to read the Source column row by row.
    source = source if source in SOURCE_TABS else ""
    if source:
        q = q.where(Task.source == SOURCE_TABS[source]["src"])

    if col_name in ("finished_at", "audited_at", "false_marked_at"):
        order = (col.desc(),)                     # newest first
        sort_label = {"finished_at": "Most recently completed first",
                      "audited_at": "Most recently audited first",
                      "false_marked_at": "Most recently flagged first"}[col_name]
    else:
        order = (PRIORITY_RANK, Task.due_at.asc())
        sort_label = "High priority first, then by deadline"
    # The Excel version of this exact page. Built after every filter above has
    # been applied, from the same query object, so the file can never show a
    # different set of rows from the screen it was downloaded off.
    if xlsx.wants(export):
        rows = db.scalars(q.order_by(*order)).all()     # no 500-row screen cap
        tabs = {"mine": "assigned to me", "assigned": "delegated by me",
                "all": "everyone"}
        note = (f"{tabs.get(scope, scope)} · "
                f"{STATUS_LABELS.get(status, status)} · "
                f"{SOURCE_TABS[source]['label'] if source else 'all work types'}"
                + (f" · {basis_label.lower()} "
                   + (f"{date_from} to {date_to}" if date_from and date_to
                      else f"from {date_from}" if date_from
                      else f"up to {date_to}")
                   if (date_from or date_to) else ""))
        return xlsx.one(f"tasks-{source or 'all'}-{status}", "Tasks",
                        xlsx.task_columns(), rows, note)

    tasks = db.scalars(q.order_by(*order).limit(500)).all()
    return templates.TemplateResponse(request, "tasks.html", {
        "user": user, "tasks": tasks, "status": status, "scope": scope,
        "source": source, "source_tabs": SOURCE_TABS, "counts": counts,
        "sort_label": sort_label,
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
        # End of tomorrow by default. Most delegated work is "by end of day",
        # and making people type a time every single time invites 12:00.
        "default_due": (clock.now() + timedelta(days=1))
                       .strftime("%Y-%m-%dT23:59"),
    })


@router.post("/tasks/new")
async def create_task(
    request: Request,
    title: str = Form(...),
    details: str = Form(""),
    doer_id: int = Form(...),
    branch_id: str = Form(""),
    priority: str = Form("medium"),
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
    flash.set(request, "assigned", f"{doer.name} — due {due_dt:%d %b, %I:%M %p}")
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
        "can_move_due": user.has(Right.CHANGE_DUE_DATE),
        # A decision step shows the doer two buttons rather than one.
        "decision_step": task.flow_step
                         if (task.flow_step and task.flow_step.is_decision) else None,
        "can_reopen": user.has(Right.REOPEN_TASK)
                      and task.status == TaskStatus.COMPLETED,
        "can_set_audit": user.has(Right.AUDIT_TASK),
        "audit_states": list(AuditState),
        "audit_labels": AUDIT_LABELS,
        "can_false_mark": user.has(Right.FALSE_MARK)
                          and task.status in (TaskStatus.SUBMITTED, TaskStatus.COMPLETED)
                          and not task.false_marked,
    })


# ------------------------------------------------- mark complete inline ----
# Most people close a task the moment they finish it, standing in front of
# the list of what they owe. Making them open the task page first, scroll to
# the bottom, submit, and then find their way back to the list was four
# actions for one. This serves the same submit form as a fragment, so a
# pop-up on the list can show it in place.
#
# It deliberately does not extend base.html: it is a piece of a page.
@router.get("/tasks/{task_id}/mark", response_class=HTMLResponse)
def mark_box(task_id: int, request: Request,
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.org_id != user.org_id or not can_view_task(user, task):
        raise HTTPException(404, "Task not found")
    # Only the doer closes their own work, and only while it is still open —
    # the same two rules the submit route enforces. Checking them here as
    # well means the button never opens a box that cannot be submitted.
    if task.doer_id != user.id:
        raise HTTPException(403, "Only the person doing this task can close it")
    if task.status not in OPEN_STATES:
        raise HTTPException(400, "This task is not open")

    capture_fields = []
    if task.flow_step and task.flow_step.capture_fields:
        capture_fields = [f.strip() for f in task.flow_step.capture_fields.split(",")
                          if f.strip()]

    return templates.TemplateResponse(request, "_markbox.html", {
        "user": user, "task": task, "capture_fields": capture_fields,
        "captured": json.loads(task.captured_data or "{}"),
        "decision_step": task.flow_step
                         if (task.flow_step and task.flow_step.is_decision) else None,
    })


def _safe_return(raw: str, fallback: str) -> str:
    """Where to land after submitting — but only somewhere on this site.

    A redirect target that arrives in a form field is a classic way to bounce
    someone onto another site from a link that looks like ours, so anything
    that is not a plain path on this app is thrown away rather than trusted.
    """
    dest = (raw or "").strip()
    if not dest.startswith("/"):
        return fallback
    if dest.startswith("//") or dest.startswith("/\\"):
        return fallback              # "//evil.com" is a full URL to a browser
    if "\n" in dest or "\r" in dest:
        return fallback              # header splitting
    return dest


# ------------------------------------------------------- edit / delete -----
@router.post("/tasks/{task_id}/edit")
def edit_task(task_id: int, title: str = Form(...), details: str = Form(""),
              doer_id: int = Form(...), priority: str = Form("medium"),
              due_at: str = Form(""),
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
    # Moving a deadline decides whether the work counts as late, which is
    # half of the doer's score for that source. So the form's value is only
    # honoured for someone holding the right; for everybody else the field is
    # disabled in the page AND ignored here, because a disabled field is a
    # courtesy, not a control.
    may_move = user.has(Right.CHANGE_DUE_DATE)
    new_due = datetime.fromisoformat(due_at) if (due_at and may_move) else task.due_at
    if may_move and task.due_at != new_due:
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
    # Everything that points at this task has to be dealt with first, or the
    # database refuses the delete and the person gets a 500 with no idea why.
    #
    # There are five such tables, and each one gets the treatment that suits
    # what it is. Notes and attachments belong to the task and go with it
    # (the model cascades those). The three below do not:

    # The notification log is a record of what we SENT. It outlives the thing
    # it was about, so the link is unhooked rather than the row destroyed.
    db.execute(update(OutboundMessage)
               .where(OutboundMessage.task_id == task.id)
               .values(task_id=None))

    # A help ticket is only a record of somebody asking for this task to
    # exist. With the task gone it refers to nothing, so it goes too.
    db.execute(sa_delete(HelpTicket).where(HelpTicket.task_id == task.id))

    # A follow-up tick says "the EA chased this task on Tuesday". It cannot
    # outlive the task either — and it is not optional, the column will not
    # take a null. This was the missing one: any task the follow-up desk had
    # ever ticked refused to delete, with nothing on screen but "Internal
    # Server Error".
    db.execute(sa_delete(Followup).where(Followup.task_id == task.id))

    db.delete(task)
    db.commit()
    return RedirectResponse("/tasks?scope=assigned&status=open", status_code=303)


# ------------------------------------------------------------- reopen ------
@router.post("/tasks/{task_id}/reopen")
def reopen_task(task_id: int, request: Request, reason: str = Form(""),
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
    task.reopened_at = clock.now()
    # the work has to be checked again once it comes back
    task.requires_audit = True
    task.audit_state = AuditState.PENDING

    db.add(TaskComment(task_id=task.id, author_id=user.id,
                       body="Reopened by auditor." + (f" Reason: {reason.strip()}"
                                                      if reason.strip() else "")))
    flash.set(request, "reopened", task.title)
    notify.queue(db, task.doer, "task_rejected", task,
                 title=task.title, remark=reason.strip() or "Reopened for rework")
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


# -------------------------------------------------------- false marking ----
@router.post("/tasks/{task_id}/false-mark")
def false_mark(task_id: int, request: Request, reason: str = Form(""),
               confirm: str = Form(""),
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
    task.false_marked_at = clock.now()
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
    flash.set(request, "flagged", f"{task.doer.name} — −10 to their score")
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
        task.started_at = clock.now()
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

    # A decision step ends in one of two outcomes and the doer picks which.
    # Anything else is recorded as a pass, so an ordinary step routes the
    # only way it can.
    step = task.flow_step
    if step is not None and step.is_decision:
        choice = (form.get("decision") or "").strip().lower()
        if choice not in ("pass", "fail"):
            raise HTTPException(
                400, f"Choose an outcome: {step.yes_label} or {step.no_label}.")
        task.decision = choice
        label = step.yes_label if choice == "pass" else step.no_label
        db.add(TaskComment(task_id=task.id, author_id=user.id,
                           body=f"Decision: {label}."))

    captured = {k[6:]: v for k, v in form.items() if k.startswith("field_") and v}
    if captured:
        task.captured_data = json.dumps(captured)

    task.submitted_at = clock.now()
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
    flash.set(request, "submitted" if task.requires_audit else "completed",
              task.title)
    # Submitted from the pop-up on a list: go back to that list, on the same
    # tab and filter the person was reading, rather than dumping them on the
    # task page they were trying to avoid opening.
    return RedirectResponse(
        _safe_return(form.get("return_to"), f"/tasks/{task_id}"),
        status_code=303)


def close_help_ticket(db: Session, task: Task) -> None:
    """A help request is finished the moment its delegation task is."""
    ticket = db.scalar(select(HelpTicket).where(HelpTicket.task_id == task.id,
                                                HelpTicket.status == HelpStatus.OPEN))
    if ticket:
        ticket.status = HelpStatus.CLOSED
        ticket.closed_at = clock.now()


# --------------------------------------------------------- audit status ----
@router.post("/tasks/{task_id}/audit-state")
def set_audit_state(task_id: int, request: Request, state: str = Form(...),
                    remark: str = Form(""),
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
        task.audited_at = clock.now()
    elif new_state == AuditState.NOT_REQUIRED:
        task.auditor_id = None
        task.audited_at = None

    if was != new_state or remark.strip():
        db.add(TaskComment(
            task_id=task.id, author_id=user.id,
            body=f"Audit: {AUDIT_LABELS[was]} → {AUDIT_LABELS[new_state]}."
                 + (f" Remark: {remark.strip()}" if remark.strip() else "")))
    db.commit()
    flash.set(request, "audited", AUDIT_LABELS[new_state])
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/audit")
def audit_task(task_id: int, request: Request, decision: str = Form(...),
               score: float = Form(8.0), remark: str = Form(""),
               user: User = Depends(require_right(Right.AUDIT_TASK)),
               db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.org_id != user.org_id:
        raise HTTPException(404, "Task not found")
    if task.status != TaskStatus.SUBMITTED:
        raise HTTPException(400, "Task is not awaiting audit")

    task.auditor_id = user.id
    task.audit_remark = remark.strip() or None
    task.audited_at = clock.now()

    if decision == "approve":
        task.audit_score = max(0.0, min(10.0, score))
        task.status = TaskStatus.COMPLETED
        task.audit_state = AuditState.COMPLETED
        task.closed_at = task.audited_at
        close_help_ticket(db, task)
        db.commit()
        flash.set(request, "audited", f"Approved — {task.title}")
        if task.flow_instance_id:
            flow_svc.advance_flow(db, task)
    else:
        task.status = TaskStatus.REJECTED
        task.audit_state = AuditState.PENDING     # it comes back for audit again
        task.submitted_at = None
        notify.queue(db, task.doer, "task_rejected", task,
                     title=task.title, remark=task.audit_remark or "—")
        db.commit()
        flash.set(request, "reopened", f"Sent back to {task.doer.name}")
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


