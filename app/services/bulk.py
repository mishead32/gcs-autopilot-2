"""Bulk import of Delegation tasks and Checklist rules from Excel.

The flow is deliberately two-step: parse and validate everything first, show
the person exactly what will happen, and only write to the database when they
confirm. A half-imported spreadsheet is worse than a rejected one.

Rows are validated independently, so one bad row doesn't block the other 499 —
the report tells you which rows failed and why, and you can import the good
ones and fix the rest.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Task, TaskSource, Priority, RecurringRule, Recurrence, User, Branch,
)

HEAD = PatternFill("solid", fgColor="0F4C81")
HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
NOTE_FONT = Font(color="6B7A8C", size=9, italic=True)

DELEGATION_COLS = [
    ("Task title", 42, "Required. What must be done."),
    ("Details", 46, "Optional. Instructions, and what 'done' looks like."),
    ("Doer email", 26, "Required. Must match a user already in the system."),
    ("Company", 26, "Optional. Defaults to the doer's own company."),
    ("Priority", 14, "low / normal / high / critical. Blank = normal."),
    ("Due date", 14, "Required. DD/MM/YYYY, e.g. 25/09/2026"),
    ("Due time", 12, "HH:MM 24-hour, e.g. 18:00. Blank = 18:00."),
    ("Needs audit", 13, "YES or NO. Blank = NO."),
]

CHECKLIST_COLS = [
    ("Task title", 42, "Required. The recurring job."),
    ("Details", 46, "Optional."),
    ("Doer email", 26, "Required. Must match a user already in the system."),
    ("Company", 26, "Optional. Defaults to the doer's own company."),
    ("Frequency", 16, "daily / weekdays / weekly / monthly"),
    ("Day", 10, "Weekly: Mon-Sun. Monthly: 1-31. Daily: leave blank."),
    ("Due time", 12, "HH:MM 24-hour, e.g. 18:00. Blank = 18:00."),
    ("Priority", 14, "low / normal / high / critical. Blank = normal."),
    ("Needs audit", 13, "YES or NO. Blank = NO."),
]

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}


# ------------------------------------------------------------- templates ---
def _sheet(wb, title, cols, examples):
    ws = wb.create_sheet(title) if wb.sheetnames != ["Sheet"] else wb.active
    ws.title = title

    for i, (name, width, note) in enumerate(cols, start=1):
        c = ws.cell(row=1, column=i, value=name)
        c.fill, c.font = HEAD, HEAD_FONT
        c.alignment = Alignment(vertical="center", wrap_text=True)
        n = ws.cell(row=2, column=i, value=note)
        n.font = NOTE_FONT
        n.alignment = Alignment(vertical="top", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = width

    ws.row_dimensions[1].height = 22
    ws.row_dimensions[2].height = 34
    ws.freeze_panes = "A3"

    for r, row in enumerate(examples, start=3):
        for c, val in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=val)
    return ws


def _dropdown(ws, col_letter, values, first=3, last=500):
    dv = DataValidation(type="list", formula1='"' + ",".join(values) + '"',
                        allow_blank=True, showDropDown=False)
    ws.add_data_validation(dv)
    dv.add(f"{col_letter}{first}:{col_letter}{last}")


def template(db: Session, org_id: int, kind: str) -> bytes:
    """Build the .xlsx the person fills in, pre-loaded with their own people."""
    users = db.scalars(
        select(User).where(User.org_id == org_id, User.active.is_(True))
        .order_by(User.name)
    ).all()
    branches = db.scalars(
        select(Branch).where(Branch.org_id == org_id).order_by(Branch.name)
    ).all()

    wb = Workbook()
    soon = (datetime.now() + timedelta(days=3)).strftime("%d/%m/%Y")
    sample_email = users[0].email if users else "someone@gcs.local"
    sample_branch = branches[0].name if branches else ""

    if kind == "delegation":
        ws = _sheet(wb, "Delegation", DELEGATION_COLS, [
            ["Reconcile September PT collections",
             "Cross-check the billing export against the register.",
             sample_email, sample_branch, "high", soon, "18:00", "YES"],
            ["Chase pending NBD follow-ups", "", sample_email, "", "normal", soon, "", "NO"],
        ])
        _dropdown(ws, "E", ["low", "normal", "high", "critical"])
        _dropdown(ws, "H", ["YES", "NO"])
    else:
        ws = _sheet(wb, "Checklist", CHECKLIST_COLS, [
            ["Post daily sales MIS to CMD", "", sample_email, sample_branch,
             "weekdays", "", "19:00", "high", "NO"],
            ["Weekly trainer performance review", "", sample_email, "",
             "weekly", "Mon", "12:00", "high", "YES"],
            ["Monthly machine maintenance audit", "", sample_email, "",
             "monthly", "1", "16:00", "critical", "YES"],
        ])
        _dropdown(ws, "E", ["daily", "weekdays", "weekly", "monthly"])
        _dropdown(ws, "H", ["low", "normal", "high", "critical"])
        _dropdown(ws, "I", ["YES", "NO"])

    # a reference tab so nobody has to guess an email address
    ref = wb.create_sheet("People & Companies")
    ref.cell(row=1, column=1, value="Name").fill = HEAD
    ref.cell(row=1, column=1).font = HEAD_FONT
    ref.cell(row=1, column=2, value="Email").fill = HEAD
    ref.cell(row=1, column=2).font = HEAD_FONT
    ref.cell(row=1, column=3, value="Company").fill = HEAD
    ref.cell(row=1, column=3).font = HEAD_FONT
    for i, u in enumerate(users, start=2):
        ref.cell(row=i, column=1, value=u.name)
        ref.cell(row=i, column=2, value=u.email)
        ref.cell(row=i, column=3, value=u.branch.name if u.branch else "")
    ref.column_dimensions["A"].width = 28
    ref.column_dimensions["B"].width = 30
    ref.column_dimensions["C"].width = 28

    ref2 = ref.cell(row=len(users) + 3, column=1, value="Companies")
    ref2.font = Font(bold=True)
    for i, b in enumerate(branches, start=len(users) + 4):
        ref.cell(row=i, column=1, value=b.name)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# -------------------------------------------------------------- parsing ----
@dataclass
class Row:
    number: int
    data: dict = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class Parsed:
    kind: str
    rows: list[Row] = field(default_factory=list)

    @property
    def good(self) -> list[Row]:
        return [r for r in self.rows if r.ok]

    @property
    def bad(self) -> list[Row]:
        return [r for r in self.rows if not r.ok]


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d/%m/%Y")
    return str(v).strip()


def _priority(v) -> Priority:
    s = _text(v).lower()
    return Priority(s) if s in {p.value for p in Priority} else Priority.NORMAL


def _yes(v) -> bool:
    return _text(v).lower() in ("yes", "y", "true", "1")


def _due(date_v, time_v) -> datetime:
    """DD/MM/YYYY plus HH:MM. Excel may hand us a real datetime already."""
    if isinstance(date_v, datetime):
        d = date_v.date()
    else:
        s = _text(date_v)
        if not s:
            raise ValueError("Due date is missing")
        s = s.split(" ")[0].replace("-", "/").replace(".", "/")
        parts = [p for p in s.split("/") if p]
        if len(parts) != 3:
            raise ValueError(f"'{s}' is not a date — use DD/MM/YYYY")
        dd, mm, yy = (int(p) for p in parts)
        if yy < 100:
            yy += 2000
        if dd > 31 and yy <= 31:          # someone typed YYYY/MM/DD
            dd, yy = yy, dd
        try:
            d = datetime(yy, mm, dd).date()
        except ValueError:
            raise ValueError(f"'{s}' is not a real date")

    t = _text(time_v) or "18:00"
    if isinstance(time_v, datetime):
        t = time_v.strftime("%H:%M")
    try:
        hh, mi = (int(x) for x in t.replace(".", ":").split(":")[:2])
        if not (0 <= hh < 24 and 0 <= mi < 60):
            raise ValueError
    except Exception:
        raise ValueError(f"'{t}' is not a time — use HH:MM, e.g. 18:00")

    return datetime.combine(d, datetime.min.time()).replace(hour=hh, minute=mi)


def parse(db: Session, org_id: int, kind: str, blob: bytes) -> Parsed:
    try:
        wb = load_workbook(io.BytesIO(blob), data_only=True)
    except Exception:
        raise ValueError("That file isn't a readable Excel workbook (.xlsx).")

    ws = wb[wb.sheetnames[0]]
    users = {u.email.lower(): u for u in db.scalars(
        select(User).where(User.org_id == org_id, User.active.is_(True))).all()}
    branches = {b.name.strip().lower(): b for b in db.scalars(
        select(Branch).where(Branch.org_id == org_id)).all()}

    out = Parsed(kind=kind)
    for i, raw in enumerate(ws.iter_rows(min_row=3, values_only=True), start=3):
        if not raw or all(v is None or _text(v) == "" for v in raw):
            continue
        # skip the note row if the person left it in place
        if _text(raw[0]).lower().startswith("required."):
            continue

        r = Row(number=i)
        try:
            title = _text(raw[0])
            if not title:
                raise ValueError("Task title is empty")

            email = _text(raw[2]).lower()
            doer = users.get(email)
            if not doer:
                raise ValueError(
                    f"No active user with the email '{_text(raw[2])}'"
                    if email else "Doer email is empty")

            bname = _text(raw[3])
            branch = branches.get(bname.lower()) if bname else None
            if bname and not branch:
                raise ValueError(f"No company called '{bname}'")

            base = {
                "title": title[:250],
                "details": _text(raw[1]) or None,
                "doer": doer,
                "branch_id": branch.id if branch else doer.branch_id,
            }

            if kind == "delegation":
                base["priority"] = _priority(raw[4])
                base["due_at"] = _due(raw[5], raw[6])
                base["requires_audit"] = _yes(raw[7])
            else:
                freq = _text(raw[4]).lower() or "daily"
                if freq not in {f.value for f in Recurrence}:
                    raise ValueError(
                        f"'{freq}' is not a frequency — use daily, weekdays, "
                        "weekly or monthly")
                base["frequency"] = Recurrence(freq)

                day_raw = _text(raw[5])
                day = None
                if freq == "weekly":
                    key = day_raw.lower()[:3]
                    if key not in WEEKDAYS:
                        raise ValueError(
                            f"Weekly needs a day — got '{day_raw}'. Use Mon-Sun.")
                    day = WEEKDAYS[key]
                elif freq == "monthly":
                    if not day_raw.isdigit() or not 1 <= int(day_raw) <= 31:
                        raise ValueError(
                            f"Monthly needs a day 1-31 — got '{day_raw}'")
                    day = int(day_raw)
                base["day_of"] = day

                t = _text(raw[6]) or "18:00"
                _due("01/01/2026", t)          # reuse the time validator
                base["due_time"] = t if ":" in t else "18:00"
                base["priority"] = _priority(raw[7])
                base["requires_audit"] = _yes(raw[8])

            r.data = base
        except ValueError as e:
            r.error = str(e)
        except Exception as e:
            r.error = f"Could not read this row ({e})"

        out.rows.append(r)

    if not out.rows:
        raise ValueError(
            "No rows found. Put your data from row 3 down, under the headings.")
    return out


# ------------------------------------------------------------- committing --
def commit(db: Session, org_id: int, actor: User, parsed: Parsed) -> int:
    """Write only the valid rows. Returns how many were created."""
    from . import notify

    made = 0
    for r in parsed.good:
        d = r.data
        if parsed.kind == "delegation":
            t = Task(
                org_id=org_id, branch_id=d["branch_id"], title=d["title"],
                details=d["details"], assigner_id=actor.id, doer_id=d["doer"].id,
                priority=d["priority"], source=TaskSource.DELEGATION,
                due_at=d["due_at"], requires_audit=d["requires_audit"],
            )
            db.add(t)
            db.flush()
            notify.queue_task_assigned(db, t)
        else:
            db.add(RecurringRule(
                org_id=org_id, branch_id=d["branch_id"], title=d["title"],
                details=d["details"], doer_id=d["doer"].id, assigner_id=actor.id,
                priority=d["priority"], frequency=d["frequency"],
                day_of=d["day_of"], due_time=d["due_time"],
                requires_audit=d["requires_audit"],
            ))
        made += 1

    db.commit()
    return made
