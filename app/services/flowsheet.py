"""A Google Sheet wired to a flow: a new row there starts a run here.

The third way to start an FMS. The start form is one at a time, the
spreadsheet upload is four hundred at once and finished, and this is the
one that keeps going — a sheet the office is already typing into, where a
row added at eleven becomes a run by quarter past.

Three things make it safe enough to leave running unattended:

  * The headings ARE the flow's questions — the same headings the upload
    template prints. Nothing is mapped by hand, so nothing is mis-mapped,
    and a renamed question is a renamed column rather than a silent
    mis-fill.

  * Every row is read by the same code the form and the upload use, so a
    row this starts is a row the form would have accepted. Two readers
    would be two sets of rules about what a valid answer is, and the day
    they disagree the sheet quietly starts work nobody meant to create.

  * What happened to each row is written back INTO the sheet. The person
    who typed the row sees the FMS number it became, or the reason it did
    not, without asking anybody.

What it deliberately does not do is guess. A heading that does not match a
question stops the whole sheet rather than importing rows with a question
unanswered, and a row it cannot read is left alone with the problem written
beside it — fix the cell and the next run picks it up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import Flow, FlowSheet, User
from . import flowbulk, sheets

# The three columns the app keeps at the end of the sheet. Named as a
# person would read them, because a person is who reads them.
REF_COL = "FMS no."
WHEN_COL = "Started on"
PROBLEM_COL = "Problem"
STAMP_COLS = (REF_COL, WHEN_COL, PROBLEM_COL)

# How many new rows one pass will start. A sheet somebody pastes three
# thousand rows into should not become three thousand runs in one tick —
# that is a tick that times out halfway and leaves nobody able to say which
# half. The rest go on the next pass, a few minutes later.
MAX_PER_RUN = 100

# Leaves room for a sheet somebody has been using for years.
MAX_SCAN_ROWS = 20000

_URL = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")


def enabled() -> bool:
    """Can this app reach Google Sheets at all?

    The same service account the mirror uses. One credential for both, so
    there is one thing to set up and one thing that can expire.
    """
    return bool(sheets.SERVICE_ACCOUNT)


def sheet_id_from(raw: str) -> str:
    """The id out of a pasted URL — or the id itself, pasted bare."""
    raw = (raw or "").strip()
    found = _URL.search(raw)
    if found:
        return found.group(1)
    return raw if re.fullmatch(r"[A-Za-z0-9_-]{20,}", raw) else ""


def service_account_email() -> str:
    """Who the sheet has to be shared with, for the page to show them.

    Nobody can guess this address, and a sheet that is not shared with it
    fails with a permission error that does not say what to do about it.
    """
    import json
    try:
        return json.loads(sheets.SERVICE_ACCOUNT or "{}").get("client_email", "")
    except ValueError:
        return ""


def _open(cfg: FlowSheet):
    """The worksheet itself. Replaced wholesale in tests — no network."""
    book = sheets._client().open_by_key(cfg.sheet_id)
    return book.worksheet(cfg.tab) if cfg.tab else book.sheet1


# --------------------------------------------------------------- reading ---
@dataclass
class Scan:
    """What one look at the sheet found."""
    headings: list = dc_field(default_factory=list)
    missing: list = dc_field(default_factory=list)   # questions with no column
    extra: list = dc_field(default_factory=list)     # columns nothing asks for
    rows: list = dc_field(default_factory=list)      # new rows, checked
    done: int = 0                                    # rows already stamped
    total: int = 0

    @property
    def ok(self) -> bool:
        return not self.missing

    @property
    def ready(self) -> list:
        return [r for r in self.rows if r.ok]

    @property
    def bad(self) -> list:
        return [r for r in self.rows if not r.ok]


def _grid(ws) -> list:
    values = ws.get_all_values()
    return values[:MAX_SCAN_ROWS]


def scan(db: Session, cfg: FlowSheet, ws=None, limit: int = MAX_PER_RUN) -> Scan:
    """Read the sheet and say which rows are new and whether they are good.

    A row is NEW when its "FMS no." cell is empty. That is the whole rule,
    and it is the reason the stamp lives in the sheet: it means the same
    thing to the software and to the person looking at the row, it survives
    a sort, and a row somebody deletes the stamp from is deliberately asked
    for again.
    """
    ws = ws if ws is not None else _open(cfg)
    grid = _grid(ws)
    out = Scan()
    if not grid:
        out.missing = [f["label"] for f in
                       flowbulk.columns(cfg.flow, flowbulk.needs_company(cfg.flow))]
        return out

    out.headings = [str(h).strip() for h in grid[0]]
    heads = flowbulk.head_map(grid[0])
    ref_at = heads.get(REF_COL.lower())

    body = grid[1:]
    out.total = sum(1 for r in body if any(str(c).strip() for c in r))

    fresh, numbers = [], []
    for n, raw in enumerate(body, start=2):
        if not any(str(c).strip() for c in raw):
            continue
        stamped = (ref_at is not None and ref_at - 1 < len(raw)
                   and str(raw[ref_at - 1]).strip())
        if stamped:
            out.done += 1
            continue
        if len(fresh) >= limit:
            break
        fresh.append(raw)
        numbers.append(n)

    # Checked by the same reader the upload uses — see flowbulk.read_rows.
    parsed = flowbulk.read_rows(db, cfg.flow, heads, fresh,
                                numbers[0] if numbers else 2)
    out.missing = [m for m in parsed.missing]
    out.extra = [h for h in parsed.extra if h not in
                 {c.lower() for c in STAMP_COLS}]
    # read_rows numbers rows sequentially from the first; the sheet's own
    # numbers are what a person needs to be told, so they are put back.
    for row, number in zip(parsed.rows, numbers):
        row.number = number
    out.rows = parsed.rows
    return out


# --------------------------------------------------------------- writing ---
def _stamp_columns(ws, headings: list) -> dict:
    """Where the three app columns are, adding them if they are not there.

    Added at the END and never moved, so a sheet somebody has arranged
    their own way keeps its arrangement.
    """
    lower = [h.strip().lower() for h in headings]
    at, width = {}, len(headings)
    for name in STAMP_COLS:
        if name.lower() in lower:
            at[name] = lower.index(name.lower()) + 1
        else:
            width += 1
            at[name] = width
            lower.append(name.lower())
            ws.update_cell(1, width, name)
    return at


def _a1(row: int, col: int) -> str:
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def write_back(ws, at: dict, row_number: int, ref: str, when: str,
               problem: str) -> None:
    """Say in the sheet what happened to that row.

    One row at a time on purpose. A batch write is fewer calls, but a batch
    that fails halfway leaves rows that were started with nothing written
    beside them — and the next pass, seeing no FMS number, starts them
    again. Duplicated work is worse than a slower write.
    """
    for name, value in ((REF_COL, ref), (WHEN_COL, when),
                        (PROBLEM_COL, problem)):
        col = at.get(name)
        if col:
            ws.update_cell(row_number, col, value)


# ----------------------------------------------------------------- doing ---
def poll(db: Session, cfg: FlowSheet, ws=None) -> dict:
    """One pass: start what is new, write back what happened.

    Returns a summary fit to show on a page and to print in a log.
    """
    from .flows import start_flow

    started, refused, problems = 0, 0, []
    ws = ws if ws is not None else _open(cfg)
    found = scan(db, cfg, ws)
    if not found.ok:
        return {"ok": False, "started": 0, "refused": 0,
                "missing": found.missing, "total": found.total,
                "error": ("The sheet has no column for: "
                          + ", ".join(found.missing))}

    at = _stamp_columns(ws, found.headings)
    actor = db.get(User, cfg.connected_by_id)
    now = clock.stamp()

    for row in found.rows:
        if not row.ok:
            refused += 1
            problems.append(f"Row {row.number}: {row.problems[0]}")
            # No FMS number, so the row stays new: fix the cell and the
            # next pass picks it up by itself.
            write_back(ws, at, row.number, "", "", "; ".join(row.problems))
            continue
        try:
            inst = start_flow(db, cfg.flow, row.name, actor,
                              context=row.context, branch_id=row.branch_id)
            db.commit()
        except Exception as exc:                      # one bad row, not all
            db.rollback()
            refused += 1
            problems.append(f"Row {row.number}: {exc}")
            write_back(ws, at, row.number, "", "", str(exc)[:300])
            continue
        started += 1
        write_back(ws, at, row.number, inst.ref or str(inst.id), now, "")

    cfg.last_run_at = clock.now()
    cfg.started_total = (cfg.started_total or 0) + started
    cfg.last_error = "; ".join(problems)[:400] if problems else None
    db.commit()
    return {"ok": True, "started": started, "refused": refused,
            "total": found.total, "done": found.done, "problems": problems}


def baseline(db: Session, cfg: FlowSheet, ws=None) -> int:
    """Mark everything already in the sheet as done, creating nothing.

    What connecting a sheet does by default. A sheet of four hundred
    members is a list of people who are already dealt with, not four
    hundred jobs waiting to be created — and a connection that could create
    them by surprise is one nobody dares make.
    """
    ws = ws if ws is not None else _open(cfg)
    grid = _grid(ws)
    if not grid:
        return 0
    at = _stamp_columns(ws, [str(h).strip() for h in grid[0]])
    marked = 0
    for n, raw in enumerate(grid[1:], start=2):
        if not any(str(c).strip() for c in raw):
            continue
        ref_at = at[REF_COL]
        already = ref_at - 1 < len(raw) and str(raw[ref_at - 1]).strip()
        if already:
            continue
        write_back(ws, at, n, "— already here when connected", "", "")
        marked += 1
    cfg.last_run_at = clock.now()
    db.commit()
    return marked


def due(db: Session) -> list:
    """Every connected sheet that should be looked at now."""
    return list(db.scalars(select(FlowSheet).where(FlowSheet.active.is_(True))))


def run_all(db: Session) -> dict:
    """Every live sheet, one pass each. What the app's own clock calls.

    One sheet failing — a permission revoked, a tab renamed — must not stop
    the others, so each is caught and recorded against its own connection
    rather than raised.
    """
    if not enabled():
        return {"checked": 0, "started": 0}
    checked = started = 0
    for cfg in due(db):
        checked += 1
        try:
            result = poll(db, cfg)
            started += result.get("started", 0)
            if not result.get("ok"):
                cfg.last_error = result.get("error", "")[:400]
                cfg.last_run_at = clock.now()
                db.commit()
        except Exception as exc:
            db.rollback()
            cfg.last_error = f"Could not read the sheet: {exc}"[:400]
            cfg.last_run_at = clock.now()
            db.commit()
    return {"checked": checked, "started": started}
