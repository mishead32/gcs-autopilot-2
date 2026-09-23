"""Reports — one place management can pull any figure from.

Six reports, every one of them filtered the same way (from date, to date,
branch, employee, priority), so nobody has to learn a different set of
controls for each:

  1. Delegation — pending and completed
  2. Checklist  — pending and completed
  3. FMS        — pending and completed
  4. Follow-ups — pending and completed, across all three
  5. Audit      — pending and completed, across all three
  6. EM score   — person-wise and branch-wise

Two rules run through all of them:

  * A pending task is filtered on its PLANNED date, a completed one on the
    date it was CLOSED. Filtering both on one column is the usual way this
    kind of report ends up quietly wrong.
  * Nobody sees a row they could not already see on the task list. The same
    visibility rules apply here as everywhere else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from .. import clock
from ..db import get_db
from ..deps import current_user
from ..models import (
    Task, TaskStatus, TaskSource, User, Role, Right, Branch, Followup,
    AuditState, AUDIT_LABELS, Priority, PRIORITY_ORDER,
)
from ..services import scoring
from ..templating import templates

router = APIRouter()

OPEN_STATES = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS,
               TaskStatus.REJECTED, TaskStatus.REOPENED)

# The three kinds of work, in the order they appear everywhere else.
SOURCES = {
    "delegation": {"src": TaskSource.DELEGATION, "label": "Delegation",
                   "blurb": "One-off tasks somebody assigned"},
    "checklist": {"src": TaskSource.RECURRING, "label": "Checklist",
                  "blurb": "Repeating tasks from a checklist rule"},
    "fms": {"src": TaskSource.FLOW, "label": "FMS",
            "blurb": "Steps inside a flow"},
}
SOURCE_OF = {v["src"]: k for k, v in SOURCES.items()}

# Which desk chases which sources — same split as the Follow-ups page.
DESKS = {
    "ea": {"label": "EA — Delegation", "short": "EA",
           "right": Right.FOLLOWUP_DELEGATION,
           "sources": (TaskSource.DELEGATION,)},
    "pc": {"label": "PC — Checklist & FMS", "short": "PC",
           "right": Right.FOLLOWUP_CHECKLIST_FMS,
           "sources": (TaskSource.RECURRING, TaskSource.FLOW)},
}

MAX_DAYS = 92          # one page of day rows stays readable


# ------------------------------------------------------------- filters ----
def _day(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date() if raw.strip() else None
    except ValueError:
        return None


@dataclass
class Filters:
    """Everything the filter bar collects, already validated."""
    date_from: date | None = None
    date_to: date | None = None
    branch_id: int | None = None
    doer_id: int | None = None
    priority: str = ""
    state: str = ""

    branches: list = field(default_factory=list)
    doers: list = field(default_factory=list)
    path: str = "/reports"
    hidden: dict = field(default_factory=dict)

    # --- what the template needs ---------------------------------------
    @property
    def f_from(self) -> str:
        return self.date_from.isoformat() if self.date_from else ""

    @property
    def f_to(self) -> str:
        return self.date_to.isoformat() if self.date_to else ""

    @property
    def active(self) -> bool:
        return bool(self.date_from or self.date_to or self.branch_id
                    or self.doer_id or self.priority)

    @property
    def clear_url(self) -> str:
        qs = urlencode(self.hidden)
        return f"{self.path}?{qs}" if qs else self.path

    def url(self, **over) -> str:
        """This page's URL with a few filter values changed."""
        q = dict(self.hidden)
        q.update({"date_from": self.f_from, "date_to": self.f_to,
                  "branch": self.branch_id or "", "doer": self.doer_id or "",
                  "priority": self.priority, "state": self.state})
        q.update(over)
        q = {k: v for k, v in q.items() if v not in ("", None)}
        return f"{self.path}?{urlencode(q)}"

    @property
    def presets(self) -> list[tuple[str, str, bool]]:
        """Quick ranges. Every one sets BOTH ends — there is no single-day
        filter anywhere in the reports."""
        today = clock.today()
        out = []
        for label, back in (("Today", 0), ("Last 7 days", 6),
                            ("Last 30 days", 29), ("Last 90 days", 89)):
            start = today - timedelta(days=back)
            on = self.date_from == start and self.date_to == today
            out.append((label, self.url(date_from=start.isoformat(),
                                        date_to=today.isoformat()), on))
        return out

    @property
    def summary(self) -> str:
        bits = []
        if self.date_from and self.date_to:
            bits.append(f"{self.date_from:%d %b %Y} to {self.date_to:%d %b %Y}")
        elif self.date_from:
            bits.append(f"from {self.date_from:%d %b %Y}")
        elif self.date_to:
            bits.append(f"up to {self.date_to:%d %b %Y}")
        else:
            bits.append("all dates")
        if self.branch_id:
            b = next((b for b in self.branches if b.id == self.branch_id), None)
            if b:
                bits.append(b.name)
        if self.doer_id:
            d = next((d for d in self.doers if d.id == self.doer_id), None)
            if d:
                bits.append(d.name)
        if self.priority:
            bits.append(f"{self.priority} priority")
        return " · ".join(bits)


def build_filters(db: Session, user: User, path: str, date_from: str, date_to: str,
                  branch: str, doer: str, priority: str = "", state: str = "",
                  hidden: dict | None = None, default_days: int | None = None) -> Filters:
    """Validate the query string once, for every report.

    A branch the viewer cannot see, or an employee who is not in the branch
    they picked, is dropped rather than refused — the filter bar is a
    convenience, not a security boundary, and the queries below are scoped
    separately.
    """
    branches = scoring.visible_branches(db, user)
    allowed = {b.id for b in branches}

    branch_id = int(branch) if branch.strip().isdigit() else None
    if branch_id is not None and branch_id not in allowed:
        branch_id = None
    if branch_id is None and len(branches) == 1:
        branch_id = branches[0].id          # one visible branch — pin to it

    doers = scoring.selectable_doers(db, user, branch_id)
    doer_id = int(doer) if doer.strip().isdigit() else None
    if doer_id is not None and doer_id not in {d.id for d in doers}:
        doer_id = None                      # not in the chosen branch

    start, end = _day(date_from), _day(date_to)
    if start and end and start > end:
        start, end = end, start             # typed the wrong way round
    if start is None and end is None and default_days:
        end = clock.today()
        start = end - timedelta(days=default_days - 1)

    pr = priority.strip().lower()
    if pr not in ("high", "medium", "low"):
        pr = ""

    return Filters(date_from=start, date_to=end, branch_id=branch_id,
                   doer_id=doer_id, priority=pr, state=state.strip().lower(),
                   branches=branches, doers=doers, path=path,
                   hidden=hidden or {})


# ---------------------------------------------------------- task queries ---
def _scoped(user: User, q):
    """Who sees which rows in a report.

    The default for an ordinary doer is their OWN work and nothing else —
    the reports are open to everybody precisely because they are scoped. The
    'See everyone's reports' right is what widens that to the company; until
    it is ticked, a doer opening the Delegation report sees only their own
    delegation tasks.
    """
    q = q.where(Task.org_id == user.org_id)
    if user.has(Right.VIEW_ALL_REPORTS):
        return q
    if user.role in (Role.OWNER, Role.ADMIN) or user.has(Right.VIEW_ALL_BRANCHES):
        return q
    if user.role == Role.MANAGER:
        return q.where(or_(Task.branch_id == user.branch_id,
                           Task.doer_id == user.id,
                           Task.assigner_id == user.id))
    return q.where(or_(Task.doer_id == user.id, Task.assigner_id == user.id))


def _apply_common(q, f: Filters):
    if f.branch_id:
        q = q.where(Task.branch_id == f.branch_id)
    if f.doer_id:
        q = q.where(Task.doer_id == f.doer_id)
    if f.priority:
        q = q.where(Task.priority == Priority(f.priority))
    return q


def _between(q, col, f: Filters):
    if f.date_from:
        q = q.where(col >= datetime.combine(f.date_from, datetime.min.time()))
    if f.date_to:
        # the whole end day, not up to midnight of it
        q = q.where(col <= datetime.combine(f.date_to, datetime.max.time()))
    if f.date_from or f.date_to:
        q = q.where(col.is_not(None))
    return q


def _sorted(rows: list[Task], by_close: bool = False) -> list[Task]:
    if by_close:
        return sorted(rows, key=lambda t: t.closed_at or t.due_at, reverse=True)
    return sorted(rows, key=lambda t: (PRIORITY_ORDER.get(t.priority, 9), t.due_at))


def _weight(rows) -> int:
    return sum(t.weight for t in rows)


# ================================================================ index ====
REPORT_CARDS = [
    {"key": "delegation", "url": "/reports/tasks?source=delegation",
     "title": "1 · Delegation", "desc": "Pending and completed delegation tasks."},
    {"key": "checklist", "url": "/reports/tasks?source=checklist",
     "title": "2 · Checklist", "desc": "Pending and completed checklist tasks."},
    {"key": "fms", "url": "/reports/tasks?source=fms",
     "title": "3 · FMS", "desc": "Pending and completed FMS steps."},
    {"key": "followups", "url": "/reports/followups", "desk": True,
     "title": "4 · Follow-ups", "desc": "Follow-up pending and completed — FMS, Checklist, Delegation."},
    {"key": "audit", "url": "/reports/audit",
     "title": "5 · Audit", "desc": "Audit pending and completed — FMS, Checklist, Delegation."},
    {"key": "score", "url": "/reports/score", "all_reports": True,
     "title": "6 · EM score", "desc": "Person-wise and branch-wise, with the full breakdown."},
]


@router.get("/reports", response_class=HTMLResponse)
def index(request: Request, user: User = Depends(current_user)):
    cards = [c for c in REPORT_CARDS
             if (not c.get("all_reports") or user.can_see_all_reports)
             and (not c.get("desk")
                  or user.can_see_all_reports or user.can_follow_up)]
    return templates.TemplateResponse(request, "reports_index.html",
                                      {"user": user, "cards": cards})


# ================================================= 1-3 · tasks by source ===
@router.get("/reports/tasks", response_class=HTMLResponse)
def task_report(request: Request, source: str = "delegation",
                date_from: str = "", date_to: str = "", branch: str = "",
                doer: str = "", priority: str = "", state: str = "pending",
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    if source not in SOURCES:
        raise HTTPException(404, "Unknown report. Pick one from the Reports page.")
    cfg = SOURCES[source]
    src = cfg["src"]

    f = build_filters(db, user, "/reports/tasks", date_from, date_to, branch,
                      doer, priority, state, hidden={"source": source})
    if f.state not in ("pending", "completed", "overdue", "all"):
        f.state = "pending"

    base = _apply_common(_scoped(user, select(Task)).where(Task.source == src), f)

    # Pending is about the PLANNED date; completed is about the date it was
    # actually closed. Same window, two different columns on purpose.
    pending = list(db.scalars(
        _between(base.where(Task.status.in_(OPEN_STATES)), Task.due_at, f)).all())
    completed = list(db.scalars(
        _between(base.where(Task.status == TaskStatus.COMPLETED), Task.closed_at, f)).all())

    now = clock.now()
    overdue = [t for t in pending if t.due_at < now]
    on_time = [t for t in completed if t.was_on_time]

    rows = {"pending": _sorted(pending), "overdue": _sorted(overdue),
            "completed": _sorted(completed, by_close=True),
            "all": _sorted(pending) + _sorted(completed, by_close=True)}[f.state]

    total = len(pending) + len(completed)
    return templates.TemplateResponse(request, "report_tasks.html", {
        "user": user, "f": f, "cfg": cfg, "source": source, "rows": rows,
        "counts": {"pending": len(pending), "completed": len(completed),
                   "overdue": len(overdue), "all": total},
        "weights": {"pending": _weight(pending), "completed": _weight(completed)},
        "rate": round(len(completed) / total * 100, 1) if total else 0.0,
        "on_time_rate": round(len(on_time) / len(completed) * 100, 1) if completed else 0.0,
    })


# ==================================================== 4 · follow-up report ==
def _followup_rows(db: Session, user: User, f: Filters) -> dict:
    """Day-by-day: how many tasks were open, how many were chased.

    'Open on that day' rather than 'open right now', so last week's figures
    do not shrink as work gets closed today. Everything is fetched in two
    queries and split up in Python — a query per day would be 180 of them.
    """
    end = f.date_to or clock.today()
    start = f.date_from or (end - timedelta(days=6))
    if (end - start).days > MAX_DAYS:
        start = end - timedelta(days=MAX_DAYS)

    span_end = datetime.combine(end, datetime.max.time())
    q = _apply_common(_scoped(user, select(Task)).where(
        Task.due_at <= span_end,
        Task.status != TaskStatus.CANCELLED), f)
    tasks = list(db.scalars(q).all())
    by_id = {t.id: t for t in tasks}

    ticks = list(db.scalars(select(Followup).where(
        Followup.org_id == user.org_id,
        Followup.day >= start, Followup.day <= end)).all())
    ticked: dict[date, set[int]] = {}
    who: dict[date, set[str]] = {}
    for t in ticks:
        if t.task_id not in by_id:
            continue
        ticked.setdefault(t.day, set()).add(t.task_id)
        if t.by:
            who.setdefault(t.day, set()).add(t.by.name.split()[0])

    days, d = [], end
    while d >= start:
        day_end = datetime.combine(d, datetime.max.time())
        open_today = [t for t in tasks
                      if t.due_at <= day_end
                      and (t.closed_at is None or t.closed_at > day_end)]
        row = {"day": d, "desks": {}, "who": ", ".join(sorted(who.get(d, ())))}
        for key, cfg in DESKS.items():
            mine = [t for t in open_today if t.source in cfg["sources"]]
            done = len([t for t in mine if t.id in ticked.get(d, ())])
            row["desks"][key] = {"due": len(mine), "done": done,
                                 "missed": len(mine) - done}
        days.append(row)
        d -= timedelta(days=1)

    totals = {k: {"due": 0, "done": 0, "missed": 0} for k in DESKS}
    for row in days:
        for k, v in row["desks"].items():
            for fld in ("due", "done", "missed"):
                totals[k][fld] += v[fld]
    grand = {fld: sum(totals[k][fld] for k in DESKS)
             for fld in ("due", "done", "missed")}
    grand["rate"] = round(grand["done"] / grand["due"] * 100, 1) if grand["due"] else 0.0
    for k in DESKS:
        t = totals[k]
        t["rate"] = round(t["done"] / t["due"] * 100, 1) if t["due"] else 0.0

    return {"days": days, "totals": totals, "grand": grand,
            "start": start, "end": end}


@router.get("/reports/followups", response_class=HTMLResponse)
def followup_report(request: Request, date_from: str = "", date_to: str = "",
                    branch: str = "", doer: str = "", priority: str = "",
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    # This one is about how well OTHER people chased the work, so it is for
    # management and for the two people who do the chasing — not for every
    # doer. The other reports only ever show a person their own work.
    if not (user.can_see_all_reports or user.can_follow_up):
        raise HTTPException(
            403, "The follow-up report is about how other people are being "
                 "chased, so it needs the \u201cSee everyone\u2019s reports\u201d right "
                 "(or the PC / EA right). Ask an admin to tick it on your user. "
                 "Your own tasks are on the Dashboard and under My Tasks.")

    f = build_filters(db, user, "/reports/followups", date_from, date_to,
                      branch, doer, priority, default_days=7)
    data = _followup_rows(db, user, f)

    staff = db.scalars(select(User).where(User.org_id == user.org_id,
                                          User.active.is_(True))).all()
    # Only people the right was explicitly ticked for. Owners and admins hold
    # every right implicitly, and listing them all as "the PC" says nothing.
    desk_people = {
        k: [u.name for u in staff
            if cfg["right"].value in u.right_set
            and u.role not in (Role.OWNER, Role.ADMIN)]
        for k, cfg in DESKS.items()
    }
    return templates.TemplateResponse(request, "report_followups.html", {
        "user": user, "f": f, "desks": DESKS, "desk_people": desk_people, **data,
    })


# ======================================================== 5 · audit report ==
@router.get("/reports/audit", response_class=HTMLResponse)
def audit_report(request: Request, date_from: str = "", date_to: str = "",
                 branch: str = "", doer: str = "", priority: str = "",
                 state: str = "pending",
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    f = build_filters(db, user, "/reports/audit", date_from, date_to, branch,
                      doer, priority, state)
    if f.state not in ("pending", "completed", "not_required", "all"):
        f.state = "pending"

    base = _between(_apply_common(_scoped(user, select(Task)), f), Task.due_at, f)
    tasks = list(db.scalars(base).all())

    # source key -> {pending, completed, not_required, total}
    grid = {k: {"pending": 0, "completed": 0, "not_required": 0, "total": 0}
            for k in SOURCES}
    for t in tasks:
        key = SOURCE_OF.get(t.source)
        if key is None:
            continue
        grid[key][t.audit_state.value] += 1
        grid[key]["total"] += 1
    grand = {fld: sum(grid[k][fld] for k in SOURCES)
             for fld in ("pending", "completed", "not_required", "total")}
    checked = grand["completed"] + grand["pending"]
    grand["rate"] = round(grand["completed"] / checked * 100, 1) if checked else 0.0

    if f.state == "all":
        rows = _sorted(tasks)
    else:
        rows = _sorted([t for t in tasks if t.audit_state.value == f.state])

    return templates.TemplateResponse(request, "report_audit.html", {
        "user": user, "f": f, "rows": rows, "grid": grid, "grand": grand,
        "sources": SOURCES, "labels": AUDIT_LABELS,
    })


# ==================================================== 6 · EM score report ===
@router.get("/reports/score", response_class=HTMLResponse)
def score_report(request: Request, date_from: str = "", date_to: str = "",
                 branch: str = "", doer: str = "", view: str = "person",
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    # This one shows other people's scores, so it is the strictest of the six.
    if not user.can_see_all_reports:
        raise HTTPException(
            403, "The EM score report shows everybody\u2019s scores, so it needs "
                 "the \u201cSee everyone\u2019s reports\u201d right. Ask an admin to tick "
                 "it on your user. Your own score is on your Dashboard.")

    f = build_filters(db, user, "/reports/score", date_from, date_to, branch,
                      doer, hidden={"view": view} if view == "branch" else {},
                      default_days=30)
    start = datetime.combine(f.date_from, datetime.min.time())
    end = datetime.combine(f.date_to, datetime.max.time())

    board = scoring.scoreboard(db, user.org_id, start, end, f.branch_id, f.doer_id)

    # Picking a branch narrows the employee list to that branch's people —
    # both in the dropdown (handled in build_filters) and in the table below.
    people = board["people"]
    if f.branch_id:
        people = [r for r in people if r["user"].branch_id == f.branch_id]

    scores = [r["card"].score for r in people]
    avg = round(sum(scores) / len(scores), 1) if scores else 0.0

    return templates.TemplateResponse(request, "report_score.html", {
        "user": user, "f": f, "board": board, "people": people,
        "units": board["branches"], "view": view if view == "branch" else "person",
        "avg": avg, "headcount": len(people),
        "best": people[0] if people else None,
        "worst": people[-1] if people else None,
    })
