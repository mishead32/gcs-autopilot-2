"""The FMS board: one row per run, the steps across the page.

Every other FMS page answers a question about ONE thing — one run, one
step, one start form. The question nobody could answer was the one asked
across the whole flow: *where is the time going*. To see that, somebody
had to open twenty runs one after another, write the dates on paper, and
subtract them by hand. That is what this module exists to stop.

A row is a run. A step is three columns — **planned**, **actual**, and the
**delay** between them — laid left to right in step order, so a column of
red reads as "step 3 is where everything stalls" without anybody adding
anything up. One glass.

Two honest simplifications, said out loud because a reader of the board
will otherwise assume them wrongly:

* **A step with several people on it is one cell.** It is planned when the
  earliest of them was due, and finished when the last of them finished —
  the step is not done until all of it is done, which is the same test the
  engine uses to move the run on.
* **A step that ran twice reports the LAST pass.** A rejected bill sent
  back to step 1 really was done twice, and the delay that matters is the
  one the run finally carried. The cell is marked `repeats` so the board
  can say "×2" rather than quietly showing one pass of two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import (Flow, FlowInstance, FlowStep, PARKED_STATES, Task,
                      TaskStatus)

# A step is finished when nothing is still with a doer. Submitted-but-not-
# yet-audited counts as finished BY THE DOER, which is the moment the board
# is about: the delay being measured is the doer's, not the auditor's.
DONE_STATES = (TaskStatus.SUBMITTED, TaskStatus.COMPLETED)

# Inside this much of the plan, a step is "on time" rather than early or
# late. Without it almost every cell reads "11 minutes early", which is
# noise dressed up as information.
TOLERANCE = timedelta(minutes=30)


@dataclass
class Cell:
    """One step of one run: planned, actual, and what fell between them."""

    pos: int
    planned: datetime | None = None
    actual: datetime | None = None
    # done | late | running | overdue | waiting | held | stopped | none
    state: str = "none"
    # Signed minutes, actual minus planned. Positive is late. None when
    # there is nothing yet to compare.
    minutes: int | None = None
    # How the delay reads on a page: "", "on time", "2 days late",
    # "4 hours early", "overdue by 3 days".
    delay: str = ""
    # How many passes this step has had on this run. 1 is the normal case.
    repeats: int = 1
    # Everyone the step went to on its last pass, for the hover text.
    doers: list[str] = field(default_factory=list)
    # One task to link to, so a cell is a door and not just a number.
    task_id: int | None = None

    @property
    def late(self) -> bool:
        return self.state in ("late", "overdue")


def _words(delta: timedelta) -> str:
    """"2 days", "4 hours", "35 minutes" — whichever reads best.

    The same ladder the run page uses, so a gap does not read as hours on
    one screen and days on another.
    """
    mins = int(abs(delta).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)} minute" + ("" if mins == 1 else "s")
    if mins < 1440:
        h = mins // 60
        return f"{h} hour" + ("" if h == 1 else "s")
    d = mins // 1440
    return f"{d} day" + ("" if d == 1 else "s")


#: Tasks raised this close together are the same pass at a step. Several
#: people on one step are created in a single transaction — milliseconds
#: apart — while a rework pass comes round minutes or weeks later.
SAME_PASS = timedelta(minutes=5)


def _passes(tasks: list[Task]) -> list[list[Task]]:
    """Every pass this step has had, oldest first, each a list of tasks.

    One pass is one group of tasks raised together. Splitting on a clock gap
    rather than on the planned date because two passes CAN share a planned
    date — a step anchored to a date on the start form is planned from that
    date however many times it runs — and that would have merged a rework
    loop into the pass it was sent back from.
    """
    out: list[list[Task]] = []
    for t in sorted(tasks, key=lambda x: (x.created_at, x.id)):
        if out and t.created_at - out[-1][-1].created_at <= SAME_PASS:
            out[-1].append(t)
        else:
            out.append([t])
    return out


def cell(step: FlowStep, tasks: list[Task], inst: FlowInstance,
         now: datetime | None = None) -> Cell:
    """Read one step of one run into a cell.

    `tasks` is every task this run ever raised against this step, in any
    state. An empty list is a step the run has not reached — which on a
    finished or stopped run means it never will.
    """
    now = now or clock.now()
    out = Cell(pos=step.position)
    if not tasks:
        out.state = "none"
        return out

    runs_at_step = _passes(tasks)
    live = runs_at_step[-1]
    out.repeats = len(runs_at_step)
    out.task_id = live[0].id
    out.doers = [t.doer.name for t in live if t.doer]
    # Planned: the earliest of them. Several people on a step are given the
    # same date, so this is that date; if ever they differ, the step was
    # planned from the moment the first of them was due.
    out.planned = min(t.due_at for t in live)

    parked = [t for t in live if t.status in PARKED_STATES]
    open_now = [t for t in live
                if t.status not in PARKED_STATES and t.status not in DONE_STATES]

    if open_now:
        if inst.on_hold or all(t.status == TaskStatus.ON_HOLD for t in live):
            out.state = "held"
        elif out.planned and out.planned < now:
            out.state = "overdue"
            out.minutes = int((now - out.planned).total_seconds() // 60)
            out.delay = "overdue by " + _words(now - out.planned)
        elif step.position == inst.current_position:
            out.state = "running"
        else:
            out.state = "waiting"
        return out

    if parked and not any(t.status in DONE_STATES for t in live):
        out.state = "stopped" if inst.cancelled_at else "held"
        return out

    finished = [t.submitted_at or t.closed_at for t in live
                if t.status in DONE_STATES and (t.submitted_at or t.closed_at)]
    if not finished:
        # Marked done with no timestamp: an older row, or data brought in
        # from outside. Say so rather than inventing a date.
        out.state = "done"
        out.delay = "no time recorded"
        return out
    out.actual = max(finished)
    gap = out.actual - out.planned
    out.minutes = int(gap.total_seconds() // 60)
    if abs(gap) <= TOLERANCE:
        out.state, out.delay = "done", "on time"
    elif gap > timedelta(0):
        out.state, out.delay = "late", _words(gap) + " late"
    else:
        out.state, out.delay = "done", _words(gap) + " early"
    return out


def grid(db: Session, flow: Flow, runs: list[FlowInstance],
         now: datetime | None = None) -> dict[int, list[Cell]]:
    """A cell per step for every run, aligned to `positions(flow)`.

    One query for every task of every run on the page, grouped here, rather
    than a query per run per step — a board of fifty runs across eight steps
    is four hundred cells, and four hundred queries is a page nobody waits
    for.
    """
    now = now or clock.now()
    cols = positions(flow)
    if not runs:
        return {}
    ids = [i.id for i in runs]
    rows = db.scalars(
        select(Task).where(Task.flow_instance_id.in_(ids))
        .order_by(Task.created_at, Task.id)
    ).all()
    by_run: dict[int, dict[int, list[Task]]] = {i: {} for i in ids}
    for t in rows:
        if t.flow_step_id is None or t.flow_instance_id not in by_run:
            continue
        pos = t.flow_step.position
        by_run[t.flow_instance_id].setdefault(pos, []).append(t)
    return {
        inst.id: [cell(step, by_run[inst.id].get(step.position, []), inst, now)
                  for step in cols]
        for inst in runs
    }


def positions(flow: Flow) -> list[FlowStep]:
    """The flow's steps, in the order the board lays them out."""
    return sorted(flow.steps, key=lambda s: s.position)


def worst_step(flow: Flow, board: dict[int, list[Cell]]) -> tuple[FlowStep, int] | None:
    """Which step is late most often, and how often. The point of the board.

    Counted over the runs on the page, not over the flow's whole history:
    the board is filtered, and a summary that ignored the filter would
    contradict the table underneath it.
    """
    cols = positions(flow)
    if not cols or not board:
        return None
    tally = [0] * len(cols)
    for cells in board.values():
        for n, c in enumerate(cells):
            if c.late:
                tally[n] += 1
    best = max(range(len(cols)), key=lambda n: tally[n])
    return (cols[best], tally[best]) if tally[best] else None


def totals(board: dict[int, list[Cell]]) -> dict[str, int]:
    """How many cells are done, late, overdue and still to come."""
    out = {"done": 0, "late": 0, "overdue": 0, "waiting": 0, "none": 0}
    for cells in board.values():
        for c in cells:
            if c.state in ("done",):
                out["done"] += 1
            elif c.state == "late":
                out["late"] += 1
            elif c.state == "overdue":
                out["overdue"] += 1
            elif c.state in ("running", "waiting", "held", "stopped"):
                out["waiting"] += 1
            else:
                out["none"] += 1
    return out


def average_delay(board: dict[int, list[Cell]], step_index: int) -> str:
    """How late one step runs on average, in words. "" when nothing to say."""
    mins = [cells[step_index].minutes for cells in board.values()
            if len(cells) > step_index
            and cells[step_index].state in ("done", "late")
            and cells[step_index].minutes is not None]
    if not mins:
        return ""
    avg = sum(mins) / len(mins)
    gap = timedelta(minutes=abs(avg))
    if gap <= TOLERANCE:
        return "on time"
    return _words(gap) + (" late" if avg > 0 else " early")
