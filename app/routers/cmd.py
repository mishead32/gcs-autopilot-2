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
from ..services import cmdboard, xlsx
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

    board = cmdboard.build(db, user.org_id, start, end, day=today)
    branches = cmdboard.by_branch(db, user.org_id, start, end, day=today)

    if xlsx.wants(export):
        # One row per company, plus the group on top, so the figures can be
        # pasted into a review pack without anybody retyping them.
        rows = [(None, board)] + branches
        return xlsx.one("cmd-board", "CMD board", [
            ("Company", lambda r: r[0].name if r[0] else "WHOLE GROUP"),
            ("DEL overdue", lambda r: r[1].position["delegation"].overdue),
            ("DEL today", lambda r: r[1].position["delegation"].today),
            ("DEL upcoming", lambda r: r[1].position["delegation"].upcoming),
            ("CL overdue", lambda r: r[1].position["recurring"].overdue),
            ("CL today", lambda r: r[1].position["recurring"].today),
            ("CL upcoming", lambda r: r[1].position["recurring"].upcoming),
            ("DEL tasks in period", lambda r: r[1].total_in_period["delegation"]),
            ("DEL audit done", lambda r: r[1].audit["delegation"].done),
            ("DEL audit pending", lambda r: r[1].audit["delegation"].pending),
            ("CL tasks in period", lambda r: r[1].total_in_period["recurring"]),
            ("CL audit done", lambda r: r[1].audit["recurring"].done),
            ("CL audit pending", lambda r: r[1].audit["recurring"].pending),
            ("DEL follow-up done", lambda r: r[1].chase["delegation"].done),
            ("DEL follow-up pending", lambda r: r[1].chase["delegation"].pending),
            ("CL follow-up done", lambda r: r[1].chase["recurring"].done),
            ("CL follow-up pending", lambda r: r[1].chase["recurring"].pending),
            ("DEL on time", lambda r: r[1].timing["delegation"].on_time),
            ("DEL delayed", lambda r: r[1].timing["delegation"].delayed),
            ("CL on time", lambda r: r[1].timing["recurring"].on_time),
            ("CL delayed", lambda r: r[1].timing["recurring"].delayed),
        ], rows, f"Position on {today:%d %b %Y} · period {label} · counts of "
                 f"tasks, not score weight")

    return templates.TemplateResponse(request, "cmd.html", {
        "user": user, "board": board, "branches": branches,
        "period": period, "periods": cmdboard.PERIODS, "label": label,
        "date_from": date_from or start.date().isoformat(),
        "date_to": date_to or end.date().isoformat(),
        "today": today,
    })
