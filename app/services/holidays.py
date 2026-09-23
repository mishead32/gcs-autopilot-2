"""Holidays — days on which no work is expected, so no work is created.

One rule, applied in three places:

  Checklist   a rule that falls on a holiday does not spawn that day. It is
              skipped, not postponed: a daily register check on Diwali is not
              owed the next morning as well.

  FMS         a step's deadline is pushed to the next working day. The work
              still has to happen, so moving the deadline is right; skipping
              the step would break the chain.

  Delegation  the same. If someone sets a deadline on a holiday the system
              moves it forward and says so, rather than silently marking the
              doer late for a day nobody was in.

A holiday with no branch covers the whole group. One with a branch covers only
that branch, so Jharkhand can keep a local festival Chandigarh does not.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from ..models import Holiday

# Never search further than this for the next working day. A run of holidays
# longer than three weeks means the calendar is wrong, and looping forever
# while a page waits is worse than a slightly wrong date.
MAX_LOOKAHEAD = 21


def holidays_for(db: Session, org_id: int, branch_id: int | None,
                 start: date, end: date) -> dict[date, str]:
    """Every holiday between two dates, as {date: name}."""
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


def is_holiday(db: Session, org_id: int, day: date,
               branch_id: int | None = None) -> bool:
    return holiday_name(db, org_id, day, branch_id) is not None


def next_working_day(db: Session, org_id: int, day: date,
                     branch_id: int | None = None) -> date:
    """The first day from `day` onwards that is not a holiday."""
    found = holidays_for(db, org_id, branch_id, day,
                         day + timedelta(days=MAX_LOOKAHEAD))
    d = day
    for _ in range(MAX_LOOKAHEAD):
        if d not in found:
            return d
        d += timedelta(days=1)
    return d


def shift_due(db: Session, org_id: int, due_at: datetime,
              branch_id: int | None = None) -> tuple[datetime, str | None]:
    """Move a deadline off a holiday, keeping the time of day.

    Returns the deadline to use and, when it moved, the name of the holiday
    that caused it — so the caller can tell the person what happened rather
    than silently changing their input.
    """
    name = holiday_name(db, org_id, due_at.date(), branch_id)
    if not name:
        return due_at, None
    moved = next_working_day(db, org_id, due_at.date() + timedelta(days=1), branch_id)
    return datetime.combine(moved, due_at.time()), name
