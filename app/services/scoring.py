"""Scoring engine.

Two date bases, deliberately kept separate:

  planned  — tasks whose DUE date falls in the window. "What was supposed to
             happen this week." This is the denominator for everything.
  closed   — tasks whose COMPLETION date falls in the window. "What actually
             got closed." Shown alongside so clearing old backlog still counts.

Penalties are computed SEPARATELY for each work source, so a doer can see
which kind of work is dragging them down:

  Delegation  (one-off tasks someone assigned)
  Checklist   (recurring rules)
  FMS         (steps inside a flow)

Within each source, first the raw miss rates:
  raw not done     = -(not completed / planned * 100)
  raw not on time  = -(closed late / completed * 100)

Then each is scaled by the doer's BENCHMARK for that source. A benchmark says
how much of this person's job that source is; the three must total 100. Half
the weight applies to "not done" and half to "not on time", so a source can
never cost more than its own benchmark:

  weight share = benchmark / 2
  not done     = raw not done    * weight share / 100
  not on time  = raw not on time * weight share / 100
  subtotal     = the two added together

  e.g. Delegation benchmark 60% (so 30% each side),
       100 assigned / 50 done -> raw -50 -> -15
       50 closed  / 25 late   -> raw -50 -> -15
       subtotal                              -30

If work was due in a source but NOTHING was closed, the "not on time" half
also takes its full penalty — none of it was delivered on time. Otherwise a
doer who ignored the work entirely would score better than one who did it
late, which is backwards.

Because the benchmarks total 100, the three subtotals can never sum below
-100. False marking then sits on top of that.

Then, once for the whole scorecard:
  false marking = -10 per task an auditor flagged

  total penalty = the three subtotals + false marking      e.g. -55
  net score     = 100 + total penalty, floored at 0        e.g. 45
  gap           = net score - 100                          e.g. -55
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import (Task, TaskStatus, TaskSource, User, Branch, Role,
                      PARKED_STATES)

FALSE_MARK_PENALTY = 10.0
CLOSED = (TaskStatus.COMPLETED,)

# display name -> the TaskSource it aggregates
SOURCE_LABELS = {
    TaskSource.DELEGATION: "Delegation",
    TaskSource.RECURRING: "Checklist",
    TaskSource.FLOW: "FMS",
}
SOURCE_ORDER = [TaskSource.DELEGATION, TaskSource.RECURRING, TaskSource.FLOW]


# ---------------------------------------------------------------- window ---
def resolve_window(date_from: str | None, date_to: str | None,
                   days: int | None = None) -> tuple[datetime, datetime]:
    """Accepts YYYY-MM-DD strings; falls back to the last `days` days."""
    if date_from and date_to:
        start = datetime.fromisoformat(date_from)
        end = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59)
        if end < start:
            start, end = end, start
        return start, end
    d = days or 7
    end = clock.now().replace(hour=23, minute=59, second=59)
    start = (end - timedelta(days=d - 1)).replace(hour=0, minute=0, second=0)
    return start, end


def _neg(x: float) -> float:
    """Round to 1dp and normalise -0.0 to 0.0 so the UI never shows '-0.0'."""
    return round(x, 1) + 0.0


# ------------------------------------------------------------ sub-scores ---
@dataclass
class SourceScore:
    """One work type (Delegation / Checklist / FMS) inside a scorecard."""
    key: str = ""
    label: str = ""
    benchmark: int = 0          # this doer's expected share of work, 0-100
    planned: int = 0
    completed: int = 0
    not_done: int = 0
    on_time: int = 0
    late: int = 0

    # raw miss rates, before the benchmark is applied
    raw_not_done: float = 0.0
    raw_late: float = 0.0
    # after weighting — these are the numbers shown on the dashboards
    not_done_penalty: float = 0.0
    late_penalty: float = 0.0

    def compute(self):
        if self.planned:
            self.raw_not_done = _neg(-self.not_done / self.planned * 100)

        if self.completed:
            self.raw_late = _neg(-self.late / self.completed * 100)
        elif self.planned:
            # Work was due but nothing was closed. "Late" is undefined here
            # (0 of 0), and leaving it at zero would let someone who did
            # NOTHING score better than someone who did the work late. None of
            # it was delivered on time, so it takes the full penalty.
            self.raw_late = -100.0

        share = self.benchmark / 2 / 100      # half the benchmark to each side
        self.not_done_penalty = _neg(self.raw_not_done * share)
        self.late_penalty = _neg(self.raw_late * share)
        return self

    @property
    def subtotal(self) -> float:
        return _neg(self.not_done_penalty + self.late_penalty)

    @property
    def has_work(self) -> bool:
        return self.planned > 0 or self.completed > 0


# ---------------------------------------------------------------- card -----
@dataclass
class Card:
    """A scorecard for a person, a branch, or the whole company."""
    planned: int = 0
    completed: int = 0
    not_done: int = 0
    on_time: int = 0
    late: int = 0
    false_marks: int = 0
    closed_in_window: int = 0
    still_open: int = 0
    overdue_now: int = 0

    not_done_penalty: float = 0.0
    late_penalty: float = 0.0
    false_penalty: float = 0.0
    score: float = 0.0
    gap: float = 0.0

    quality: float | None = None
    sources: dict = field(default_factory=dict)   # TaskSource.value -> SourceScore
    benchmarks: dict = field(default_factory=dict)
    # aggregate cards (a branch, the whole company) average their members
    # rather than re-deriving from raw counts, because each doer is weighted
    # by their own benchmark
    total_override: float | None = None

    def compute(self):
        if self.planned:
            self.not_done_penalty = _neg(-self.not_done / self.planned * 100)
        if self.completed:
            self.late_penalty = _neg(-self.late / self.completed * 100)
        self.false_penalty = _neg(-self.false_marks * FALSE_MARK_PENALTY)

        self.score = round(max(0.0, 100 + self.total_penalty), 1)
        self.gap = _neg(self.score - 100)
        return self

    # --- the breakdown the dashboards show -------------------------------
    @property
    def source_list(self) -> list[SourceScore]:
        return [self.sources[s.value] for s in SOURCE_ORDER if s.value in self.sources]

    @property
    def source_total(self) -> float:
        return _neg(sum(s.subtotal for s in self.source_list))

    @property
    def total_penalty(self) -> float:
        """Delegation + Checklist + FMS + false marking. The headline figure."""
        if self.total_override is not None:
            return _neg(self.total_override)
        return _neg(self.source_total + self.false_penalty)

    @property
    def completion_rate(self) -> float:
        return round(self.completed / self.planned * 100, 1) if self.planned else 0.0

    @property
    def on_time_rate(self) -> float:
        return round(self.on_time / self.completed * 100, 1) if self.completed else 0.0

    @property
    def scored_by_system(self) -> int:
        """How many of the 100 points this scorecard's benchmarks cover.

        The benchmarks no longer have to total 100 — the remainder is judged
        by hand. So a 40/10/10 person can never be scored below 40 by the
        software, and saying so on the page stops 40 being read as a result
        rather than as "the software had 60 points to give and gave none".
        """
        return sum(self.benchmarks.values()) if self.benchmarks else 100

    @property
    def scored_by_hand(self) -> int:
        return max(0, 100 - self.scored_by_system)

    @property
    def partly_manual(self) -> bool:
        return self.scored_by_system < 100

    @property
    def band(self) -> str:
        """Good / warn / bad, measured against what the software actually scores.

        A fixed 85 would call every 40/10/10 person "bad" the moment they
        lost a few points, because their ceiling is 60 to begin with. The
        thresholds scale with the share the software is responsible for.
        """
        top = self.scored_by_system or 100
        floor = 100 - top
        span = top / 100.0
        return ("good" if self.score >= floor + 85 * span
                else "warn" if self.score >= floor + 60 * span
                else "bad")


DEFAULT_BENCHMARKS = {
    TaskSource.DELEGATION.value: 60,
    TaskSource.RECURRING.value: 20,
    TaskSource.FLOW.value: 20,
}


def _w(tasks) -> int:
    """Total score weight of a set of tasks (high 5, medium 2, low 1)."""
    return sum(t.weight for t in tasks)


def _build(planned: list[Task], closed_in_window: list[Task],
           benchmarks: dict | None = None) -> Card:
    bm = benchmarks or DEFAULT_BENCHMARKS
    c = Card(benchmarks=dict(bm))
    # Everything below counts WEIGHT, not rows. A high-priority task is worth
    # five, medium two, low one, so missing one high job costs what missing
    # five ordinary ones would. The task lists still show one row per task —
    # only the arithmetic changes.
    c.planned = _w(planned)
    c.closed_in_window = _w(closed_in_window)

    done = [t for t in planned if t.status in CLOSED]
    c.completed = _w(done)
    c.not_done = c.planned - c.completed
    c.on_time = _w([t for t in done if t.was_on_time])
    c.late = c.completed - c.on_time
    c.still_open = sum(1 for t in planned if t.status not in
                       (TaskStatus.COMPLETED,) + PARKED_STATES)
    c.overdue_now = sum(1 for t in planned if t.is_overdue)
    c.false_marks = sum(1 for t in set(planned) | set(closed_in_window) if t.false_marked)

    # per-source breakdown — always present, even at zero, so the layout is stable
    for src in SOURCE_ORDER:
        s = SourceScore(key=src.value, label=SOURCE_LABELS[src],
                        benchmark=bm.get(src.value, 0))
        rows = [t for t in planned if t.source == src]
        s.planned = _w(rows)
        sdone = [t for t in rows if t.status in CLOSED]
        s.completed = _w(sdone)
        s.not_done = s.planned - s.completed
        s.on_time = _w([t for t in sdone if t.was_on_time])
        s.late = s.completed - s.on_time
        c.sources[src.value] = s.compute()

    scores = [t.audit_score for t in done if t.audit_score is not None]
    c.quality = round(sum(scores) / len(scores), 1) if scores else None
    return c.compute()


def _aggregate(planned: list[Task], closed: list[Task],
               members: list[Card]) -> Card:
    """A branch / company card.

    Counts come from the raw tasks, but the SCORE is the mean of the member
    doers' scores. Deriving it from raw counts would be wrong, because every
    doer is weighted by their own benchmark — a company score has to respect
    those, not average the tasks.
    """
    c = _build(planned, closed)
    if not members:
        return c

    n = len(members)
    c.total_override = sum(m.total_penalty for m in members) / n
    for src in SOURCE_ORDER:
        k = src.value
        if k in c.sources:
            sub = sum(m.sources[k].subtotal for m in members if k in m.sources) / n
            c.sources[k].not_done_penalty = _neg(
                sum(m.sources[k].not_done_penalty for m in members if k in m.sources) / n)
            c.sources[k].late_penalty = _neg(
                sum(m.sources[k].late_penalty for m in members if k in m.sources) / n)
    c.false_penalty = _neg(sum(m.false_penalty for m in members) / n)
    return c.compute()


# ---------------------------------------------------------------- board ----
def scoreboard(db: Session, org_id: int, start: datetime, end: datetime,
               branch_id: int | None = None, doer_id: int | None = None) -> dict:
    """Everything the Performance page needs, in one pass over the tasks."""
    q = select(Task).where(Task.org_id == org_id)
    if branch_id:
        q = q.where(Task.branch_id == branch_id)
    if doer_id:
        q = q.where(Task.doer_id == doer_id)
    all_tasks = list(db.scalars(q).all())

    # A cancelled task was un-planned — a declined help request, a job that
    # stopped mattering. Counting it as 'not done' would punish the person
    # who was right to stop, so it leaves the denominator entirely.
    planned = [t for t in all_tasks
               if start <= t.due_at <= end and t.status not in PARKED_STATES]
    closed = [t for t in all_tasks
              if t.closed_at is not None and start <= t.closed_at <= end]

    users = {u.id: u for u in db.scalars(
        select(User).where(User.org_id == org_id)).all()}
    branches = {b.id: b for b in db.scalars(
        select(Branch).where(Branch.org_id == org_id)).all()}

    by_user_p, by_user_c, by_br_p, by_br_c = {}, {}, {}, {}
    for t in planned:
        by_user_p.setdefault(t.doer_id, []).append(t)
        by_br_p.setdefault(t.branch_id, []).append(t)
    for t in closed:
        by_user_c.setdefault(t.doer_id, []).append(t)
        by_br_c.setdefault(t.branch_id, []).append(t)

    people = []
    for uid in set(by_user_p) | set(by_user_c):
        u = users.get(uid)
        if not u:
            continue
        people.append({"user": u,
                       "card": _build(by_user_p.get(uid, []), by_user_c.get(uid, []),
                                      u.benchmarks)})
    people.sort(key=lambda r: (-r["card"].score, r["user"].name))

    by_person = {r["user"].id: r["card"] for r in people}

    units = []
    for bid in set(by_br_p) | set(by_br_c):
        members = [c for uid, c in by_person.items()
                   if users.get(uid) and users[uid].branch_id == bid]
        units.append({"branch": branches.get(bid),
                      "card": _aggregate(by_br_p.get(bid, []), by_br_c.get(bid, []),
                                         members)})
    units.sort(key=lambda r: -r["card"].score)

    return {
        "overall": _aggregate(planned, closed, list(by_person.values())),
        "people": people,
        "branches": units,
        "start": start,
        "end": end,
        "branch_id": branch_id,
        "doer_id": doer_id,
    }


def user_scorecard(db: Session, user: User, days: int = 30,
                   start: datetime | None = None,
                   end: datetime | None = None) -> Card:
    if start is None or end is None:
        start, end = resolve_window(None, None, days)
    tasks = list(db.scalars(select(Task).where(Task.doer_id == user.id)).all())
    planned = [t for t in tasks
               if start <= t.due_at <= end and t.status not in PARKED_STATES]
    closed = [t for t in tasks if t.closed_at and start <= t.closed_at <= end]
    return _build(planned, closed, user.benchmarks)


def visible_branches(db: Session, user: User) -> list[Branch]:
    from ..models import Right
    q = select(Branch).where(Branch.org_id == user.org_id).order_by(Branch.name)
    branches = list(db.scalars(q).all())
    if (user.has(Right.VIEW_ALL_BRANCHES) or user.has(Right.VIEW_ALL_REPORTS)
            or user.role in (Role.OWNER, Role.ADMIN)):
        return branches
    return [b for b in branches if b.id == user.branch_id]


def selectable_doers(db: Session, user: User, branch_id: int | None = None) -> list[User]:
    """Doers the viewer may filter by — narrowed to a company when one is picked."""
    allowed = {b.id for b in visible_branches(db, user)}
    q = select(User).where(User.org_id == user.org_id, User.active.is_(True))
    people = list(db.scalars(q.order_by(User.name)).all())
    out = []
    for p in people:
        if p.branch_id is not None and p.branch_id not in allowed:
            continue
        if branch_id and p.branch_id != branch_id:
            continue
        out.append(p)
    return out
