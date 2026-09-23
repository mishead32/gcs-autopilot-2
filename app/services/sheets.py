"""Live mirror of the database into a Google Sheet.

This is a one-way copy: the app writes, the Sheet is read-only truth-wise.
Two-way sync sounds appealing but creates two systems that both claim to be
correct, and the first conflict costs more than it ever saved.

What it gives you:
  * a backup you can open on any phone, and download as Excel any time
  * pivot tables and charts in a tool you already know
  * a paper trail that survives even if the app is retired

One tab per table, rewritten in full each run. At GCS's scale (a few thousand
rows) a full rewrite is faster and far more reliable than tracking deltas, and
it self-heals if a run is ever missed.

Set up:
  GOOGLE_SHEET_ID          the id from the Sheet's URL
  GOOGLE_SERVICE_ACCOUNT   the service-account JSON, as one line
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Task, TaskStatus, User, Branch, RecurringRule, Flow, FlowInstance,
    Attachment, TaskComment,
)

SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SERVICE_ACCOUNT = os.getenv("GOOGLE_SERVICE_ACCOUNT", "")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# a Sheet caps at 10 million cells; this keeps any one tab sane
MAX_ROWS_PER_TAB = 20000


def enabled() -> bool:
    return bool(SHEET_ID and SERVICE_ACCOUNT)


def _client():
    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(SERVICE_ACCOUNT)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _d(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, bool):
        return "YES" if v else "NO"
    return str(v)


# ------------------------------------------------------------- the tabs ----
def _tasks(db: Session, org_id: int) -> tuple[list[str], list[list]]:
    from .scoring import SOURCE_LABELS
    from ..models import TaskSource

    rows = db.scalars(
        select(Task).where(Task.org_id == org_id).order_by(Task.id)
    ).all()
    head = ["Task ID", "Title", "Details", "Type", "Company", "Doer",
            "Doer email", "Assigned by", "Priority", "Status", "Due",
            "Created", "Started", "Submitted", "Closed", "On time?",
            "Overdue now?", "Needs audit", "Audit status", "Audited on",
            "Audit score", "Auditor", "Audit remark", "False marked", "False marked by", "False mark reason",
            "Reopened times", "Completion note", "Attachments", "Notes",
            "FMS flow", "FMS reference"]
    out = []
    for t in rows[:MAX_ROWS_PER_TAB]:
        out.append([
            t.id, t.title, t.details or "",
            SOURCE_LABELS.get(TaskSource(t.source.value), t.source.value),
            t.branch.name if t.branch else "",
            t.doer.name, t.doer.email, t.assigner.name,
            t.priority.value, t.status.value.replace("_", " "),
            _d(t.due_at), _d(t.created_at), _d(t.started_at),
            _d(t.submitted_at), _d(t.closed_at),
            "" if t.was_on_time is None else _d(t.was_on_time),
            _d(t.is_overdue), _d(t.requires_audit),
            t.audit_label, _d(t.audited_at),
            "" if t.audit_score is None else t.audit_score,
            t.auditor.name if t.auditor else "", t.audit_remark or "",
            _d(t.false_marked),
            t.false_marked_by.name if t.false_marked_by else "",
            t.false_mark_reason or "", t.reopen_count,
            t.completion_note or "", len(t.attachments), len(t.comments),
            t.flow_instance.flow.name if t.flow_instance else "",
            t.flow_instance.reference if t.flow_instance else "",
        ])
    return head, out


def _users(db: Session, org_id: int):
    rows = db.scalars(select(User).where(User.org_id == org_id)
                      .order_by(User.name)).all()
    head = ["User ID", "Name", "Email", "WhatsApp", "Role", "Company",
            "Department", "Active", "Benchmark Delegation", "Benchmark Checklist",
            "Benchmark FMS", "Rights", "Created"]
    return head, [[
        u.id, u.name, u.email, u.phone or "", u.role.value,
        u.branch.name if u.branch else "",
        u.department.name if u.department else "",
        _d(u.active), u.bm_delegation, u.bm_checklist, u.bm_fms,
        "all" if u.role.value in ("owner", "admin") else ", ".join(sorted(u.right_set)),
        _d(u.created_at),
    ] for u in rows]


def _checklist(db: Session, org_id: int):
    rows = db.scalars(select(RecurringRule).where(RecurringRule.org_id == org_id)
                      .order_by(RecurringRule.title)).all()
    names = {b.id: b.name for b in db.scalars(
        select(Branch).where(Branch.org_id == org_id)).all()}
    head = ["Rule ID", "Title", "Details", "Doer", "Doer email", "Company",
            "Frequency", "Day", "Due time", "Priority", "Needs audit",
            "Active", "Last created on"]
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    out = []
    for r in rows:
        if r.frequency.value == "weekly" and r.day_of is not None:
            day = days[r.day_of]
        elif r.frequency.value == "monthly":
            day = str(r.day_of or 1)
        else:
            day = ""
        out.append([r.id, r.title, r.details or "", r.doer.name, r.doer.email,
                    names.get(r.branch_id, ""),
                    r.frequency.value, day, r.due_time, r.priority.value,
                    _d(r.requires_audit), _d(r.active), _d(r.last_spawned_on)])
    return head, out


def _fms(db: Session, org_id: int):
    rows = db.scalars(select(FlowInstance).where(FlowInstance.org_id == org_id)
                      .order_by(FlowInstance.id)).all()
    head = ["Run ID", "Flow", "Reference", "Started by", "Started",
            "Completed", "Current step", "Total steps", "Status"]
    return head, [[
        i.id, i.flow.name, i.reference, i.started_by.name,
        _d(i.started_at), _d(i.completed_at), i.current_position,
        len(i.flow.steps), "Completed" if i.completed_at else "Running",
    ] for i in rows]


def _attachments(db: Session, org_id: int):
    rows = db.scalars(
        select(Attachment).join(Task).where(Task.org_id == org_id)
        .order_by(Attachment.id)
    ).all()
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    head = ["Attachment ID", "Task ID", "Task", "File name", "Type", "Size KB",
            "Uploaded by", "Uploaded", "Open link"]
    return head, [[
        a.id, a.task_id, a.task.title, a.filename, a.content_type or "",
        round(a.size / 1024, 1), a.uploaded_by.name, _d(a.created_at),
        f"{base}/attachments/{a.id}" if base else f"/attachments/{a.id}",
    ] for a in rows]


def _notes(db: Session, org_id: int):
    rows = db.scalars(
        select(TaskComment).join(Task).where(Task.org_id == org_id)
        .order_by(TaskComment.id)
    ).all()
    head = ["Note ID", "Task ID", "Task", "Author", "Note", "When"]
    return head, [[c.id, c.task_id, c.task.title, c.author.name,
                   c.body, _d(c.created_at)] for c in rows]


def _scores(db: Session, org_id: int):
    """A 30-day scorecard per person — the numbers CMD sir reviews."""
    from . import scoring

    start, end = scoring.resolve_window(None, None, 30)
    board = scoring.scoreboard(db, org_id, start, end)
    head = ["Name", "Company", "Benchmark D/C/F",
            "Delegation not done", "Delegation late", "Delegation score",
            "Checklist not done", "Checklist late", "Checklist score",
            "FMS not done", "FMS late", "FMS score",
            "False marking", "Total penalty", "Net score",
            "Planned", "Completed", "Overdue now", "Window"]
    out = []
    for r in board["people"]:
        u, c = r["user"], r["card"]
        cells = [u.name, u.branch.name if u.branch else "",
                 f"{u.bm_delegation}/{u.bm_checklist}/{u.bm_fms}"]
        for s in c.source_list:
            cells += [s.not_done_penalty, s.late_penalty, s.subtotal]
        cells += [c.false_penalty, c.total_penalty, c.score,
                  c.planned, c.completed, c.overdue_now,
                  f"{start:%d %b} – {end:%d %b %Y}"]
        out.append(cells)
    return head, out


def _companies(db: Session, org_id: int):
    rows = db.scalars(select(Branch).where(Branch.org_id == org_id)
                      .order_by(Branch.name)).all()
    return ["Company ID", "Name", "City"], [[b.id, b.name, b.city or ""]
                                            for b in rows]


def _help(db: Session, org_id: int):
    from ..models import HelpTicket
    rows = db.scalars(select(HelpTicket).where(HelpTicket.org_id == org_id)
                      .order_by(HelpTicket.id)).all()
    head = ["Ticket ID", "Subject", "Details", "Raised by", "Asked of",
            "Priority", "Needed by", "Status", "Decline reason",
            "Raised on", "Closed on", "Task ID"]
    return head, [[
        h.id, h.subject, h.details or "", h.raiser.name, h.helper.name,
        h.priority.value, _d(h.needed_by), h.status.value,
        h.decline_reason or "", _d(h.created_at), _d(h.closed_at),
        h.task_id or "",
    ] for h in rows[:MAX_ROWS_PER_TAB]]


TABS = [
    ("Tasks", _tasks),
    ("Help Desk", _help),
    ("Scores", _scores),
    ("Checklist", _checklist),
    ("FMS Runs", _fms),
    ("Attachments", _attachments),
    ("Notes", _notes),
    ("Users", _users),
    ("Companies", _companies),
]


# ---------------------------------------------------------------- syncing --
def sync(db: Session, org_id: int) -> dict:
    """Rewrite every tab. Returns a per-tab row count, or an error."""
    if not enabled():
        return {"ok": False, "error": "Google Sheet is not configured"}

    try:
        gc = _client()
        book = gc.open_by_key(SHEET_ID)
    except Exception as e:
        return {"ok": False, "error": f"Could not open the Sheet: {e}"}

    existing = {ws.title: ws for ws in book.worksheets()}
    counts, errors = {}, {}

    for title, build in TABS:
        try:
            head, rows = build(db, org_id)
            values = [head] + [[("" if v is None else v) for v in r] for r in rows]

            ws = existing.get(title)
            if ws is None:
                ws = book.add_worksheet(title=title,
                                        rows=max(len(values) + 50, 100),
                                        cols=max(len(head), 10))
                existing[title] = ws

            ws.clear()
            if values:
                ws.update(values, "A1", value_input_option="RAW")
                ws.freeze(rows=1)
            counts[title] = len(rows)
        except Exception as e:
            errors[title] = str(e)

    # a stamp so anyone opening the Sheet knows how fresh it is
    try:
        meta = existing.get("Last sync") or book.add_worksheet(
            title="Last sync", rows=20, cols=3)
        meta.clear()
        meta.update([
            ["Last updated", datetime.now().strftime("%d %b %Y, %I:%M %p")],
            ["Rows written", sum(counts.values())],
            ["Source", "GCS Autopilot — this Sheet is a read-only copy"],
            ["Note", "Edits made here are overwritten on the next sync."],
        ], "A1", value_input_option="RAW")
    except Exception:
        pass

    return {"ok": not errors, "counts": counts, "errors": errors,
            "total": sum(counts.values()),
            "at": datetime.now().strftime("%d %b %Y, %I:%M %p")}


def sheet_url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}" if SHEET_ID else ""
