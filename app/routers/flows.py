from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, manager_up, require_right
from ..models import Flow, FlowStep, FlowInstance, Task, User, Branch, Priority, Right
from ..services import flows as flow_svc
from ..templating import templates

router = APIRouter()


@router.get("/flows", response_class=HTMLResponse)
def flow_list(request: Request, user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    flows = db.scalars(
        select(Flow).where(Flow.org_id == user.org_id).order_by(Flow.name)
    ).all()
    running = db.scalars(
        select(FlowInstance)
        .where(FlowInstance.org_id == user.org_id, FlowInstance.completed_at.is_(None))
        .order_by(FlowInstance.started_at.desc()).limit(50)
    ).all()
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
        "user": user, "branches": branches, "doers": doers, "priorities": list(Priority),
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
    )
    db.add(flow)
    db.flush()

    # steps arrive as parallel arrays: step_title[], step_doer[], ...
    titles = form.getlist("step_title")
    doers = form.getlist("step_doer")
    tats = form.getlist("step_tat")
    prios = form.getlist("step_priority")
    audits = form.getlist("step_audit")
    proofs = form.getlist("step_proof")
    fields = form.getlist("step_fields")
    instr = form.getlist("step_instructions")

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
            priority=Priority(prios[i]) if i < len(prios) and prios[i] else Priority.MEDIUM,
            requires_audit=(audits[i] == "1") if i < len(audits) else False,
            # proof is required unless the step explicitly says otherwise
            requires_attachment=(proofs[i] != "0") if i < len(proofs) else True,
            capture_fields=(fields[i] if i < len(fields) else "").strip() or None,
        ))

    if pos == 0:
        raise HTTPException(400, "Add at least one step")
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

    inst = flow_svc.start_flow(db, flow, reference, user, overrides)
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
