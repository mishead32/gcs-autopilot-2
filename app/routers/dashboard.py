from datetime import datetime

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, manager_up, admin_up
from ..models import (
    Task, TaskStatus, User, Role, RecurringRule, Recurrence, Priority,
    Branch, OutboundMessage, FlowInstance
)
from ..services import scoring, recurring
from ..templating import templates

router = APIRouter()
OPEN = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS,
        TaskStatus.REJECTED, TaskStatus.REOPENED)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Doer dashboard - what's on my plate right now."""
    mine = db.scalars(
        select(Task).where(Task.doer_id == user.id, Task.status.in_(OPEN))
        .order_by(Task.due_at.asc())
    ).all()
    submitted = db.scalars(
        select(Task).where(Task.doer_id == user.id, Task.status == TaskStatus.SUBMITTED)
    ).all()

    now = datetime.utcnow()
    buckets = {
        "overdue": [t for t in mine if t.due_at < now],
        "today": [t for t in mine if t.due_at.date() == now.date() and t.due_at >= now],
        "upcoming": [t for t in mine if t.due_at.date() > now.date()],
    }

    awaiting_audit = []
    if user.role in (Role.OWNER, Role.ADMIN, Role.MANAGER):
        q = select(Task).where(Task.org_id == user.org_id, Task.status == TaskStatus.SUBMITTED)
        if user.role == Role.MANAGER:
            q = q.where(Task.branch_id == user.branch_id)
        awaiting_audit = db.scalars(q.order_by(Task.submitted_at)).all()

    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "buckets": buckets,
        "submitted": submitted,
        "awaiting_audit": awaiting_audit,
        "card": scoring.user_scorecard(db, user, days=30),
    })


def _resolve_filters(db: Session, user: User, branch: str, doer: str):
    """Shared by the page and its live API so both agree on what is allowed."""
    branches = scoring.visible_branches(db, user)
    allowed = {b.id for b in branches}

    branch_id = int(branch) if branch else None
    if branch_id and branch_id not in allowed:
        branch_id = None
    # a manager with only one visible company is pinned to it
    if branch_id is None and len(branches) == 1:
        branch_id = branches[0].id

    doers = scoring.selectable_doers(db, user, branch_id)
    doer_id = int(doer) if doer else None
    if doer_id and doer_id not in {d.id for d in doers}:
        doer_id = None   # doer isn't in the selected company — drop the filter

    return branches, branch_id, doers, doer_id


def _pack(c):
    return {
        "score": c.score, "gap": c.gap, "band": c.band,
        "planned": c.planned, "completed": c.completed, "not_done": c.not_done,
        "on_time": c.on_time, "late": c.late, "false_marks": c.false_marks,
        "closed_in_window": c.closed_in_window, "overdue_now": c.overdue_now,
        "not_done_penalty": c.not_done_penalty, "late_penalty": c.late_penalty,
        "false_penalty": c.false_penalty, "total_penalty": c.total_penalty,
        "completion_rate": c.completion_rate, "on_time_rate": c.on_time_rate,
        "sources": [{
            "key": s.key, "label": s.label, "benchmark": s.benchmark,
            "raw_not_done": s.raw_not_done, "raw_late": s.raw_late,
            "planned": s.planned,
            "completed": s.completed, "not_done": s.not_done, "late": s.late,
            "not_done_penalty": s.not_done_penalty, "late_penalty": s.late_penalty,
            "subtotal": s.subtotal,
        } for s in c.source_list],
    }


@router.get("/stats", response_class=HTMLResponse)
def stats(request: Request, date_from: str = "", date_to: str = "",
          branch: str = "", doer: str = "", days: int = 7,
          user: User = Depends(manager_up), db: Session = Depends(get_db)):
    branches, branch_id, doers, doer_id = _resolve_filters(db, user, branch, doer)
    start, end = scoring.resolve_window(date_from or None, date_to or None, days)
    board = scoring.scoreboard(db, user.org_id, start, end, branch_id, doer_id)

    all_doers = scoring.selectable_doers(db, user, None)
    return templates.TemplateResponse(request, "stats.html", {
        "user": user, "board": board, "branches": branches,
        "branch_id": branch_id, "doers": doers, "all_doers": all_doers,
        "doer_id": doer_id, "days": days,
        "date_from": start.strftime("%Y-%m-%d"),
        "date_to": end.strftime("%Y-%m-%d"),
        "pinned": len(branches) == 1,
    })


@router.get("/api/stats")
def stats_api(date_from: str = "", date_to: str = "", branch: str = "",
              doer: str = "", days: int = 7,
              user: User = Depends(manager_up), db: Session = Depends(get_db)):
    """Polled by the Performance page so the live scores refresh in place."""
    _, branch_id, _, doer_id = _resolve_filters(db, user, branch, doer)
    start, end = scoring.resolve_window(date_from or None, date_to or None, days)
    board = scoring.scoreboard(db, user.org_id, start, end, branch_id, doer_id)

    return {
        "overall": _pack(board["overall"]),
        "people": [{"id": r["user"].id, "name": r["user"].name,
                    "branch": r["user"].branch.name if r["user"].branch else "—",
                    **_pack(r["card"])} for r in board["people"]],
        "branches": [{"name": r["branch"].name if r["branch"] else "Unassigned",
                      **_pack(r["card"])} for r in board["branches"]],
    }


@router.get("/api/my-score")
def my_score_api(days: int = 30, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    """Live refresh for the doer's own score panel."""
    return _pack(scoring.user_scorecard(db, user, days=days))


# ------------------------------------------------------- recurring rules ---
@router.get("/recurring", response_class=HTMLResponse)
def recurring_list(request: Request, user: User = Depends(manager_up),
                   db: Session = Depends(get_db)):
    rules = db.scalars(
        select(RecurringRule).where(RecurringRule.org_id == user.org_id)
        .order_by(RecurringRule.title)
    ).all()
    doers = db.scalars(
        select(User).where(User.org_id == user.org_id, User.active.is_(True)).order_by(User.name)
    ).all()
    branches = db.scalars(select(Branch).where(Branch.org_id == user.org_id)).all()
    return templates.TemplateResponse(request, "recurring.html", {
        "user": user, "rules": rules, "doers": doers, "branches": branches,
        "frequencies": list(Recurrence), "priorities": list(Priority),
    })


@router.post("/recurring")
async def create_rule(
    request: Request,
    title: str = Form(...), details: str = Form(""), doer_id: int = Form(...),
    branch_id: str = Form(""), frequency: str = Form("daily"), day_of: str = Form(""),
    due_time: str = Form("18:00"), priority: str = Form("normal"),
    requires_audit: str = Form(""),
    user: User = Depends(manager_up), db: Session = Depends(get_db),
):
    # see the note in tasks.create_task
    form = await request.form()
    _proof = form.getlist("requires_attachment")
    proof_required = True if not _proof else ("1" in _proof)
    doer = db.get(User, doer_id)
    db.add(RecurringRule(
        org_id=user.org_id,
        branch_id=int(branch_id) if branch_id else (doer.branch_id if doer else None),
        title=title.strip(), details=details.strip() or None,
        doer_id=doer_id, assigner_id=user.id,
        priority=Priority(priority), frequency=Recurrence(frequency),
        day_of=int(day_of) if day_of else None,
        due_time=due_time, requires_audit=bool(requires_audit),
        requires_attachment=proof_required,
    ))
    db.commit()
    return RedirectResponse("/recurring", status_code=303)


@router.post("/recurring/{rule_id}/toggle")
def toggle_rule(rule_id: int, user: User = Depends(manager_up), db: Session = Depends(get_db)):
    rule = db.get(RecurringRule, rule_id)
    if rule and rule.org_id == user.org_id:
        rule.active = not rule.active
        db.commit()
    return RedirectResponse("/recurring", status_code=303)


@router.post("/recurring/run")
def run_recurring(user: User = Depends(manager_up), db: Session = Depends(get_db)):
    recurring.run_spawn(db)
    return RedirectResponse("/recurring", status_code=303)


# ------------------------------------------------------- google sheet -----
@router.get("/sheet", response_class=HTMLResponse)
def sheet_page(request: Request, user: User = Depends(admin_up),
               db: Session = Depends(get_db)):
    from ..services import sheets
    return templates.TemplateResponse(request, "sheet.html", {
        "user": user, "enabled": sheets.enabled(), "url": sheets.sheet_url(),
        "tabs": [t[0] for t in sheets.TABS], "result": None,
    })


@router.post("/sheet/sync", response_class=HTMLResponse)
def sheet_sync(request: Request, user: User = Depends(admin_up),
               db: Session = Depends(get_db)):
    from ..services import sheets
    result = sheets.sync(db, user.org_id) if sheets.enabled() else {
        "ok": False, "error": "Google Sheet is not configured on this server."}
    return templates.TemplateResponse(request, "sheet.html", {
        "user": user, "enabled": sheets.enabled(), "url": sheets.sheet_url(),
        "tabs": [t[0] for t in sheets.TABS], "result": result,
    })


# ------------------------------------------------------- message outbox ----
@router.get("/outbox", response_class=HTMLResponse)
def outbox(request: Request, user: User = Depends(manager_up), db: Session = Depends(get_db)):
    msgs = db.scalars(
        select(OutboundMessage).where(OutboundMessage.org_id == user.org_id)
        .order_by(OutboundMessage.created_at.desc()).limit(100)
    ).all()
    return templates.TemplateResponse(request, "outbox.html", {"user": user, "msgs": msgs})
