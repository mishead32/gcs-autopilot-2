from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select, func, or_, update, delete
from sqlalchemy.orm import Session

from .. import clock
from ..db import get_db
from ..deps import require_right
from ..models import (
    User, Role, Branch, Department, Right, RIGHT_LABELS, DEFAULT_RIGHTS,
    Task, TaskComment, Attachment, RecurringRule, Flow, FlowStep, FlowInstance,
    OutboundMessage, HelpTicket, Holiday, Followup,
)
from ..security import hash_password
from ..services import xlsx
from ..templating import templates

router = APIRouter(prefix="/admin")
manage = require_right(Right.MANAGE_USER)


def _benchmarks(form) -> tuple[int, int, int]:
    """Read the three benchmark inputs, defaulting to 60/20/20."""
    def num(name, default):
        raw = (form.get(name) or "").strip()
        try:
            return int(raw) if raw else default
        except ValueError:
            raise HTTPException(400, f"'{raw}' is not a whole number for {name}.")
    return num("bm_delegation", 60), num("bm_checklist", 20), num("bm_fms", 20)


def _valid_rights(raw: list[str]) -> list[Right]:
    out = []
    for r in raw:
        try:
            out.append(Right(r))
        except ValueError:
            continue
    return out


def _branch_links(db: Session, b: Branch) -> dict:
    """Everything filed under a company. Nothing may be orphaned by a delete."""
    def n(model, *where):
        return db.scalar(select(func.count()).select_from(model).where(*where)) or 0

    return {
        "staff": n(User, User.branch_id == b.id),
        "tasks": n(Task, Task.branch_id == b.id),
        "rules": n(RecurringRule, RecurringRule.branch_id == b.id),
        "flows": n(Flow, Flow.branch_id == b.id),
        "depts": n(Department, Department.branch_id == b.id),
    }


@router.get("/users", response_class=HTMLResponse)
def users(request: Request, export: str = "", user: User = Depends(manage),
          db: Session = Depends(get_db)):
    people = db.scalars(
        select(User).where(User.org_id == user.org_id).order_by(User.name)
    ).all()

    # The staff list as a spreadsheet — rights spelled out in words rather
    # than the stored codes, because the point of downloading it is usually
    # to have somebody who does not use the software check who can do what.
    if xlsx.wants(export):
        return xlsx.one("users", "Users & rights", [
            ("Name", lambda u: u.name),
            ("Email", lambda u: u.email),
            ("WhatsApp", lambda u: u.phone or ""),
            ("Role", lambda u: u.role.value.title()),
            ("Branch", lambda u: u.branch.name if u.branch else ""),
            ("Department", lambda u: u.department.name if u.department else ""),
            ("Active", lambda u: "Yes" if u.active else "No"),
            ("Benchmark — Delegation", lambda u: u.bm_delegation),
            ("Benchmark — Checklist", lambda u: u.bm_checklist),
            ("Benchmark — FMS", lambda u: u.bm_fms),
            ("Scored by software", lambda u: u.scored_by_system),
            ("Judged by hand", lambda u: u.scored_by_hand),
            ("Rights", _rights_text),
        ], people,
            "Owner and admin hold every right, whatever the Rights column lists.")
    branches = db.scalars(
        select(Branch).where(Branch.org_id == user.org_id).order_by(Branch.name)
    ).all()
    depts = db.scalars(select(Department).where(Department.org_id == user.org_id)).all()
    return templates.TemplateResponse(request, "admin_users.html", {
        "user": user, "people": people, "branches": branches, "depts": depts,
        "roles": list(Role), "rights": list(Right), "right_labels": RIGHT_LABELS,
        "default_rights": {r.value: [x.value for x in v]
                           for r, v in DEFAULT_RIGHTS.items()},
    })


@router.post("/users")
async def create_user(request: Request, user: User = Depends(manage),
                      db: Session = Depends(get_db)):
    form = await request.form()
    email = form["email"].strip().lower()

    if db.scalar(select(User).where(User.org_id == user.org_id, User.email == email)):
        raise HTTPException(400, f"A user with the email {email} already exists.")

    role = Role(form.get("role", "doer"))
    granted = _valid_rights(form.getlist("rights")) or DEFAULT_RIGHTS[role]

    u = User(
        org_id=user.org_id,
        name=form["name"].strip(),
        email=email,
        phone=(form.get("phone") or "").strip() or None,
        password_hash=hash_password(form["password"]),
        role=role,
        branch_id=int(form["branch_id"]) if form.get("branch_id") else None,
        department_id=int(form["department_id"]) if form.get("department_id") else None,
    )
    u.set_rights(granted)
    try:
        u.set_benchmarks(*_benchmarks(form))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.add(u)
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/users/{user_id}", response_class=HTMLResponse)
def edit_user_form(user_id: int, request: Request, user: User = Depends(manage),
                   db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target or target.org_id != user.org_id:
        raise HTTPException(404, "User not found")
    branches = db.scalars(
        select(Branch).where(Branch.org_id == user.org_id).order_by(Branch.name)
    ).all()
    depts = db.scalars(select(Department).where(Department.org_id == user.org_id)).all()
    return templates.TemplateResponse(request, "admin_user_edit.html", {
        "user": user, "target": target, "branches": branches, "depts": depts,
        "roles": list(Role), "rights": list(Right), "right_labels": RIGHT_LABELS,
    })


@router.post("/users/{user_id}")
async def update_user(user_id: int, request: Request, user: User = Depends(manage),
                      db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target or target.org_id != user.org_id:
        raise HTTPException(404, "User not found")

    form = await request.form()
    target.name = form["name"].strip()
    target.phone = (form.get("phone") or "").strip() or None
    target.branch_id = int(form["branch_id"]) if form.get("branch_id") else None
    target.department_id = int(form["department_id"]) if form.get("department_id") else None

    # never let someone strip their own admin rights and lock themselves out
    if target.id != user.id:
        target.role = Role(form.get("role", target.role.value))
        target.set_rights(_valid_rights(form.getlist("rights")))

    try:
        target.set_benchmarks(*_benchmarks(form))
    except ValueError as e:
        raise HTTPException(400, str(e))

    if form.get("password"):
        target.password_hash = hash_password(form["password"])

    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


def _links(db: Session, u: User) -> dict:
    """Everything in the system that points at this person."""
    def n(model, *where):
        return db.scalar(select(func.count()).select_from(model).where(*where)) or 0

    return {
        "doing": n(Task, Task.doer_id == u.id),
        "assigned": n(Task, Task.assigner_id == u.id),
        "notes": n(TaskComment, TaskComment.author_id == u.id),
        "rules": n(RecurringRule, or_(RecurringRule.doer_id == u.id,
                                      RecurringRule.assigner_id == u.id)),
        "steps": n(FlowStep, FlowStep.default_doer_id == u.id),
        "runs": n(FlowInstance, FlowInstance.started_by_id == u.id),
        "help": n(HelpTicket, or_(HelpTicket.raiser_id == u.id,
                                  HelpTicket.helper_id == u.id)),
        "chased": n(Followup, Followup.by_id == u.id),
        "files": n(Attachment, Attachment.uploaded_by_id == u.id),
    }


def _blockers(db: Session, actor: User, target: User) -> str | None:
    """Reasons a user must not be deleted at all."""
    if target.id == actor.id:
        return "You can't delete your own account."
    if target.role in (Role.OWNER, Role.ADMIN):
        others = db.scalar(select(func.count()).select_from(User).where(
            User.org_id == target.org_id, User.id != target.id,
            User.active.is_(True), User.role.in_([Role.OWNER, Role.ADMIN])
        )) or 0
        if others == 0:
            return ("This is the last active owner/admin. Create another one "
                    "first, or nobody will be able to administer the system.")
    return None


@router.get("/users/{user_id}/delete", response_class=HTMLResponse)
def delete_user_form(user_id: int, request: Request, user: User = Depends(manage),
                     db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target or target.org_id != user.org_id:
        raise HTTPException(404, "User not found")

    others = db.scalars(
        select(User).where(User.org_id == user.org_id, User.id != target.id,
                           User.active.is_(True)).order_by(User.name)
    ).all()
    return templates.TemplateResponse(request, "admin_user_delete.html", {
        "user": user, "target": target, "links": _links(db, target),
        "others": others, "blocker": _blockers(db, user, target),
    })


@router.post("/users/{user_id}/delete")
def delete_user(user_id: int, mode: str = Form("transfer"),
                transfer_to: str = Form(""), confirm: str = Form(""),
                user: User = Depends(manage), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target or target.org_id != user.org_id:
        raise HTTPException(404, "User not found")

    blocker = _blockers(db, user, target)
    if blocker:
        raise HTTPException(400, blocker)
    if confirm != target.name.strip():
        raise HTTPException(
            400, "Type the person's name exactly to confirm the deletion.")

    links = _links(db, target)
    has_history = any(links.values())

    if has_history and mode == "transfer":
        heir = db.get(User, int(transfer_to)) if transfer_to else None
        if not heir or heir.org_id != user.org_id or heir.id == target.id:
            raise HTTPException(400, "Choose a valid person to transfer the work to.")

        db.execute(update(Task).where(Task.doer_id == target.id)
                   .values(doer_id=heir.id))
        db.execute(update(Task).where(Task.assigner_id == target.id)
                   .values(assigner_id=heir.id))
        db.execute(update(TaskComment).where(TaskComment.author_id == target.id)
                   .values(author_id=heir.id))
        db.execute(update(Attachment).where(Attachment.uploaded_by_id == target.id)
                   .values(uploaded_by_id=heir.id))
        db.execute(update(RecurringRule).where(RecurringRule.doer_id == target.id)
                   .values(doer_id=heir.id))
        db.execute(update(RecurringRule).where(RecurringRule.assigner_id == target.id)
                   .values(assigner_id=heir.id))
        db.execute(update(FlowStep).where(FlowStep.default_doer_id == target.id)
                   .values(default_doer_id=heir.id))
        db.execute(update(FlowInstance).where(FlowInstance.started_by_id == target.id)
                   .values(started_by_id=heir.id))
        db.execute(update(HelpTicket).where(HelpTicket.raiser_id == target.id)
                   .values(raiser_id=heir.id))
        db.execute(update(HelpTicket).where(HelpTicket.helper_id == target.id)
                   .values(helper_id=heir.id))
        # Follow-up ticks. The column will not take a null, so a tick left
        # pointing at a deleted person stops the delete dead — which is how
        # this went unnoticed until somebody tried it on the live site.
        db.execute(update(Followup).where(Followup.by_id == target.id)
                   .values(by_id=heir.id))

    elif has_history:   # mode == "purge"
        # their tasks go, and anything hanging off those tasks goes with them
        tids = [t.id for t in db.scalars(select(Task).where(
            or_(Task.doer_id == target.id, Task.assigner_id == target.id))).all()]
        if tids:
            db.execute(delete(HelpTicket).where(HelpTicket.task_id.in_(tids)))
            db.execute(delete(TaskComment).where(TaskComment.task_id.in_(tids)))
            db.execute(delete(Attachment).where(Attachment.task_id.in_(tids)))
            # a tick says "this task was chased" — with the task gone it
            # says nothing, and the database will not let it stay
            db.execute(delete(Followup).where(Followup.task_id.in_(tids)))
            db.execute(update(OutboundMessage)
                       .where(OutboundMessage.task_id.in_(tids))
                       .values(task_id=None))
            db.execute(delete(Task).where(Task.id.in_(tids)))
        db.execute(delete(HelpTicket).where(
            or_(HelpTicket.raiser_id == target.id,
                HelpTicket.helper_id == target.id)))
        db.execute(delete(TaskComment).where(TaskComment.author_id == target.id))
        # Ticks this person made on work that is staying. Purge means their
        # record goes, so the ticks go with it rather than being pinned on
        # somebody who never made them.
        db.execute(delete(Followup).where(Followup.by_id == target.id))
        # Files they uploaded onto other people's tasks are that task's
        # proof, not this person's property — the file stays and the name on
        # it becomes whoever is doing the deleting, the same treatment flow
        # runs already get.
        db.execute(update(Attachment).where(Attachment.uploaded_by_id == target.id)
                   .values(uploaded_by_id=user.id))
        db.execute(delete(RecurringRule).where(
            or_(RecurringRule.doer_id == target.id,
                RecurringRule.assigner_id == target.id)))
        db.execute(update(FlowStep).where(FlowStep.default_doer_id == target.id)
                   .values(default_doer_id=None))
        # flow runs they started stay, but must point at someone who exists
        db.execute(update(FlowInstance).where(FlowInstance.started_by_id == target.id)
                   .values(started_by_id=user.id))

    # audit trails always survive the person; just unhook the name
    for col in ("auditor_id", "false_marked_by_id", "reopened_by_id"):
        db.execute(update(Task).where(getattr(Task, col) == target.id)
                   .values(**{col: None}))

    db.delete(target)
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/toggle")
def toggle_user(user_id: int, user: User = Depends(manage), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if target and target.org_id == user.org_id and target.id != user.id:
        target.active = not target.active
        db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/branches")
def create_branch(name: str = Form(...), city: str = Form(""),
                  user: User = Depends(manage), db: Session = Depends(get_db)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Give the branch a name.")
    existing = db.scalar(select(Branch).where(Branch.org_id == user.org_id,
                                              Branch.name == name))
    if existing:
        raise HTTPException(400, f"'{name}' is already on the list.")
    db.add(Branch(org_id=user.org_id, name=name, city=city.strip() or None))
    db.commit()
    return RedirectResponse("/admin/branches", status_code=303)


def _branch_or_404(db: Session, user: User, branch_id: int) -> Branch:
    b = db.get(Branch, branch_id)
    if not b or b.org_id != user.org_id:
        raise HTTPException(404, "Branch not found")
    return b


@router.get("/branches", response_class=HTMLResponse)
def branches_page(request: Request, export: str = "",
                  user: User = Depends(manage), db: Session = Depends(get_db)):
    branches = db.scalars(
        select(Branch).where(Branch.org_id == user.org_id).order_by(Branch.name)
    ).all()
    if xlsx.wants(export):
        use = {b.id: _branch_links(db, b) for b in branches}
        return xlsx.one("branches", "Branches", [
            ("Branch", lambda b: b.name),
            ("People", lambda b: use[b.id]["staff"]),
            ("Departments", lambda b: use[b.id]["depts"]),
            ("Tasks", lambda b: use[b.id]["tasks"]),
            ("Checklist rules", lambda b: use[b.id]["rules"]),
            ("FMS flows", lambda b: use[b.id]["flows"]),
        ], branches)
    return templates.TemplateResponse(request, "admin_branches.html", {
        "user": user, "branches": branches,
        "branch_use": {b.id: _branch_links(db, b) for b in branches},
    })


@router.get("/branches/{branch_id}", response_class=HTMLResponse)
def edit_branch_form(branch_id: int, request: Request,
                     user: User = Depends(manage), db: Session = Depends(get_db)):
    branch = _branch_or_404(db, user, branch_id)
    others = db.scalars(
        select(Branch).where(Branch.org_id == user.org_id, Branch.id != branch.id)
        .order_by(Branch.name)
    ).all()
    return templates.TemplateResponse(request, "admin_branch_edit.html", {
        "user": user, "branch": branch, "others": others,
        "links": _branch_links(db, branch),
    })


@router.post("/branches/{branch_id}")
def edit_branch(branch_id: int, name: str = Form(...), city: str = Form(""),
                user: User = Depends(manage), db: Session = Depends(get_db)):
    branch = _branch_or_404(db, user, branch_id)
    name = name.strip()
    if not name:
        raise HTTPException(400, "Give the branch a name.")
    clash = db.scalar(select(Branch).where(Branch.org_id == user.org_id,
                                           Branch.name == name,
                                           Branch.id != branch.id))
    if clash:
        raise HTTPException(400, f"'{name}' is already on the list.")
    # Renaming is safe: everything points at the id, not the name, so past
    # tasks and scores follow the new name rather than being orphaned.
    branch.name = name
    branch.city = city.strip() or None
    db.commit()
    return RedirectResponse("/admin/branches", status_code=303)


@router.post("/branches/{branch_id}/delete")
def delete_branch(branch_id: int, move_to: str = Form(""), confirm: str = Form(""),
                  user: User = Depends(manage), db: Session = Depends(get_db)):
    """Delete a company, moving anything filed under it somewhere safe first.

    A company is never deleted out from under live data. Either everything is
    moved to another company, or it is left unassigned — but it is never left
    pointing at a company that no longer exists, which would break the
    Performance filters and the Google Sheet.
    """
    branch = _branch_or_404(db, user, branch_id)
    if confirm != branch.name.strip():
        raise HTTPException(400, "Type the branch name exactly to confirm.")

    links = _branch_links(db, branch)
    target_id = None
    if move_to:
        target = _branch_or_404(db, user, int(move_to))
        if target.id == branch.id:
            raise HTTPException(400, "Choose a different branch to move things to.")
        target_id = target.id
    elif any(links.values()):
        # No destination chosen: everything becomes unassigned rather than
        # silently disappearing from reports.
        target_id = None

    for model, col in ((User, User.branch_id), (Task, Task.branch_id),
                       (RecurringRule, RecurringRule.branch_id),
                       (Flow, Flow.branch_id), (Department, Department.branch_id)):
        db.execute(update(model).where(col == branch.id).values(branch_id=target_id))

    db.delete(branch)
    db.commit()
    return RedirectResponse("/admin/branches", status_code=303)


# ------------------------------------------------------------ departments ---
# A department belongs to one branch, or to none when it spans the group
# (Accounts, HR). It is only ever a label on a person — no task, score or
# report is filed under it — which is why deleting one is far simpler than
# deleting a branch: the people keep everything, they just lose the label.
def _dept_or_404(db: Session, user: User, dept_id: int) -> Department:
    d = db.get(Department, dept_id)
    if not d or d.org_id != user.org_id:
        raise HTTPException(404, "Department not found")
    return d


def _dept_staff(db: Session, dept: Department) -> int:
    return db.scalar(select(func.count()).select_from(User)
                     .where(User.department_id == dept.id)) or 0


def _dept_clash(db: Session, user: User, name: str, branch_id, skip_id=None):
    """Same name twice under the same branch. The same name under two
    different branches is fine — 'Front Desk' exists at every one of them."""
    q = select(Department).where(Department.org_id == user.org_id,
                                 Department.name == name,
                                 Department.branch_id.is_(None)
                                 if branch_id is None
                                 else Department.branch_id == branch_id)
    if skip_id:
        q = q.where(Department.id != skip_id)
    return db.scalar(q)


@router.get("/departments", response_class=HTMLResponse)
def departments_page(request: Request, export: str = "",
                     user: User = Depends(manage), db: Session = Depends(get_db)):
    depts = db.scalars(
        select(Department).where(Department.org_id == user.org_id)
        .order_by(Department.name)
    ).all()
    if xlsx.wants(export):
        staff = {d.id: _dept_staff(db, d) for d in depts}
        return xlsx.one("departments", "Departments", [
            ("Department", lambda d: d.name),
            ("Branch", lambda d: d.branch.name if d.branch else "All branches"),
            ("People", lambda d: staff.get(d.id, 0)),
        ], depts)
    branches = db.scalars(
        select(Branch).where(Branch.org_id == user.org_id).order_by(Branch.name)
    ).all()
    by_id = {b.id: b for b in branches}
    return templates.TemplateResponse(request, "admin_departments.html", {
        "user": user, "depts": depts, "branches": branches, "branch_of": by_id,
        "staff": {d.id: _dept_staff(db, d) for d in depts},
    })


@router.post("/departments")
def create_department(name: str = Form(...), branch_id: str = Form(""),
                      user: User = Depends(manage), db: Session = Depends(get_db)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Give the department a name.")
    bid = int(branch_id) if branch_id.strip().isdigit() else None
    if bid is not None:
        _branch_or_404(db, user, bid)
    if _dept_clash(db, user, name, bid):
        where = "that branch" if bid else "the all-branches list"
        raise HTTPException(400, f"'{name}' is already on {where}.")
    db.add(Department(org_id=user.org_id, name=name, branch_id=bid))
    db.commit()
    return RedirectResponse("/admin/departments", status_code=303)


@router.get("/departments/{dept_id}", response_class=HTMLResponse)
def edit_department_form(dept_id: int, request: Request,
                         user: User = Depends(manage), db: Session = Depends(get_db)):
    dept = _dept_or_404(db, user, dept_id)
    branches = db.scalars(
        select(Branch).where(Branch.org_id == user.org_id).order_by(Branch.name)
    ).all()
    people = db.scalars(
        select(User).where(User.department_id == dept.id).order_by(User.name)
    ).all()
    return templates.TemplateResponse(request, "admin_department_edit.html", {
        "user": user, "dept": dept, "branches": branches, "people": people,
    })


@router.post("/departments/{dept_id}")
def edit_department(dept_id: int, name: str = Form(...), branch_id: str = Form(""),
                    user: User = Depends(manage), db: Session = Depends(get_db)):
    dept = _dept_or_404(db, user, dept_id)
    name = name.strip()
    if not name:
        raise HTTPException(400, "Give the department a name.")
    bid = int(branch_id) if branch_id.strip().isdigit() else None
    if bid is not None:
        _branch_or_404(db, user, bid)
    if _dept_clash(db, user, name, bid, skip_id=dept.id):
        where = "that branch" if bid else "the all-branches list"
        raise HTTPException(400, f"'{name}' is already on {where}.")
    # Renaming is safe: people point at the id, not the name.
    dept.name = name
    dept.branch_id = bid
    db.commit()
    return RedirectResponse("/admin/departments", status_code=303)


@router.post("/departments/{dept_id}/delete")
def delete_department(dept_id: int, confirm: str = Form(""),
                      user: User = Depends(manage), db: Session = Depends(get_db)):
    """Remove a department. Anyone in it simply loses the label.

    Nothing is filed under a department, so nobody's tasks, history or score
    is touched — but the people are unhooked first anyway, because Postgres
    enforces the foreign key even where SQLite lets it slide.
    """
    dept = _dept_or_404(db, user, dept_id)
    if confirm != dept.name.strip():
        raise HTTPException(400, "Type the department name exactly to confirm.")
    db.execute(update(User).where(User.department_id == dept.id)
               .values(department_id=None))
    db.delete(dept)
    db.commit()
    return RedirectResponse("/admin/departments", status_code=303)


# --------------------------------------------------------------- holidays ---
@router.get("/holidays", response_class=HTMLResponse)
def holidays_page(request: Request, year: str = "",
                  user: User = Depends(manage), db: Session = Depends(get_db)):
    from datetime import date as _date
    yr = int(year) if year.isdigit() else clock.today().year
    rows = db.scalars(
        select(Holiday).where(
            Holiday.org_id == user.org_id,
            Holiday.day >= _date(yr, 1, 1),
            Holiday.day <= _date(yr, 12, 31),
        ).order_by(Holiday.day)
    ).all()
    branches = db.scalars(
        select(Branch).where(Branch.org_id == user.org_id).order_by(Branch.name)
    ).all()
    years = sorted({h.day.year for h in db.scalars(
        select(Holiday).where(Holiday.org_id == user.org_id)).all()} | {yr})
    return templates.TemplateResponse(request, "admin_holidays.html", {
        "user": user, "holidays": rows, "branches": branches,
        "year": yr, "years": years, "today": clock.today(),
    })


@router.post("/holidays")
def add_holiday(day: str = Form(...), name: str = Form(...),
                branch_id: str = Form(""),
                user: User = Depends(manage), db: Session = Depends(get_db)):
    from datetime import date as _date
    name = name.strip()
    if not name:
        raise HTTPException(400, "Give the holiday a name, e.g. Diwali.")
    try:
        d = _date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "That is not a valid date.")

    bid = int(branch_id) if branch_id else None
    if bid and not db.get(Branch, bid):
        raise HTTPException(400, "Unknown branch")

    clash = db.scalar(select(Holiday).where(
        Holiday.org_id == user.org_id, Holiday.day == d,
        Holiday.branch_id.is_(None) if bid is None else Holiday.branch_id == bid))
    if clash:
        raise HTTPException(
            400, f"{d:%d %b %Y} is already marked as '{clash.name}' for that scope.")

    db.add(Holiday(org_id=user.org_id, branch_id=bid, day=d, name=name))
    db.commit()
    return RedirectResponse(f"/admin/holidays?year={d.year}", status_code=303)


@router.post("/holidays/{holiday_id}/delete")
def delete_holiday(holiday_id: int, user: User = Depends(manage),
                   db: Session = Depends(get_db)):
    h = db.get(Holiday, holiday_id)
    if not h or h.org_id != user.org_id:
        raise HTTPException(404, "Holiday not found")
    yr = h.day.year
    db.delete(h)
    db.commit()
    return RedirectResponse(f"/admin/holidays?year={yr}", status_code=303)


def _rights_text(u: User) -> str:
    """The rights this person holds, in the same words the page shows."""
    if u.role in (Role.OWNER, Role.ADMIN):
        return "All rights"
    held = [RIGHT_LABELS[r] for r in Right if r.value in u.right_set]
    return ", ".join(held) if held else "Execute only"
