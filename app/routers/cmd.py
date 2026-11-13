"""The CMD board — one page, four questions, counts only.

Read-only from end to end: nothing on this page changes a task. It exists so
that somebody running the group can see the position in ten seconds without
opening a report, and every number links to the list it came from, so
"41 overdue" is one click from the forty-one.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import clock
from ..db import get_db
from ..deps import manager_up
from ..models import User
from ..models import Organization
from ..services import approvals, cmdboard, xlsx
from ..templating import templates

router = APIRouter()


@router.get("/cmd", response_class=HTMLResponse)
def cmd_board(request: Request, period: str = "this_month",
              date_from: str = "", date_to: str = "", export: str = "",
              user: User = Depends(manager_up),
              db: Session = Depends(get_db)):
    if period not in cmdboard.PERIOD_LABELS:
        period = "this_month"
    start, end, label = cmdboard.window(period, date_from, date_to)
    today = clock.today()

    # Once for the whole page. The group board and the company table are
    # the same rows counted two ways, and reading them twice doubled the
    # heaviest query in the software for nothing.
    rows = cmdboard.rows_for(db, user.org_id, start, end)
    chased = cmdboard.chased_on(db, user.org_id, today)
    board = cmdboard.build(db, user.org_id, start, end, day=today,
                           rows=rows, chased=chased)
    branches = cmdboard.by_branch(db, user.org_id, start, end, day=today,
                                  rows=rows, chased=chased)

    if xlsx.wants(export):
        # One row per company, plus the group on top, so the figures can be
        # pasted into a review pack without anybody retyping them.
        rows = [(None, board)] + branches
        # Columns generated from the same list the page uses, in the
        # same order, for the same reason: a kind of work added to the
        # board has to turn up in the file without anybody remembering
        # to add fourteen more lambdas here.
        def _col(key, part, field):
            return lambda r: getattr(getattr(r[1], part)[key], field)

        def _tot(key):
            return lambda r: r[1].total_in_period[key]

        cols = [("Company", lambda r: r[0].name if r[0] else "WHOLE GROUP")]
        SHORT = {"delegation": "DEL", "recurring": "CL", "flow": "FMS"}
        for src, _label in cmdboard.SOURCES:
            k, tag = src.value, SHORT[src.value]
            cols += [(f"{tag} overdue", _col(k, "position", "overdue")),
                     (f"{tag} today", _col(k, "position", "today")),
                     (f"{tag} upcoming", _col(k, "position", "upcoming"))]
        for src, _label in cmdboard.SOURCES:
            k, tag = src.value, SHORT[src.value]
            # Needs-audit and not-finished-yet are exported too, so the
            # sheet adds up the same way the page does: needs audit =
            # not finished yet + done + pending.
            cols += [(f"{tag} tasks in period", _tot(k)),
                     (f"{tag} needs audit", _col(k, "audit", "total")),
                     (f"{tag} not finished yet", _col(k, "audit", "waiting")),
                     (f"{tag} audit done", _col(k, "audit", "done")),
                     (f"{tag} audit pending", _col(k, "audit", "pending"))]
        for src, _label in cmdboard.SOURCES:
            k, tag = src.value, SHORT[src.value]
            cols += [(f"{tag} follow-up done", _col(k, "chase", "done")),
                     (f"{tag} follow-up pending", _col(k, "chase", "pending"))]
        for src, _label in cmdboard.SOURCES:
            k, tag = src.value, SHORT[src.value]
            cols += [(f"{tag} on time", _col(k, "timing", "on_time")),
                     (f"{tag} delayed", _col(k, "timing", "delayed"))]
        return xlsx.one(
            "cmd-board", "CMD board", cols, rows,
            f"Position on {today:%d/%m/%Y} · period {label} · "
            "counts of tasks, not score weight")

    # Who is holding work that has been owed a month or more. Read now
    # rather than for the chosen period: "pending for a month" is a fact
    # about today, and filtering it by a month-long window would answer a
    # different question while looking like this one.
    stale = cmdboard.stale(db, user.org_id)

    # What is with CMD Sir for approval, from the copy of his sheet. Read
    # from the copy and never from Google here: this page is opened all
    # day, and a board that waits on somebody else's server to draw is a
    # board that is sometimes blank.
    org = db.get(Organization, user.org_id)
    appr = (approvals.summary(approvals.items(db, user.org_id))
            if approvals.configured(org) else None)
    return templates.TemplateResponse(request, "cmd.html", {
        "user": user, "board": board, "branches": branches,
        # One list for every section and for the company table, so a
        # kind of work can never be shown in some of them and not others.
        "KINDS": [(src.value, label, cmdboard.QUERY_NAME[src.value])
                  for src, label in cmdboard.SOURCES],
        "SHORT": {"delegation": "DEL", "recurring": "CL", "flow": "FMS"},
        "appr": appr, "appr_read_at": org.appr_read_at if org else None,
        "appr_error": org.appr_error if org else None,
        "appr_from": org.appr_start_row if org else None,
        "stale": stale, "stale_days": cmdboard.STALE_DAYS,
        "stale_total": sum(r.count for r in stale),
        "people_shown": cmdboard.STALE_PEOPLE,
        "period": period, "periods": cmdboard.PERIODS, "label": label,
        "date_from": date_from or start.date().isoformat(),
        "date_to": date_to or end.date().isoformat(),
        "today": today,
    })
