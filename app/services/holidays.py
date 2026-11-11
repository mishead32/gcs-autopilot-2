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

from sqlalchemy import event, select, or_
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
    """Why nobody is in that day — the holiday's name, or the weekly off.

    Answered from this session's calendar, so asking about three hundred
    days in a row — which several things here do — costs the one read
    that built it and nothing after.
    """
    return calendar(db, org_id).closed(day, branch_id)


def is_closed(db: Session, org_id: int, day: date,
              branch_id: int | None = None) -> bool:
    return closed_reason(db, org_id, day, branch_id) is not None


# Kept under the old name because it reads better at the call sites that only
# care whether work should be created at all.
def is_holiday(db: Session, org_id: int, day: date,
               branch_id: int | None = None) -> bool:
    return is_closed(db, org_id, day, branch_id)


# ------------------------------------------------------- the whole year ---
# Every function above asks the database one question about one day. That
# is right for a page that needs one answer, and ruinous for anything that
# needs thousands.
#
# Counting how late one task is walks a day at a time, asking "was the
# office shut?" on each — and the morning escalation asks that for every
# overdue task in the system. Two thousand tasks, each up to a hundred and
# twenty days late, is a quarter of a million SELECTs for one email. On a
# laptop with the database in the same process that is slow; on a free host
# with the database across the network, each one costing ten milliseconds,
# it is not slow, it is broken — the page never finishes and the eleven
# o'clock run never ends.
#
# So: read the calendar ONCE, into memory, and answer from there. One query
# for the holidays, one for the companies, and every question after that
# costs nothing. The answers are identical to the one-at-a-time functions —
# `closed` below is `closed_reason` with the lookups done in advance — and
# a Calendar is built per request and thrown away, so it can never serve a
# holiday somebody added five minutes ago.


class Calendar:
    """Which days this organisation is shut, held in memory.

    Build one at the top of anything that asks about more than a handful
    of days, and pass it down. Everything it answers, it answers without
    touching the database.
    """

    __slots__ = ("org_id", "_named", "_off", "_default_off")

    def __init__(self, db: Session, org_id: int):
        self.org_id = org_id
        # {date: {branch_id or None: name}} — one row per holiday, kept
        # with its branch so a Jharkhand festival is not taken off
        # Chandigarh's count.
        self._named: dict[date, dict[int | None, str]] = {}
        for h in db.scalars(select(Holiday).where(Holiday.org_id == org_id)):
            self._named.setdefault(h.day, {})[h.branch_id] = h.name
        self._off: dict[int, int | None] = {
            b.id: b.weekly_off
            for b in db.scalars(select(Branch).where(Branch.org_id == org_id))
        }
        # Work with no company is treated as head office: closed Sunday.
        # The safer of the two mistakes, because it errs towards doing the
        # work early rather than expecting somebody in on their day off.
        self._default_off = 6

    def off_day(self, branch_id: int | None) -> int | None:
        if branch_id is None:
            return self._default_off
        return self._off.get(branch_id, self._default_off)

    def holiday(self, day: date, branch_id: int | None = None) -> str | None:
        """The holiday's name if that day is one here, otherwise None."""
        found = self._named.get(day)
        if not found:
            return None
        # A holiday with no company covers the whole group; one with a
        # company covers only that company.
        return found.get(None) or (found.get(branch_id)
                                   if branch_id is not None else None)

    def closed(self, day: date, branch_id: int | None = None) -> str | None:
        """Why nobody is in that day — the holiday's name, or the weekly off."""
        name = self.holiday(day, branch_id)
        if name:
            return name
        off = self.off_day(branch_id)
        if off is not None and day.weekday() == off:
            return f"a {DAY_NAMES[off]}"
        return None

    def is_shut(self, day: date, branch_id: int | None = None) -> bool:
        return self.closed(day, branch_id) is not None

    def open_days(self, after: date, upto: date,
                  branch_id: int | None = None, cap: int = 400) -> int:
        """Working days in (after, upto] — the counting loop, with no SQL."""
        if upto <= after:
            return 0
        start = max(after + timedelta(days=1), upto - timedelta(days=cap))
        n, d = 0, start
        while d <= upto:
            if not self.is_shut(d, branch_id):
                n += 1
            d += timedelta(days=1)
        return n


#: Where a session keeps the calendar it has already read. A Session is
#: per request here (and per pass in the background loop), so this lives
#: exactly as long as one unit of work and can never serve yesterday's
#: calendar to tomorrow's page.
_CACHE_KEY = "gcs_calendar"


def calendar(db: Session, org_id: int) -> Calendar:
    """This session's calendar, read once and then remembered.

    Kept on the Session rather than anywhere longer-lived. A request is a
    session, so the calendar is read at most once per page however many
    times it is asked for — and it is thrown away with the request, so a
    holiday somebody adds is in effect on the very next click.

    `forget` below drops it the moment a holiday or a company is written
    in this same session, so code that adds a holiday and then asks about
    it gets the right answer rather than the one from eight lines ago.
    """
    got = db.info.get(_CACHE_KEY)
    if got is not None and got.org_id == org_id:
        return got
    made = Calendar(db, org_id)
    db.info[_CACHE_KEY] = made
    return made


def forget(db: Session) -> None:
    """Drop this session's cached calendar — the dates have changed."""
    db.info.pop(_CACHE_KEY, None)


@event.listens_for(Session, "after_flush")
def _calendar_changed(session, _ctx):
    """Throw the cache away if this flush touched the calendar.

    Without this, a route that adds a holiday and then works out a
    deadline in the same breath would use the calendar as it was before
    the holiday existed. Hooked rather than left to each caller to
    remember, because the one that forgets is the one that is wrong.
    """
    for row in (list(session.new) + list(session.dirty)
                + list(session.deleted)):
        if isinstance(row, (Holiday, Branch)):
            session.info.pop(_CACHE_KEY, None)
            return


@event.listens_for(Session, "do_orm_execute")
def _calendar_maybe_changed(state):
    """And again for a DELETE or UPDATE written as a statement.

    A bulk statement never loads the rows it changes, so the session's
    new/dirty/deleted sets stay empty and the hook above sees nothing —
    which is how a holiday deleted with `delete(Holiday)` went on being
    observed for the rest of the request. (The older after_bulk_delete
    hook does not fire for `session.execute(delete(...))` at all, which
    is how this was missed the first time.)

    Blunt on purpose: the cache costs one query to rebuild, so any ORM
    write drops it rather than this trying to work out from a compiled
    statement which table was hit.
    """
    if state.is_delete or state.is_update:
        state.session.info.pop(_CACHE_KEY, None)


#: Never count further back than this. A gap longer than a year means a
#: flow nobody has touched since it was made, and the exact number stopped
#: being interesting several hundred days ago.
MAX_GAP = 400


def open_days_between(db: Session, org_id: int, after: date, upto: date,
                      branch_id: int | None = None,
                      cap: int = MAX_GAP) -> int:
    """How many working days fall in (after, upto] for one company.

    Working days, not calendar days, because the two answer different
    questions. "Nothing has been entered for three days" over a Friday and
    a long weekend is not three days of nobody doing their job — it is one,
    and a report that cannot tell the difference is one people stop reading
    by the second Monday.

    The holidays are read ONCE for the whole span rather than a query per
    day: this is called for every flow against every company, and a day at
    a time would be thousands of round trips to answer one email.
    """
    return Calendar(db, org_id).open_days(after, upto, branch_id, cap)


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


def push_due(db: Session, org_id: int, due_at: datetime,
             branch_id: int | None = None) -> tuple[datetime, str | None]:
    """Move a deadline off a closed day, FORWARD to the next working day.

    The opposite direction from `shift_due`, and used by FMS for two
    reasons.

    A chain cannot be pulled backwards. An FMS step is due a day after the
    one before it, so dragging a Sunday step back to Saturday puts it on
    the same day as the step it is waiting for — and then the step after
    that is a day later, which is Sunday again, so it lands on Saturday
    too. A four-step tail ends up stacked on one afternoon, every step
    "due" before the work it depends on can possibly be finished. That is
    not a tighter deadline, it is an impossible one.

    And the work genuinely has to happen. A checklist job on a closed day
    is brought forward because the day it covers will not come again; an
    FMS step is a link in a chain that has to run whatever the calendar
    does, so the honest answer is the next day somebody is in.

    Never moves a deadline that is already fine, and never further than
    MAX_LOOKBACK days — a run of closed days longer than three weeks means
    the calendar is wrong, and a deadline three weeks out is not a fix.
    """
    why = closed_reason(db, org_id, due_at.date(), branch_id)
    if not why:
        return due_at, None
    moved = next_working_day(db, org_id,
                             due_at.date() + timedelta(days=1), branch_id)
    return datetime.combine(moved, due_at.time()), why


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
