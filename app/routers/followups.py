"""Follow-ups — who chased the work that has not been done.

Two people do the chasing, and the split is by source:

    PC  ->  Checklist and FMS
    EA  ->  Delegation

Each of them gets a list of every open task in their half, for a given day,
and ticks the ones they actually chased. One tick per task per day: a task
pending for a fortnight has to be chased every day, and yesterday's tick must
not make today look covered. That is the whole point of the feature — without
the per-day rule management would see "followed up" on a task nobody has
touched since it was raised.

The management view of all this lives under Reports → Follow-ups, where it
shares its filters with every other report.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from .. import clock
from ..db import get_db
from ..deps import current_user
from ..models import (
    Task, TaskStatus, TaskSource, User, Role, Right, Followup,
    PRIORITY_ORDER,
)
from ..templating import templates

router = APIRouter()

OPEN_STATES = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS,
               TaskStatus.REJECTED, TaskStatus.REOPENED)

# Which sources each follow-up role is answerable for.
DESKS = {
    "ea": {
        "label": "EA — Delegation",
        "short": "EA",
        "right": Right.FOLLOWUP_DELEGATION,
        "sources": (TaskSource.DELEGATION,),
    },
    "pc": {
        "label": "PC — Checklist & FMS",
        "short": "PC",
        "right": Right.FOLLOWUP_CHECKLIST_FMS,
        "sources": (TaskSource.RECURRING, TaskSource.FLOW),
    },
}


def _parse_day(raw: str) -> date:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date() if raw.strip() \
            else clock.today()
    except ValueError:
        return clock.today()


def _visible(user: User, q, desk_right: Right | None = None):
    """Who can see which rows on the follow-up pages.

    Holding the desk right widens the view to the whole group on purpose: the
    PC's job is to chase everybody's Checklist and FMS work, so a PC who is
    filed as an ordinary doer must still see all of it. Without this they
    would open the page and see only their own three tasks.
    """
    if desk_right is not None and desk_right.value in user.right_set:
        return q
    if user.role in (Role.OWNER, Role.ADMIN) or user.has(Right.VIEW_ALL_BRANCHES):
        return q
    if user.role == Role.MANAGER:
        return q.where(or_(Task.branch_id == user.branch_id,
                           Task.doer_id == user.id,
                           Task.assigner_id == user.id))
    return q.where(or_(Task.doer_id == user.id, Task.assigner_id == user.id))


def _desk_for(user: User) -> str:
    """Which desk to open by default for this person."""
    if user.has(Right.FOLLOWUP_DELEGATION) and not user.has(Right.FOLLOWUP_CHECKLIST_FMS):
        return "ea"
    if user.has(Right.FOLLOWUP_CHECKLIST_FMS) and not user.has(Right.FOLLOWUP_DELEGATION):
        return "pc"
    return "ea"


def _open_tasks(db: Session, user: User, sources, day: date,
                desk_right: Right | None = None) -> list[Task]:
    """Every task in these sources that was still open on `day`.

    'Open on that day' rather than 'open right now', so yesterday's list does
    not silently shrink as work gets closed today. A report you cannot
    reproduce tomorrow is not a report.
    """
    end = datetime.combine(day, datetime.max.time())
    q = select(Task).where(
        Task.org_id == user.org_id,
        Task.source.in_(sources),
        Task.due_at <= end,
        or_(Task.closed_at.is_(None), Task.closed_at > end),
        Task.status != TaskStatus.CANCELLED,
    )
    rows = list(db.scalars(_visible(user, q, desk_right)).all())
    rows.sort(key=lambda t: (PRIORITY_ORDER.get(t.priority, 9), t.due_at))
    return rows


def _ticks(db: Session, task_ids: list[int], day: date) -> dict[int, Followup]:
    if not task_ids:
        return {}
    rows = db.scalars(select(Followup).where(Followup.task_id.in_(task_ids),
                                             Followup.day == day)).all()
    return {f.task_id: f for f in rows}


# ------------------------------------------------------------- the desk ----
@router.get("/followups", response_class=HTMLResponse)
def followups(request: Request, desk: str = "", day: str = "",
              date_from: str = "", date_to: str = "",
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The desk itself, plus a from/to range view over it.

    Ticking is a per-day act — you either chased a task on Tuesday or you
    did not — so the tick list is always one day. The from/to range is how
    you look back over a stretch of days; picking one of them opens that
    day's tick list. `day` is still accepted so old links keep working.
    """
    desk = desk if desk in DESKS else _desk_for(user)
    cfg = DESKS[desk]

    # A bare ?day= (or nothing at all) means a single day, from == to.
    start = _parse_day(date_from) if date_from else _parse_day(day)
    end = _parse_day(date_to) if date_to else start
    if start > end:
        start, end = end, start
    if (end - start).days > 92:
        start = end - timedelta(days=92)      # keep one page readable

    can_tick = user.has(cfg["right"])
    span = (end - start).days + 1

    ctx = {
        "user": user, "desk": desk, "cfg": cfg, "desks": DESKS,
        "can_tick": can_tick, "span": span,
        "date_from": start.isoformat(), "date_to": end.isoformat(),
    }

    if span > 1:
        # A range — a day-by-day summary, each row a link to its tick list.
        rows = []
        d = end
        while d >= start:
            tasks = _open_tasks(db, user, cfg["sources"], d, cfg["right"])
            ticks = _ticks(db, [t.id for t in tasks], d)
            rows.append({"day": d, "due": len(tasks), "done": len(ticks),
                         "missed": len(tasks) - len(ticks),
                         "who": ", ".join(sorted({f.by.name.split()[0]
                                                  for f in ticks.values()}))})
            d -= timedelta(days=1)
        ctx.update({
            "range_rows": rows,
            "due": sum(r["due"] for r in rows),
            "done": sum(r["done"] for r in rows),
            "missed": sum(r["missed"] for r in rows),
        })
        return templates.TemplateResponse(request, "followups.html", ctx)

    on = start
    tasks = _open_tasks(db, user, cfg["sources"], on, cfg["right"])
    ticks = _ticks(db, [t.id for t in tasks], on)
    ctx.update({
        "range_rows": None,
        "day": on, "day_str": on.isoformat(),
        "prev_day": (on - timedelta(days=1)).isoformat(),
        "next_day": (on + timedelta(days=1)).isoformat(),
        "is_today": on == clock.today(),
        "tasks": tasks, "ticks": ticks,
        "due": len(tasks), "done": len(ticks), "missed": len(tasks) - len(ticks),
    })
    return templates.TemplateResponse(request, "followups.html", ctx)


@router.post("/followups/{task_id}/tick")
def tick(task_id: int, day: str = Form(""), desk: str = Form("ea"),
         remark: str = Form(""),
         user: User = Depends(current_user), db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.org_id != user.org_id:
        raise HTTPException(404, "Task not found")

    cfg = DESKS.get(desk) or DESKS["ea"]
    if not user.has(cfg["right"]):
        raise HTTPException(403, f"You are not the {cfg['short']} for this desk.")
    if task.source not in cfg["sources"]:
        raise HTTPException(400, f"That task is not on the {cfg['short']} desk.")

    on = _parse_day(day)
    if on > clock.today():
        raise HTTPException(400, "You can't record a follow-up for a future date.")

    existing = db.scalar(select(Followup).where(Followup.task_id == task.id,
                                                Followup.day == on))
    if existing:
        # Ticking again is how you untick — the same button both ways, so
        # a mis-click is one click to undo rather than a support question.
        db.delete(existing)
    else:
        db.add(Followup(org_id=user.org_id, task_id=task.id, day=on,
                        by_id=user.id, remark=remark.strip() or None))
    db.commit()
    return RedirectResponse(f"/followups?desk={desk}&day={on.isoformat()}",
                            status_code=303)


# --------------------------------------------------------------- report ----
@router.get("/followups/report")
def report_moved(date_from: str = "", date_to: str = ""):
    """Lives under Reports now, with the same filters as every other report."""
    q = f"?date_from={date_from}&date_to={date_to}" if (date_from or date_to) else ""
    return RedirectResponse(f"/reports/followups{q}", status_code=307)
