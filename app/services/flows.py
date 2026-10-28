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
import os
import json
from datetime import datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .. import clock
from ..models import (
    CONTEXT_PEOPLE,
    Department, Flow, FlowStep, FlowInstance, Task, TaskComment, TaskStatus,
    TaskSource, User
)
from . import notify, holidays


# The hour a bare date is taken to mean. Somebody answering "20 Oct" means
# that working day, not one second past midnight: counting a two-day TAT from
# midnight quietly gives everyone two nights less than they expected.
FIELD_DATE_HOUR = int(os.getenv("FIELD_DATE_HOUR", "9"))


def _answered_moment(inst: FlowInstance, label: str) -> datetime | None:
    """The date somebody typed on the start form, as a moment — or None.

    Accepts what the three date-ish inputs actually post: a plain date, a
    datetime-local value with a T in the middle, and the same with seconds.
    Anything else is not a date and is reported as such rather than guessed
    at, because a guessed deadline is worse than an obvious one.
    """
    try:
        answers = json.loads(inst.context or "{}")
    except (ValueError, TypeError):
        return None
    raw = answers.get(label)
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip().replace("/", "-")
    for fmt, bare in (("%Y-%m-%dT%H:%M:%S", False), ("%Y-%m-%dT%H:%M", False),
                      ("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True),
                      ("%d-%m-%Y", True)):
        try:
            when = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return when.replace(hour=FIELD_DATE_HOUR) if bare else when
    return None


def dept_people(db: Session, org_id: int, branch_id: int | None,
                dept_name: str | None) -> list[User]:
    """Everybody holding a position at one company: "Accounts at Spa Kora".

    Matched on the department's NAME rather than its id, because there is a
    separate Accounts row under every company — matching the id would pin
    the step to one of them, which is the whole thing this avoids.

    Case and stray spaces are ignored: "accounts " and "Accounts" are the
    same position to everybody except a database.
    """
    if not dept_name or not branch_id:
        return []
    want = dept_name.strip().lower()
    if not want:
        return []
    rows = db.scalars(
        select(User).join(Department, User.department_id == Department.id)
        .where(User.org_id == org_id, User.active.is_(True),
               func.lower(func.trim(Department.name)) == want)
        .order_by(User.name)
    ).all()
    # Owners and admins hand work out; nobody hands work to them. An admin
    # who happens to sit in the Accounts department is not the accountant.
    rows = [u for u in rows if u.receives_work]

    # Somebody's OWN company beats somebody who merely also covers it.
    #
    # One accountant doing Bodyzone and Spa Kora belongs to one of them and
    # covers the other. The day Spa Kora hires its own, Spa Kora's runs
    # should go to the new person without anybody having to remember to
    # take Spa Kora off the first one's record — and Bodyzone's runs should
    # carry on exactly as before. So the stand-in is used only where the
    # company has nobody of its own.
    own = [u for u in rows if u.branch_id == branch_id]
    if own:
        return own
    return [u for u in rows if branch_id in u.extra_branch_ids]


def answered_person(db: Session, inst: FlowInstance, label: str) -> User | None:
    """The person a run named in answer to one question, or None.

    The id is read first, because that is what was resolved when the run was
    started and it stays right even if two people share a name or somebody
    is renamed afterwards. The typed name is the fallback, for a run started
    before person questions existed or imported by some other route.
    """
    if not label:
        return None
    try:
        data = json.loads(inst.context or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    picked = (data.get(CONTEXT_PEOPLE) or {}) if isinstance(
        data.get(CONTEXT_PEOPLE), dict) else {}
    uid = picked.get(label)
    if uid:
        person = db.get(User, int(uid)) if str(uid).isdigit() else None
        if person is not None and person.active:
            return person

    name = str(data.get(label, "") or "").strip()
    if not name:
        return None
    want = " ".join(name.split()).lower()
    rows = db.scalars(select(User).where(User.org_id == inst.org_id,
                                         User.active.is_(True))).all()
    for u in rows:
        if (u.email or "").strip().lower() == want:
            return u
    hits = [u for u in rows
            if " ".join((u.name or "").split()).lower() == want]
    # Two people with the same name and no id to separate them: answering
    # with either would be a guess, and a guess about who does the work is
    # the one thing not worth saving a click for.
    return hits[0] if len(hits) == 1 else None


def step_people(db: Session, inst: FlowInstance, step: FlowStep) -> list[User]:
    """Who this step opens for on THIS run.

    Three ways a flow can answer it, in the order they are checked:

      an answer   whoever this run named in a person question — the only
                  one that differs run to run, so one renewal flow serves
                  four hundred members with four hundred trainers
      a position  whoever is in Accounts at the company this run is for
      a name      the person written into the flow itself
    """
    if step.doer_field:
        person = answered_person(db, inst, step.doer_field)
        return [person] if person is not None else []
    if step.doer_dept:
        return dept_people(db, inst.org_id, inst.branch_id, step.doer_dept)
    people = [db.get(User, i) for i in step.doer_ids]
    return [p for p in people if p is not None and p.active]


def missing_positions(db: Session, flow: Flow, branch_id: int | None) -> list[str]:
    """Positions this flow needs that the chosen company has nobody in.

    Checked before a run is started rather than when the step comes round,
    because a step that cannot find its person is found three days later by
    whoever is holding the work — and by then the run is half done.
    """
    gaps = []
    for step in flow.steps:
        if not step.doer_dept:
            continue
        if dept_people(db, flow.org_id, branch_id, step.doer_dept):
            continue
        if step.doer_dept not in gaps:
            gaps.append(step.doer_dept)
    return gaps


def _deadline_base(db: Session, inst: FlowInstance, step: FlowStep,
                   opens_at: datetime | None = None) -> datetime:
    """The moment this step's turnaround is counted from.

    Four answers, and the flow says which it wants:

      nothing   — now, because the step has just opened. The usual case.
      0         — when the RUN was started: the moment somebody filled in
                  the start form. A chain measured from there keeps its
                  dates however long an earlier step took, which is what
                  people mean by "within a week of the bill arriving".
      a number  — that step's PLANNED date, so a chain hanging off one date
                  does not drift every time somebody closes a step late.
      a field   — a DATE ANSWERED ON THE START FORM. "Due three days after
                  the bill date", where the bill date is not when anything
                  happened in here but something somebody typed in.
    """
    if step.due_from_field:
        answered = _answered_moment(inst, step.due_from_field)
        if answered is not None:
            return answered
        # The question was renamed, left blank, or answered with something
        # that is not a date. Falling back to the step opening keeps the run
        # moving — a step with no deadline at all would never be chased.
        return opens_at or clock.now()
    if step.due_from_pos is None:
        return opens_at or clock.now()
    if step.due_from_pos == 0:
        return inst.started_at or clock.now()
    earlier = [t for t in inst.tasks
               if t.flow_step and t.flow_step.position == step.due_from_pos]
    if not earlier:
        return opens_at or clock.now()   # that step has not run on this instance
    # The most recent run of it, so a rework loop measures from the latest
    # pass rather than from a date two rounds old.
    return max(t.due_at for t in earlier)


def _spawn_step_task(db: Session, inst: FlowInstance, step: FlowStep,
                     doer: User, assigner: User, waiting: bool = False,
                     opens_at: datetime | None = None,
                     provisional: bool = False) -> Task:
    """One task for one person on one step.

    opens_at is the moment this step is EXPECTED to open, used while the
    whole run is being planned ahead: step 3 has not opened and cannot be
    counted from "now", so it is counted from when step 2 is due to finish.
    Left out — the step is opening for real — and "now" is the answer,
    which is what it has always been.

    provisional says the flow does not name anybody for this step, so the
    plan is holding it against the person who started the run. It is said
    on the task rather than left to be discovered, because somebody seeing
    a step they do not recognise on their list should be able to find out
    why in one click instead of asking.
    """
    base = _deadline_base(db, inst, step, opens_at)
    unit = step.tat_unit or "hours"
    value = step.tat_value if step.tat_value is not None else (step.tat_hours or 0)
    # Two kinds of turnaround. A SPAN is a length added to the anchor; a
    # CALENDAR unit is a date that comes round, found at or after it. Both
    # start from the same anchor, so "the date of joining, then the next
    # 15th" is one setting rather than arithmetic somebody does by hand.
    if unit in (clock.BEFORE_UNIT, clock.AFTER_UNIT):
        # A deadline a number of days either side of a date somebody typed
        # on the start form: "follow up thirty days before the membership
        # expires", "first check-in three days after it starts".
        #
        # The point of both is that the date is fixed by the FORM, not by
        # when the step happened to open. Every other span counts from the
        # moment the step before it was finished, so a slow week moves
        # every deadline after it — which is right for a chain of work and
        # wrong for "three days after the membership starts".
        days = max(0, int(value or 0))
        anchor = _answered_moment(inst, step.due_from_field or "")
        if anchor is None:
            # The question was renamed, left blank, or answered with
            # something that is not a date. A day from now keeps the run
            # moving with a deadline somebody can actually meet; counting
            # from "now" instead would hand them one in the past.
            want = clock.add_span(opens_at or clock.now(), "hours", 24)
        elif unit == clock.BEFORE_UNIT:
            want = anchor - timedelta(days=days)
        else:
            want = anchor + timedelta(days=days)
    elif unit == clock.FORM_UNIT:
        # The deadline IS the moment somebody typed on the start form —
        # "due at the joining date and time" means exactly that, with
        # nothing added to it.
        #
        # Read here rather than taken from the anchor above, because the two
        # failures want different answers. An anchor that cannot be read
        # falls back to now and the step still gets its TAT on top; this one
        # has no TAT on top, so falling back to now would hand somebody a
        # task that was already late the second it arrived. A day is the
        # honest stand-in: the run keeps moving and the deadline is one
        # somebody can actually meet.
        want = _answered_moment(inst, step.due_from_field or "")
        if want is None:
            want = clock.add_span(opens_at or clock.now(), "hours", 24)
    elif unit in clock.CALENDAR_UNITS:
        want = clock.next_occurrence(base, unit, value)
    else:
        want = clock.add_span(base, unit, value)
    # The work still has to happen, so the step is never skipped — only its
    # deadline moves off a closed day, FORWARD to the next working day.
    #
    # Forward, not back, because a chain cannot be dragged backwards: a
    # Sunday step pulled back to Saturday lands on the day of the step it
    # is waiting for, and then the step after it is Sunday again and lands
    # on that same Saturday too. The tail of a run ends up stacked on one
    # afternoon, each step due before the work it depends on could possibly
    # be done. See holidays.push_due.
    due_at, _moved = holidays.push_due(db, inst.org_id, want, doer.branch_id)

    details = step.instructions
    if provisional:
        who = (f"nobody is in {step.doer_dept} at this run's company"
               if step.doer_dept
               else "the flow does not say who does this step")
        details = ((details + "\n\n") if details else "") + (
            f"— Planned provisionally: {who}, so it is held against "
            "whoever started the run until somebody is set. Fix it under "
            "Edit steps & questions and the plan follows next time this "
            "run moves.")

    task = Task(
        org_id=inst.org_id,
        branch_id=doer.branch_id,
        title=f"{step.title} — {inst.reference}",
        details=details,
        how_to=step.how_to,
        assigner_id=assigner.id,
        doer_id=doer.id,
        priority=step.priority,
        source=TaskSource.FLOW,
        due_at=due_at,
        flow_instance_id=inst.id,
        flow_step_id=step.id,
        requires_audit=step.requires_audit,
        requires_attachment=step.requires_attachment,
        flow_waiting=waiting,
    )
    db.add(task)
    db.flush()

    # A step still waiting its turn is not news. The person can see it on
    # their list with the date it is planned for; a WhatsApp saying "this is
    # ready" when it is not would teach them to ignore the next one, which
    # is the message that matters.
    if not waiting:
        notify.queue(
            db, doer, "flow_step_ready", task,
            title=step.title, flow=inst.flow.name, reference=inst.reference,
            due=task.due_at.strftime("%d %b, %I:%M %p"),
        )
    return task


# --------------------------------------------------------- the plan ------
# A run used to show one step at a time: step 2 did not exist until step 1
# was marked, so the person on step 3 had no idea anything was coming until
# the day it landed on them. They could not book around it, and a flow that
# takes three weeks looked like a single task that appeared out of nowhere.
#
# So the whole run is laid out the moment the start form is submitted. Every
# step gets a task with the date it is PLANNED for, chained off the one
# before it: form submitted 16 Oct, step 1 (TAT 1 day) due 17 Oct, step 2
# (TAT 2 days) due 19 Oct, and so on. Everybody can see their part of it
# from day one.
#
# Only the step the run is actually on is live work. The rest are visible,
# and can be got on with early, but they are not chased and not scored until
# their turn comes: nobody is marked late for work that was not yet theirs.

# How many steps ahead a plan is drawn. A rework loop would otherwise plan
# the same four steps round and round forever.
MAX_PLANNED_STEPS = 40


def straight_path(flow: Flow, start_pos: int) -> list[FlowStep]:
    """The steps a run goes through if every decision goes the first way.

    A decision cannot be read before somebody makes it, so the plan follows
    the first outcome and is redrawn the moment a real decision is taken.
    Showing nothing past the first decision would put most flows back to one
    step at a time, which is the thing being fixed.

    A route back to a step already in the plan ends it: that is a rework
    loop, and a plan that listed step 2 four times would be noise, not a
    plan.
    """
    by_pos = {s.position: s for s in flow.steps}
    order = sorted(by_pos)
    out: list[FlowStep] = []
    seen: set[int] = set()
    pos = start_pos
    while pos in by_pos and pos not in seen and len(out) < MAX_PLANNED_STEPS:
        step = by_pos[pos]
        out.append(step)
        seen.add(pos)
        target = step.next_step_pos
        if target == 0:
            break                       # this step deliberately ends the flow
        if target is None:
            nxt = next((p for p in order if p > pos), None)
            if nxt is None:
                break
            pos = nxt
        else:
            pos = target
    return out


def _touched(task: Task) -> bool:
    """Has anybody done anything with this task yet?

    A waiting task nobody has opened can be taken off the plan silently when
    a decision sends the run elsewhere. One somebody has started, written on
    or attached to is a record of real work and is never removed.
    """
    # started_at is NOT evidence of anything. Every task is marked live the
    # moment it is created — there is no "accept" step in this software —
    # so a task nobody has looked at still carries one.
    return bool(task.submitted_at or task.closed_at or task.completion_note
                or task.comments or task.attachments
                or task.status not in (TaskStatus.PENDING,
                                       TaskStatus.IN_PROGRESS))


def _run_tasks(db: Session, inst: FlowInstance) -> list[Task]:
    """Every task of this run, read FRESH from the database.

    Not inst.tasks. That collection is loaded once and does not notice rows
    added later in the same request, so a plan drawn right after a step was
    opened would not see the step it had just opened — and the check for
    "did anything change" would answer no and throw the work away.
    """
    return list(db.scalars(
        select(Task).where(Task.flow_instance_id == inst.id)).all())


def replan(db: Session, inst: FlowInstance, assigner: User) -> int:
    """Lay out the rest of the run, with a planned date on every step.

    Called when the run starts and again whenever it moves, so the plan
    stays true: a decision that sends the flow elsewhere drops the steps
    that are no longer coming and adds the ones that now are, and a step
    finished three days late pushes the dates after it along rather than
    handing somebody a task that was already overdue when it arrived.

    Only steps AHEAD of where the run is are touched. Live work, finished
    work, and anything anybody has so much as opened are left exactly as
    they are.
    """
    if inst.completed_at or inst.cancelled_at:
        return 0
    path = straight_path(inst.flow, inst.current_position)
    if not path:
        return 0
    ahead = path[1:]
    keep_ids = {s.id for s in ahead}
    changed = 0
    rows = _run_tasks(db, inst)

    # Steps that were planned and are not coming after all.
    for t in rows:
        if t.flow_waiting and t.flow_step_id not in keep_ids and not _touched(t):
            db.delete(t)
            changed += 1
    db.flush()
    rows = _run_tasks(db, inst)

    # The chain starts from when the step being worked on is due to finish.
    live = [t for t in rows
            if t.flow_step_id == path[0].id and t.status in _LIVE_TASK_STATES]
    prev_due = max((t.due_at for t in live), default=clock.now())

    for step in ahead:
        mine = [t for t in rows
                if t.flow_step_id == step.id and t.status in _LIVE_TASK_STATES]
        if mine:
            # Already planned. Re-date whatever is still waiting and
            # untouched, so a late step moves what comes after it.
            for t in mine:
                if t.flow_waiting and not _touched(t):
                    fresh = _planned_due(db, inst, step, t.doer, prev_due)
                    if fresh != t.due_at:
                        t.due_at = fresh
                        changed += 1
            prev_due = max(t.due_at for t in mine)
            continue
        people = step_people(db, inst, step)
        provisional = False
        if not people:
            # Nobody is named for this step. It used to be dropped from the
            # plan here, with a comment saying it could not be shown to
            # anybody in advance — and that was the bug behind "only step 1
            # is showing". A flow whose later steps have no doer set, or
            # whose position nobody at that company holds, planned exactly
            # one step. The people on steps 2 to 8 saw nothing coming, and
            # the run read "1 of 8" for ever.
            #
            # The step is not skipped when its turn actually comes:
            # _open_step falls back to the person in front of it rather
            # than losing the work. So the plan was disagreeing with what
            # the software was going to do anyway — the honest fix is to
            # plan the same thing, and SAY it is provisional, rather than
            # hide a step that is definitely coming.
            people = [assigner]
            provisional = True
        # One decision, one person — the same rule the live step follows.
        if step.is_decision:
            people = people[:1]
        made = [_spawn_step_task(db, inst, step, p, assigner,
                                 waiting=True, opens_at=prev_due,
                                 provisional=provisional)
                for p in people]
        changed += len(made)
        prev_due = max(t.due_at for t in made)
    db.flush()
    return changed


def _planned_due(db: Session, inst: FlowInstance, step: FlowStep,
                 doer: User, opens_at: datetime) -> datetime:
    """The date a step is planned for, if it opens at `opens_at`."""
    base = _deadline_base(db, inst, step, opens_at)
    unit = step.tat_unit or "hours"
    value = step.tat_value if step.tat_value is not None else (step.tat_hours or 0)
    if unit in (clock.BEFORE_UNIT, clock.AFTER_UNIT):
        anchor = _answered_moment(inst, step.due_from_field or "")
        days = max(0, int(value or 0))
        if anchor is None:
            want = clock.add_span(opens_at, "hours", 24)
        else:
            want = (anchor - timedelta(days=days) if unit == clock.BEFORE_UNIT
                    else anchor + timedelta(days=days))
    elif unit == clock.FORM_UNIT:
        want = _answered_moment(inst, step.due_from_field or "")
        if want is None:
            want = clock.add_span(opens_at, "hours", 24)
    elif unit in clock.CALENDAR_UNITS:
        want = clock.next_occurrence(base, unit, value)
    else:
        want = clock.add_span(base, unit, value)
    # Forward to the next working day, for the same reason as above: this
    # is the function that dates the whole chain, so pulling a Sunday back
    # here is what stacks four steps on one Saturday.
    due_at, _moved = holidays.push_due(db, inst.org_id, want,
                                       doer.branch_id if doer else None)
    return due_at


def _activate(db: Session, inst: FlowInstance, step: FlowStep,
              fallback: User, assigner: User) -> Task | None:
    """Hand a planned step over for real, or open it if it was not planned.

    Returns None when the step is already finished — somebody got on with
    it early, which they are allowed to do — so the caller can carry on
    past it rather than reopening work that is done.
    """
    mine = [t for t in _run_tasks(db, inst) if t.flow_step_id == step.id]
    live = [t for t in mine if t.status in _LIVE_TASK_STATES]
    if not live:
        # Done EARLY means finished while it was still only planned — the
        # flag is left on it for exactly this reason. A step finished the
        # ordinary way, when its turn came, must still be reopened: that is
        # what a rework loop is, and treating "already completed" as "no
        # need" would send a rejected bill sailing past the step it was
        # rejected back to.
        if mine and all(t.status == TaskStatus.COMPLETED and t.flow_waiting
                        for t in mine):
            return None
        return _open_step(db, inst, step, fallback, assigner)

    first = None
    for t in live:
        if not t.flow_waiting:
            first = first or t
            continue
        t.flow_waiting = False
        # The deadline was an estimate while the step was waiting. Now the
        # step has really opened, a TAT counted from "when this step opens"
        # is counted from this moment — otherwise a run that slipped three
        # days would hand somebody a task that was already overdue.
        t.due_at = _planned_due(db, inst, step, t.doer, clock.now())
        notify.queue(
            db, t.doer, "flow_step_ready", t,
            title=step.title, flow=inst.flow.name, reference=inst.reference,
            due=t.due_at.strftime("%d %b, %I:%M %p"),
        )
        first = first or t
    return first


def catch_up(db: Session, inst: FlowInstance) -> bool:
    """Make one run tell the truth: unstick it, then plan the rest of it.

    Two things this fixes, both visible on the same screen.

    A run that was ALREADY GOING when planning arrived has no plan — the
    plan is drawn when a run starts and whenever it moves, and that run did
    both before any of this existed. So a four-step flow with one step done
    showed one row, and a report asking how much FMS work was outstanding
    answered nothing, because three of its four steps did not exist yet.

    And a run can be stuck: its current step finished, nothing opened after
    it, and the run sat there reading "1 of 4 done · now on step 1" with
    not one task on anybody's list. Whatever lost that moment — an audit
    approved while the server was restarting, an error halfway through —
    the run is still owed its next step, and nobody can give it one by
    hand.

    Returns True when something was actually changed, so a caller can skip
    a pointless commit.
    """
    if inst.completed_at or inst.cancelled_at or inst.held_at:
        return False
    steps = sorted(inst.flow.steps, key=lambda s: s.position)
    if not steps:
        return False
    assigner = inst.started_by
    moved = False

    guard = 0
    while guard < MAX_PLANNED_STEPS:
        guard += 1
        step = next((s for s in steps if s.position == inst.current_position), None)
        if step is None:
            # The step it was sitting on has since been deleted from the
            # flow. Ending the run is honest; silently sliding it somewhere
            # else would hide that the flow was changed underneath it.
            inst.completed_at = clock.now()
            moved = True
            break

        mine = [t for t in _run_tasks(db, inst) if t.flow_step_id == step.id]
        if [t for t in mine if t.status in _WITH_DOER]:
            break                       # somebody has it; the run is fine

        if not mine:
            # Never opened at all. Open it — this is the live step.
            _open_step(db, inst, step, assigner, assigner)
            db.flush()
            moved = True
            break

        # Every task for this step is closed, and nothing came after it.
        # Follow where the step says to go, exactly as finishing it would.
        closed = sorted(mine, key=lambda t: (t.closed_at or t.submitted_at
                                             or t.due_at))
        target = step.route(closed[-1].decision or "pass")
        if target is None:
            nxt = next((s for s in steps if s.position > step.position), None)
        elif target == 0:
            nxt = None
        else:
            nxt = next((s for s in steps if s.position == target), None)
        if nxt is None:
            inst.completed_at = clock.now()
            moved = True
            break
        inst.current_position = nxt.position
        moved = True

    changed = replan(db, inst, assigner)
    db.flush()
    return moved or changed > 0


def catch_up_all(db: Session, limit: int = 200) -> int:
    """Every run that is still going. Returns how many were changed.

    Runs on a timer rather than when somebody opens a page, so the reports
    are right whether or not anybody has been looking at the runs — which
    is the whole point of a report.
    """
    live = db.scalars(
        select(FlowInstance)
        .where(FlowInstance.completed_at.is_(None),
               FlowInstance.cancelled_at.is_(None),
               FlowInstance.held_at.is_(None))
        .order_by(FlowInstance.id).limit(limit)
    ).all()
    changed = 0
    for inst in live:
        try:
            if catch_up(db, inst):
                changed += 1
        except Exception as exc:                       # one bad run, not all
            db.rollback()
            print(f"Could not catch up run {inst.id}: {exc!r}")
    if changed:
        db.commit()
    return changed


# How many answers go into a run's name, and how long one of them may be
# before it is skipped.
#
# Two. One is often ambiguous — "January" is twelve runs over three years —
# and three starts collecting whatever the form happens to ask next: a
# month-and-year form that also records who typed it gives "January · 2026
# · Amit Kumar", where the name was "January · 2026" and the rest is noise.
# Two answers is what somebody says out loud to identify the thing.
NAME_PARTS = 2
NAME_PART_MAX = 60
NAME_MAX = 150


def reference_from(flow: Flow, context: dict | None) -> str:
    """Name a run from what was answered on its start form.

    There used to be a "Reference" box at the top of every start form, asked
    for on top of whatever the flow's own questions were. It was there
    because a run needs a name — it goes in every step's title, in the
    submissions register, in escalation emails — but it meant every form had
    a question nobody had added to it, and the answer repeated something the
    real questions had already asked.

    So the answers name the run. The first two short ones, in the order the
    form asks them, which is the order somebody would say them out loud:
    "January · 2026", "Sharma & Co · INV-7741".

    Long answers are skipped rather than truncated. A long-answer box holds
    a paragraph, and the first sixty characters of a paragraph makes a worse
    name than the question after it.

    Returns "" when nothing usable was answered; the caller falls back to
    the run's own FMS number, which is always a true name even if it is not
    a descriptive one.
    """
    data = context or {}
    parts: list[str] = []
    for field in flow.start_form_fields:
        if len(parts) >= NAME_PARTS:
            break
        if field["type"] == "file":
            # The answer is a filename chosen on somebody's laptop. It names
            # the document, not the thing the run is about.
            continue
        if field["type"] == "person":
            # Who DOES the work, not what the run is about. The doer is on
            # every task already; putting them in the name as well gives
            # "Ritu Bansal · Amit Kumar" where the run is about Ritu.
            continue
        value = str(data.get(field["label"], "") or "").strip()
        if not value or len(value) > NAME_PART_MAX:
            continue
        parts.append(" ".join(value.split()))
    return " \u00b7 ".join(parts)[:NAME_MAX]


def start_flow(db: Session, flow: Flow, reference: str, starter: User,
               doer_overrides: dict[int, int] | None = None,
               context: dict | None = None,
               branch_id: int | None = None) -> FlowInstance:
    """doer_overrides maps flow_step.id -> user.id for steps with no default doer.

    `reference` may be blank. A run still needs a name — it goes into every
    step's title — so a blank one is built from the start-form answers, and
    failing that from the run's own FMS number. Done here rather than in the
    router so every way of starting a run gets the same answer.
    """
    if not flow.steps:
        raise ValueError("This flow has no steps configured.")

    reference = (reference or "").strip() or reference_from(flow, context)

    inst = FlowInstance(
        org_id=flow.org_id,
        flow_id=flow.id,
        reference=reference,
        # A flow tied to one company says so itself; otherwise it is asked
        # when the run is started.
        branch_id=branch_id or flow.branch_id,
        started_by_id=starter.id,
        current_position=flow.steps[0].position,
        context=json.dumps(context or {}),
    )
    db.add(inst)
    db.flush()

    if not inst.reference:
        # Nothing was answered that could name it — a flow with no questions
        # at all, or one whose only questions are files and paragraphs. Its
        # own number is a true name, and it is the one people will quote.
        inst.reference = inst.ref or f"Run {inst.id}"
        db.flush()

    first = flow.steps[0]
    _open_step(db, inst, first, starter, starter, doer_overrides)
    # And everything after it, each with the date it is planned for, so the
    # people on steps 2, 3 and 4 can see what is coming instead of being
    # surprised by it on the day.
    replan(db, inst, starter)
    db.commit()
    return inst



def _open_step(db: Session, inst: FlowInstance, step: FlowStep,
               fallback: User, assigner: User,
               overrides: dict | None = None) -> Task:
    """Open a step: one task for each person who does it.

    A step with three names on it is three tasks, not one task with three
    names. Each person attaches their own proof, is audited on their own
    work and scored on it, and nobody is marked late because a colleague
    was. The run then waits for ALL of them before moving on, which is what
    "three people do this step" actually means.

    The first task is returned, because every caller wants something to
    point at and the first is as good an answer as any.
    """
    override = (overrides or {}).get(step.id)
    if override:
        people = [db.get(User, override)]
    else:
        people = step_people(db, inst, step)
    if not people:
        # A step assigned to a position should never get here — the run was
        # refused at the start if the company had nobody in it. If it does
        # (somebody left, mid-run), the work still has to go somewhere, and
        # the person in front of it is a better answer than nowhere.
        people = [fallback]

    # A decision step comes down to ONE person. Two people choosing two
    # different outcomes is a flow that has to go both ways at once, and
    # there is no honest answer to that.
    #
    # The run was refused at the start if its position held two people, so
    # this is for the case that check cannot see: somebody JOINED that
    # department after the run began. The step opens for the first of them
    # by name rather than for both, and says so on the task, because
    # quietly handing the same decision to two people is the one outcome
    # that cannot be unwound.
    if step.is_decision and len(people) > 1:
        chosen, rest = people[0], people[1:]
        made = _spawn_step_task(db, inst, step, chosen, assigner)
        db.add(TaskComment(
            task_id=made.id, author_id=assigner.id,
            body=("This is a decision step, so it goes to one person. "
                  + ", ".join(p.name for p in rest) +
                  (" is" if len(rest) == 1 else " are") +
                  f" also in {step.doer_dept} at this company, but two "
                  "people could choose two different outcomes.")))
        return made

    made = [_spawn_step_task(db, inst, step, p, assigner) for p in people]
    return made[0]


def _siblings_open(db: Session, task: Task) -> bool:
    """Is anybody else still working on this same step of this same run?

    Still with the DOER, not merely unclosed: somebody who has handed their
    part in has finished it, and the run should not wait on an auditor to
    say so. An earlier pass of a rework loop has already been closed, so a
    step that comes round twice is not held up by its own history.
    """
    if not task.flow_step_id:
        return False
    return bool(db.scalar(select(func.count()).select_from(Task).where(
        Task.flow_instance_id == task.flow_instance_id,
        Task.flow_step_id == task.flow_step_id,
        Task.id != task.id,
        Task.status.in_(_WITH_DOER))))


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

    # Somebody getting on with a later step early does not move the run.
    #
    # Every step is visible from the moment the form is submitted and any of
    # them can be marked, which is deliberate. But the run is still sitting
    # on step 1: advancing from step 3 because step 3 was ticked would skip
    # the two steps in between and leave their work open behind it. The
    # early tick is kept — when the run reaches that step it finds it
    # already done and carries straight past.
    # The flag stays on. It is how a step finished BEFORE its turn is told
    # apart from one finished when its turn came, which is what stops a
    # rework loop skipping the step it was sent back to.
    if task.flow_waiting:
        db.commit()
        return None
    if task.flow_step and task.flow_step.position != inst.current_position:
        db.commit()
        return None

    # A step several people share only counts as finished when the last of
    # them finishes. Moving on after the first would quietly cancel the work
    # of everybody else, which is the opposite of asking three people to do
    # something.
    if _siblings_open(db, task):
        db.commit()
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

    # Walk forward until a step that still needs doing. A step somebody got
    # on with early is already finished, and reopening it would throw their
    # work away and ask for it twice.
    new_task = None
    guard = 0
    while nxt is not None and guard < MAX_PLANNED_STEPS:
        guard += 1
        inst.current_position = nxt.position
        new_task = _activate(db, inst, nxt, task.doer, task.assigner)
        if new_task is not None:
            break
        done_early = [t for t in _run_tasks(db, inst)
                      if t.flow_step_id == nxt.id]
        target = nxt.route((done_early[-1].decision if done_early else None)
                           or "pass")
        if target is None:
            nxt = next((s for s in steps if s.position > nxt.position), None)
        elif target == 0:
            nxt = None
        else:
            nxt = next((s for s in steps if s.position == target), None)

    if new_task is None:
        inst.completed_at = clock.now()
        db.commit()
        return None

    # The plan is redrawn every time the run moves: a decision that sends it
    # elsewhere drops the steps that are no longer coming, and a step
    # finished late pushes the dates after it along.
    replan(db, inst, task.assigner)
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

    # Count STEPS finished, not tasks finished. The two are different as
    # soon as a step has more than one person on it, or has been sent back
    # and opened again — and both are normal. A step with three people and
    # a rework loop had five tasks against it, four of them closed, and a
    # run sitting on step 1 of 8 reported "4 of 8 steps done" with a
    # half-filled progress bar. It had done one step.
    #
    # A step counts when it has tasks and none of them is still with its
    # doer: that is the same test the engine uses to decide the run may
    # move on, so the bar and the run always agree.
    by_step: dict[int, list[Task]] = {}
    for t in inst.tasks:
        if t.flow_step_id is not None:
            by_step.setdefault(t.flow_step_id, []).append(t)
    done = sum(1 for rows in by_step.values()
               if rows and not any(t.status in _WITH_DOER for t in rows))
    return min(done, total), total


# ---------------------------------------------------------- hold & stop ---
# Which task states a hold applies to: everything that has not been closed.
# A step still with the auditor counts — the run is frozen, and that includes
# whoever was about to check it.
_LIVE_TASK_STATES = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS,
                     TaskStatus.SUBMITTED, TaskStatus.REJECTED,
                     TaskStatus.REOPENED)

# Still with the DOER — not yet handed in. Deliberately narrower than the
# list above, which also counts work sitting with an auditor.
#
# The difference is what decides when a run moves. A step is finished when
# the people doing it have finished it; whether anybody has checked it yet
# is a question about the auditor, and a flow that waits on an audit queue
# is a flow where one slow approval stops four people working.
_WITH_DOER = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS,
              TaskStatus.REJECTED, TaskStatus.REOPENED)


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
