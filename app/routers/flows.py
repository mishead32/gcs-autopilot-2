from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock, flash, search
import json

from ..db import get_db
from ..deps import current_user, manager_up, require_right
from ..models import (Flow, FlowStep, FlowInstance, Task, User, Branch, Priority,
                      Right, FIELD_TYPES)
from ..services import flows as flow_svc, xlsx
from ..templating import templates

router = APIRouter()


def _read_start_form(form) -> str | None:
    """Turn the builder's parallel arrays into the stored JSON.

    Rows with no label are skipped, so an empty row somebody added and never
    filled in does not become a blank question on the start page.
    """
    labels = form.getlist("sf_label")
    types = form.getlist("sf_type")
    opts = form.getlist("sf_options")
    reqs = form.getlist("sf_required")
    rows = []
    for i, label in enumerate(labels):
        label = (label or "").strip()
        if not label:
            continue
        kind = types[i] if i < len(types) and types[i] in FIELD_TYPES else "text"
        choices = []
        if kind == "select":
            raw = opts[i] if i < len(opts) else ""
            choices = [o.strip() for o in raw.split(",") if o.strip()]
            if not choices:
                raise HTTPException(
                    400, f"'{label}' is a list question, so it needs some "
                         "choices — type them separated by commas.")
        rows.append({"label": label, "type": kind, "options": choices,
                     "required": (reqs[i] != "0") if i < len(reqs) else True})
    return json.dumps(rows) if rows else None


@router.get("/flows", response_class=HTMLResponse)
def flow_list(request: Request, export: str = "", q: str = "",
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    text_q = search.clean(q)
    flows = db.scalars(
        search.apply(select(Flow).where(Flow.org_id == user.org_id),
                     [Flow.name, Flow.description], text_q)
        .order_by(Flow.name)
    ).all()
    running = db.scalars(
        select(FlowInstance)
        .where(FlowInstance.org_id == user.org_id, FlowInstance.completed_at.is_(None))
        .order_by(FlowInstance.started_at.desc()).limit(50)
    ).all()
    if xlsx.wants(export):
        # Two tabs: how the flows are built, and what is running right now.
        steps = [(fl, st) for fl in flows for st in fl.steps]
        return xlsx.book("fms-flows", [
            ("Flow steps", [
                ("Flow", lambda r: r[0].name),
                ("Branch", lambda r: r[0].branch.name if r[0].branch else "All branches"),
                ("Step", lambda r: r[1].position),
                ("Step title", lambda r: r[1].title),
                ("Who does it", lambda r: r[1].default_doer.name if r[1].default_doer else "Picked at run time"),
                ("TAT", lambda r: r[1].tat_label),
                ("Priority", lambda r: r[1].priority.value.title()),
                ("Decision step", lambda r: "Yes" if r[1].is_decision else ""),
                ("Outcome 1", lambda r: r[1].yes_label if r[1].is_decision else ""),
                ("Goes to", lambda r: r[1].next_step_pos if r[1].next_step_pos is not None else "next in order"),
                ("Outcome 2", lambda r: r[1].no_label if r[1].is_decision else ""),
                ("Then goes to", lambda r: r[1].fail_step_pos if r[1].is_decision else ""),
                ("Needs audit", lambda r: "Yes" if r[1].requires_audit else ""),
                ("Proof required", lambda r: "Yes" if r[1].requires_attachment else ""),
                ("Fields captured", lambda r: r[1].capture_fields or ""),
            ], steps, "One row per step, in the order the flow runs them."),
            ("Running now", [
                ("Flow", lambda i: i.flow.name),
                ("Reference", lambda i: i.reference),
                ("Started", lambda i: i.started_at),
                ("Started by", lambda i: i.started_by.name if i.started_by else ""),
                ("Steps done", lambda i: flow_svc.flow_progress(i)[0]),
                ("Steps total", lambda i: flow_svc.flow_progress(i)[1]),
                ("Currently at step", lambda i: i.current_position),
            ], running, "Runs that have not finished yet."),
        ])

    return templates.TemplateResponse(request, "flows.html", {
        "q": text_q, "can_manage": user.has(Right.MANAGE_FLOW),
        "user": user, "flows": flows, "running": running,
        "progress": {i.id: flow_svc.flow_progress(i) for i in running},
    })


@router.get("/flows/new", response_class=HTMLResponse)
def new_flow_form(request: Request, user: User = Depends(require_right(Right.MANAGE_FLOW)),
                  db: Session = Depends(get_db)):
    branches = db.scalars(select(Branch).where(Branch.org_id == user.org_id)).all()
    doers = db.scalars(
        select(User).where(User.org_id == user.org_id, User.active.is_(True)).order_by(User.name)
    ).all()
    return templates.TemplateResponse(request, "flow_new.html", {
        "user": user, "branches": branches, "doers": doers,
        "priorities": list(Priority),
        "span_units": clock.SPAN_UNITS, "span_choices": clock.SPAN_CHOICES,
        "field_types": FIELD_TYPES,
    })


def _read_steps(form) -> list[dict]:
    """The step cards, as the columns a FlowStep stores.

    One reader for building a flow and for editing one, so a field cannot
    mean one thing on the way in and another thing later. Cards with no title
    are skipped — an empty card somebody added and never filled in is not a
    step. The order of the list IS the order of the flow.
    """
    titles = form.getlist("step_title")
    doers = form.getlist("step_doer")
    tats = form.getlist("step_tat")
    tat_units = form.getlist("step_tat_unit")
    dfroms = form.getlist("step_due_from")
    prios = form.getlist("step_priority")
    audits = form.getlist("step_audit")
    proofs = form.getlist("step_proof")
    fields = form.getlist("step_fields")
    instr = form.getlist("step_instructions")
    nexts = form.getlist("step_next")
    fails = form.getlist("step_fail")
    decides = form.getlist("step_decision")
    yes_lbl = form.getlist("step_yes")
    no_lbl = form.getlist("step_no")

    def _pos(values, i):
        """A routing box: a step number, or 0 meaning 'the flow ends here'.

        Blank is read as 'the step after this one', which is what somebody
        who ignored the box almost certainly meant.
        """
        raw = (values[i] if i < len(values) else "").strip()
        if raw == "":
            return None
        try:
            return max(0, int(raw))
        except ValueError:
            return None

    out = []
    for i, title in enumerate(titles):
        if not title.strip():
            continue
        raw_id = (form.getlist("step_id")[i]
                  if i < len(form.getlist("step_id")) else "").strip()
        out.append({
            "step_id": int(raw_id) if raw_id.isdigit() else None,
            "position": len(out) + 1,
            "title": title.strip(),
            "instructions": (instr[i] if i < len(instr) else "").strip() or None,
            "default_doer_id": int(doers[i]) if i < len(doers) and doers[i] else None,
            "tat_hours": int(tats[i]) if i < len(tats) and tats[i] else 24,
            "tat_unit": (tat_units[i] if i < len(tat_units)
                         and tat_units[i] in clock.SPAN_UNITS else "hours"),
            "tat_value": int(tats[i]) if i < len(tats) and tats[i] else 24,
            # Not `or None`: 0 is a real answer here — "count from when the
            # run was started" — and would otherwise be read as "not set".
            "due_from_pos": _pos(dfroms, i),
            "priority": (Priority(prios[i]) if i < len(prios) and prios[i]
                         else Priority.MEDIUM),
            "requires_audit": (audits[i] == "1") if i < len(audits) else False,
            # proof is required unless the step explicitly says otherwise
            "requires_attachment": (proofs[i] != "0") if i < len(proofs) else True,
            "capture_fields": (fields[i] if i < len(fields) else "").strip() or None,
            "next_step_pos": _pos(nexts, i),
            "fail_step_pos": _pos(fails, i),
            "is_decision": (decides[i] == "1") if i < len(decides) else False,
            "pass_label": (yes_lbl[i] if i < len(yes_lbl) else "").strip() or None,
            "fail_label": (no_lbl[i] if i < len(no_lbl) else "").strip() or None,
        })
    if not out:
        raise HTTPException(400, "Add at least one step")
    return out


def _check_routes(rows: list[dict]) -> None:
    """Every route must point at a step this flow actually has.

    A route to a step that does not exist strands the run, and it is found by
    whoever is holding the bill rather than by whoever built the flow — so it
    is caught here, before anything is saved.
    """
    valid = {r["position"] for r in rows}
    for r in rows:
        where = f"Step {r['position']} ({r['title']})"
        for label, target in (("“after this step, open step”", r["next_step_pos"]),
                              ("second outcome", r["fail_step_pos"])):
            if target and target not in valid:
                raise HTTPException(
                    400, f"{where}: its {label} points at step {target}, which "
                         f"this flow does not have. Steps are numbered 1 to "
                         f"{max(valid)}.")
        if r["due_from_pos"] and r["due_from_pos"] not in valid:
            raise HTTPException(
                400, f"{where}: its clock is set to start from step "
                     f"{r['due_from_pos']}, which this flow does not have.")
        if r["due_from_pos"] == r["position"]:
            raise HTTPException(
                400, f"{where} cannot start its clock from itself.")
        if r["is_decision"] and r["fail_step_pos"] is None:
            raise HTTPException(
                400, f"{where} is a decision step, so it needs a step number "
                     "for the second outcome too (or 0 to end the flow there).")


STEP_COLUMNS = ("title", "instructions", "default_doer_id", "tat_hours",
                "tat_unit", "tat_value", "due_from_pos", "priority",
                "requires_audit", "requires_attachment", "capture_fields",
                "next_step_pos", "fail_step_pos", "is_decision",
                "pass_label", "fail_label", "position")


@router.post("/flows/new")
async def create_flow(request: Request, user: User = Depends(require_right(Right.MANAGE_FLOW)),
                      db: Session = Depends(get_db)):
    form = await request.form()
    rows = _read_steps(form)
    _check_routes(rows)

    flow = Flow(
        org_id=user.org_id,
        branch_id=int(form["branch_id"]) if form.get("branch_id") else None,
        name=form["name"].strip(),
        description=(form.get("description") or "").strip() or None,
        start_form=_read_start_form(form),
    )
    db.add(flow)
    db.flush()
    for r in rows:
        db.add(FlowStep(flow_id=flow.id,
                        **{k: r[k] for k in STEP_COLUMNS}))
    db.commit()
    return RedirectResponse(f"/flows/{flow.id}", status_code=303)


@router.get("/flows/{flow_id}", response_class=HTMLResponse)
def flow_detail(flow_id: int, request: Request, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    flow = db.get(Flow, flow_id)
    if not flow or flow.org_id != user.org_id:
        raise HTTPException(404, "Flow not found")
    instances = db.scalars(
        select(FlowInstance).where(FlowInstance.flow_id == flow.id)
        .order_by(FlowInstance.started_at.desc()).limit(50)
    ).all()
    doers = db.scalars(
        select(User).where(User.org_id == user.org_id, User.active.is_(True)).order_by(User.name)
    ).all()
    return templates.TemplateResponse(request, "flow_detail.html", {
        "user": user, "flow": flow, "instances": instances, "doers": doers,
        "progress": {i.id: flow_svc.flow_progress(i) for i in instances},
    })


def _flow_or_404(db: Session, user: User, flow_id: int) -> Flow:
    flow = db.get(Flow, flow_id)
    if not flow or flow.org_id != user.org_id:
        raise HTTPException(404, "Flow not found")
    return flow


def _run_counts(db: Session, flow: Flow) -> tuple[int, int]:
    """How many runs this flow has ever had, and how many are still going.

    The first decides whether it can be deleted, the second whether its steps
    can be taken away. They are different questions: a flow whose runs all
    finished is safe to restructure but still owns the history of what was
    done, so it is never deleted out from under it.
    """
    total = len(db.scalars(select(FlowInstance.id).where(
        FlowInstance.flow_id == flow.id)).all())
    live = len(db.scalars(select(FlowInstance.id).where(
        FlowInstance.flow_id == flow.id,
        FlowInstance.completed_at.is_(None),
        FlowInstance.cancelled_at.is_(None))).all())
    return total, live


def _mark_usage(db: Session, flow: Flow) -> None:
    """Hang a used_count on each step: how much real work points at it.

    A step nothing has ever been handed out for can be removed freely. One
    that tasks point at cannot, because deleting it would orphan work that
    is on somebody's desk or in somebody's history.
    """
    for st in flow.steps:
        st.used_count = len(db.scalars(
            select(Task.id).where(Task.flow_step_id == st.id)).all())


@router.get("/flows/{flow_id}/edit", response_class=HTMLResponse)
def edit_flow_form(flow_id: int, request: Request,
                   user: User = Depends(require_right(Right.MANAGE_FLOW)),
                   db: Session = Depends(get_db)):
    flow = _flow_or_404(db, user, flow_id)
    branches = db.scalars(select(Branch).where(Branch.org_id == user.org_id)).all()
    doers = db.scalars(
        select(User).where(User.org_id == user.org_id, User.active.is_(True))
        .order_by(User.name)).all()
    runs, live = _run_counts(db, flow)
    _mark_usage(db, flow)
    return templates.TemplateResponse(request, "flow_edit.html", {
        "user": user, "flow": flow, "branches": branches,
        "field_types": FIELD_TYPES, "doers": doers,
        "priorities": list(Priority),
        "span_units": clock.SPAN_UNITS, "span_choices": clock.SPAN_CHOICES,
        "runs": runs, "live": live,
    })


@router.post("/flows/{flow_id}/edit")
async def edit_flow(flow_id: int, request: Request,
                    user: User = Depends(require_right(Right.MANAGE_FLOW)),
                    db: Session = Depends(get_db)):
    """Change a flow's name, branch, description, start form and steps.

    Every change is for FUTURE runs. A task that has already been handed out
    carries its own copy of the title, the deadline and the person it went
    to, so rewording a step never rewrites work somebody is already holding —
    the same rule as editing a checklist rule.

    The one thing that is refused: removing a step that work has already been
    handed out for, while runs of this flow are still going. Those runs point
    at their steps by number, and taking one away strands whatever is sitting
    on that desk.
    """
    flow = _flow_or_404(db, user, flow_id)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Give the flow a name.")

    # A form that says nothing about steps is not asking for them to be
    # deleted — it is the name-and-questions form. Only a form that actually
    # carries step cards is allowed to change the steps.
    touching_steps = "step_title" in form
    rows = _read_steps(form) if touching_steps else []
    if touching_steps:
        _check_routes(rows)

    existing = {s.id: s for s in flow.steps} if touching_steps else {}
    kept = {r["step_id"] for r in rows if r["step_id"] in existing}
    _, live = _run_counts(db, flow)

    # What is being removed, and whether it may be.
    for step_id, step in existing.items():
        if step_id in kept:
            continue
        used = len(db.scalars(
            select(Task.id).where(Task.flow_step_id == step.id)).all())
        if used and live:
            raise HTTPException(
                400, f"Step {step.position} ({step.title}) cannot be removed: "
                     f"{used} task(s) were handed out for it and {live} run(s) "
                     "of this flow are still going. Finish or stop those runs "
                     "first, or change the step instead of removing it.")
        if used:
            raise HTTPException(
                400, f"Step {step.position} ({step.title}) cannot be removed: "
                     f"{used} task(s) were handed out for it and deleting it "
                     "would erase them from the record. Change what it says "
                     "instead.")

    flow.name = name
    flow.description = (form.get("description") or "").strip() or None
    flow.branch_id = int(form["branch_id"]) if form.get("branch_id") else None
    flow.start_form = _read_start_form(form)
    # The old comma line is superseded once a real form exists, so it cannot
    # come back and add duplicate questions later.
    if flow.start_form:
        flow.start_fields = None

    for step_id, step in existing.items():
        if step_id not in kept:
            db.delete(step)
    for r in rows:
        step = existing.get(r["step_id"])
        if step is None:
            db.add(FlowStep(flow_id=flow.id, **{k: r[k] for k in STEP_COLUMNS}))
        else:
            for col in STEP_COLUMNS:
                setattr(step, col, r[col])
    db.commit()
    flash.set(request, "info",
              f"{flow.name} saved — {len(rows)} step(s). Runs already going "
              "keep the steps they started with." if touching_steps
              else f"{flow.name} saved.")
    return RedirectResponse(f"/flows/{flow.id}", status_code=303)


@router.post("/flows/{flow_id}/toggle")
def toggle_flow(flow_id: int, request: Request,
                user: User = Depends(require_right(Right.MANAGE_FLOW)),
                db: Session = Depends(get_db)):
    """Stop (or allow) new runs. Nothing already running is touched."""
    flow = _flow_or_404(db, user, flow_id)
    flow.active = not flow.active
    db.commit()
    flash.set(request, "info",
              f"{flow.name} is {'on — new runs can be started' if flow.active else 'off — no new runs can be started'}.")
    return RedirectResponse(f"/flows/{flow.id}", status_code=303)


@router.post("/flows/{flow_id}/delete")
def delete_flow(flow_id: int, request: Request, confirm: str = Form(""),
                user: User = Depends(require_right(Right.MANAGE_FLOW)),
                db: Session = Depends(get_db)):
    """Delete a template that has never been used.

    A flow with runs behind it is never deleted, however the request is
    worded: those runs' tasks, proof and audit trail are the record of work
    that really happened, and a template is the cheapest thing in the
    picture. Switching it off achieves what deleting it was meant to —
    nobody can start it again — without erasing any of that.
    """
    flow = _flow_or_404(db, user, flow_id)
    if confirm != "yes":
        raise HTTPException(400, "Deleting a flow has to be confirmed.")
    runs, _ = _run_counts(db, flow)
    if runs:
        raise HTTPException(
            400, f"“{flow.name}” has {runs} run(s) behind it, so it cannot be "
                 "deleted — their tasks and proof belong to those runs. "
                 "Switch it off instead: no new run can be started from a "
                 "flow that is off.")
    name = flow.name
    db.delete(flow)            # its steps go with it (cascade)
    db.commit()
    flash.set(request, "info", f"“{name}” deleted. It had never been run.")
    return RedirectResponse("/flows", status_code=303)


@router.post("/flows/{flow_id}/start")
async def start_flow(flow_id: int, request: Request,
                     user: User = Depends(require_right(Right.CREATE_TASK)),
                     db: Session = Depends(get_db)):
    flow = _flow_or_404(db, user, flow_id)
    if not flow.active:
        raise HTTPException(
            400, f"“{flow.name}” is switched off, so no new run can be "
                 "started from it. Switch it back on under Edit flow if it "
                 "is still in use.")
    form = await request.form()
    reference = (form.get("reference") or "").strip()
    if not reference:
        raise HTTPException(400, "A reference is required (member name, lead id, invoice no.)")

    overrides = {}
    first = flow.steps[0] if flow.steps else None
    if first and form.get("first_doer"):
        overrides[first.id] = int(form["first_doer"])

    # Whatever this particular flow asks for at the start — a bill number, a
    # member name — goes straight into the run's context, where every later
    # step can read it.
    context = {}
    for field in flow.start_form_fields:
        value = (form.get(field["key"]) or "").strip()
        if not value:
            if field["required"]:
                raise HTTPException(
                    400, f"'{field['label']}' is needed to start this flow.")
            continue
        if field["type"] == "number":
            try:
                float(value)
            except ValueError:
                raise HTTPException(
                    400, f"'{field['label']}' should be a number — got '{value}'.")
        if field["type"] == "yesno" and value not in ("Yes", "No"):
            raise HTTPException(
                400, f"'{field['label']}' should be Yes or No — got '{value}'.")
        if field["type"] == "select" and value not in field["options"]:
            raise HTTPException(
                400, f"'{value}' is not one of the choices for "
                     f"'{field['label']}'.")
        context[field["label"]] = value

    inst = flow_svc.start_flow(db, flow, reference, user, overrides, context)
    return RedirectResponse(f"/flows/instance/{inst.id}", status_code=303)


# ------------------------------------------------- pause, resume, stop ----
# The error you get trying to delete a step said "cancel the flow run
# instead" — and there was nothing anywhere that could do it. These are that
# missing thing, plus the pause people actually want more often: a bill nobody
# has sent yet is not an abandoned run, it is a wait.
def _run_for_control(db: Session, user: User, instance_id: int) -> FlowInstance:
    inst = db.get(FlowInstance, instance_id)
    if not inst or inst.org_id != user.org_id:
        raise HTTPException(404, "That FMS run was not found.")
    return inst


@router.post("/flows/instance/{instance_id}/hold")
def hold_instance(instance_id: int, request: Request, reason: str = Form(""),
                  user: User = Depends(require_right(Right.MANAGE_FLOW)),
                  db: Session = Depends(get_db)):
    inst = _run_for_control(db, user, instance_id)
    if inst.completed_at:
        raise HTTPException(400, "This run has already finished.")
    if inst.cancelled_at:
        raise HTTPException(400, "This run was stopped — it cannot be paused.")
    if inst.held_at:
        raise HTTPException(400, "This run is already on hold.")
    n = flow_svc.hold_run(db, inst, user, reason)
    db.commit()
    flash.set(request, "held", f"{inst.reference} — {n} step(s) paused")
    return RedirectResponse(f"/flows/instance/{instance_id}", status_code=303)


@router.post("/flows/instance/{instance_id}/resume")
def resume_instance(instance_id: int, request: Request, shift: str = Form("1"),
                    user: User = Depends(require_right(Right.MANAGE_FLOW)),
                    db: Session = Depends(get_db)):
    inst = _run_for_control(db, user, instance_id)
    if not inst.held_at:
        raise HTTPException(400, "This run is not on hold.")
    n, days = flow_svc.resume_run(db, inst, user, shift_deadlines=(shift != "0"))
    db.commit()
    detail = f"{inst.reference} — {n} step(s) back on"
    if shift != "0" and days:
        detail += f", planned dates moved on {days} day(s)"
    flash.set(request, "resumed", detail)
    return RedirectResponse(f"/flows/instance/{instance_id}", status_code=303)


@router.post("/flows/instance/{instance_id}/stop")
def stop_instance(instance_id: int, request: Request, reason: str = Form(""),
                  confirm: str = Form(""),
                  user: User = Depends(require_right(Right.MANAGE_FLOW)),
                  db: Session = Depends(get_db)):
    inst = _run_for_control(db, user, instance_id)
    if inst.completed_at:
        raise HTTPException(400, "This run has already finished.")
    if inst.cancelled_at:
        raise HTTPException(400, "This run was already stopped.")
    # Stopping cannot be undone, so it takes a deliberate confirmation rather
    # than one mis-aimed click on a page full of buttons.
    if confirm != "yes":
        raise HTTPException(400, "Stopping a run has to be confirmed.")
    n = flow_svc.stop_run(db, inst, user, reason)
    db.commit()
    flash.set(request, "stopped", f"{inst.reference} — {n} open step(s) cancelled")
    return RedirectResponse(f"/flows/instance/{instance_id}", status_code=303)


@router.get("/flows/instance/{instance_id}", response_class=HTMLResponse)
def instance_detail(instance_id: int, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    inst = db.get(FlowInstance, instance_id)
    if not inst or inst.org_id != user.org_id:
        raise HTTPException(404, "Not found")
    tasks = db.scalars(
        select(Task).where(Task.flow_instance_id == inst.id).order_by(Task.created_at)
    ).all()
    try:
        started_with = json.loads(inst.context or "{}")
    except json.JSONDecodeError:
        started_with = {}
    return templates.TemplateResponse(request, "flow_instance.html", {
        "user": user, "inst": inst, "tasks": tasks,
        "progress": flow_svc.flow_progress(inst),
        "can_control": user.has(Right.MANAGE_FLOW),
        "started_with": started_with,
    })
