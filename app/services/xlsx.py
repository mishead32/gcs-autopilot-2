"""Excel downloads.

One writer for every list in the app, so a Delegation report and the user
list come out looking the same: bold header frozen at the top, a filter
dropdown on every column, columns wide enough to read, and real dates and
numbers in the cells rather than text that looks like them.

The last part matters more than it sounds. A date written as "25 Sep 2026"
is a string to Excel: it will not sort, it will not filter by month, and a
pivot table cannot group it. So a datetime goes in as a datetime with a
display format, and a count goes in as a number.

Every export is built from the SAME query the page ran, in the route that
already ran it. There is no second copy of the filtering to drift out of
step with the first — an export that quietly disagrees with the screen it
came from is worse than no export.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime

from fastapi import Response
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .. import clock

MEDIA = ("application/vnd.openxmlformats-officedocument."
         "spreadsheetml.sheet")

HEAD_FILL = PatternFill("solid", fgColor="0F4C75")
HEAD_FONT = Font(bold=True, color="FFFFFF", size=11)
TITLE_FONT = Font(bold=True, size=13, color="0F4C75")
NOTE_FONT = Font(size=9, color="64748B")
THIN = Side(style="thin", color="D8E0E8")
CELL_BORDER = Border(bottom=THIN)

DATE_FMT = "dd mmm yyyy hh:mm AM/PM"
DAY_FMT = "dd mmm yyyy"

MIN_WIDTH, MAX_WIDTH = 10, 52


def _value(row, col):
    """A column is (header, getter). The getter is a callable or an attribute
    name; anything that blows up on a particular row yields a blank cell
    rather than a failed download."""
    _, getter = col[0], col[1]
    try:
        v = getter(row) if callable(getter) else getattr(row, getter, None)
    except Exception:
        return None
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if v is None or isinstance(v, (str, int, float, datetime, date)):
        return v
    # Anything else — a tuple, an enum, a model object somebody added a
    # column for later — is written as text rather than bringing the whole
    # download down. openpyxl raises on a type it does not know, and one
    # odd cell must never cost somebody their report.
    return str(v)


def _safe_name(name: str) -> str:
    """Excel refuses : \\ / ? * [ ] in a sheet name, and caps it at 31."""
    return re.sub(r"[:\\/?*\[\]]", "-", name)[:31] or "Sheet"


def add_sheet(wb: Workbook, title: str, columns, rows, note: str = "",
              first: bool = False):
    """One tab: an optional title line, a note line, then the table."""
    ws = wb.active if first else wb.create_sheet()
    ws.title = _safe_name(title)

    top = 1
    if title:
        ws.cell(row=top, column=1, value=title).font = TITLE_FONT
        top += 1
    if note:
        ws.cell(row=top, column=1, value=note).font = NOTE_FONT
        top += 1
    if title or note:
        ws.cell(row=top, column=1,
                value=f"Exported {clock.stamp()}").font = NOTE_FONT
        top += 2

    head_row = top
    for i, col in enumerate(columns, start=1):
        c = ws.cell(row=head_row, column=i, value=col[0])
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[head_row].height = 22

    widths = [len(str(col[0])) + 4 for col in columns]
    r = head_row
    for row in rows:
        r += 1
        for i, col in enumerate(columns, start=1):
            v = _value(row, col)
            cell = ws.cell(row=r, column=i, value=v)
            cell.border = CELL_BORDER
            cell.alignment = Alignment(vertical="top")
            if isinstance(v, datetime):
                cell.number_format = DATE_FMT
                shown = 20
            elif isinstance(v, date):
                cell.number_format = DAY_FMT
                shown = 14
            else:
                shown = len(str(v)) if v is not None else 0
            widths[i - 1] = max(widths[i - 1], min(shown + 3, MAX_WIDTH))

    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(MIN_WIDTH, w)

    # Freeze under the header and give every column a filter dropdown, so a
    # long export is usable the moment it opens.
    ws.freeze_panes = ws.cell(row=head_row + 1, column=1)
    if rows:
        ws.auto_filter.ref = (f"A{head_row}:"
                              f"{get_column_letter(len(columns))}{r}")
    else:
        ws.cell(row=head_row + 1, column=1,
                value="Nothing matched these filters.").font = NOTE_FONT
    return ws


def book(filename: str, sheets) -> Response:
    """Build the workbook and hand it back as a download.

    `sheets` is a list of (title, columns, rows, note).
    """
    wb = Workbook()
    for n, (title, columns, rows, note) in enumerate(sheets):
        add_sheet(wb, title, columns, rows, note, first=(n == 0))
    buf = io.BytesIO()
    wb.save(buf)
    return _download(buf.getvalue(), filename)


def one(filename: str, title: str, columns, rows, note: str = "") -> Response:
    """The common case: a single tab."""
    return book(filename, [(title, columns, rows, note)])


def _download(data: bytes, filename: str) -> Response:
    stamped = f"{filename}-{clock.today():%Y-%m-%d}.xlsx"
    return Response(
        content=data,
        media_type=MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{stamped}"',
                 "Content-Length": str(len(data)),
                 # never let a proxy or the browser hand back yesterday's file
                 "Cache-Control": "no-store"},
    )


def wants(value: str) -> bool:
    """Did the page ask for the Excel version of itself?"""
    return (value or "").strip().lower() in ("xlsx", "excel", "1", "yes")


# ------------------------------------------------------- shared columns ----
# A task looks the same in every export it appears in, so the Delegation
# report and the Audit report can be read side by side.
def task_columns(show_doer: bool = True):
    cols = [
        ("Task", lambda t: t.title),
        ("Branch", lambda t: t.branch.name if t.branch else ""),
    ]
    if show_doer:
        cols += [("Doer", lambda t: t.doer.name if t.doer else ""),
                 ("Doer email", lambda t: t.doer.email if t.doer else "")]
    cols += [
        ("Assigned by", lambda t: t.assigner.name if t.assigner else ""),
        ("Work type", lambda t: SOURCE_NAMES.get(t.source.value, t.source.value)),
        ("Priority", lambda t: t.priority.value.title()),
        ("Weight", lambda t: t.weight),
        ("Status", lambda t: t.status.value.replace("_", " ").title()),
        ("Planned date", lambda t: t.due_at),
        ("Completed on", lambda t: t.closed_at or t.submitted_at),
        ("On time", lambda t: "" if t.was_on_time is None
                              else ("Yes" if t.was_on_time else "No")),
        ("Days late", _days_late),
        ("Audit", lambda t: t.audit_label),
        ("Auditor", lambda t: t.auditor.name if t.auditor else ""),
        ("Audited on", lambda t: t.audited_at),
        ("Audit score", lambda t: t.audit_score),
        ("Audit remark", lambda t: t.audit_remark or ""),
        ("False marking", lambda t: "Yes" if t.false_marked else ""),
        ("False marking reason", lambda t: t.false_mark_reason or ""),
        ("Reopened", lambda t: t.reopen_count or 0),
        ("Attachments", lambda t: len(t.attachments)),
        ("Completion note", lambda t: t.completion_note or ""),
        ("Flow run", lambda t: (t.flow_instance.reference
                                if t.flow_instance else "")),
    ]
    return cols


SOURCE_NAMES = {"delegation": "Delegation", "recurring": "Checklist",
                "flow": "FMS"}


def _days_late(t):
    """Whole days past the deadline — blank while it is still in hand.

    Counted against the moment it was finished, or against now for work that
    is still open, which is the figure somebody chasing a list actually wants.
    """
    end = t.closed_at or t.submitted_at
    if end is None:
        end = clock.now()
        if end <= t.due_at:
            return ""
    elif end <= t.due_at:
        return ""
    return round((end - t.due_at).total_seconds() / 86400, 1)
