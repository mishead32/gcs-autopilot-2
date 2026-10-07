"""Task Detective AI — the page where the second opinions live.

Everything here is read-only except one button, which asks the detective to
look at finished work it has not seen yet. Nothing on this page changes a
task, a score or an audit: the manual audit is untouched and still the only
thing that decides anything.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .. import clock, flash, lastview, search
from ..db import get_db
from ..deps import current_user, manager_up
from ..models import (AiAudit, AiVerdict, AI_VERDICT_LABELS, AI_SUSPECT,
                      Branch, Task, TaskSource, User)
from ..services import detective, xlsx
from ..templating import templates

router = APIRouter()

# The tabs, in the order a person cares about them: the doubtful ones first,
# because they are the only reason to open this page.
VERDICT_TABS = [
    ("suspect", "Needs a look"),
    ("unrelated", "Proof does not match"),
    ("no_proof", "No real proof"),
    ("weak", "Proof is thin"),
    ("ok", "Proof looks right"),
    ("error", "Could not check"),
    ("all", "Everything"),
]
VERDICT_LABELS = dict(VERDICT_TABS)

SOURCES = {"delegation": TaskSource.DELEGATION,
           "checklist": TaskSource.RECURRING,
           "fms": TaskSource.FLOW}


def _day(raw: str):
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date() if raw.strip() else None
    except ValueError:
        return None



def _back(request: Request) -> str:
    """Back to the Detective page, with the verdict tab and filters intact.

    lastview remembers one page across the whole site, so asking it for "the
    last list" after pressing a button HERE can hand back the task list or
    the dashboard — which is exactly what it did. The remembered address is
    used only when it really is this page.
    """
    saved = lastview.url(request, "/detective")
    return saved if saved.split("?")[0] == "/detective" else "/detective"


@router.get("/detective", response_class=HTMLResponse)
def detective_page(request: Request, verdict: str = "suspect",
                   source: str = "", doer: str = "", branch: str = "",
                   date_from: str = "", date_to: str = "", q: str = "",
                   export: str = "",
                   user: User = Depends(manager_up),
                   db: Session = Depends(get_db)):
    verdict = verdict if verdict in VERDICT_LABELS else "suspect"
    text_q = search.clean(q)

    # Only the work the detective is set to watch. Rows from a wider setting
    # — or from before the start date — stay in the database but off the
    # page: a list of six hundred failed checks on work nothing will ever
    # look at again is noise that buries the handful that matter.
    base = detective.in_scope(
        select(AiAudit).join(Task, Task.id == AiAudit.task_id)
        .where(AiAudit.org_id == user.org_id))

    doer_id = int(doer) if doer.strip().isdigit() else None
    branch_id = int(branch) if branch.strip().isdigit() else None
    if doer_id:
        base = base.where(Task.doer_id == doer_id)
    if branch_id:
        base = base.where(Task.branch_id == branch_id)
    if source in SOURCES:
        base = base.where(Task.source == SOURCES[source])
    if text_q:
        where = search.clause([Task.title, Task.details, AiAudit.remark], text_q)
        if where is not None:
            base = base.where(where)

    start, end = _day(date_from), _day(date_to)
    if start and end and start > end:
        start, end = end, start
    if start:
        base = base.where(AiAudit.created_at >= datetime.combine(
            start, datetime.min.time()))
    if end:
        base = base.where(AiAudit.created_at <= datetime.combine(
            end, datetime.max.time()))

    # Counted over everything the filters allow, BEFORE narrowing to one
    # verdict, so a tab still says how much it holds while you stand on
    # another one.
    counts = {k: 0 for k, _ in VERDICT_TABS}
    # The LATEST check for each task, not every check ever made. A task that
    # was looked at again after the doer attached something better would
    # otherwise appear twice, once under "does not match" and once under
    # "looks right", and a list that contradicts itself is worse than no
    # list. The earlier readings are kept, and are on the task's own page.
    newest: dict[int, AiAudit] = {}
    for r in db.scalars(base.order_by(AiAudit.created_at.asc(),
                                      AiAudit.id.asc())).all():
        newest[r.task_id] = r
    rows_all = sorted(newest.values(),
                      key=lambda r: (r.created_at, r.id), reverse=True)
    for r in rows_all:
        counts[r.verdict.value] = counts.get(r.verdict.value, 0) + 1
    counts["suspect"] = sum(1 for r in rows_all if r.verdict in AI_SUSPECT)
    counts["all"] = len(rows_all)

    if verdict == "suspect":
        rows = [r for r in rows_all if r.verdict in AI_SUSPECT]
    elif verdict == "all":
        rows = rows_all
    else:
        rows = [r for r in rows_all if r.verdict.value == verdict]

    # How much finished work has never been looked at — the honest headline
    # for a page that would otherwise imply it had seen everything.
    checked = select(AiAudit.task_id).where(AiAudit.org_id == user.org_id)
    unchecked = db.scalar(detective.in_scope(
        select(func.count()).select_from(Task).where(
            Task.org_id == user.org_id,
            Task.submitted_at.is_not(None),
            Task.id.not_in(checked)))) or 0

    if xlsx.wants(export):
        return xlsx.one("task-detective", "Task Detective AI", [
            ("Task ID", lambda r: r.task.ref or ""),
            ("Task", lambda r: r.task.title),
            ("Doer", lambda r: r.task.doer.name if r.task.doer else ""),
            ("Work type", lambda r: xlsx.SOURCE_NAMES.get(
                r.task.source.value, r.task.source.value)),
            ("Planned date", lambda r: r.task.due_at),
            ("Marked done", lambda r: r.task.closed_at or r.task.submitted_at),
            ("AI verdict", lambda r: r.label),
            ("How sure", lambda r: f"{r.confidence}%"),
            ("What the AI said", lambda r: r.remark),
            ("What it looked at", lambda r: r.looked_at),
            ("Files", lambda r: r.files_seen),
            ("Images", lambda r: r.images_seen),
            ("Checked on", lambda r: r.created_at),
            ("Human audit", lambda r: r.task.audit_label),
            ("Human auditor", lambda r: r.task.auditor.name if r.task.auditor else ""),
        ], rows, f"{VERDICT_LABELS[verdict]} · {len(rows)} of {len(rows_all)} "
                 f"checked · advisory only, no score is changed by this")

    people = db.scalars(select(User).where(User.org_id == user.org_id,
                                           User.active.is_(True))
                        .order_by(User.name)).all()
    branches = db.scalars(select(Branch).where(Branch.org_id == user.org_id)
                          .order_by(Branch.name)).all()
    return templates.TemplateResponse(request, "detective.html", {
        "user": user, "rows": rows[:500], "counts": counts,
        "verdict": verdict, "verdict_tabs": VERDICT_TABS,
        "source": source, "doer_id": doer_id, "branch_id": branch_id,
        "people": people, "branches": branches, "q": text_q,
        "date_from": start.isoformat() if start else "",
        "date_to": end.isoformat() if end else "",
        "unchecked": unchecked,
        "on": detective.available(), "why_not": detective.why_not(),
        "scope": detective.scope_words(),
        "work_types": [(k, lbl) for k, lbl in
                       [("delegation", "Delegation"), ("checklist", "Checklist"),
                        ("fms", "FMS")]
                       if SOURCES[k] in detective.WATCHED],
        "labels": AI_VERDICT_LABELS,
    })


@router.post("/detective/run")
def run_detective(request: Request, background: BackgroundTasks,
                  limit: str = Form("25"),
                  user: User = Depends(manager_up),
                  db: Session = Depends(get_db)):
    """Look at finished work the detective has not seen yet.

    Capped per press, and run in the background, because this is the one
    place that can spend a lot of somebody's free allowance in one go. A
    person who wants two hundred checked presses the button eight times and
    can see it working, which is better than one press that silently runs
    for ten minutes.
    """
    if not detective.available():
        flash.set(request, "info", detective.why_not())
        return RedirectResponse(_back(request), status_code=303)
    try:
        n = max(1, min(100, int(limit)))
    except ValueError:
        n = 25
    checked = select(AiAudit.task_id).where(AiAudit.org_id == user.org_id)
    todo = list(db.scalars(detective.in_scope(
        select(Task.id).where(Task.org_id == user.org_id,
                              Task.submitted_at.is_not(None),
                              Task.id.not_in(checked)))
        .order_by(Task.submitted_at.desc()).limit(n)).all())
    for task_id in todo:
        background.add_task(detective.review_quietly, task_id)
    flash.set(request, "info",
              f"Checking {len(todo)} task(s) in the background. "
              "Refresh in a minute to see what came back."
              if todo else "Every finished task has already been checked.")
    return RedirectResponse(_back(request), status_code=303)


@router.post("/detective/retry")
def retry_failed(request: Request, background: BackgroundTasks,
                 limit: str = Form("25"),
                 user: User = Depends(manager_up),
                 db: Session = Depends(get_db)):
    """Look again at the tasks whose last check failed.

    A whole batch can fail for one reason that has since been fixed — the
    key was missing, the free allowance ran out for the minute, or Google
    retired the model and every call came back 404. Those are not verdicts
    and they should not sit there as if they were, so they can be retried as
    a batch rather than one at a time.

    Only the tasks whose NEWEST check failed: one that failed and was later
    looked at properly has an answer already.
    """
    if not detective.available():
        flash.set(request, "info", detective.why_not())
        return RedirectResponse(_back(request), status_code=303)
    try:
        n = max(1, min(100, int(limit)))
    except ValueError:
        n = 25

    newest: dict[int, AiAudit] = {}
    for r in db.scalars(detective.in_scope(
            select(AiAudit).join(Task, Task.id == AiAudit.task_id)
            .where(AiAudit.org_id == user.org_id))
            .order_by(AiAudit.created_at.asc(), AiAudit.id.asc())).all():
        newest[r.task_id] = r
    todo = [r.task_id for r in sorted(
        (r for r in newest.values() if r.verdict == AiVerdict.ERROR),
        key=lambda r: r.created_at, reverse=True)][:n]

    for task_id in todo:
        background.add_task(detective.review_quietly, task_id)
    flash.set(request, "info",
              f"Looking again at {len(todo)} task(s) that could not be "
              "checked. Refresh in a minute."
              if todo else "Nothing is waiting on a failed check.")
    return RedirectResponse(_back(request), status_code=303)


@router.post("/detective/task/{task_id}")
def recheck(task_id: int, request: Request, background: BackgroundTasks,
            user: User = Depends(manager_up), db: Session = Depends(get_db)):
    """Check one task again — after the doer has attached something better."""
    task = db.get(Task, task_id)
    if not task or task.org_id != user.org_id:
        flash.set(request, "info", "No such task.")
    elif not detective.available():
        flash.set(request, "info", detective.why_not())
    else:
        background.add_task(detective.review_quietly, task.id)
        flash.set(request, "info", f"Looking at {task.ref or task.title} again.")
    return RedirectResponse(lastview.url(request, f"/tasks/{task_id}"),
                            status_code=303)
