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

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from .. import clock
from . import chasing
from ..models import (AuditState, Branch, Followup, PARKED_STATES, Task,
                      TaskSource, TaskStatus)

# The two kinds of work this board is about. FMS is deliberately left out —
# it was not asked for, and a flow's steps answer a different question.
# The three kinds of work, in the order they appear everywhere else.
#
# FMS was left out of this board at first, on the grounds that a flow's
# steps answer a different question. They do not: an FMS step is work
# somebody owes on a date, exactly like a delegation task, and leaving
# it out meant the board that is supposed to show the group's position
# was silent about most of the group's work.
SOURCES = [(TaskSource.DELEGATION, "Delegation"),
           (TaskSource.RECURRING, "Checklist"),
           (TaskSource.FLOW, "FMS")]

# What each one is called in a URL, for the links under every figure.
QUERY_NAME = {TaskSource.DELEGATION.value: "delegation",
              TaskSource.RECURRING.value: "checklist",
              TaskSource.FLOW.value: "fms"}

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


def rows_for(db: Session, org_id: int, start: datetime,
             end: datetime) -> list[Task]:
    """The only tasks the board can possibly need, read once.

    Two things were wrong with reading them inside build().

    It had no date bound at all: every delegation and checklist task the
    company had EVER created came back, so the board got slower every
    month and would have gone on doing that for ever. A board needs
    exactly two sets — what is open right now, whatever its date, and
    what falls in the period being looked at. Everything else is history
    it does not use.

    And build() was called once for the group and once more for every
    company, so the whole lot was read eight or nine times over to draw
    one page. Read here instead, once, and sliced per company in memory.

    The rule comes with the task: the chasing test asks a checklist task
    which rule it came from, and left lazy that is one query per task.
    """
    return list(db.scalars(
        select(Task)
        .where(Task.org_id == org_id,
               Task.source.in_([s for s, _ in SOURCES]),
               or_(Task.due_at.between(start, end),
                   Task.status.in_(OPEN_STATES)))
        .options(joinedload(Task.rule))).unique().all())


def chased_on(db: Session, org_id: int, day: date) -> set:
    """Which tasks were chased on this day — one query, not one per task."""
    return {r for r in db.scalars(
        select(Followup.task_id).where(Followup.org_id == org_id,
                                       Followup.day == day)).all()}


def build(db: Session, org_id: int, start: datetime, end: datetime,
          branch_id: int | None = None, day: date | None = None,
          rows: list[Task] | None = None, chased: set | None = None) -> Board:
    """Every number on the board, from one pass over the tasks.

    `rows` and `chased` let a caller drawing several boards read the data
    once and hand the same lists to each — see by_branch. Left out, this
    reads them itself, so a single board is still one call.
    """
    day = day or clock.today()
    now = clock.now()
    today_start = datetime.combine(day, datetime.min.time())
    today_end = datetime.combine(day, datetime.max.time())

    tasks = rows_for(db, org_id, start, end) if rows is None else rows
    if branch_id:
        tasks = [t for t in tasks if t.branch_id == branch_id]
    if chased is None:
        chased = chased_on(db, org_id, day)

    board = Board().blank()
    for t in tasks:
        key = t.source.value
        if t.status in PARKED_STATES:
            continue                      # stopped work is nobody's failure

        # --- live position: what is owed right now ----------------------
        if _open(t):
            pos = board.position[key]
            # is_overdue, not a date comparison of our own.
            #
            # Two differences, and each one produced a figure somebody
            # could not reconcile with the list it opened. An FMS step
            # whose turn has not come is never late however old its plan
            # is — nobody has been asked for it. And a task due at ten
            # this morning is late at eleven: counting it as "due today"
            # until midnight made the board say 0 overdue while the rows
            # on the screen were already red.
            #
            # "Upcoming" is the remainder, so the three always add up to
            # what is open and no figure can fall between them.
            if t.is_overdue:
                pos.overdue += 1
            elif t.due_at.date() == day:
                pos.today += 1
            else:
                pos.upcoming += 1
            # --- chasing: of what is open today, what was chased today --
            # Same rule as the follow-up desk, read from the same place, or
            # the board and the desk would quietly report two different
            # numbers for the same question. A daily checklist job is only
            # chased when it is high priority, and the rest leave the count
            # altogether rather than sitting in it as permanent pending.
            if t.due_at <= today_end and chasing.needs_chasing(t):
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
              day: date | None = None, rows: list[Task] | None = None,
              chased: set | None = None
              ) -> list[tuple[Branch | None, Board]]:
    """The same board, one per company, worst first.

    Ordered by overdue work: the point of the table is to show which unit
    needs a telephone call, and alphabetical order buries that.
    """
    branches = {b.id: b for b in db.scalars(
        select(Branch).where(Branch.org_id == org_id)).all()}
    # Read once for every company rather than once per company. With
    # eight companies this was nine passes over the same table.
    if rows is None:
        rows = rows_for(db, org_id, start, end)
    if chased is None:
        chased = chased_on(db, org_id, day or clock.today())
    out = []
    for bid, branch in branches.items():
        board = build(db, org_id, start, end, branch_id=bid, day=day,
                      rows=rows, chased=chased)
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
                f"{on:%d/%m/%Y}")
    if period == "this_week":
        monday = on - timedelta(days=on.weekday())
        saturday = monday + timedelta(days=5)
        return (datetime.combine(monday, datetime.min.time()),
                datetime.combine(saturday, datetime.max.time()),
                f"{monday:%d/%m/%Y} – {saturday:%d/%m/%Y}")
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
            return a, b, f"{a:%d/%m/%Y} – {b:%d/%m/%Y}"
        except ValueError:
            pass
    first = on.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    last = nxt - timedelta(days=1)
    return (datetime.combine(first, datetime.min.time()),
            datetime.combine(last, datetime.max.time()),
            f"{first:%B %Y}")


# ======================================================= stale work ========
# Who is sitting on work that has been owed for a month or more.
#
# The board above answers "where does the group stand"; this answers the
# question CMD asks next, which is "and who". Deliberately NOT limited to
# the two sources above: an FMS step owed since September is owed by a
# person exactly as a delegation task is, and leaving flows out would put
# the worst cases on the one screen that does not show them.
STALE_DAYS = 30

# Enough to see the shape of somebody's backlog without turning the panel
# into a report; the link beside each person opens their full list.
STALE_SHOWN = 8

# How many people get their work listed out. Everybody past this is still
# NAMED — the question was "who", and a panel that hides people answers a
# different one — but without their tasks underneath, so a bad month does
# not produce a panel nobody scrolls to the end of.
STALE_PEOPLE = 12

LABELS = {TaskSource.DELEGATION: "Delegation",
          TaskSource.RECURRING: "Checklist",
          TaskSource.FLOW: "FMS"}


def _days_owed(t: Task, day: date) -> int:
    """Whole days since the planned date passed."""
    return max(0, (day - t.due_at.date()).days)


@dataclass
class StaleRow:
    """One person, and the work they have been holding too long."""
    user: object = None
    tasks: list = field(default_factory=list)
    day: date = None

    @property
    def count(self) -> int:
        return len(self.tasks)

    @property
    def oldest(self) -> int:
        """Days owed on the one they have held longest."""
        return max((_days_owed(t, self.day) for t in self.tasks), default=0)

    def owed(self, t: Task) -> int:
        return _days_owed(t, self.day)

    @property
    def shown(self) -> list:
        return self.tasks[:STALE_SHOWN]

    @property
    def hidden(self) -> int:
        return max(0, self.count - STALE_SHOWN)

    @property
    def sources(self) -> str:
        """"FMS 4, Delegation 1" — what the backlog is made of."""
        tally: dict[str, int] = {}
        for t in self.tasks:
            name = LABELS.get(t.source, t.source.value)
            tally[name] = tally.get(name, 0) + 1
        return ", ".join(f"{k} {v}" for k, v in
                         sorted(tally.items(), key=lambda kv: -kv[1]))


def stale(db: Session, org_id: int, branch_id: int | None = None,
          days: int = STALE_DAYS, on: date | None = None) -> list[StaleRow]:
    """Everybody holding work whose planned date passed `days` ago or more.

    Counted on the PLANNED date, like every other lateness figure here, so
    this agrees with the overdue column instead of telling a second story.

    Three kinds of work are deliberately NOT counted against anybody, and
    they are the same three `is_overdue` leaves out — a board that names
    people has to use the same rule as the colour on their own task list,
    or somebody is told off here for work that is not red there:

      handed in     the doer has finished; what is left is the auditor's
                    queue, which the doer does not control
      parked        cancelled, or held because its FMS run is paused
      not its turn  an FMS step still waiting. Its date is a PLAN, shown
                    so people can see work coming. A slow run can leave
                    that plan a month old while the person whose step it
                    is has not been asked for anything yet.

    The person is the DOER. Work with nobody on it cannot be owed by
    anybody and is left out rather than filed under a blank name.
    """
    day = on or clock.today()
    cutoff = datetime.combine(day, datetime.min.time()) - timedelta(days=days)

    q = (select(Task)
         .where(Task.org_id == org_id,
                Task.status.in_(OPEN_STATES),
                Task.submitted_at.is_(None),
                Task.flow_waiting.is_(False),
                Task.due_at < cutoff,
                Task.doer_id.isnot(None))
         .options(joinedload(Task.doer), joinedload(Task.branch),
                  joinedload(Task.flow_instance)))
    if branch_id:
        q = q.where(Task.branch_id == branch_id)

    rows: dict[int, StaleRow] = {}
    for t in db.scalars(q).unique().all():
        if t.doer is None:
            continue
        row = rows.setdefault(t.doer_id, StaleRow(user=t.doer, day=day))
        row.tasks.append(t)

    for row in rows.values():
        row.tasks.sort(key=lambda t: t.due_at)       # longest held first

    # Worst first by how long the oldest has been owed, not by how many:
    # one task owed since August is a bigger problem than six owed since
    # last month, and whoever reads this has limited attention.
    return sorted(rows.values(), key=lambda r: (-r.oldest, -r.count,
                                                (r.user.name or "").lower()))
