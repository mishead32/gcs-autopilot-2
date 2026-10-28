from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import clock, flash, search
import json

from ..db import get_db
from ..deps import current_user, manager_up, require_right
from ..models import (Flow, FlowStep, FlowInstance, Task, User, Branch, Department,
                      Attachment,
                      Priority,
                      Right, FIELD_TYPES, BUILDER_FIELD_TYPES,
                      RUN_PREFIX, ref_text, CONTEXT_PEOPLE, context_answers)
from ..services import (flows as flow_svc, flowbulk, flowform, xlsx, storage,
                        shrink)
from ..templating import templates

router = APIRouter()


def _ref_typed(raw: str) -> str:
    """What somebody meant when they typed it in a search box.

    "FMS-12", "fms 12", "FMS12" and a bare "12" are all the same submission
    to the person typing, and a search that only matches the first of those
    is a search people stop using.
    """
    text = (raw or "").upper().replace(" ", "").replace("-", "")
    if text.startswith(RUN_PREFIX):
        text = text[len(RUN_PREFIX):]
    if text.isdigit():
        return ref_text(RUN_PREFIX, int(text))
    return (raw or "").upper().replace(" ", "")


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
def flow_list(request: Request, export: str = "", q: str = "", company: str = "",
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    text_q = search.clean(q)
    flows = db.scalars(
        search.apply(select(Flow).where(Flow.org_id == user.org_id),
                     [Flow.name, Flow.description], text_q)
        .order_by(Flow.name)
    ).all()
    # The running list answers the search too, by FMS number or by what the
    # run is about. A box that searches the templates but not the runs tells
    # somebody looking for FMS-12 that there is no such thing.
    run_q = select(FlowInstance).where(
        FlowInstance.org_id == user.org_id,
        FlowInstance.completed_at.is_(None))
    if text_q:
        run_q = run_q.where(or_(
            func.upper(FlowInstance.ref) == _ref_typed(text_q),
            search.clause([FlowInstance.reference], text_q),
        ))
    # One list with Bodyzone's flows, Spa Kora's and the school's mixed
    # together is three people each reading past two thirds of it. Narrowed
    # to one company, or grouped company by company when it is not.
    #
    # "group" is its own answer rather than a blank: a flow built for every
    # company is a real category — the HR and accounts ones are — and
    # "no company" and "all companies" are the same rows read two ways.
    branches = db.scalars(select(Branch).where(Branch.org_id == user.org_id)
                          .order_by(Branch.name)).all()
    pick = (company or "").strip()
    if pick == "group":
        flows = [f for f in flows if f.branch_id is None]
    elif pick.isdigit():
        flows = [f for f in flows if f.branch_id == int(pick)]
    else:
        pick = ""
    if pick == "group":
        run_q = run_q.where(FlowInstance.branch_id.is_(None))
    elif pick:
        run_q = run_q.where(FlowInstance.branch_id == int(pick))
    running = db.scalars(
        run_q.order_by(FlowInstance.started_at.desc()).limit(50)).all()
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
                ("How it will be done", lambda r: r[1].how_to or ""),
                ("Fields captured", lambda r: r[1].capture_fields or ""),
            ], steps, "One row per step, in the order the flow runs them."),
            ("Running now", [
                ("Flow", lambda i: i.flow.name),
                ("FMS no.", lambda i: i.label),
                ("What it is about", lambda i: i.reference),
                ("Started", lambda i: i.started_at),
                ("Started by", lambda i: i.started_by.name if i.started_by else ""),
                ("Steps done", lambda i: flow_svc.flow_progress(i)[0]),
                ("Steps total", lambda i: flow_svc.flow_progress(i)[1]),
                ("Currently at step", lambda i: i.current_position),
            ], running, "Runs that have not finished yet."),
        ])

    # How many runs each flow has ever had, counted in one query rather than
    # one per flow — the list is the page everybody lands on.
    counts = dict(db.execute(
        select(FlowInstance.flow_id, func.count())
        .where(FlowInstance.org_id == user.org_id)
        .group_by(FlowInstance.flow_id)).all())
    def _by_company(rows):
        """[(company name, rows)] — named companies first, group-wide last.

        A heading with nothing under it is noise, so a company with no
        flows is left out entirely rather than printed empty.
        """
        out = []
        for b in branches:
            got = [r for r in rows if r.branch_id == b.id]
            if got:
                out.append((b.name, got))
        shared = [r for r in rows if r.branch_id is None]
        if shared:
            out.append(("Every company", shared))
        return out

    return templates.TemplateResponse(request, "flows.html", {
        "q": text_q, "can_manage": user.has(Right.MANAGE_FLOW),
        "user": user, "flows": flows, "running": running,
        "branches": branches, "company": pick,
        "flow_groups": _by_company(flows),
        "run_groups": _by_company(running),
        "runs": {f.id: counts.get(f.id, 0) for f in flows},
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
        "span_units": clock.unit_labels(), "span_choices": clock.span_options(),
        "field_types": BUILDER_FIELD_TYPES,
        "dept_names": _position_names(db, user.org_id),
        # A brand-new flow has no questions yet; the page builds the list
        # again in the browser as questions are typed.
        "person_fields": [],
    })


def _position_names(db: Session, org_id: int) -> list[str]:
    """Every position that exists anywhere in the group, once each.

    Departments are stored per company — there is an Accounts row under
    Spa Kora and another under BIPS — but a step names the POSITION, not one
    company's copy of it. So the list is the distinct names, and whichever
    company a run is for supplies the person.
    """
    rows = db.scalars(select(Department.name)
                      .where(Department.org_id == org_id)).all()
    return sorted({(n or "").strip() for n in rows if (n or "").strip()},
                  key=str.lower)


def _read_steps(form) -> list[dict]:
    """The step cards, as the columns a FlowStep stores.

    One reader for building a flow and for editing one, so a field cannot
    mean one thing on the way in and another thing later. Cards with no title
    are skipped — an empty card somebody added and never filled in is not a
    step. The order of the list IS the order of the flow.
    """
    titles = form.getlist("step_title")
    doers = form.getlist("step_doer")
    more_doers = form.getlist("step_doers")
    tats = form.getlist("step_tat")
    tat_units = form.getlist("step_tat_unit")
    dfroms = form.getlist("step_due_from")
    prios = form.getlist("step_priority")
    audits = form.getlist("step_audit")
    proofs = form.getlist("step_proof")
    fields = form.getlist("step_fields")
    instr = form.getlist("step_instructions")
    howto = form.getlist("step_howto")
    nexts = form.getlist("step_next")
    fails = form.getlist("step_fail")
    decides = form.getlist("step_decision")
    yes_lbl = form.getlist("step_yes")
    no_lbl = form.getlist("step_no")

    def _people(values, i, exclude: str = "") -> str | None:
        """'3,7,7,x' -> '3,7'. One hidden box per step, comma joined.

        Cleaned here rather than trusted: the box is written by script, and
        a step that quietly carried a stray id would hand work to somebody
        nobody chose. exclude drops whoever is already the primary doer.
        """
        raw = (values[i] if i < len(values) else "") or ""
        skip = (exclude or "").strip()
        out = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and part != skip and part not in out:
                out.append(part)
        return ",".join(out) or None

    def _is_field(values, i) -> bool:
        return (values[i] if i < len(values) else "").strip().startswith("field:")

    def _field_label(values, i) -> str | None:
        """'field:Bill Date' -> 'Bill Date'. Anything else -> None."""
        raw = (values[i] if i < len(values) else "").strip()
        return raw[6:].strip()[:160] or None if raw.startswith("field:") else None

    def _dept(i) -> str | None:
        """'dept:Accounts' -> 'Accounts'. A name or a blank -> None."""
        raw = (doers[i] if i < len(doers) else "").strip()
        return raw[5:].strip()[:120] or None if raw.startswith("dept:") else None

    def _doer_field(i) -> str | None:
        """'field:PT trainer' -> 'PT trainer'. Anything else -> None.

        The step goes to whoever THIS run named in that question, which is
        the only way a step's doer can differ from run to run.
        """
        raw = (doers[i] if i < len(doers) else "").strip()
        return raw[6:].strip()[:160] or None if raw.startswith("field:") else None

    def _doer_id(i) -> int | None:
        raw = (doers[i] if i < len(doers) else "").strip()
        return int(raw) if raw.isdigit() else None

    def _unit(i) -> str:
        raw = tat_units[i] if i < len(tat_units) else ""
        return raw if raw in clock.SPAN_UNITS else "hours"

    def _tat(values, i) -> int:
        """The second TAT box as a number — 24 when it is not one.

        "Take timestamp from form" posts a question label through this same
        box, which is deliberate: one box, one name, one value per step, so
        the server's parallel lists stay lined up with the step numbers. It
        is read as the question by _clock_from below and as 0 here.
        """
        raw = (values[i] if i < len(values) else "").strip()
        try:
            return int(raw)
        except ValueError:
            return 0 if raw else 24

    def _clock_from(i) -> tuple[int | None, str | None]:
        """Where this step's clock starts: (step position, form question).

        Two boxes can set it, and which one is in charge depends on the TAT
        unit. Normally it is the "start this step's clock from" dropdown.
        With the unit set to "take the timestamp from the form" the question
        named in the TAT box IS the clock, and the anchor box is hidden on
        the page — so it is ignored here too rather than left to contradict
        what the person chose.
        """
        if _unit(i) == clock.FORM_UNIT:
            return None, _field_label(tats, i)
        return (None if _is_field(dfroms, i) else _pos(dfroms, i),
                _field_label(dfroms, i))

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
            "default_doer_id": _doer_id(i),
            # "Accounts", resolved to a person per company when the run
            # starts. Mutually exclusive with a name by construction: it is
            # one dropdown with one value.
            "doer_dept": _dept(i),
            # "PT trainer", resolved to whoever THAT run named. The third
            # and last answer to "who does this step", and the only one
            # that can differ run to run. Mutually exclusive with the other
            # two by construction: it is one dropdown with one value.
            "doer_field": _doer_field(i),
            # Stripped of whoever is already named in "Who does it": the same
            # person on a step twice is one person, and leaving them in both
            # places means changing the primary doer would quietly leave the
            # old one on the step as an extra.
            "extra_doer_ids": _people(more_doers, i,
                                      doers[i] if i < len(doers) else ""),
            "tat_hours": _tat(tats, i),
            "tat_unit": _unit(i),
            "tat_value": _tat(tats, i),
            # Not `or None`: 0 is a real answer here — "count from when the
            # run was started" — and would otherwise be read as "not set".
            #
            # The same box can also name a date question on the start form,
            # posted as "field:Bill Date". The two are mutually exclusive by
            # construction: it is one dropdown with one value.
            "due_from_pos": _clock_from(i)[0],
            "due_from_field": _clock_from(i)[1],
            "priority": (Priority(prios[i]) if i < len(prios) and prios[i]
                         else Priority.MEDIUM),
            "requires_audit": (audits[i] == "1") if i < len(audits) else False,
            # proof is required unless the step explicitly says otherwise
            "requires_attachment": (proofs[i] != "0") if i < len(proofs) else True,
            "how_to": (howto[i] if i < len(howto) else "").strip() or None,
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

        # A decision step may have exactly one doer. Two people choosing two
        # different outcomes is a flow that has to go two ways at once, and
        # there is no honest answer to it — better refused while the flow is
        # being built than discovered by whoever is holding the bill.
        if r["is_decision"] and r.get("extra_doer_ids"):
            raise HTTPException(
                400, f"{where} is a decision step, so only one person can do "
                     "it — two people could pick two different outcomes and "
                     "the flow would have to go both ways at once. Either "
                     "give it a single doer, or make it a normal step.")
        for label, target in (("“after this step, open step”", r["next_step_pos"]),
                              ("second outcome", r["fail_step_pos"])):
            if target and target not in valid:
                raise HTTPException(
                    400, f"{where}: its {label} points at step {target}, which "
                         f"this flow does not have. Steps are numbered 1 to "
                         f"{max(valid)}.")
        # "Take the timestamp from the form" has to say WHICH question.
        # Saved without one there is nothing to be due at, and the step would
        # quietly fall back to a day from whenever it opened — a deadline
        # nobody set and nobody would understand.
        if r["tat_unit"] == clock.FORM_UNIT and not r["due_from_field"]:
            raise HTTPException(
                400, f"{where}: its TAT is set to take the timestamp from the "
                     "start form, so it needs to say which question. Pick one "
                     "in the box beside it — and if the form has no date "
                     "question yet, add one (answer type: Date, or Date and "
                     "time) and it will appear there.")
        # And counting BACKWARDS has to say which date it is counting back
        # from, for the same reason — but with a sharper edge. The anchor
        # box also offers "when this step opens" and "step 2's planned
        # date", and neither is a date on the form, so a step saved that way
        # would count back from nothing and fall back to a day from now.
        # That is a deadline twenty days wrong, on a step whose whole job is
        # to land before something.
        if (r["tat_unit"] in (clock.BEFORE_UNIT, clock.AFTER_UNIT)
                and not r["due_from_field"]):
            _way = ("BEFORE" if r["tat_unit"] == clock.BEFORE_UNIT else "AFTER")
            raise HTTPException(
                400, f"{where}: it is set to be due a number of days {_way} a "
                     "date, so it needs to say which date — and that has to "
                     "be a date question on the start form, not another "
                     "step's planned date. Pick one under “Start this step's "
                     "clock from”. If the form has no date question yet, add "
                     "one (answer type: Date, or Date and time) and it will "
                     "appear there.")
        if r["due_from_pos"] and r["due_from_pos"] not in valid:
            raise HTTPException(
                400, f"{where}: its clock is set to start from step "
                     f"{r['due_from_pos']}, which this flow does not have.")
        if r["due_from_pos"] == r["position"]:
            raise HTTPException(
                400, f"{where} cannot start its clock from itself.")
        # A decision step assigned to a POSITION used to be refused here.
        # That was the wrong moment to ask. While a flow is being built
        # nobody knows which company it will run for, so the question
        # "could this be two people?" has no answer yet and the refusal was
        # on a maybe — which left one flow per company as the only way to
        # use a position on a decision step, the exact thing positions
        # exist to avoid.
        #
        # It is checked when a RUN starts instead, where the company is
        # known and the answer is a fact. See _one_decider below.
        # Named extras alongside a position would be dropped when the run
        # resolves the position, so they are refused rather than ignored.
        if r["doer_dept"] and r.get("extra_doer_ids"):
            raise HTTPException(
                400, f"{where} is assigned to “{r['doer_dept']}”, so the "
                     "names under “Also done by” cannot be used too — the "
                     "position already decides who gets it. Clear the names, "
                     "or give the step a person instead of a position.")
        if r["is_decision"] and r["fail_step_pos"] is None:
            raise HTTPException(
                400, f"{where} is a decision step, so it needs a step number "
                     "for the second outcome too (or 0 to end the flow there).")


STEP_COLUMNS = ("title", "instructions", "how_to", "default_doer_id", "doer_dept",
                "doer_field",
                "tat_hours",
                "tat_unit", "tat_value", "due_from_pos", "due_from_field", "priority",
                "extra_doer_ids",
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
    # ?built=new says the save went through, which is the only moment the
    # browser may throw away the draft of this flow. Clearing it on submit
    # instead lost twenty minutes of work every time the save was refused.
    return RedirectResponse(f"/flows/{flow.id}?built=new", status_code=303)


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
    branches = db.scalars(
        select(Branch).where(Branch.org_id == user.org_id).order_by(Branch.name)
    ).all()
    return templates.TemplateResponse(request, "flow_detail.html", {
        "user": user, "flow": flow, "instances": instances[:8], "doers": doers,
        "branches": branches,
        # How many runs in total, so the card can say so without listing
        # fifty of them on a page whose job is the start form.
        "submissions": db.scalar(select(func.count()).select_from(FlowInstance)
                                 .where(FlowInstance.flow_id == flow.id)) or 0,
        # Which positions this flow names, so the start form can say what
        # the company it is about to run for will have to have somebody in.
        "positions": [s.doer_dept for s in flow.steps if s.doer_dept],
        # Everybody a person question may name, for its dropdown.
        "staff": flowform.staff(db, user.org_id),
        # A file question needs somewhere to put the file. Saying so on the
        # form beats a picker that silently loses the document.
        "uploads_on": storage.uploads_available(),
        "upload_accept": storage.ACCEPT_ATTR,
        "upload_max": storage.MAX_UPLOAD_MB,
        "progress": {i.id: flow_svc.flow_progress(i) for i in instances},
    })


@router.get("/flows/{flow_id}/submissions", response_class=HTMLResponse)
def flow_submissions(flow_id: int, request: Request, state: str = "",
                     q: str = "", who: str = "", export: str = "",
                     user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    """Every run of one flow, with the answers it was started with.

    The question this page exists for is "what has been submitted to this
    form" — which the run pages could only answer one run at a time, by
    opening fifty of them. Here each run is a row and each start-form
    question is a column, which is the shape people already have in their
    heads: a register.

    Open to anybody who can sign in, like starting a flow. Be aware of what
    that means before building a form here: whatever a start question
    captures, everybody who can sign in can read in this table.
    """
    flow = _flow_or_404(db, user, flow_id)

    rows = db.scalars(
        select(FlowInstance).where(FlowInstance.flow_id == flow.id)
        .order_by(FlowInstance.started_at.desc())
    ).all()

    # The answers, per run. A flow's questions change over its life, so the
    # columns are the questions it asks NOW followed by any other label the
    # runs actually carry — an answer to a question since removed is still
    # data somebody submitted, and dropping it off the table would quietly
    # lose it.
    answers: dict[int, dict] = {}
    for inst in rows:
        try:
            data = json.loads(inst.context or "{}")
        except json.JSONDecodeError:
            data = {}
        answers[inst.id] = context_answers(data) if isinstance(data, dict) else {}

    columns = [f["label"] for f in flow.start_form_fields]
    for data in answers.values():
        for label in data:
            if label not in columns:
                columns.append(label)

    # Filters. Applied in Python rather than SQL because "state" is a
    # property of three columns read together and "the answers" are JSON —
    # and because a flow with thousands of runs is not a thing this system
    # has, or should have without somebody noticing first.
    text_q = search.clean(q)
    if state == "running":
        rows = [i for i in rows if i.completed_at is None
                and i.cancelled_at is None and not i.on_hold]
    elif state == "done":
        rows = [i for i in rows if i.completed_at is not None]
    elif state == "held":
        rows = [i for i in rows if i.on_hold]
    elif state == "stopped":
        rows = [i for i in rows if i.cancelled_at is not None]
    if who == "mine":
        rows = [i for i in rows if i.started_by_id == user.id]
    if text_q:
        needle = text_q.lower()
        typed = _ref_typed(text_q)
        rows = [i for i in rows
                if (i.ref or "").upper() == typed
                or needle in (i.reference or "").lower()
                or any(needle in str(v).lower()
                       for v in answers[i.id].values())]

    if xlsx.wants(export):
        cols = [
            ("FMS no.", lambda i: i.label),
            ("What it is about", lambda i: i.reference),
            ("Started by", lambda i: i.started_by.name if i.started_by else ""),
            ("Started on", lambda i: i.started_at),
            ("Company", lambda i: i.branch.name if i.branch else ""),
            ("State", lambda i: i.state),
            ("Step reached", lambda i: i.current_position),
            ("Steps done", lambda i: flow_svc.flow_progress(i)[0]),
            ("Steps total", lambda i: flow_svc.flow_progress(i)[1]),
            ("Finished on", lambda i: i.completed_at),
        ] + [
            # Bound as a default argument, not closed over: a loop variable
            # read inside a lambda is whatever it ended up as, so without
            # this every column would print the last question's answer.
            (label, (lambda i, _l=label: answers[i.id].get(_l, "")))
            for label in columns
        ]
        return xlsx.book(f"fms-{flow.name[:20]}-submissions", [
            ("Submissions", cols, rows,
             "One row per run of this flow, newest first, with the answers "
             "it was started with."),
        ])

    return templates.TemplateResponse(request, "flow_submissions.html", {
        "user": user, "flow": flow, "rows": rows, "answers": answers,
        "columns": columns, "state": state, "q": text_q, "who": who,
        "progress": {i.id: flow_svc.flow_progress(i) for i in rows},
        "total": db.scalar(select(func.count()).select_from(FlowInstance)
                           .where(FlowInstance.flow_id == flow.id)) or 0,
    })


# ------------------------------------------------- starting many at once --
# A renewal flow is run for four hundred members. Filling the start form
# four hundred times is not a thing anybody does — they stop using the
# system and keep the list in Excel, which is where it was before.

@router.get("/flows/{flow_id}/bulk", response_class=HTMLResponse)
def bulk_form(flow_id: int, request: Request, user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    flow = _flow_or_404(db, user, flow_id)
    return templates.TemplateResponse(request, "flow_bulk.html", {
        "user": user, "flow": flow, "parsed": None,
        "cols": flowbulk.columns(flow, flowbulk.needs_company(flow)),
        "uploads_on": storage.uploads_available(),
        "file_questions": [f["label"] for f in flow.start_form_fields
                           if f["type"] == "file"],
    })


@router.get("/flows/{flow_id}/bulk/template")
def bulk_template(flow_id: int, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    """The file to fill in — its headings are this flow's own questions."""
    flow = _flow_or_404(db, user, flow_id)
    blob = flowbulk.template(db, flow)
    # ASCII only, and a plain hyphen. An HTTP header is latin-1, so a flow
    # whose name carries a dash somebody pasted from Word — or any Hindi
    # in it — would otherwise fail to download at all rather than download
    # under a slightly wrong name.
    safe = "".join(c for c in flow.name
                   if c.isascii() and (c.isalnum() or c in " -_")).strip()
    return xlsx.download(blob, f"{safe[:40] or 'flow'} - rows")


@router.post("/flows/{flow_id}/bulk", response_class=HTMLResponse)
async def bulk_check(flow_id: int, request: Request,
                     user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    """Read the file and show what is ready and what needs fixing.

    Nothing is created here. The whole point of a separate check is that
    somebody sees four hundred rows resolved — including WHO each step will
    go to — before any of it exists.
    """
    flow = _flow_or_404(db, user, flow_id)
    form = await request.form()
    picked = form.get("file")
    if not _is_upload(picked):
        raise HTTPException(400, "Choose the filled-in .xlsx file first.")
    blob = await picked.read()
    try:
        parsed = flowbulk.parse(db, flow, blob)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # Held in the session so confirming does not need the file again. Only
    # the answers, which are small; the file itself is not kept.
    request.session["flowbulk"] = {
        "flow_id": flow.id,
        "rows": [{"number": r.number, "context": r.context,
                  "branch_id": r.branch_id, "name": r.name}
                 for r in parsed.ready],
    }
    return templates.TemplateResponse(request, "flow_bulk.html", {
        "user": user, "flow": flow, "parsed": parsed,
        "cols": flowbulk.columns(flow, flowbulk.needs_company(flow)),
        "uploads_on": storage.uploads_available(),
        "file_questions": [f["label"] for f in flow.start_form_fields
                           if f["type"] == "file"],
    })


@router.post("/flows/{flow_id}/bulk/start")
def bulk_start(flow_id: int, request: Request,
               user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    """Start the rows the check said were ready."""
    flow = _flow_or_404(db, user, flow_id)
    held = request.session.pop("flowbulk", None) or {}
    if held.get("flow_id") != flow.id or not held.get("rows"):
        raise HTTPException(
            400, "There is nothing waiting to be started. Upload the file "
                 "again — the check is only kept for the page that shows it.")
    if not flow.active:
        raise HTTPException(
            400, f"“{flow.name}” is switched off, so no new run can be "
                 "started from it.")

    made = []
    for row in held["rows"]:
        try:
            made.append(flow_svc.start_flow(
                db, flow, "", user, context=dict(row.get("context") or {}),
                branch_id=row.get("branch_id")))
        except Exception:                             # noqa: BLE001
            db.rollback()
    flash.set(request, "saved",
              f"{len(made)} run(s) started from the file"
              + (f", {len(held['rows']) - len(made)} could not be"
                 if len(made) != len(held["rows"]) else ""))
    return RedirectResponse(f"/flows/{flow.id}/submissions", status_code=303)


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
        "field_types": BUILDER_FIELD_TYPES, "doers": doers,
        "priorities": list(Priority),
        "span_units": clock.unit_labels(), "span_choices": clock.span_options(),
        "dept_names": _position_names(db, user.org_id),
        # The person questions this flow already asks, so a step can be set
        # to follow one of them.
        "person_fields": [f["label"] for f in flow.start_form_fields
                          if f["type"] == "person"],
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
    return RedirectResponse(f"/flows/{flow.id}?built=edit", status_code=303)


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
                     user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    """Start a run. Open to anybody who can sign in.

    It used to need the "Delegate tasks" right, because starting a flow does
    hand work to other people. But that is what a flow IS: the whole point
    of writing one down is that the office already agreed who does each step
    and in what order. The person starting it is not choosing to give
    anybody work — they are saying a bill arrived, or somebody applied for
    leave, and the flow decides the rest.

    Gating it on the delegate right meant only managers could report that a
    bill had arrived, which put a manager in front of every form and is the
    opposite of what a flow is for. Editing a flow, holding a run and
    stopping one still need MANAGE_FLOW: deciding what the steps ARE is a
    different act from reporting that the thing happened.
    """
    flow = _flow_or_404(db, user, flow_id)
    if not flow.active:
        raise HTTPException(
            400, f"“{flow.name}” is switched off, so no new run can be "
                 "started from it. Switch it back on under Edit flow if it "
                 "is still in use.")
    form = await request.form()
    # No longer asked for. The start form shows the flow's own questions and
    # nothing else; the run is named from the answers, and from its own FMS
    # number when there are none. Still read from the form so anything that
    # posts one — an older page left open in a tab, a script — is honoured
    # rather than ignored.
    reference = (form.get("reference") or "").strip()

    # No "first step doer (override)" any more. The flow already says who
    # does step 1 — that is the whole point of writing the flow down — and
    # a box on the start form inviting the starter to send it somewhere
    # else undid that decision at the worst possible moment: quietly, run by
    # run, by whoever happened to be filling the form in. To change who does
    # step 1, change the step.
    overrides: dict[int, int] = {}

    # Which company this run is for. A flow tied to one company answers for
    # itself; otherwise it is asked, and any step assigned to a position
    # rather than a person needs the answer.
    raw_branch = (form.get("run_branch") or "").strip()
    run_branch = int(raw_branch) if raw_branch.isdigit() else flow.branch_id
    if run_branch is not None:
        picked = db.get(Branch, run_branch)
        if not picked or picked.org_id != user.org_id:
            raise HTTPException(400, "That is not one of your companies.")
    positions = [s.doer_dept for s in flow.steps if s.doer_dept]
    if positions and run_branch is None:
        raise HTTPException(
            400, "This flow has steps assigned to a position rather than a "
                 "person, so it needs to know which company this run is for. "
                 "Choose one at the top of the start form.")
    # Checked now rather than when the step comes round: a step that cannot
    # find its person is otherwise discovered three days later by whoever is
    # holding the work, with the run already half done.
    # A decision step has to come down to ONE person: two people choosing
    # two different outcomes is a flow that must go two ways at once. With a
    # position that is only knowable now, when the company is.
    for step in flow.steps:
        if not (step.is_decision and step.doer_dept):
            continue
        holders = flow_svc.dept_people(db, flow.org_id, run_branch,
                                       step.doer_dept)
        if len(holders) > 1:
            company = db.get(Branch, run_branch)
            raise HTTPException(
                400, f"Step {step.position} ({step.title}) is a decision "
                     f"step, and {company.name if company else 'that company'} "
                     f"has {len(holders)} people in {step.doer_dept}: "
                     + ", ".join(h.name for h in holders) +
                     ". Two of them could choose two different outcomes, so "
                     "the flow would have to go both ways at once. Either "
                     "give that step one person by name, or leave one person "
                     "in that department at this company.")
    gaps = flow_svc.missing_positions(db, flow, run_branch)
    if gaps:
        company = db.get(Branch, run_branch)
        raise HTTPException(
            400, f"{company.name if company else 'That company'} has nobody "
                 f"in {', '.join(gaps)}. Either add the person under "
                 f"Setup → Users with that company and department, or — if "
                 f"somebody at another company already does "
                 f"{company.name if company else 'it'} as well — open their "
                 f"record and tick {company.name if company else 'this company'} "
                 "under “Also covers these companies”.")

    # Whatever this particular flow asks for at the start — a bill number, a
    # member name — goes straight into the run's context, where every later
    # step can read it.
    context = {}
    uploads = []                       # (question label, UploadFile)
    for field in flow.start_form_fields:
        if field["type"] == "file":
            # Read, checked and stored only once the run exists, because an
            # attachment belongs to a task and there is no task yet. Refused
            # here, before anything is created, so a run is never started
            # with half its paperwork.
            picked = form.get(field["key"])
            if not _is_upload(picked):
                if field["required"]:
                    raise HTTPException(
                        400, f"'{field['label']}' needs a file to start this flow.")
                continue
            if not storage.uploads_available():
                raise HTTPException(
                    400, "File uploads are switched off on this server, so "
                         f"'{field['label']}' cannot be answered. Ask an "
                         "admin to set up storage.")
            uploads.append((field["label"], picked))
            continue
        # The same check the bulk importer runs, so a file cannot create a
        # run this form would have refused.
        value, person, problem = flowform.check_answer(
            db, flow.org_id, field, form.get(field["key"]))
        if problem:
            raise HTTPException(400, problem)
        if not value:
            continue
        context[field["label"]] = value
        if person is not None:
            # Which person, not just the name they are known by. A step set
            # to follow this answer needs to be sure, and two people can
            # share a name.
            context.setdefault(CONTEXT_PEOPLE, {})[field["label"]] = person.id

    # The files are read BEFORE the run is created: a file over the limit, or
    # of a type this server does not take, should stop the start rather than
    # leave a run standing with the document missing.
    ready = []
    for label, up in uploads:
        data = await up.read()
        data, ctype, fname = shrink.shrink(data, up.content_type or "",
                                           up.filename or "file")
        err = storage.check(fname, ctype, len(data))
        if err:
            raise HTTPException(400, f"'{label}': {err}")
        ready.append((label, fname, ctype, data))

    inst = flow_svc.start_flow(db, flow, reference, user, overrides, context,
                               branch_id=run_branch)
    if ready:
        _attach_to_run(db, inst, user, ready)
        db.commit()
    return RedirectResponse(f"/flows/instance/{inst.id}", status_code=303)


def _is_upload(value) -> bool:
    """A filled-in file box, as opposed to an empty one or a typed answer."""
    return hasattr(value, "filename") and bool((value.filename or "").strip())


def _attach_to_run(db: Session, inst: FlowInstance, user: User, ready) -> None:
    """Save each start-form file against the run and its first step.

    The task is the run's first step, which is where it was uploaded and
    what every existing rule about who may see or remove a file already
    works from. flow_instance_id is what makes it the RUN's file, so the
    person on step 4 can open it without being handed step 1.
    """
    first = sorted(inst.tasks, key=lambda t: (t.flow_step.position
                                              if t.flow_step else 0))[0]
    for label, fname, ctype, data in ready:
        if storage.mode() == "db":
            db.add(Attachment(
                task_id=first.id, uploaded_by_id=user.id, filename=fname[:250],
                stored_name=storage.safe_key(first.id, fname), size=len(data),
                content_type=ctype, storage="db", data=data,
                flow_instance_id=inst.id, start_field=label[:160]))
        else:
            key = storage.save_local(first.id, fname, data)
            db.add(Attachment(
                task_id=first.id, uploaded_by_id=user.id, filename=fname[:250],
                stored_name=key, size=len(data), content_type=ctype,
                storage="local", flow_instance_id=inst.id,
                start_field=label[:160]))


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
    # Opening a run is the moment somebody is asking what it is doing, so it
    # is a fair moment to make sure the answer is true: a run started before
    # the plan existed gets one now, and one that got stuck is unstuck. It
    # does nothing at all to a run that is already right.
    if flow_svc.catch_up(db, inst):
        db.commit()
    tasks = db.scalars(
        select(Task).where(Task.flow_instance_id == inst.id).order_by(Task.created_at)
    ).all()
    try:
        started_with = context_answers(json.loads(inst.context or "{}"))
    except json.JSONDecodeError:
        started_with = {}
    run_files = db.scalars(
        select(Attachment).where(Attachment.flow_instance_id == inst.id)
        .order_by(Attachment.id)).all()
    return templates.TemplateResponse(request, "flow_instance.html", {
        "user": user, "inst": inst, "tasks": tasks, "run_files": run_files,
        "progress": flow_svc.flow_progress(inst),
        "can_control": user.has(Right.MANAGE_FLOW),
        "started_with": started_with,
    })
