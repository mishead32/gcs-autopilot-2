"""The To Do List sheet: finished work comes back to it.

The CMD's To Do List sheet is where tasks are written down first and then
entered in GCS Autopilot; column I carries the Autopilot ID (DEL-1125,
FMS-12). This reads column I from a starting row down and, for every row
whose work is finished in Autopilot, writes back to the SAME row:

  * column K  the proof the doer attached while completing it (a link)
  * the row   coloured dark green, columns B to K

What it will and will not touch:

  * Only Delegation (DEL-) and FMS (FMS-) IDs. Anything else in column I
    ("FMS Dated- 23 Nov 2026", CL-…) is ignored.
  * A row whose column I is blank is skipped: nothing is read from or written
    to it. Rows above the starting row are never read at all.
  * A row is finished only when EVERY ID in its cell is finished. A
    Delegation task is finished when it is Completed, or handed in by the
    doer and waiting for audit (Submitted). An FMS ID names a whole run, finished when the run is.
  * Column K is never overwritten. If somebody already pasted a link there,
    it stays; the row is still coloured.
  * Writing needs the service account to be an EDITOR of the sheet.

A row is remembered once done (TodoMark) so it is not rewritten every pass,
and looked at again only if its ID cell changes.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import (Attachment, FlowInstance, Organization, Task, TaskSource,
                      TaskStatus, TodoMark, ref_text)
from . import approvals, sheets

DEFAULT_SHEET = "1p-bsAGpT5cVUSw4zkvJG96zHmcKFvDc29l2rf_AgBO8"
DEFAULT_TAB = "Weekly/Today's Scheduling"
DEFAULT_FROM = 59554
DEFAULT_TIMES = "12:00,17:00"      # when it looks at the sheet, every day
MAX_PER_PASS = 150

#: The dark green the sheet already uses on finished rows (#6AA84F).
GREEN = {"red": 0.41568628, "green": 0.65882355, "blue": 0.30980393}
#: B to K, zero-based and end-exclusive, as the Sheets API wants them.
COLOR_FROM_COL, COLOR_TO_COL = 1, 11

EDIT_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
enabled = approvals.enabled
sheet_id_from = approvals.sheet_id_from

_REF = re.compile(r"\b(DEL|FMS)\s*-\s*0*(\d+)\b", re.I)


def robot() -> str:
    return approvals.robot()


def settings(org: Organization) -> dict:
    return {
        "on": bool(org.todo_on),
        "sheet": org.todo_sheet_id or DEFAULT_SHEET,
        "tab": (org.todo_tab or "").strip() or DEFAULT_TAB,
        "from_row": max(2, org.todo_from_row or DEFAULT_FROM),
        "times": read_times(org.todo_times) or read_times(DEFAULT_TIMES),
    }


def read_times(raw: str | None) -> list[tuple[int, int]]:
    """"12:00, 17:00" -> [(12, 0), (17, 0)], sorted. Bad pieces are dropped."""
    out = set()
    for piece in re.split(r"[,;\s]+", raw or ""):
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", piece.strip())
        if m and int(m.group(1)) <= 23 and int(m.group(2) or 0) <= 59:
            out.add((int(m.group(1)), int(m.group(2) or 0)))
    return sorted(out)


def times_text(times: list[tuple[int, int]]) -> str:
    return ", ".join(f"{h:02d}:{m:02d}" for h, m in times)


# --------------------------------------------------------------- reading ---
def refs_in(text: str) -> list[str]:
    """DEL-1130 and FMS-12 out of whatever was typed, in order, no repeats.

    "Boney-DEL-1130\\nRaj-DEL-1131" gives both; "FMS Dated- 23 Nov" gives none.
    """
    out: list[str] = []
    for kind, num in _REF.findall(text or ""):
        ref = ref_text(kind.upper(), int(num))
        if ref not in out:
            out.append(ref)
    return out


def _client():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(sheets.SERVICE_ACCOUNT), scopes=EDIT_SCOPE)
    return gspread.authorize(creds)


def fetch(sheet_id: str, tab: str, first_row: int) -> list[tuple[int, str, str]]:
    """[(row number, column I, column K)] from first_row down. Replaced in tests.

    Only I to K is read, and only from the starting row, so a sheet of sixty
    thousand rows costs one small request.
    """
    ws = _client().open_by_key(sheet_id).worksheet(tab)
    rows = ws.get_values(f"I{first_row}:K")
    out = []
    for i, row in enumerate(rows):
        row = list(row) + ["", "", ""]
        out.append((first_row + i, row[0] or "", row[2] or ""))
    return out


def write(sheet_id: str, tab: str, updates: list[tuple[int, str | None]]) -> None:
    """Write K (where given) and colour B:K for each row. Replaced in tests."""
    book = _client().open_by_key(sheet_id)
    ws = book.worksheet(tab)
    values = [{"range": f"K{row}", "values": [[text]]}
              for row, text in updates if text is not None]
    if values:
        ws.batch_update(values, value_input_option="USER_ENTERED")
    requests = [{
        "repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": row - 1,
                      "endRowIndex": row, "startColumnIndex": COLOR_FROM_COL,
                      "endColumnIndex": COLOR_TO_COL},
            "cell": {"userEnteredFormat": {"backgroundColor": GREEN}},
            "fields": "userEnteredFormat.backgroundColor"}}
        for row, _ in updates]
    if requests:
        book.batch_update({"requests": requests})


# ---------------------------------------------------------------- status ---
def _site(org: Organization) -> str:
    return ((org.site_url or os.getenv("SITE_URL") or
             "https://gcs-autopilot-2.onrender.com").strip().rstrip("/"))


def _link(att: Attachment, site: str) -> str:
    if att.is_link:
        return att.url
    return f"{site}/attachments/{att.id}/view"


def look(db: Session, org: Organization, ref: str) -> tuple[str, list[str]]:
    """("done" | "open" | "missing", the proof links) for one Autopilot ID."""
    site = _site(org)
    if ref.startswith("FMS-"):
        run = db.scalar(select(FlowInstance).where(
            FlowInstance.org_id == org.id, FlowInstance.ref == ref))
        if run is None:
            return "missing", []
        if run.completed_at is None or run.cancelled_at is not None:
            return "open", []
        steps = db.scalars(select(Task).where(
            Task.flow_instance_id == run.id).order_by(Task.id)).all()
        links = [_link(a, site) for t in steps for a in t.proof_files]
        return "done", links
    task = db.scalar(select(Task).where(
        Task.org_id == org.id, Task.ref == ref,
        Task.source == TaskSource.DELEGATION))
    if task is None:
        return "missing", []
    # Handed in by the doer counts as done, even while it waits for audit:
    # the doer has finished and attached their proof. Only a task still with
    # the doer (pending, in progress, sent back, reopened, on hold) waits.
    if task.status not in (TaskStatus.COMPLETED, TaskStatus.SUBMITTED):
        return "open", []
    return "done", [_link(a, site) for a in task.proof_files]


def _k_text(found: list[tuple[str, list[str]]]) -> str:
    """What goes in column K. A lone link is just the link."""
    if len(found) == 1 and len(found[0][1]) == 1:
        return found[0][1][0]
    lines = []
    for ref, links in found:
        if links:
            lines += [f"{ref}: {u}" for u in links]
        else:
            lines.append(f"{ref}: No attachment")
    return "\n".join(lines)


# --------------------------------------------------------------- running ---
def run(db: Session, org: Organization) -> dict:
    """One pass. Never raises; says what happened."""
    cfg = settings(org)
    out = {"ok": False, "marked": 0, "waiting": 0, "why": ""}
    if not enabled():
        out["why"] = "No Google credential is set on the server."
        return out
    try:
        rows = fetch(cfg["sheet"], cfg["tab"], cfg["from_row"])
    except Exception as exc:                          # noqa: BLE001
        out["why"] = _explain(exc, cfg)
        org.todo_error = out["why"]
        org.todo_read_at = clock.now()
        db.commit()
        return out

    done = {m.row_no: m for m in db.scalars(select(TodoMark).where(
        TodoMark.org_id == org.id)).all()}
    todo: list[tuple[int, str, list[str], str | None]] = []
    waiting: list[dict] = []
    for row_no, cell_i, cell_k in rows:
        cell_i = (cell_i or "").strip()
        if not cell_i:
            continue                                  # blank I: leave it alone
        refs = refs_in(cell_i)
        if not refs:
            continue
        mark = done.get(row_no)
        if mark and mark.ids_text == cell_i[:300]:
            continue
        found, state = [], "done"
        for ref in refs:
            st, links = look(db, org, ref)
            if st != "done":
                state = st
                waiting.append({"row": row_no, "id": ref,
                                "why": ("not found in Autopilot" if st == "missing"
                                        else "not completed yet")})
                break
            found.append((ref, links))
        if state != "done":
            continue
        k = None if (cell_k or "").strip() else _k_text(found)
        todo.append((row_no, cell_i, refs, k))
        if len(todo) >= MAX_PER_PASS:
            break

    if todo:
        try:
            write(cfg["sheet"], cfg["tab"], [(r, k) for r, _, _, k in todo])
        except Exception as exc:                      # noqa: BLE001
            out["why"] = _explain(exc, cfg)
            org.todo_error = out["why"]
            org.todo_read_at = clock.now()
            db.commit()
            return out
        for row_no, cell_i, refs, k in todo:
            m = done.get(row_no)
            if m is None:
                m = TodoMark(org_id=org.id, row_no=row_no)
                db.add(m)
            m.ids_text, m.refs = cell_i[:300], ",".join(refs)[:300]
            m.wrote, m.marked_at = (k or ""), clock.now()
    org.todo_error = None
    org.todo_read_at = clock.now()
    org.todo_note = json.dumps({"marked": len(todo), "waiting": waiting[:40],
                                "waiting_n": len(waiting)})
    db.commit()
    out.update(ok=True, marked=len(todo), waiting=len(waiting))
    return out


def _explain(exc: Exception, cfg: dict) -> str:
    name, text = type(exc).__name__, str(exc)
    if "PERMISSION" in text.upper() or "403" in text:
        return ("The sheet is not open to the service account as an EDITOR. "
                f"Share it with {robot() or 'the service account'} and choose "
                "Editor, not Viewer.")
    if name == "WorksheetNotFound":
        return f"There is no tab called “{cfg['tab']}” in that sheet."
    if name == "SpreadsheetNotFound" or "404" in text:
        return "That sheet was not found. Check the link."
    return f"{name}: {text}"[:300]


def last_slot(org: Organization, now: datetime) -> datetime:
    """The most recent scheduled time that has already passed."""
    times = settings(org)["times"]
    today = [now.replace(hour=h, minute=m, second=0, microsecond=0)
             for h, m in times]
    gone = [t for t in today if t <= now]
    if gone:
        return gone[-1]
    h, m = times[-1]
    return (now - timedelta(days=1)).replace(hour=h, minute=m, second=0,
                                             microsecond=0)


def due(org: Organization, now: datetime | None = None) -> bool:
    """Owed when a scheduled time (12:00 and 17:00 unless changed) has passed
    since the sheet was last looked at."""
    if not settings(org)["on"]:
        return False
    now = now or clock.now()
    return org.todo_read_at is None or org.todo_read_at < last_slot(org, now)


def run_due(db: Session) -> dict | None:
    org = db.scalar(select(Organization))
    if org is None or not due(org):
        return None
    return run(db, org)


def note(org: Organization) -> dict:
    try:
        return json.loads(org.todo_note or "{}")
    except ValueError:
        return {}
