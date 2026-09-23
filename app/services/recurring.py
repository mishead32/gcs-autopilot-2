"""Recurring task engine.

`run_spawn(db)` is idempotent for a given day - call it on app start and from a
scheduler (cron / APScheduler) every few minutes. It only creates a task for a
rule once per due date.
"""
from datetime import datetime, date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import RecurringRule, Recurrence, Task, TaskSource
from . import notify, holidays


def is_due_today(rule: RecurringRule, today: date) -> bool:
    if rule.frequency == Recurrence.DAILY:
        return True
    if rule.frequency == Recurrence.WEEKDAYS:
        return today.weekday() < 5
    if rule.frequency == Recurrence.WEEKLY:
        return today.weekday() == (rule.day_of if rule.day_of is not None else 0)
    if rule.frequency == Recurrence.MONTHLY:
        target = rule.day_of or 1
        # if the month is short, fire on the last day instead
        nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        last_day = (nxt - timedelta(days=1)).day
        return today.day == min(target, last_day)
    return False


def run_spawn(db: Session, today: date | None = None) -> int:
    today = today or date.today()
    created = 0
    rules = db.scalars(select(RecurringRule).where(RecurringRule.active.is_(True))).all()

    for rule in rules:
        if rule.last_spawned_on == today:
            continue
        if not is_due_today(rule, today):
            continue
        # A holiday is skipped, not postponed. Nobody owes two register checks
        # the morning after Diwali. last_spawned_on is still stamped so the
        # rule does not try again later the same day.
        if holidays.is_holiday(db, rule.org_id, today, rule.branch_id):
            rule.last_spawned_on = today
            continue

        hh, mm = (int(x) for x in rule.due_time.split(":"))
        due_at = datetime.combine(today, datetime.min.time()).replace(hour=hh, minute=mm)

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
            rule_id=rule.id,
            requires_audit=rule.requires_audit,
            requires_attachment=rule.requires_attachment,
        )
        db.add(task)
        db.flush()
        notify.queue_task_assigned(db, task)
        rule.last_spawned_on = today
        created += 1

    db.commit()
    return created
