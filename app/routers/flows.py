from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
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
def flow_list(request: Request, export: str = "",
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    flows = db.scalars(
        select(Flow).where(Flow.org_id == user.org_id).order_by(Flow.name)
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


@router.post("/flows/new")
async def create_flow(request: Request, user: User = Depends(require_right(Right.MANAGE_FLOW)),
                      db: Session = Depends(get_db)):
    form = await request.form()
    flow = Flow(
        org_id=user.org_id,
        branch_id=int(form["branch_id"]) if form.get("branch_id") else None,
        name=form["name"].strip(),
        description=(form.get("description") or "").strip() or None,
        start_form=_read_start_form(form),
    )
    db.add(flow)
    db.flush()

    # steps arrive as parallel arrays: step_title[], step_doer[], ...
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

    pos = 0
    for i, title in enumerate(titles):
        if not title.strip():
            continue
        pos += 1
        db.add(FlowStep(
            flow_id=flow.id,
            position=pos,
            title=title.strip(),
            instructions=(instr[i] if i < len(instr) else "").strip() or None,
            default_doer_id=int(doers[i]) if i < len(doers) and doers[i] else None,
            tat_hours=int(tats[i]) if i < len(tats) and tats[i] else 24,
            tat_unit=(tat_units[i] if i < len(tat_units)
                      and tat_units[i] in clock.SPAN_UNITS else "hours"),
            tat_value=int(tats[i]) if i < len(tats) and tats[i] else 24,
            due_from_pos=_pos(dfroms, i) or None,
            priority=Priority(prios[i]) if i < len(prios) and prios[i] else Priority.MEDIUM,
            requires_audit=(audits[i] == "1") if i < len(audits) else False,
            # proof is required unless the step explicitly says otherwise
            requires_attachment=(proofs[i] != "0") if i < len(proofs) else True,
            capture_fields=(fields[i] if i < len(fields) else "").strip() or None,
            next_step_pos=_pos(nexts, i),
            fail_step_pos=_pos(fails, i),
            is_decision=(decides[i] == "1") if i < len(decides) else False,
            pass_label=(yes_lbl[i] if i < len(yes_lbl) else "").strip() or None,
            fail_label=(no_lbl[i] if i < len(no_lbl) else "").strip() or None,
        ))

    if pos == 0:
        raise HTTPException(400, "Add at least one step")
    db.flush()

    # A route pointing at a step that does not exist would strand the run, so
    # it is caught here rather than discovered by whoever is holding the bill.
    valid = {s.position for s in db.scalars(
        select(FlowStep).where(FlowStep.flow_id == flow.id)).all()}
    for st in db.scalars(select(FlowStep).where(FlowStep.flow_id == flow.id)).all():
        for label, target in (("goes to", st.next_step_pos),
                              ("rejected route", st.fail_step_pos)):
            if target and target not in valid:
                raise HTTPException(
                    400, f"Step {st.position} ({st.title}): its {label} points at "
                         f"step {target}, which this flow does not have. "
                         f"Steps are numbered 1 to {max(valid)}.")
        if st.due_from_pos and st.due_from_pos not in valid:
            raise HTTPException(
                400, f"Step {st.position} ({st.title}): its planned date is tied "
                     f"to step {st.due_from_pos}, which this flow does not have.")
        if st.due_from_pos == st.position:
            raise HTTPException(
                400, f"Step {st.position} ({st.title}) cannot take its planned "
                     "date from itself.")
        if st.is_decision and st.fail_step_pos is None:
            raise HTTPException(
                400, f"Step {st.position} ({st.title}) is a decision step, so it "
                     "needs a step number for the second outcome too "
                     "(or 0 to end the flow there).")
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


@router.get("/flows/{flow_id}/edit", response_class=HTMLResponse)
def edit_flow_form(flow_id: int, request: Request,
                   user: User = Depends(require_right(Right.MANAGE_FLOW)),
                   db: Session = Depends(get_db)):
    flow = db.get(Flow, flow_id)
    if not flow or flow.org_id != user.org_id:
        raise HTTPException(404, "Flow not found")
    branches = db.scalars(select(Branch).where(Branch.org_id == user.org_id)).all()
    return templates.TemplateResponse(request, "flow_edit.html", {
        "user": user, "flow": flow, "branches": branches,
        "field_types": FIELD_TYPES,
    })


@router.post("/flows/{flow_id}/edit")
async def edit_flow(flow_id: int, request: Request,
                    user: User = Depends(require_right(Right.MANAGE_FLOW)),
                    db: Session = Depends(get_db)):
    """Change a flow's name, branch, description and start form.

    Steps are deliberately not editable here. A run in progress points at
    its steps by position, so re-numbering them underneath a live run would
    reroute work already on somebody's plate — that needs its own careful
    screen rather than being bolted onto this one.
    """
    flow = db.get(Flow, flow_id)
    if not flow or flow.org_id != user.org_id:
        raise HTTPException(404, "Flow not found")
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Give the flow a name.")
    flow.name = name
    flow.description = (form.get("description") or "").strip() or None
    flow.branch_id = int(form["branch_id"]) if form.get("branch_id") else None
    flow.start_form = _read_start_form(form)
    # The old comma line is superseded once a real form exists, so it cannot
    # come back and add duplicate questions later.
    if flow.start_form:
        flow.start_fields = None
    db.commit()
    return RedirectResponse(f"/flows/{flow.id}", status_code=303)


@router.post("/flows/{flow_id}/start")
async def start_flow(flow_id: int, request: Request,
                     user: User = Depends(require_right(Right.CREATE_TASK)),
                     db: Session = Depends(get_db)):
    flow = db.get(Flow, flow_id)
    if not flow or flow.org_id != user.org_id:
        raise HTTPException(404, "Flow not found")
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


@router.get("/flows/instance/{instance_id}", response_class=HTMLResponse)
def instance_detail(instance_id: int, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    inst = db.get(FlowInstance, instance_id)
    if not inst or inst.org_id != user.org_id:
        raise HTTPException(404, "Not found")
    tasks = db.scalars(
        select(Task).where(Task.flow_instance_id == inst.id).order_by(Task.created_at)
    ).all()
    return templates.TemplateResponse(request, "flow_instance.html", {
        "user": user, "inst": inst, "tasks": tasks,
        "progress": flow_svc.flow_progress(inst),
    })
