"""Which open work the follow-up desk actually owes a chase on.

Not every open task is worth a phone call. A daily checklist job that comes
round again tomorrow does not need somebody ringing about it today unless it
genuinely matters — and a desk handed 150 of them stops ticking any of them,
which costs more than the chasing was ever worth.

So:

    Checklist, DAILY                high priority only
    Checklist, any other frequency  every priority
    FMS                             every priority
    Delegation and Help Desk        every priority, unchanged

The excluded ones leave the list ENTIRELY — they are not counted as pending,
and they are not counted in the total either. That is the whole point of the
rule: a desk that can never reach 100% because the denominator is full of
work nobody expects them to chase learns to ignore the number, and then the
number is worth nothing.

This lives on its own so the follow-up desk and the CMD board can never
drift apart. They used to be two readings of "owed" written in two places;
one rule in one file is the only way they stay the same number.
"""
from __future__ import annotations

from ..models import Priority, Recurrence, Task, TaskSource


def needs_chasing(task: Task) -> bool:
    """True when the desk is expected to follow this task up."""
    if task.source != TaskSource.RECURRING:
        # FMS, delegation and help-desk work: all of it, every priority.
        return True

    rule = task.rule
    if rule is None:
        # A checklist task whose rule has since been deleted. Keeping it
        # chaseable is the safe way round: it is real work somebody is still
        # holding, and dropping it would hide it from the desk for good.
        return True

    if rule.frequency != Recurrence.DAILY:
        return True

    return task.priority == Priority.HIGH


def why_skipped(task: Task) -> str | None:
    """A sentence for anybody wondering where a task went, or None."""
    if needs_chasing(task):
        return None
    return ("Daily checklist work is only chased when it is high priority — "
            "this one is " + task.priority.value + ".")
