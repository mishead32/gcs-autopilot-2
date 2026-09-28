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

from .. import clock
from ..models import (
    Task, TaskSource, Priority, RecurringRule, Recurrence, User, Branch,
)

HEAD = PatternFill("solid", fgColor="0F4C81")
HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
NOTE_FONT = Font(color="6B7A8C", size=9, italic=True)

DELEGATION_COLS = [
    ("Task title", 42, "Required. What must be done."),
    ("Details", 46, "Optional. Instructions, and what 'done' looks like."),
    ("Doer", 26, "Required. Their email, or their full name as it is spelt in Users."),
    ("Company", 26, "Optional. Defaults to the doer's own company."),
    ("Priority", 14, "high / medium / low. High counts 5x, medium 2x, low 1x. Blank = medium."),
    ("Due date", 14, "Required. DD/MM/YYYY, e.g. 25/09/2026"),
    ("Due time", 12, "HH:MM 24-hour, e.g. 18:00. Blank = 18:00."),
    ("Needs audit", 13, "YES or NO. Blank = NO."),
]

CHECKLIST_COLS = [
    ("Task title", 42, "Required. The recurring job."),
    ("Details", 46, "Optional."),
    ("Doer", 26, "Required. Their email, or their full name as it is spelt in Users."),
    ("Company", 26, "Optional. Defaults to the doer's own company."),
    ("Frequency", 16, "daily / weekdays / weekly / monthly / yearly"),
    ("Day", 10, "Weekly: Mon-Sun. Monthly: 1-31. Yearly: DD/MM. Daily: blank."),
    ("Due time", 12, "HH:MM 24-hour, e.g. 18:00. Blank = 18:00."),
    ("Priority", 14, "high / medium / low. High counts 5x, medium 2x, low 1x. Blank = medium."),
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
    soon = (clock.now() + timedelta(days=3)).strftime("%d/%m/%Y")
    sample_email = users[0].email if users else "someone@gcs.local"
    sample_branch = branches[0].name if branches else ""

    if kind == "delegation":
        ws = _sheet(wb, "Delegation", DELEGATION_COLS, [
            ["Reconcile September PT collections",
             "Cross-check the billing export against the register.",
             sample_email, sample_branch, "high", soon, "18:00", "YES"],
            ["Chase pending NBD follow-ups", "", sample_email, "", "medium", soon, "", "NO"],
        ])
        _dropdown(ws, "E", ["high", "medium", "low"])
        _dropdown(ws, "H", ["YES", "NO"])
    else:
        ws = _sheet(wb, "Checklist", CHECKLIST_COLS, [
            ["Post daily sales MIS to CMD", "", sample_email, sample_branch,
             "weekdays", "", "19:00", "high", "NO"],
            ["Weekly trainer performance review", "", sample_email, "",
             "weekly", "Mon", "12:00", "high", "YES"],
            ["Monthly machine maintenance audit", "", sample_email, "",
             "monthly", "1", "16:00", "high", "YES"],
        ])
        _dropdown(ws, "E", ["daily", "weekdays", "weekly", "monthly", "yearly"])
        _dropdown(ws, "H", ["high", "medium", "low"])
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
    return Priority(s) if s in {p.value for p in Priority} else Priority.MEDIUM


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

    # A person can be named by their email or by their name. Every real list
    # anybody keeps has names in it, and making somebody look up fourteen
    # email addresses to import three hundred rows is work the software
    # should do. Two names spelt the same is the only case that has to be
    # refused, and it is refused loudly rather than guessed at.
    people = db.scalars(
        select(User).where(User.org_id == org_id, User.active.is_(True))).all()
    users = {u.email.lower(): u for u in people}
    by_name: dict[str, list[User]] = {}
    for u in people:
        by_name.setdefault(_norm_name(u.name), []).append(u)
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

            doer = _find_person(_text(raw[2]), users, by_name)

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
                freq = _text(raw[4]).lower()
                if not freq:
                    raise ValueError(
                        "Frequency is empty — use daily, weekdays, weekly, "
                        "monthly or yearly. A blank one used to be read as "
                        "daily, which is a task landing on somebody every "
                        "single day because a cell was missed.")
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
                elif freq == "yearly":
                    day = yearly_day(day_raw)
                base["day_of"] = day
                # the same wording the Checklist page uses, so the preview and
                # the saved rule cannot describe the schedule differently
                base["schedule_label"] = RecurringRule(
                    frequency=base["frequency"], day_of=day).schedule_label

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


# ------------------------------------------------------- people by name ----
def _norm_name(name: str) -> str:
    """Fold a name down to what two spellings of it have in common.

    Case and extra spaces are noise. A middle name or an initial is not, so
    "Alok Kumar" and "Alok K Kumar" stay different people — guessing there
    would hand somebody else's work to the wrong person.
    """
    return " ".join((name or "").lower().split())


def _find_person(raw: str, users: dict, by_name: dict):
    """Turn whatever is in the Doer column into one person, or say why not."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("The Doer column is empty — put their name or email")

    found = users.get(text.lower())
    if found:
        return found
    if "@" in text:
        raise ValueError(f"No active user with the email '{text}'")

    matches = by_name.get(_norm_name(text), [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"There are {len(matches)} active people called '{text}' — "
            "use their email address instead so the right one gets it")
    raise ValueError(
        f"Nobody active is called '{text}'. Check the spelling against the "
        "Users page, or use their email address")


def yearly_day(raw: str) -> int:
    """DD/MM (or a real date) stored as MMDD — 17/04 becomes 417."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("Yearly needs the date it falls on — use DD/MM")
    if isinstance(raw, datetime):
        return raw.month * 100 + raw.day
    parts = [p for p in text.replace("-", "/").replace(".", "/").split("/") if p]
    if len(parts) < 2 or not all(p.isdigit() for p in parts[:2]):
        raise ValueError(f"'{text}' is not a date — yearly wants DD/MM, e.g. 17/04")
    dd, mm = int(parts[0]), int(parts[1])
    if dd > 31 and mm <= 31:               # somebody typed MM/DD
        dd, mm = mm, dd
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        raise ValueError(f"'{text}' is not a real date — yearly wants DD/MM")
    return mm * 100 + dd
