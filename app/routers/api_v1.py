"""The programmatic interface: /api/v1, used with an API key.

Every call is `Authorization: Bearer <key>` (or `X-API-Key`). A key carries
permissions, and acts as one person: tasks it creates are assigned by that
person and need that person's right to create tasks.

Answers are JSON. Nothing here uploads files — a start question that wants a
file cannot be answered through it.
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (ApiKey, Branch, Flow, Priority, Right, Task, TaskComment,
                      TaskSource, TaskStatus, User, CONTEXT_PEOPLE)
from ..services import apikeys, flowform, holidays, notify, scoring
from ..services import flows as flow_svc

router = APIRouter(prefix="/api/v1")


def key_for(*needed: str):
    """The caller's key, checked: real, not revoked, not over the limit, and
    allowed to do at least one of `needed` (no argument = any key)."""
    def _dep(request: Request, db: Session = Depends(get_db)) -> ApiKey:
        raw = ""
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            raw = auth[7:].strip()
        raw = raw or (request.headers.get("x-api-key") or "").strip()
        row = apikeys.lookup(db, raw)
        if row is None:
            raise HTTPException(401, "Missing, wrong or revoked API key.")
        if apikeys.too_many(row.id):
            raise HTTPException(429, "Too many requests — at most "
                                     f"{apikeys.LIMIT_PER_MINUTE} a minute per key.")
        if needed and not (apikeys.scopes_of(row) & set(needed)):
            raise HTTPException(403, "This key may not do that. It needs: "
                                     + " or ".join(needed) + ".")
        apikeys.touch(db, row)
        return row
    return _dep


def _iso(d):
    return d.isoformat(timespec="seconds") if d else None


def _person(u: User | None):
    return {"id": u.id, "name": u.name, "email": u.email} if u else None


def _task(t: Task) -> dict:
    return {
        "id": t.id, "ref": t.ref, "title": t.title, "details": t.details,
        "status": t.status.value, "priority": t.priority.value,
        "source": t.source.value, "due_at": _iso(t.due_at),
        "created_at": _iso(t.created_at), "started_at": _iso(t.started_at),
        "submitted_at": _iso(t.submitted_at), "closed_at": _iso(t.closed_at),
        "doer": _person(t.doer), "assigner": _person(t.assigner),
        "branch": ({"id": t.branch.id, "name": t.branch.name} if t.branch else None),
        "flow_run_id": t.flow_instance_id, "requires_audit": t.requires_audit,
        "false_marked": t.false_marked, "completion_note": t.completion_note,
    }


# --------------------------------------------------------------- who ------
@router.get("/me")
def me(key: ApiKey = Depends(key_for())):
    return {"key": key.name, "scopes": sorted(apikeys.scopes_of(key)),
            "acts_as": _person(key.acting_user)}


@router.get("/users")
def users(key: ApiKey = Depends(key_for()), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).where(User.org_id == key.org_id,
                                         User.active.is_(True)).order_by(User.name)).all()
    return {"users": [{"id": u.id, "name": u.name, "email": u.email,
                       "role": u.role.value,
                       "branch": ({"id": u.branch.id, "name": u.branch.name}
                                  if u.branch else None)} for u in rows]}


@router.get("/branches")
def branches(key: ApiKey = Depends(key_for()), db: Session = Depends(get_db)):
    rows = db.scalars(select(Branch).where(Branch.org_id == key.org_id)
                      .order_by(Branch.name)).all()
    return {"branches": [{"id": b.id, "name": b.name} for b in rows]}


# ------------------------------------------------------------- scores -----
@router.get("/scores")
def scores(date_from: str = "", date_to: str = "", days: int = 7,
           branch_id: int | None = None, doer_id: int | None = None,
           key: ApiKey = Depends(key_for("scores")), db: Session = Depends(get_db)):
    """The EM score for a period: everyone, one company, or one person."""
    from .dashboard import _pack
    try:
        start, end = scoring.resolve_window(date_from or None, date_to or None, days)
    except ValueError:
        raise HTTPException(400, "Dates are YYYY-MM-DD.")
    board = scoring.scoreboard(db, key.org_id, start, end, branch_id, doer_id)
    return {
        "from": start.date().isoformat(), "to": end.date().isoformat(),
        "overall": _pack(board["overall"]),
        "people": [{"id": r["user"].id, "name": r["user"].name,
                    "email": r["user"].email,
                    "branch": r["user"].branch.name if r["user"].branch else None,
                    **_pack(r["card"])} for r in board["people"]],
        "branches": [{"id": r["branch"].id if r["branch"] else None,
                      "name": r["branch"].name if r["branch"] else "Unassigned",
                      **_pack(r["card"])} for r in board["branches"]],
    }


# -------------------------------------------------------------- tasks -----
@router.get("/tasks")
def tasks(status: str = "", doer_id: int | None = None, branch_id: int | None = None,
          source: str = "", due_from: str = "", due_to: str = "",
          limit: int = 100, offset: int = 0,
          key: ApiKey = Depends(key_for("tasks_read")), db: Session = Depends(get_db)):
    q = select(Task).where(Task.org_id == key.org_id)
    try:
        if status:
            q = q.where(Task.status == TaskStatus(status))
        if source:
            q = q.where(Task.source == TaskSource(source))
        if due_from:
            q = q.where(Task.due_at >= datetime.fromisoformat(due_from))
        if due_to:
            q = q.where(Task.due_at <= datetime.fromisoformat(due_to))
    except ValueError:
        raise HTTPException(400, "A filter value is not valid (status, source or date).")
    if doer_id:
        q = q.where(Task.doer_id == doer_id)
    if branch_id:
        q = q.where(Task.branch_id == branch_id)
    limit = max(1, min(limit, 500))
    rows = db.scalars(q.order_by(Task.due_at.desc(), Task.id.desc())
                      .limit(limit).offset(max(0, offset))).all()
    return {"count": len(rows), "limit": limit, "offset": max(0, offset),
            "tasks": [_task(t) for t in rows]}


@router.get("/tasks/{task_id}")
def task_one(task_id: int, key: ApiKey = Depends(key_for("tasks_read")),
             db: Session = Depends(get_db)):
    t = db.get(Task, task_id)
    if not t or t.org_id != key.org_id:
        raise HTTPException(404, "No such task.")
    out = _task(t)
    out["comments"] = [{"by": c.author.name, "at": _iso(c.created_at), "body": c.body}
                       for c in t.comments]
    return out


class NewTask(BaseModel):
    title: str = Field(min_length=1, max_length=250)
    details: str = ""
    doer_ids: list[int] = []
    doer_emails: list[str] = []
    due_at: str
    priority: str = "medium"
    branch_id: int | None = None
    requires_audit: bool = False
    requires_attachment: bool = True


@router.post("/tasks", status_code=201)
def task_create(body: NewTask, key: ApiKey = Depends(key_for("tasks_write")),
                db: Session = Depends(get_db)):
    """One task per person, assigned by the key's person."""
    boss = key.acting_user
    if not boss.has(Right.CREATE_TASK):
        raise HTTPException(403, f"{boss.name}, whom this key acts as, may not "
                                 "create tasks.")
    try:
        prio = Priority(body.priority)
        due = datetime.fromisoformat(body.due_at)
    except ValueError:
        raise HTTPException(400, "priority is low, medium or high; due_at is "
                                 "like 2026-11-30T18:00:00.")
    doers, seen = [], set()
    for did in body.doer_ids:
        u = db.get(User, did)
        if not u or u.org_id != key.org_id or not u.active:
            raise HTTPException(400, f"Unknown doer id {did}.")
        if u.id not in seen:
            seen.add(u.id); doers.append(u)
    for mail in body.doer_emails:
        u = db.scalar(select(User).where(User.org_id == key.org_id,
                                         User.email == mail.strip().lower(),
                                         User.active.is_(True)))
        if not u:
            raise HTTPException(400, f"No active person with the email {mail}.")
        if u.id not in seen:
            seen.add(u.id); doers.append(u)
    if not doers:
        raise HTTPException(400, "Give doer_ids or doer_emails — who is to do it.")
    if len(doers) > 50:
        raise HTTPException(400, "At most 50 people in one call.")
    if body.branch_id:
        b = db.get(Branch, body.branch_id)
        if not b or b.org_id != key.org_id:
            raise HTTPException(400, "That is not one of your companies.")

    made = []
    for person in doers:
        due_dt, moved = holidays.shift_due(
            db, key.org_id, due, body.branch_id or person.branch_id)
        t = Task(org_id=key.org_id, branch_id=body.branch_id or person.branch_id,
                 title=body.title.strip(), details=body.details.strip() or None,
                 assigner_id=boss.id, doer_id=person.id, priority=prio,
                 source=TaskSource.DELEGATION, due_at=due_dt,
                 requires_audit=body.requires_audit,
                 requires_attachment=body.requires_attachment)
        db.add(t)
        db.flush()
        db.add(TaskComment(task_id=t.id, author_id=boss.id,
                           body=f"Created through the API key “{key.name}”."
                           + (f" Due date moved to {due_dt:%d/%m/%Y %I:%M %p} — "
                              f"closed day ({moved})." if moved else "")))
        notify.queue_task_assigned(db, t)
        made.append(t)
    db.commit()
    return {"created": [_task(t) for t in made]}


# --------------------------------------------------------------- flows ----
@router.get("/flows")
def flows(key: ApiKey = Depends(key_for("flows_write", "tasks_read")),
          db: Session = Depends(get_db)):
    rows = db.scalars(select(Flow).where(Flow.org_id == key.org_id,
                                         Flow.active.is_(True)).order_by(Flow.name)).all()
    return {"flows": [{
        "id": f.id, "name": f.name, "branch_id": f.branch_id,
        "needs_company": any(s.doer_dept for s in f.steps) and f.branch_id is None,
        "questions": [{"label": q["label"], "type": q["type"],
                       "options": q["options"], "required": q["required"]}
                      for q in f.start_form_fields]} for f in rows]}


class StartRun(BaseModel):
    answers: dict[str, str] = {}
    branch_id: int | None = None
    reference: str = ""


@router.post("/flows/{flow_id}/start", status_code=201)
def flow_start(flow_id: int, body: StartRun,
               key: ApiKey = Depends(key_for("flows_write")),
               db: Session = Depends(get_db)):
    """Start an FMS run, answering its start form by question label."""
    boss = key.acting_user
    flow = db.get(Flow, flow_id)
    if not flow or flow.org_id != key.org_id:
        raise HTTPException(404, "No such flow.")
    if not flow.active:
        raise HTTPException(400, f"“{flow.name}” is switched off.")
    branch = body.branch_id or flow.branch_id
    if branch is not None:
        b = db.get(Branch, branch)
        if not b or b.org_id != key.org_id:
            raise HTTPException(400, "That is not one of your companies.")
    if any(s.doer_dept for s in flow.steps) and branch is None:
        raise HTTPException(400, "This flow needs branch_id — which company the run is for.")
    blocked = flow_svc.start_blockers(db, flow, branch)
    if blocked:
        raise HTTPException(400, " ".join(blocked))

    known = {f["label"] for f in flow.start_form_fields}
    extra = [k for k in body.answers if k not in known]
    if extra:
        raise HTTPException(400, "Not questions of this flow: " + ", ".join(extra)
                            + ". Questions are: " + ", ".join(sorted(known)) + ".")
    context = {}
    for field in flow.start_form_fields:
        if field["type"] == "file":
            if field["required"]:
                raise HTTPException(
                    400, f"'{field['label']}' needs a file, which cannot be sent "
                         "through the API. Start this flow from the website.")
            continue
        value, person, problem = flowform.check_answer(
            db, flow.org_id, field, body.answers.get(field["label"]))
        if problem:
            raise HTTPException(400, problem)
        if not value:
            continue
        context[field["label"]] = value
        if person is not None:
            context.setdefault(CONTEXT_PEOPLE, {})[field["label"]] = person.id
    try:
        inst = flow_svc.start_flow(db, flow, body.reference, boss, {}, context,
                                   branch_id=branch)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"run_id": inst.id, "ref": inst.ref, "reference": inst.reference,
            "steps_opened": [_task(t) for t in inst.tasks if t.flow_step
                             and t.flow_step.position == inst.current_position]}
