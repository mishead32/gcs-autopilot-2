"""Flow Management System engine.

start_flow()   -> creates a FlowInstance and spawns the task for step 1
advance_flow() -> called when a flow task closes; spawns whatever comes next

Steps are not simply run in numerical order. Each one names where the flow
goes when it closes, which is what makes a real process expressible:

    1 Verify the bill                     -> 2
    2 Submit to the manager               -> 3
    3 Verified or rejected?   (decision)  -> verified: 4   rejected: 1
    4 Send to CMD for approval            -> finish

Step 3 is a DECISION: the person doing it picks one of two outcomes when they
submit, and each outcome routes somewhere of its own. "Rejected" going back to
step 1 is an ordinary rework loop, not a special case.

A step whose route is 0 ends the flow. A step whose route is NULL predates
routing altogether and falls back to "the next step in order", so flows built
before this keep behaving exactly as they did.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .. import clock
from ..models import (
    Flow, FlowStep, FlowInstance, Task, TaskComment, TaskStatus, TaskSource, User
)
from . import notify, holidays


def _deadline_base(db: Session, inst: FlowInstance, step: FlowStep) -> datetime:
    """The moment this step's turnaround is counted from.

    Normally that is now — the step has just opened. But a step can be tied
    to an earlier step's PLANNED date instead, so a chain hanging off one
    date does not drift every time somebody closes a step late.
    """
    if not step.due_from_pos:
        return clock.now()
    earlier = [t for t in inst.tasks
               if t.flow_step and t.flow_step.position == step.due_from_pos]
    if not earlier:
        return clock.now()          # that step has not run on this instance
    # The most recent run of it, so a rework loop measures from the latest
    # pass rather than from a date two rounds old.
    return max(t.due_at for t in earlier)


def _spawn_step_task(db: Session, inst: FlowInstance, step: FlowStep,
                     doer: User, assigner: User) -> Task:
    base = _deadline_base(db, inst, step)
    want = clock.add_span(base, step.tat_unit or "hours",
                          step.tat_value or step.tat_hours or 0)
    # The work still has to happen, so the step is never skipped — only its
    # deadline moves off a holiday. Skipping it would break the chain.
    due_at, _moved = holidays.shift_due(db, inst.org_id, want, doer.branch_id)

    task = Task(
        org_id=inst.org_id,
        branch_id=doer.branch_id,
        title=f"{step.title} — {inst.reference}",
        details=step.instructions,
        assigner_id=assigner.id,
        doer_id=doer.id,
        priority=step.priority,
        source=TaskSource.FLOW,
        due_at=due_at,
        flow_instance_id=inst.id,
        flow_step_id=step.id,
        requires_audit=step.requires_audit,
        requires_attachment=step.requires_attachment,
    )
    db.add(task)
    db.flush()
    notify.queue(
        db, doer, "flow_step_ready", task,
        title=step.title, flow=inst.flow.name, reference=inst.reference,
        due=task.due_at.strftime("%d %b, %I:%M %p"),
    )
    return task


def start_flow(db: Session, flow: Flow, reference: str, starter: User,
               doer_overrides: dict[int, int] | None = None,
               context: dict | None = None) -> FlowInstance:
    """doer_overrides maps flow_step.id -> user.id for steps with no default doer."""
    if not flow.steps:
        raise ValueError("This flow has no steps configured.")

    inst = FlowInstance(
        org_id=flow.org_id,
        flow_id=flow.id,
        reference=reference,
        started_by_id=starter.id,
        current_position=flow.steps[0].position,
        context=json.dumps(context or {}),
    )
    db.add(inst)
    db.flush()

    first = flow.steps[0]
    doer_id = (doer_overrides or {}).get(first.id) or first.default_doer_id or starter.id
    doer = db.get(User, doer_id)
    _spawn_step_task(db, inst, first, doer, starter)
    db.commit()
    return inst


# A rework loop that never converges would spawn tasks forever. Nobody legs
# a bill round a five-step flow forty times, so this is a runaway, not a
# workload — the run is stopped and the trail left in place to look at.
MAX_STEPS_PER_RUN = 200


def advance_flow(db: Session, task: Task) -> Task | None:
    """Close out a flow step and open whatever it routes to."""
    inst = task.flow_instance
    if inst is None or inst.completed_at:
        return None
    # A run that is paused or stopped does not open its next step. Without
    # this, approving an audit on a held run would quietly start it moving
    # again behind the back of whoever paused it.
    if inst.held_at is not None or inst.cancelled_at is not None:
        return None

    # carry the doer's captured field values forward into the flow context
    try:
        ctx = json.loads(inst.context or "{}")
        ctx.update(json.loads(task.captured_data or "{}"))
        inst.context = json.dumps(ctx)
    except json.JSONDecodeError:
        pass

    steps = sorted(inst.flow.steps, key=lambda s: s.position)
    step = task.flow_step
    done_pos = step.position if step else inst.current_position

    spawned = db.scalar(select(func.count()).select_from(Task)
                        .where(Task.flow_instance_id == inst.id)) or 0
    if spawned >= MAX_STEPS_PER_RUN:
        inst.completed_at = clock.now()
        db.add(TaskComment(
            task_id=task.id, author_id=task.doer_id,
            body=f"This run was stopped after {spawned} steps — the flow is "
                 "looping without finishing. Check where the decision steps "
                 "route to."))
        db.commit()
        return None

    target = step.route(task.decision or "pass") if step else None
    if target is None:
        # Built before routing existed: carry on down the list, as before.
        nxt = next((s for s in steps if s.position > done_pos), None)
    elif target == 0:
        nxt = None                      # this step deliberately ends the flow
    else:
        nxt = next((s for s in steps if s.position == target), None)
        if nxt is None:
            # Routed at a step that has since been removed. Ending the run is
            # honest; silently sliding to the next one would hide it.
            nxt = None

    if nxt is None:
        inst.completed_at = clock.now()
        db.commit()
        return None

    inst.current_position = nxt.position
    doer = db.get(User, nxt.default_doer_id) if nxt.default_doer_id else task.doer
    new_task = _spawn_step_task(db, inst, nxt, doer, task.assigner)
    db.commit()
    return new_task


def flow_progress(inst: FlowInstance) -> tuple[int, int]:
    """How far along a run is.

    Counting positions behind the current one stops meaning anything once a
    flow can loop backwards — a rejected bill returning to step 1 would read
    as "0 of 5 done" after four steps of real work. So this counts the steps
    actually CLOSED on this run, which only ever goes up.
    """
    total = len(inst.flow.steps)
    if inst.completed_at:
        return total, total
    done = sum(1 for t in inst.tasks if t.status == TaskStatus.COMPLETED)
    return min(done, total), total


# ---------------------------------------------------------- hold & stop ---
# Which task states a hold applies to: everything that has not been closed.
# A step still with the auditor counts — the run is frozen, and that includes
# whoever was about to check it.
_LIVE_TASK_STATES = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS,
                     TaskStatus.SUBMITTED, TaskStatus.REJECTED,
                     TaskStatus.REOPENED)


def _open_tasks(db: Session, inst: FlowInstance) -> list[Task]:
    return list(db.scalars(select(Task).where(
        Task.flow_instance_id == inst.id,
        Task.status.in_(_LIVE_TASK_STATES))).all())


def hold_run(db: Session, inst: FlowInstance, by: User, reason: str = "") -> int:
    """Pause a run. Its open steps step out of everyone's way.

    Each task remembers the state it was in, so resuming puts it back exactly
    there rather than assuming "in progress" — which would erase the fact
    that an auditor had sent it back to be redone.
    """
    if inst.completed_at or inst.cancelled_at or inst.held_at:
        return 0
    inst.held_at = clock.now()
    inst.held_by_id = by.id
    inst.hold_reason = (reason or "").strip() or None

    tasks = _open_tasks(db, inst)
    for t in tasks:
        t.held_from = t.status.value
        t.status = TaskStatus.ON_HOLD
        db.add(TaskComment(
            task_id=t.id, author_id=by.id,
            body="Put on hold — this FMS run is paused."
                 + (f" Reason: {inst.hold_reason}" if inst.hold_reason else "")))
    return len(tasks)


def resume_run(db: Session, inst: FlowInstance, by: User,
               shift_deadlines: bool = True) -> tuple[int, int]:
    """Start a held run moving again.

    Returns (tasks resumed, whole days the hold lasted).

    The deadlines move forward by however long the hold lasted. This is the
    part that matters: without it, a run held for three days comes back with
    every step already overdue, and the doer loses score for three days when
    the work was frozen by somebody else's decision. The hold is measured to
    the minute and applied to the minute, so a half-day pause shifts things
    by half a day rather than being rounded away.
    """
    if not inst.held_at or inst.completed_at or inst.cancelled_at:
        return 0, 0
    paused_for = clock.now() - inst.held_at
    days = max(0, round(paused_for.total_seconds() / 86400))

    tasks = list(db.scalars(select(Task).where(
        Task.flow_instance_id == inst.id,
        Task.status == TaskStatus.ON_HOLD)).all())
    for t in tasks:
        try:
            t.status = TaskStatus(t.held_from) if t.held_from \
                else TaskStatus.IN_PROGRESS
        except ValueError:
            t.status = TaskStatus.IN_PROGRESS
        t.held_from = None
        if shift_deadlines:
            t.due_at = t.due_at + paused_for
        db.add(TaskComment(
            task_id=t.id, author_id=by.id,
            body=f"Resumed after {_span_words(paused_for)} on hold."
                 + (f" Planned date moved to {t.due_at:%d %b %Y, %I:%M %p}."
                    if shift_deadlines else
                    " The planned date was left where it was.")))

    inst.held_days = (inst.held_days or 0) + days
    inst.held_at = None
    inst.held_by_id = None
    inst.hold_reason = None
    return len(tasks), days


def stop_run(db: Session, inst: FlowInstance, by: User, reason: str = "") -> int:
    """Abandon a run for good. Its open steps are cancelled.

    Deliberately not a delete. The run happened, people did work on it, and
    the record of why it was abandoned is usually the point.
    """
    if inst.cancelled_at or inst.completed_at:
        return 0
    inst.cancelled_at = clock.now()
    inst.cancelled_by_id = by.id
    inst.cancel_reason = (reason or "").strip() or None
    inst.held_at = None
    inst.held_by_id = None

    tasks = list(db.scalars(select(Task).where(
        Task.flow_instance_id == inst.id,
        Task.status.in_(_LIVE_TASK_STATES + (TaskStatus.ON_HOLD,)))).all())
    for t in tasks:
        t.status = TaskStatus.CANCELLED
        t.held_from = None
        db.add(TaskComment(
            task_id=t.id, author_id=by.id,
            body="Cancelled — this FMS run was stopped."
                 + (f" Reason: {inst.cancel_reason}" if inst.cancel_reason else "")))
    return len(tasks)


def _span_words(delta) -> str:
    """"2 days", "4 hours", "35 minutes" — whichever reads best."""
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)} minute" + ("" if mins == 1 else "s")
    if mins < 1440:
        h = mins // 60
        return f"{h} hour" + ("" if h == 1 else "s")
    d = mins // 1440
    return f"{d} day" + ("" if d == 1 else "s")
