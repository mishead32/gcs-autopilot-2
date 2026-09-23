"""Flow Management System engine.

start_flow()  -> creates a FlowInstance and spawns the task for step 1
advance_flow() -> called when a flow task closes; spawns the next step's task
"""
import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import (
    Flow, FlowStep, FlowInstance, Task, TaskStatus, TaskSource, User
)
from . import notify, holidays


def _spawn_step_task(db: Session, inst: FlowInstance, step: FlowStep,
                     doer: User, assigner: User) -> Task:
    # The work still has to happen, so the step is never skipped — only its
    # deadline moves off a holiday. Skipping it would break the chain.
    due_at, _moved = holidays.shift_due(
        db, inst.org_id, datetime.utcnow() + timedelta(hours=step.tat_hours),
        doer.branch_id)

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


def advance_flow(db: Session, task: Task) -> Task | None:
    """Close out a flow step and open the next one. Returns the new task, if any."""
    inst = task.flow_instance
    if inst is None or inst.completed_at:
        return None

    # carry the doer's captured field values forward into the flow context
    try:
        ctx = json.loads(inst.context or "{}")
        ctx.update(json.loads(task.captured_data or "{}"))
        inst.context = json.dumps(ctx)
    except json.JSONDecodeError:
        pass

    steps = sorted(inst.flow.steps, key=lambda s: s.position)
    done_pos = task.flow_step.position if task.flow_step else inst.current_position
    nxt = next((s for s in steps if s.position > done_pos), None)

    if nxt is None:
        inst.completed_at = datetime.utcnow()
        db.commit()
        return None

    inst.current_position = nxt.position
    doer = db.get(User, nxt.default_doer_id) if nxt.default_doer_id else task.doer
    new_task = _spawn_step_task(db, inst, nxt, doer, task.assigner)
    db.commit()
    return new_task


def flow_progress(inst: FlowInstance) -> tuple[int, int]:
    steps = sorted(inst.flow.steps, key=lambda s: s.position)
    total = len(steps)
    if inst.completed_at:
        return total, total
    done = sum(1 for s in steps if s.position < inst.current_position)
    return done, total
