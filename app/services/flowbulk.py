"""Starting many runs of one flow from a spreadsheet.

A renewal flow is run for four hundred members. Filling the start form four
hundred times is not a thing anybody does — they stop using the system and
keep the list in Excel, which is where it was before.

So: download a file whose column headings ARE this flow's own questions,
fill it in, upload it, see what is ready and what needs fixing, and start
the ready ones. One row, one run.

Two things make this worth doing properly rather than quickly.

The columns are the flow's questions, generated from the flow itself. A
fixed template that everybody maps by hand is how a column ends up in the
wrong place and four hundred runs are created with the trainer in the
member-name field. Here the heading IS the question, so a file built for
one flow cannot be read as another — and if the questions change, the old
file says so instead of importing wrongly.

And the answers are checked by the same code the start form uses
(`flowform.check_answer`). If the two drifted, a file would import rows the
form would have refused, and the difference would be found weeks later by
somebody holding work that should never have existed.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field as dc_field

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Branch, CONTEXT_PEOPLE, Flow, User
from . import flowform

HEAD = PatternFill("solid", fgColor="0F4C81")
HEAD_FONT = Font(color="FFFFFF", bold=True, size=11)
NOTE_FONT = Font(color="6B7280", size=9, italic=True)

# The column every file has, because a run is for one company and a step
# given to a position cannot be resolved without knowing which.
COMPANY = "Which company"

# Rows past this are refused rather than half-imported. Four hundred is the
# size of the job described; ten thousand is a mistake, and finding out
# after twenty minutes of work is worse than being told now.
MAX_ROWS = 2000

# How far down the sheet the dropdowns reach.
VALIDATE_TO = 1000


def _note(field: dict) -> str:
    """What to write under a heading so the column is filled in correctly."""
    kind = field["type"]
    need = "Required." if field["required"] else "Optional."
    if kind == "select":
        return f"{need} One of: " + ", ".join(field["options"]) + "."
    if kind == "person":
        return (f"{need} The person's name as it is on GCS Autopilot, or "
                "their email address.")
    if kind == "yesno":
        return f"{need} YES or NO."
    if kind == "number":
        return f"{need} A number."
    if kind == "date":
        return f"{need} A date, e.g. 2026-11-20."
    if kind == "datetime":
        return f"{need} A date and time, e.g. 2026-11-20 15:30."
    if kind == "textarea":
        return f"{need} Long text."
    return need


def columns(flow: Flow, with_company: bool) -> list[dict]:
    """The questions this file asks, in the order the start form asks them.

    File questions are left out. A spreadsheet cell cannot hold a document,
    and a column people cannot fill in is a column they try to fill in.
    """
    out = [f for f in flow.start_form_fields if f["type"] != "file"]
    if with_company:
        out = [{"label": COMPANY, "type": "company", "options": [],
                "required": bool([s for s in flow.steps if s.doer_dept])}] + out
    return out


def needs_company(flow: Flow) -> bool:
    """Does this flow's file need a company column?

    A flow pinned to one company answers for itself. Otherwise it is worth
    asking: a step given to a position cannot be resolved without it, and
    the company is what the run is filed under in every report.
    """
    return flow.branch_id is None


def template(db: Session, flow: Flow) -> bytes:
    """The .xlsx to fill in: this flow's questions as column headings."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Rows"

    cols = columns(flow, needs_company(flow))
    for i, f in enumerate(cols, start=1):
        c = ws.cell(row=1, column=i, value=f["label"])
        c.fill, c.font = HEAD, HEAD_FONT
        c.alignment = Alignment(vertical="center", wrap_text=True)
        n = ws.cell(row=2, column=i, value=_note(f))
        n.font = NOTE_FONT
        n.alignment = Alignment(vertical="top", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = max(
            16, min(34, len(f["label"]) + 6))
    ws.row_dimensions[1].height = 24
    ws.row_dimensions[2].height = 34
    ws.freeze_panes = "A3"

    # Dropdowns where there is a known list, so the common mistakes cannot
    # be typed in the first place.
    people = [u.name for u in flowform.staff(db, flow.org_id)]
    branches = [b.name for b in db.scalars(
        select(Branch).where(Branch.org_id == flow.org_id)
        .order_by(Branch.name)).all()]
    for i, f in enumerate(cols, start=1):
        letter = get_column_letter(i)
        if f["type"] == "select":
            _dropdown(ws, letter, f["options"])
        elif f["type"] == "yesno":
            _dropdown(ws, letter, ["YES", "NO"])
        elif f["type"] == "person":
            _dropdown(ws, letter, people)
        elif f["type"] == "company":
            _dropdown(ws, letter, branches)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _dropdown(ws, letter: str, values: list[str]) -> None:
    """A list a cell must come from — when Excel will take one.

    Excel caps an inline list at 255 characters, and a gym with eighty
    trainers is well past that. Rather than truncate the list — which would
    forbid two thirds of the staff — the column is left free to type in and
    the answer is checked on upload, where a wrong name is reported with
    the right one suggested.
    """
    joined = ",".join(v.replace(",", " ") for v in values if v)
    if not joined or len(joined) > 255:
        return
    dv = DataValidation(type="list", formula1=f'"{joined}"',
                        allow_blank=True, showDropDown=False)
    ws.add_data_validation(dv)
    dv.add(f"{letter}3:{letter}{VALIDATE_TO}")


# ------------------------------------------------------------- reading ----

@dataclass
class Row:
    number: int                       # the row in the sheet, as Excel shows it
    context: dict = dc_field(default_factory=dict)
    people: dict = dc_field(default_factory=dict)
    branch_id: int | None = None
    name: str = ""                    # what the run will be called
    problems: list = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class Parsed:
    rows: list = dc_field(default_factory=list)
    missing: list = dc_field(default_factory=list)   # headings not in the file
    extra: list = dc_field(default_factory=list)     # headings nothing asked for

    @property
    def ready(self) -> list:
        return [r for r in self.rows if r.ok]

    @property
    def bad(self) -> list:
        return [r for r in self.rows if not r.ok]


def _cell(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        # A date typed into Excel comes back as a datetime. Written the way
        # the start form writes one, so both routes store the same string.
        return (value.strftime("%Y-%m-%d %H:%M")
                if getattr(value, "hour", 0) or getattr(value, "minute", 0)
                else value.strftime("%Y-%m-%d"))
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return " ".join(str(value).split())


def parse(db: Session, flow: Flow, blob: bytes) -> Parsed:
    """Read the file against THIS flow's questions and say what is wrong."""
    try:
        wb = load_workbook(io.BytesIO(blob), data_only=True)
    except Exception:
        raise ValueError("That file is not a readable Excel workbook (.xlsx).")
    ws = wb[wb.sheetnames[0]]
    heads = head_map(next(ws.iter_rows(min_row=1, max_row=1,
                                       values_only=True), ()))

    return read_rows(db, flow, heads,
                     ws.iter_rows(min_row=3, values_only=True), 3)


def head_map(values) -> dict:
    """{heading in lower case: its 1-based column} for one row of headings."""
    heads = {}
    for i, raw in enumerate(values, start=1):
        key = _cell(raw).lower()
        if key and key not in heads:
            heads[key] = i
    return heads


def read_rows(db: Session, flow: Flow, heads: dict, raw_rows, first: int
              ) -> Parsed:
    """Rows checked against this flow's questions, wherever they came from.

    Shared by the spreadsheet upload and the live Google Sheet on purpose.
    Two readers would be two sets of rules about what a valid answer is, and
    the day they disagree a sheet quietly starts runs the file would have
    refused — found weeks later by somebody holding work nobody meant to
    create.
    """
    cols = columns(flow, needs_company(flow))
    wanted = {f["label"].strip().lower(): f for f in cols}

    out = Parsed()
    out.missing = [f["label"] for f in cols
                   if f["label"].strip().lower() not in heads]
    out.extra = [h for h in heads if h not in wanted]
    if out.missing:
        # Reading on would mean importing rows with whole questions
        # unanswered, which is exactly the silent wrongness this avoids.
        return out

    branches = {b.name.strip().lower(): b for b in db.scalars(
        select(Branch).where(Branch.org_id == flow.org_id)).all()}

    for n, raw in enumerate(raw_rows, start=first):
        if not raw or all(_cell(v) == "" for v in raw):
            continue
        if len(out.rows) >= MAX_ROWS:
            out.rows.append(Row(number=n, problems=[
                f"More than {MAX_ROWS} rows. Split the file and upload it "
                "in parts."]))
            break

        row = Row(number=n)
        for f in cols:
            idx = heads[f["label"].strip().lower()] - 1
            value = _cell(raw[idx]) if idx < len(raw) else ""

            if f["type"] == "company":
                if not value:
                    if f["required"]:
                        row.problems.append(
                            "This flow has steps given to a position, so "
                            "each run needs a company.")
                    continue
                found = branches.get(value.lower())
                if found is None:
                    row.problems.append(
                        f"“{value}” is not one of your companies.")
                else:
                    row.branch_id = found.id
                continue

            stored, person, problem = flowform.check_answer(
                db, flow.org_id, f, value)
            if problem:
                row.problems.append(problem)
                continue
            if not stored:
                continue
            row.context[f["label"]] = stored
            if person is not None:
                row.people[f["label"]] = person.id

        if row.people:
            row.context[CONTEXT_PEOPLE] = dict(row.people)
        row.name = _name_for(flow, row.context)
        out.rows.append(row)

    return out


def _name_for(flow: Flow, context: dict) -> str:
    """What the run will be called, shown on the check page before it is."""
    from .flows import reference_from
    return reference_from(flow, context)


def commit(db: Session, flow: Flow, starter: User, parsed: Parsed) -> list:
    """Start a run for every row that is ready. Returns the runs made.

    Committed per row rather than all at once: four hundred runs is a lot of
    work to lose to the four hundredth, and a row that fails in a way the
    check could not see should not take the other three hundred and
    ninety-nine with it.
    """
    from .flows import start_flow

    made = []
    for row in parsed.ready:
        try:
            inst = start_flow(db, flow, "", starter, context=dict(row.context),
                              branch_id=row.branch_id)
            made.append(inst)
        except Exception as exc:                      # noqa: BLE001
            db.rollback()
            row.problems.append(
                f"Could not be started: {type(exc).__name__}. The rows before "
                "it were.")
    return made
