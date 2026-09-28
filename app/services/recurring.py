"""Recurring task engine.

`run_spawn(db)` is safe to call as often as you like — on app start and from
the daily loop — because a rule only ever produces one task per day it covers.

Two dates matter here and they are not always the same:

  covers_day   the day the job is FOR — the Sunday the report is due.
  due_at       when it has to be in by, which is when the task appears.

They differ whenever the job falls on a day nobody is in. Work planned for a
closed day is created on the last working day BEFORE it, so the Sunday report
is on somebody's desk on Saturday rather than being handed in late on Monday.

That means a Saturday can carry two copies of a daily job — its own, and the
Sunday one brought forward. That is deliberate: the work still has to be done,
and doing it a day early is the point.
"""
from datetime import datetime, date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import RecurringRule, Recurrence, Task, TaskComment, TaskSource
from . import notify, holidays

# How far ahead to look for closed days whose work has to be pulled back.
# Ten days covers a Sunday, a long festival run, and the two together.
LOOKAHEAD_DAYS = 10


def is_due_today(rule: RecurringRule, today: date) -> bool:
    """Does this rule's schedule land on that calendar day?

    Says nothing about whether anybody is in that day — that is decided
    separately, so the two questions cannot get tangled.
    """
    if rule.frequency == Recurrence.DAILY:
        return True
    if rule.frequency == Recurrence.WEEKDAYS:
        return today.weekday() < 5
    if rule.frequency == Recurrence.WEEKLY:
        return today.weekday() == (rule.day_of if rule.day_of is not None else 0)
    if rule.frequency == Recurrence.MONTHLY:
        return today.day == _month_day(rule.day_of or 1, today)
    if rule.frequency == Recurrence.YEARLY:
        # day_of holds the month and day together as MMDD — 417 is 17 April —
        # because a yearly rule needs both and there is only one column.
        want = rule.day_of or 101
        month, dom = divmod(want, 100)
        if not 1 <= month <= 12:
            return False
        return today.month == month and today.day == _month_day(dom, today)
    return False


def _month_day(target: int, today: date) -> int:
    """The day of the month to fire on, pulled back in a short month.

    A rule set for the 31st fires on the 30th in April and the 28th in
    February, rather than not firing at all.
    """
    nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    last_day = (nxt - timedelta(days=1)).day
    return min(max(target, 1), last_day)


def _already_made(db: Session, rule: RecurringRule, covers: date) -> bool:
    """Has this rule's job for that day already been created?

    Keyed on the day the task is FOR, not the day it was made, so bringing
    Sunday's job forward to Saturday cannot collide with Saturday's own.
    """
    return db.scalar(
        select(Task.id).where(Task.rule_id == rule.id,
                              Task.covers_day == covers).limit(1)) is not None


def days_to_spawn(db: Session, rule: RecurringRule, today: date) -> list[date]:
    """Which days' work this rule owes, if it is run on `today`.

    Today's own, when today is a working day — plus any closed day coming up
    whose last working day before it is today. That second part is what pulls
    a Sunday job back onto Saturday.
    """
    if holidays.is_closed(db, rule.org_id, today, rule.branch_id):
        # Nobody is in today, so nothing is created today. Whatever was due
        # today was already created on the last working day before it.
        return []

    out = []
    if is_due_today(rule, today):
        out.append(today)

    for n in range(1, LOOKAHEAD_DAYS + 1):
        day = today + timedelta(days=n)
        if not holidays.is_closed(db, rule.org_id, day, rule.branch_id):
            break            # the run of closed days has ended; stop looking
        if not is_due_today(rule, day):
            continue
        # Only the working day immediately before the closed run creates it,
        # so a three-day festival does not have three days all making it.
        if holidays.previous_working_day(
                db, rule.org_id, day - timedelta(days=1), rule.branch_id) == today:
            out.append(day)
    return out


def run_spawn(db: Session, today: date | None = None) -> int:
    today = today or clock.today()
    created = 0
    rules = db.scalars(select(RecurringRule).where(RecurringRule.active.is_(True))).all()

    for rule in rules:
        for covers in days_to_spawn(db, rule, today):
            if _already_made(db, rule, covers):
                continue

            hh, mm = (int(x) for x in rule.due_time.split(":"))
            due_at = datetime.combine(today, datetime.min.time()).replace(
                hour=hh, minute=mm)

            task = Task(
                org_id=rule.org_id,
                branch_id=rule.branch_id,
                title=rule.title,
                details=rule.details,
                assigner_id=rule.assigner_id,
                doer_id=rule.doer_id,
                priority=rule.priority,
                source=TaskSource.RECURRING,
                due_at=due_at,
                covers_day=covers,
                rule_id=rule.id,
                requires_audit=rule.requires_audit,
                requires_attachment=rule.requires_attachment,
            )
            db.add(task)
            db.flush()

            # Say why it turned up early, on the task itself, so nobody has to
            # work out why Saturday has two of these.
            if covers != today:
                why = holidays.closed_reason(db, rule.org_id, covers,
                                             rule.branch_id) or "a closed day"
                db.add(TaskComment(
                    task_id=task.id, author_id=rule.assigner_id,
                    body=f"This is the {covers:%A %d %b} job, brought forward "
                         f"because {covers:%d %b} is {why}."))

            notify.queue_task_assigned(db, task)
            rule.last_spawned_on = today
            created += 1

    db.commit()
    return created
