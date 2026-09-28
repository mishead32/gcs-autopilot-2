"""Days nobody is in, and what happens to work that lands on one.

Two kinds of non-working day:

  Weekly off   Sunday for the offices. Set per company, because the gym and
               the spa are busiest on a Sunday — a group-wide rule would
               quietly take every Sunday job off those two rotas.

  Holiday      a named date on the Holidays page. One with no company covers
               the whole group; one with a company covers only that company,
               so Jharkhand can keep a local festival Chandigarh does not.

And one rule for both: work planned for such a day is brought FORWARD to the
last working day before it.

That direction is the whole point. Pushing it to the next working day means
the report due on Sunday is handed in on Monday — late, by a day, every time.
Bringing it forward means it is on somebody's desk on Saturday, done before
the office closes. So:

  Checklist    a rule due on a non-working day spawns its task on the last
               working day before it, marked for the day it covers.

  FMS          a step's deadline moves back to the last working day before.

  Delegation   the same, and the task says so, rather than silently marking
               the doer late for a day nobody was in.

The one thing never done is moving a deadline into the past. If the last
working day before has already gone, the deadline stays where it was — an
impossible deadline helps nobody.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from ..models import Holiday, Branch
from .. import clock

# Never search further than this for a working day. A run of closed days
# longer than three weeks means the calendar is wrong, and looping forever
# while somebody waits for a page is worse than a slightly wrong date.
MAX_LOOKBACK = 21

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday"]


def holidays_for(db: Session, org_id: int, branch_id: int | None,
                 start: date, end: date) -> dict[date, str]:
    """Every named holiday between two dates, as {date: name}."""
    q = select(Holiday).where(
        Holiday.org_id == org_id,
        Holiday.day >= start,
        Holiday.day <= end,
        or_(Holiday.branch_id.is_(None), Holiday.branch_id == branch_id),
    )
    return {h.day: h.name for h in db.scalars(q).all()}


def holiday_name(db: Session, org_id: int, day: date,
                 branch_id: int | None = None) -> str | None:
    """The holiday's name if that day is one, otherwise None."""
    return holidays_for(db, org_id, branch_id, day, day).get(day)


def weekly_off(db: Session, branch_id: int | None) -> int | None:
    """Which weekday this company is closed, or None if it never closes.

    A task with no company at all is treated as head office, which is closed
    on Sunday — the safer of the two mistakes, because it errs towards doing
    the work early rather than expecting somebody in on their day off.
    """
    if branch_id is None:
        return 6                                   # Sunday
    branch = db.get(Branch, branch_id)
    if branch is None:
        return 6
    return branch.weekly_off


def closed_reason(db: Session, org_id: int, day: date,
                  branch_id: int | None = None) -> str | None:
    """Why nobody is in that day — the holiday's name, or the weekly off."""
    name = holiday_name(db, org_id, day, branch_id)
    if name:
        return name
    off = weekly_off(db, branch_id)
    if off is not None and day.weekday() == off:
        return f"a {DAY_NAMES[off]}"
    return None


def is_closed(db: Session, org_id: int, day: date,
              branch_id: int | None = None) -> bool:
    return closed_reason(db, org_id, day, branch_id) is not None


# Kept under the old name because it reads better at the call sites that only
# care whether work should be created at all.
def is_holiday(db: Session, org_id: int, day: date,
               branch_id: int | None = None) -> bool:
    return is_closed(db, org_id, day, branch_id)


def previous_working_day(db: Session, org_id: int, day: date,
                         branch_id: int | None = None) -> date:
    """The last day on or before `day` that somebody is actually in."""
    d = day
    for _ in range(MAX_LOOKBACK):
        if not is_closed(db, org_id, d, branch_id):
            return d
        d -= timedelta(days=1)
    return d


def next_working_day(db: Session, org_id: int, day: date,
                     branch_id: int | None = None) -> date:
    """The first day from `day` onwards that somebody is in.

    Still here for the one case that genuinely needs it: choosing when to
    START something, where going backwards would mean starting in the past.
    """
    d = day
    for _ in range(MAX_LOOKBACK):
        if not is_closed(db, org_id, d, branch_id):
            return d
        d += timedelta(days=1)
    return d


def shift_due(db: Session, org_id: int, due_at: datetime,
              branch_id: int | None = None) -> tuple[datetime, str | None]:
    """Move a deadline off a closed day, back to the working day before it.

    Returns the deadline to use and, when it moved, why — so the caller can
    tell the person what happened rather than silently changing their input.
    """
    why = closed_reason(db, org_id, due_at.date(), branch_id)
    if not why:
        return due_at, None

    moved = previous_working_day(db, org_id,
                                 due_at.date() - timedelta(days=1), branch_id)
    shifted = datetime.combine(moved, due_at.time())

    # Never invent a deadline that has already passed. Somebody setting a task
    # on next Sunday should get next Saturday; somebody setting one on
    # yesterday's Sunday gets it left alone rather than handed a deadline they
    # were already late for the moment it was created.
    if shifted <= clock.now():
        return due_at, None
    return shifted, why
