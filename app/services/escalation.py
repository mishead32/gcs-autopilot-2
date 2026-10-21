"""Overdue work climbs the ladder, one rung a day, until it is closed.

This is the approved escalation process, in code. The shape of it, from the
document:

    Day 1 overdue   the HOD of that company
    Day 2 overdue   the EA to the CMD
    Day 3 overdue   the CMD

and each message carries the task's own ID — DEL-341, CL-07, FMS-12 — so the
reply names the work rather than describing it.

Four decisions are worth reading before the code, because each of them was
a choice between two defensible answers.


ONE MESSAGE A DAY PER TASK, NOT A CATCH-UP OF EVERY RUNG IT HAS PASSED

A task that is five days overdue the first morning this runs — which is
every old task, the morning it is switched on — does not generate five
emails. It generates the one for the rung it has reached. Escalation exists
to put a thing in front of the person who can move it; delivering four
days of history to the CMD at once buries today's news in it. The rungs
that were never sent are simply never sent, and the log says so by not
having a row for them.


NEVER TWICE FOR THE SAME RUNG

The promise in the approved document, and it is enforced by the database
rather than by the code remembering: unique on (task, level, repeat).
Two web processes, a restart mid-run, somebody pressing "Run the check
now" an hour after the morning run — none of them can send the HOD the
same escalation twice. The second insert loses on the constraint and is
rolled back to a skip.


WHAT COUNTS AS A DAY OVERDUE

By default, a day somebody was in. The offices keep Sunday off, the gym
and the spa do not, and that is already recorded per company. Counting
calendar days means a task due on Saturday reaches the CMD on Tuesday,
having spent one of its three days on a Sunday when nobody could have
done it, and the first person to notice that is the doer being asked to
explain it. Switchable, because it is a judgement rather than a fact.


WHICH WORK ESCALATES

By default, exactly the work the follow-up desk is expected to chase —
`chasing.needs_chasing`, the one rule in the one file. So a low-priority
daily checklist job does not reach the CMD because somebody did not tick
it on a Tuesday, and an FMS step whose turn has not come round is never
escalated at all. Those are the same two exclusions the desk already
makes, and having escalation disagree with the desk about what is owed
would mean two answers to one question.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import clock
from ..models import (
    Branch, Escalation, EscalationRule, Organization, Priority, Task,
    TaskStatus, PARKED_STATES,
)
from . import chasing, holidays, mail

# The states in which the work is still owed by the doer.
OPEN = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS,
        TaskStatus.REJECTED, TaskStatus.REOPENED)

# Never count further back than this. A task overdue by more than a quarter
# is a data problem rather than a late job, and walking a company's closed
# days one at a time for years is a slow query somebody is waiting on.
MAX_DAYS = 120

# How many reminders to send after the top rung, when the setting says to
# repeat. Thirty working days of "still open" is already past the point
# where another email is what fixes it.
MAX_REPEATS = 30


# ------------------------------------------------------------- the ladder --

def ladder(db: Session, org_id: int, branch_id: int | None) -> dict[int, EscalationRule]:
    """This company's rungs by level, falling back to the group's.

    A company with its own rung 1 and nothing else gets its own HOD and the
    group's EA and CMD, which is exactly the real arrangement: the HOD
    differs per company, the two above them do not.
    """
    rows = db.scalars(
        select(EscalationRule).where(EscalationRule.org_id == org_id)
    ).all()
    out: dict[int, EscalationRule] = {}
    for r in rows:                                 # group-wide first …
        if r.branch_id is None:
            out[r.level] = r
    for r in rows:                                 # … then overridden
        if branch_id is not None and r.branch_id == branch_id:
            out[r.level] = r
    # A rung with nobody to send to is not a rung. Left in the table on
    # purpose — somebody half-filling the form should see their row again,
    # not lose it — but it cannot be escalated to.
    return {lvl: r for lvl, r in out.items() if r.email or r.person is not None}


def top_level(rungs: dict[int, EscalationRule]) -> int:
    return max(rungs) if rungs else 0


def missing_ladders(db: Session, org_id: int) -> list[str]:
    """Companies that would escalate to nobody. Shown on the Setup page."""
    out = []
    for b in db.scalars(select(Branch).where(Branch.org_id == org_id)
                        .order_by(Branch.name)).all():
        if not ladder(db, org_id, b.id):
            out.append(b.name)
    return out


# --------------------------------------------------------- days overdue  ---

def days_overdue(db: Session, task: Task, on: date | None = None,
                 working_days: bool = True) -> int:
    """How many days late this task is, as of `on` (today by default).

    0 means not overdue yet. On working days, a day the company was shut
    does not count — so a Saturday deadline is one day overdue on Monday,
    not two.
    """
    on = on or clock.today()
    due = task.due_at.date()
    if on <= due:
        return 0
    if not working_days:
        return min((on - due).days, MAX_DAYS)

    count = 0
    day = due + timedelta(days=1)
    while day <= on and count < MAX_DAYS:
        if not holidays.is_closed(db, task.org_id, day, task.branch_id):
            count += 1
        day += timedelta(days=1)
    return count


# ------------------------------------------------------------ which work ---

def in_scope(task: Task, scope: str) -> bool:
    """Does this kind of work escalate at all?"""
    if task.status in PARKED_STATES or task.status not in OPEN:
        return False
    if task.submitted_at is not None:
        # Handed in and sitting with an auditor. The doer has done their
        # part; escalating it reports them for the audit queue's backlog.
        return False
    if task.flow_waiting:
        # A planned FMS step whose turn has not come. Nobody can be late
        # for work the run has not reached.
        return False
    if scope == "all":
        return True
    if scope == "high":
        return task.priority == Priority.HIGH
    return chasing.needs_chasing(task)


def overdue_tasks(db: Session, org_id: int, on: date | None = None,
                  working_days: bool = True, scope: str = "chased"
                  ) -> list[tuple[Task, int]]:
    """Every task that is late today, with how late, newest overdue first."""
    on = on or clock.today()
    # Due before `on` began. A task due later today is not overdue, and a
    # task due earlier today is one the doer still has the day for — the
    # first morning it can be called late is the morning after its date.
    rows = db.scalars(
        select(Task).where(
            Task.org_id == org_id,
            Task.status.in_(OPEN),
            Task.submitted_at.is_(None),
            Task.due_at < datetime.combine(on, time.min),
        )
    ).all()
    out = []
    for t in rows:
        if not in_scope(t, scope):
            continue
        n = days_overdue(db, t, on, working_days)
        if n >= 1:
            out.append((t, n))
    out.sort(key=lambda p: (-p[1], p[0].id))
    return out


# ------------------------------------------------------ what to send next --

def already(db: Session, task_id: int) -> list[Escalation]:
    return db.scalars(
        select(Escalation).where(Escalation.task_id == task_id)
        .order_by(Escalation.level, Escalation.repeat_n)
    ).all()


def next_step(sent: list[Escalation], late: int, rungs: dict[int, EscalationRule],
              after_top: str, today: date) -> tuple[int, int] | None:
    """Which (level, repeat) is owed right now, or None.

    `sent` is what has already gone out for this task.
    """
    if not rungs:
        return None
    top = top_level(rungs)
    # One a day, whatever else is true. Two runs in one morning, or a run
    # after somebody pressed the button by hand, must not stack.
    if any(e.day == today for e in sent):
        return None

    level = min(late, top)
    if level < min(rungs):
        return None
    # The nearest rung at or below the days overdue. A ladder of 1, 2, 5
    # leaves day 3 and day 4 on rung 2, which is what somebody who typed
    # 5 meant: the CMD on the fifth day.
    level = max(l for l in rungs if l <= level)

    done = {(e.level, e.repeat_n) for e in sent}
    if (level, 0) not in done:
        return level, 0

    if level < top or after_top != "repeat":
        return None
    # The top rung, already sent, and the task is still open. Another
    # reminder to the same person, numbered so the log reads honestly.
    used = [e.repeat_n for e in sent if e.level == top]
    nxt = max(used) + 1
    return (top, nxt) if nxt <= MAX_REPEATS else None


# ------------------------------------------------------------ the message --

def _lines(task: Task, late: int) -> str:
    return "\n".join([
        f"    Task ID:      {task.ref or task.id}",
        f"    Task:         {task.title}",
        f"    Doer:         {task.doer.name if task.doer else '—'}",
        f"    Assigned by:  {task.assigner.name if task.assigner else '—'}",
        f"    Company:      {task.branch.name if task.branch else 'Head office'}",
        f"    Priority:     {task.priority.value.upper()}",
        f"    Due date:     {task.due_at.strftime('%d %b %Y, %I:%M %p')}",
        f"    Days overdue: {late}",
    ])


def _default_message(level: int, top: int, below_sent: bool) -> str:
    """The sentence the approved templates use, when none was typed in.

    `below_sent` is whether any lower rung was ACTUALLY escalated to for this
    task. It is not always true of a task that has reached rung 3, and the
    difference matters: work already sixteen days overdue the first morning
    this runs — which is every old task, the morning it is switched on —
    reaches the top rung at once, having never been escalated below it. The
    approved wording says "still open after escalation to the HOD and the
    EA", and sending that to the CMD about a task neither of them was ever
    told about is a false statement in writing, over the group's name, to
    the one person least able to check it. So the sentence follows what the
    log says happened rather than what the level implies.
    """
    if level <= 1:
        return ("The following task is overdue and has not been closed. "
                "Please review it with the doer and ensure it is completed "
                "today.")
    if not below_sent:
        return ("The following task is overdue and has not been closed. It "
                "has reached this level by the number of days it is overdue "
                "rather than through the levels below. Please see that it is "
                "closed.")
    if level < top:
        return ("The task below is now overdue. It was escalated a level "
                "below yesterday and is still open. Please follow up so "
                "that it is closed at the earliest.")
    return ("The task below is still open after escalation at every level "
            "below. It is being brought to your notice for direction.")


def compose(db: Session, task: Task, late: int, level: int, repeat_n: int,
            rule: EscalationRule, rungs: dict[int, EscalationRule],
            site: str = "", sent: list[Escalation] | None = None
            ) -> tuple[str, str, list[str]]:
    """Subject, body, and the CC list. Nothing is sent from here."""
    top = top_level(rungs)
    below_sent = any(e.level < level for e in (sent or []))
    company = task.branch.name if task.branch else "Head office"
    tag = "URGENT Escalation" if rule.urgent else "Escalation"
    subject = (f"[{tag} L{level}] Task {task.ref or task.id} overdue by "
               f"{late} day(s) — {company}")
    if repeat_n:
        subject = (f"[{tag} L{level} · reminder {repeat_n}] Task "
                   f"{task.ref or task.id} still open — {company}")

    body = (rule.message or "").strip() or \
        _default_message(level, top, below_sent)
    parts = [rule.greeting + ",", "", body, "", _lines(task, late)]
    if site:
        parts += ["", f"Open the task: {site.rstrip('/')}/tasks/{task.id}"]
    parts += ["", "Regards,", "GCS Task Escalation System (auto-generated)"]

    cc: list[str] = []
    if rule.cc_doer and task.doer is not None:
        cc.append(task.doer.email or "")
    if rule.cc_assigner and task.assigner is not None:
        cc.append(task.assigner.email or "")
    if rule.cc_earlier:
        # Everybody already escalated to below this rung, so the level above
        # replying reaches the ones who were asked first.
        for lvl in sorted(l for l in rungs if l < level):
            cc.append(rungs[lvl].email)
    cc = [a for a in mail.clean(cc) if a.lower() != (rule.email or "").lower()]
    return subject, "\n".join(parts), cc


# ---------------------------------------------------------------- the run --

def raise_one(db: Session, task: Task, late: int, level: int, repeat_n: int,
              rule: EscalationRule, rungs: dict[int, EscalationRule],
              today: date, send: bool = True, site: str = "",
              sent: list[Escalation] | None = None) -> Escalation | None:
    """Write the escalation down, then try to send it.

    The row first, and committed before the email goes anywhere. If the
    process dies between the two, the worst case is one escalation recorded
    as queued that nobody received — recoverable, and visible on the log.
    The other order risks sending the CMD the same email every morning
    because nothing was ever written down.
    """
    esc = Escalation(
        org_id=task.org_id, task_id=task.id, level=level, repeat_n=repeat_n,
        day=today, days_overdue=late,
        to_name=rule.name, to_email=rule.email,
    )
    esc.subject, esc.body, cc = compose(db, task, late, level, repeat_n,
                                        rule, rungs, site, sent)
    esc.cc_emails = ",".join(cc) or None
    db.add(esc)
    try:
        db.commit()
    except IntegrityError:
        # Somebody else got there first — the same morning run in another
        # web process, or the button pressed twice. That is the constraint
        # doing its job, not an error worth reporting.
        db.rollback()
        return None

    if not send:
        return esc
    ok, why = mail.send([esc.to_email], esc.subject, esc.body, cc)
    esc.status = "sent" if ok else ("failed" if mail.enabled() else "queued")
    esc.error = None if ok else (why or None)
    esc.sent_at = clock.now() if ok else None
    db.commit()
    return esc


def run(db: Session, org: Organization, on: date | None = None,
        send: bool = True, site: str = "", force: bool = False) -> dict:
    """The morning check. Safe to call again — see the module docstring."""
    today = on or clock.today()
    out = {"day": today, "looked": 0, "raised": 0, "sent": 0, "failed": 0,
           "queued": 0, "skipped_no_ladder": 0, "ran": False, "why": ""}

    if not org.esc_on and not force:
        out["why"] = "Escalation is switched off."
        return out

    rows = overdue_tasks(db, org.id, today, org.esc_working_days, org.esc_scope)
    out["looked"] = len(rows)
    out["ran"] = True

    for task, late in rows:
        rungs = ladder(db, org.id, task.branch_id)
        if not rungs:
            out["skipped_no_ladder"] += 1
            continue
        sent = already(db, task.id)
        step = next_step(sent, late, rungs, org.esc_after_top, today)
        if step is None:
            continue
        level, repeat_n = step
        esc = raise_one(db, task, late, level, repeat_n, rungs[level], rungs,
                        today, send, site, sent)
        if esc is None:
            continue
        out["raised"] += 1
        out[{"sent": "sent", "failed": "failed"}.get(esc.status, "queued")] += 1

    org.esc_last_run = today
    db.commit()
    return out


def due_today(db: Session, org: Organization, on: date | None = None) -> bool:
    """Has this morning's check still to happen?

    The date is the trigger rather than a timer: a restart, a second web
    process or a host that sleeps all night cannot make the check run twice
    in a day, and cannot make it skip one either.
    """
    today = on or clock.today()
    if not org.esc_on:
        return False
    if org.esc_last_run == today:
        return False
    return clock.now().hour >= max(0, min(23, org.esc_hour or 0))


def run_daily(db: Session, site: str = "") -> dict | None:
    """Called by the background loop. None when there was nothing to do."""
    org = db.scalar(select(Organization))
    if org is None or not due_today(db, org):
        return None
    return run(db, org, site=site)


# ------------------------------------------------------------- the ladder --
# What a brand-new organisation starts with, so the page is not an empty
# form somebody has to guess the shape of. The names are left blank: who
# the HOD is differs per company and typing a wrong one in is worse than
# typing none.

DEFAULT_RUNGS = [
    (1, "The HOD of that company", False, None),
    (2, "The EA to the CMD", False, None),
    (3, "The CMD", True, "Respected CMD Sir"),
]


def seed(db: Session, org_id: int) -> int:
    """Lay out the three group-wide rungs, empty, if there are none at all.

    The wording is deliberately left blank rather than pre-filled with the
    approved sentences. Empty means the software writes the sentence itself,
    and its sentence is the approved one in the ordinary case and an honest
    variant in the one case the approved wording would be untrue — see
    `_default_message`. Typing the approved text into the box would freeze
    the untrue version in, which is the opposite of what approving it meant.
    """
    if db.scalar(select(EscalationRule).where(EscalationRule.org_id == org_id)):
        return 0
    for level, _who, urgent, salutation in DEFAULT_RUNGS:
        db.add(EscalationRule(
            org_id=org_id, branch_id=None, level=level, to_name=None,
            salutation=salutation, message=None, urgent=urgent,
            cc_doer=True, cc_earlier=level > 1,
        ))
    db.commit()
    return len(DEFAULT_RUNGS)
