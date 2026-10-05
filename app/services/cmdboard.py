"""The CMD board — the whole group's position in counts, not scores.

This answers four questions a person running the group asks every morning,
and nothing else:

  · what is late, what is due today, what is coming
  · how much work is waiting on an auditor
  · whether anybody is chasing the work that has not been done
  · how much of what WAS done arrived on time

Counts, not weights. The scoring engine counts weight — a High task is worth
five — because a score has to reflect what the work was worth. This board is
a head count: "41 overdue" means forty-one jobs, which is what somebody
asking "how bad is it" means. The two numbers will differ, on purpose, and
each page says which it is showing.

Delegation and Checklist are kept apart the whole way down, because they
fail for different reasons: delegation piles up when somebody is overloaded,
a checklist slips when a routine has quietly stopped being done.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import (AuditState, Branch, Followup, PARKED_STATES, Task,
                      TaskSource, TaskStatus)

# The two kinds of work this board is about. FMS is deliberately left out —
# it was not asked for, and a flow's steps answer a different question.
SOURCES = [(TaskSource.DELEGATION, "Delegation"),
           (TaskSource.RECURRING, "Checklist")]

OPEN_STATES = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS,
               TaskStatus.REJECTED, TaskStatus.REOPENED)


def _open(t: Task) -> bool:
    """Still owed by the person doing it.

    A task handed in and waiting on an auditor is NOT open: the doer has
    finished with it. Counting it as open here would put it in the overdue
    column and tell CMD that work was not done when it was done days ago.
    """
    return (t.status not in (TaskStatus.COMPLETED,) + PARKED_STATES
            and t.submitted_at is None)


@dataclass
class Position:
    """What is owed right now, for one source."""
    overdue: int = 0
    today: int = 0
    upcoming: int = 0

    @property
    def total(self) -> int:
        return self.overdue + self.today + self.upcoming


@dataclass
class AuditBox:
    """Audit position for one source, over the period."""
    total: int = 0            # tasks that need an audit at all
    done: int = 0
    pending: int = 0          # finished, waiting for the auditor
    waiting: int = 0          # not finished yet, so not on anybody's desk

    @property
    def done_pct(self) -> float:
        base = self.done + self.pending
        return round(self.done / base * 100, 1) if base else 0.0


@dataclass
class ChaseBox:
    """Follow-ups for one source, for one day.

    A follow-up is a tick against a task on a DAY — chasing something
    yesterday does not cover it today — so this is always a single day's
    picture, however long the period is. The day is stated on the page.
    """
    open_tasks: int = 0
    done: int = 0

    @property
    def pending(self) -> int:
        return max(0, self.open_tasks - self.done)

    @property
    def done_pct(self) -> float:
        return round(self.done / self.open_tasks * 100, 1) if self.open_tasks else 0.0


@dataclass
class TimingBox:
    """Of the work finished in the period, how much was on time."""
    finished: int = 0
    on_time: int = 0
    delayed: int = 0

    @property
    def on_time_pct(self) -> float:
        return round(self.on_time / self.finished * 100, 1) if self.finished else 0.0


@dataclass
class Board:
    position: dict = field(default_factory=dict)     # source value -> Position
    audit: dict = field(default_factory=dict)        # source value -> AuditBox
    chase: dict = field(default_factory=dict)        # source value -> ChaseBox
    timing: dict = field(default_factory=dict)       # source value -> TimingBox
    total_in_period: dict = field(default_factory=dict)   # source value -> int

    def blank(self):
        for src, _ in SOURCES:
            self.position.setdefault(src.value, Position())
            self.audit.setdefault(src.value, AuditBox())
            self.chase.setdefault(src.value, ChaseBox())
            self.timing.setdefault(src.value, TimingBox())
            self.total_in_period.setdefault(src.value, 0)
        return self


def _in_period(t: Task, start: datetime, end: datetime) -> bool:
    """Is this task part of the period?

    By PLANNED date — "the work this month was supposed to see". The same
    basis the reports use, so a figure here and a figure there agree.
    """
    return start <= t.due_at <= end


def build(db: Session, org_id: int, start: datetime, end: datetime,
          branch_id: int | None = None, day: date | None = None) -> Board:
    """Every number on the board, from one pass over the tasks."""
    day = day or clock.today()
    now = clock.now()
    today_start = datetime.combine(day, datetime.min.time())
    today_end = datetime.combine(day, datetime.max.time())

    q = select(Task).where(Task.org_id == org_id,
                           Task.source.in_([s for s, _ in SOURCES]))
    if branch_id:
        q = q.where(Task.branch_id == branch_id)
    tasks = list(db.scalars(q).all())

    # Which open tasks were chased today, in one query rather than one per
    # task — the board has to stay fast on a few thousand rows.
    chased = {r for r in db.scalars(
        select(Followup.task_id).where(Followup.org_id == org_id,
                                       Followup.day == day)).all()}

    board = Board().blank()
    for t in tasks:
        key = t.source.value
        if t.status in PARKED_STATES:
            continue                      # stopped work is nobody's failure

        # --- live position: what is owed right now ----------------------
        if _open(t):
            pos = board.position[key]
            if t.due_at < today_start:
                pos.overdue += 1
            elif t.due_at <= today_end:
                pos.today += 1
            else:
                pos.upcoming += 1
            # --- chasing: of what is open today, what was chased today --
            if t.due_at <= today_end:     # only work already owed is chased
                ch = board.chase[key]
                ch.open_tasks += 1
                if t.id in chased:
                    ch.done += 1

        if not _in_period(t, start, end):
            continue
        board.total_in_period[key] += 1

        # --- audit -------------------------------------------------------
        ab = board.audit[key]
        if t.requires_audit or t.audit_state != AuditState.NOT_REQUIRED:
            ab.total += 1
            if t.audit_state == AuditState.COMPLETED:
                ab.done += 1
            elif t.audit_state == AuditState.PENDING:
                ab.pending += 1
            else:
                ab.waiting += 1

        # --- on time vs delayed ------------------------------------------
        # Only work the doer has actually finished can be on time or late,
        # and "on time" is measured against when they handed it in — not
        # when an auditor got round to it.
        if t.submitted_at is not None:
            tb = board.timing[key]
            tb.finished += 1
            if t.submitted_at <= t.due_at:
                tb.on_time += 1
            else:
                tb.delayed += 1

    return board


def by_branch(db: Session, org_id: int, start: datetime, end: datetime,
              day: date | None = None) -> list[tuple[Branch | None, Board]]:
    """The same board, one per company, worst first.

    Ordered by overdue work: the point of the table is to show which unit
    needs a telephone call, and alphabetical order buries that.
    """
    branches = {b.id: b for b in db.scalars(
        select(Branch).where(Branch.org_id == org_id)).all()}
    out = []
    for bid, branch in branches.items():
        board = build(db, org_id, start, end, branch_id=bid, day=day)
        if any(b.total for b in board.position.values()) or \
           any(board.total_in_period.values()):
            out.append((branch, board))
    out.sort(key=lambda r: -sum(p.overdue for p in r[1].position.values()))
    return out


# ------------------------------------------------------------- periods ----
# Named windows, so CMD never has to type a date to answer the usual
# questions. "This month" is the default because a month is the unit the
# group reviews in.
PERIODS = [
    ("this_month", "This month"),
    ("today", "Today"),
    ("this_week", "This week"),
    ("last_month", "Last month"),
    ("all", "Overall"),
    ("custom", "Pick dates"),
]
PERIOD_LABELS = dict(PERIODS)


def window(period: str, date_from: str = "", date_to: str = "",
           on: date | None = None) -> tuple[datetime, datetime, str]:
    """Turn a period name into a window, and a label to print on the page."""
    on = on or clock.today()
    if period == "today":
        return (datetime.combine(on, datetime.min.time()),
                datetime.combine(on, datetime.max.time()),
                f"{on:%d %b %Y}")
    if period == "this_week":
        monday = on - timedelta(days=on.weekday())
        saturday = monday + timedelta(days=5)
        return (datetime.combine(monday, datetime.min.time()),
                datetime.combine(saturday, datetime.max.time()),
                f"{monday:%d %b} – {saturday:%d %b %Y}")
    if period == "last_month":
        first_this = on.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return (datetime.combine(first_prev, datetime.min.time()),
                datetime.combine(last_prev, datetime.max.time()),
                f"{first_prev:%B %Y}")
    if period == "all":
        return (datetime(2000, 1, 1),
                datetime.combine(on, datetime.max.time()) + timedelta(days=3650),
                "everything so far")
    if period == "custom" and date_from and date_to:
        try:
            a = datetime.fromisoformat(date_from)
            b = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59)
            if b < a:
                a, b = b, a
            return a, b, f"{a:%d %b %Y} – {b:%d %b %Y}"
        except ValueError:
            pass
    first = on.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    last = nxt - timedelta(days=1)
    return (datetime.combine(first, datetime.min.time()),
            datetime.combine(last, datetime.max.time()),
            f"{first:%B %Y}")
