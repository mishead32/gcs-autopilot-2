"""Self-initiative — the Leadership & Self-Initiative form's answers.

Everybody is expected to take one initiative beyond their assigned duties
in whatever period is being looked at, and says so by filling a Google
Form. The form's response sheet is read here (read-only, like the Pending
Approvals sheet) and kept as a copy. The score rule is simple:

    no initiative dated inside the selected dates    -15
    at least one                                       0

The date that counts is the one the person typed as "Date of initiative",
not the day they happened to submit the form.

Until a sheet has been connected AND read once, nobody is penalised: a
penalty for a form the software cannot see would be a penalty for our own
missing setup.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import clock
from ..models import Initiative, InitiativeAlias, Organization, User
from . import approvals, sheets

READONLY = approvals.READONLY
DEFAULT_TAB = "Form responses 1"
PENALTY = -15.0          # the benchmark: 15 points, lost when nothing is filed

# Columns, A = 0.
C_STAMP, C_NAME, C_DESIG, C_COMPANY, C_WHAT, C_DATE, C_PROOF = range(7)

enabled = approvals.enabled
robot = approvals.robot
sheet_id_from = approvals.sheet_id_from


def configured(org: Organization | None) -> bool:
    return bool(org and org.init_sheet_id)


def active(org: Organization | None) -> bool:
    """Is the rule in force? Only once the sheet has really been read."""
    return bool(org and org.init_sheet_id and org.init_read_at)


# ----------------------------------------------------------------- names ---
def name_key(raw: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (raw or "").lower()).split())


def match_name(raw: str, users: list[User], aliases: dict[str, int]) -> int | None:
    """Who a name on the form means, or None when it cannot be said.

    The form is filled in by hand, so names come as "Kiran", "kiran ",
    "Kiran Sandhu". A hand-made alias wins; then the whole name; then a
    first name or part of the name that belongs to exactly ONE active
    person. Two Kirans is not a match — it is a question for a human, and
    guessing would give one of them somebody else's initiative.
    """
    key = name_key(raw)
    if not key:
        return None
    if key in aliases:
        return aliases[key]
    whole = [u for u in users if name_key(u.name) == key]
    if len(whole) == 1:
        return whole[0].id
    live = [u for u in users if u.active]
    want = set(key.split())
    part = [u for u in live if want <= set(name_key(u.name).split())]
    if len(part) == 1:
        return part[0].id
    return None


# ----------------------------------------------------------------- dates ---
_FORMATS = ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y",
            "%d.%m.%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d-%b-%Y",
            "%d/%m/%y", "%d-%m-%y")


def parse_when(text: str) -> datetime | None:
    text = (text or "").strip()
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def initiative_day(date_text: str, filed: datetime | None,
                   today: date | None = None) -> date | None:
    """The day the initiative was taken.

    What was typed, unless it is impossible: a year like 1026 or 2062 is a
    typo for the year the form was filed, and a date that cannot exist is
    not guessed at. With no usable date at all the day it was filed stands
    in, since somebody who filled the form plainly took an initiative.
    """
    today = today or clock.today()
    got = parse_when(date_text)
    if got is None:
        m = re.fullmatch(r"\s*(\d{1,2})\D(\d{1,2})\D(\d{3,4})\s*", date_text or "")
        if m and filed:
            try:
                return date(filed.year, int(m.group(2)), int(m.group(1)))
            except ValueError:
                pass
        return filed.date() if filed else None
    d = got.date()
    if filed and (d.year < 2020 or d.year > today.year + 1):
        try:
            return d.replace(year=filed.year)
        except ValueError:
            return filed.date()
    return d


# --------------------------------------------------------------- reading ---
def fetch(sheet_id: str, tab: str) -> list[list[str]]:
    """The sheet's rows as text, headings included. Replaced in tests."""
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(sheets.SERVICE_ACCOUNT), scopes=READONLY)
    book = gspread.authorize(creds).open_by_key(sheet_id)
    return book.worksheet(tab or DEFAULT_TAB).get_values("A1:G")


def parse(rows: list[list[str]], today: date | None = None) -> list[dict]:
    out = []
    for n, row in enumerate(rows[1:], start=2):          # row 1 = headings
        row = list(row) + [""] * (7 - len(row))
        name = (row[C_NAME] or "").strip()
        if not name:
            continue
        filed = parse_when(row[C_STAMP])
        out.append({
            "row_no": n, "name_raw": name[:160],
            "company_raw": (row[C_COMPANY] or "").strip()[:160],
            "date_text": (row[C_DATE] or "").strip()[:80],
            "filed_at": filed,
            "done_on": initiative_day(row[C_DATE], filed, today),
            "description": (row[C_WHAT] or "").strip(),
            "proof_url": (row[C_PROOF] or "").strip(),
        })
    return out


def _why(exc: Exception) -> str:
    low = str(exc).lower()
    if "403" in str(exc) or "permission" in low:
        who = robot() or "the service account"
        return (f"Google refused: the sheet is not shared with {who}. Open the "
                "sheet, press Share, add that address as a Viewer, and read again.")
    if "404" in str(exc) or "not found" in low:
        return "Google cannot find that sheet. Check the link was copied whole."
    if "worksheetnotfound" in low or "unable to parse range" in low:
        return ("The sheet was found but not the tab. Check the tab name is "
                "exactly as written at the bottom of the sheet.")
    return f"Could not read the sheet: {str(exc)[:300]}"


def aliases_of(db: Session, org_id: int) -> dict[str, int]:
    return {a.name_key: a.user_id for a in db.scalars(
        select(InitiativeAlias).where(InitiativeAlias.org_id == org_id)).all()}


def rematch(db: Session, org_id: int) -> None:
    """Match names again, e.g. after somebody was mapped by hand."""
    users = list(db.scalars(select(User).where(User.org_id == org_id)).all())
    al = aliases_of(db, org_id)
    for r in db.scalars(select(Initiative).where(Initiative.org_id == org_id)).all():
        r.user_id = match_name(r.name_raw, users, al)
    db.commit()


def sync(db: Session, org: Organization) -> dict:
    """Read the sheet and replace the copy. Never raises; on failure the
    last copy stays and the reason is written beside it."""
    if not enabled():
        return {"ok": False, "why": "No Google credential is set on the server."}
    if not configured(org):
        return {"ok": False, "why": "No sheet has been connected yet."}
    try:
        recs = parse(fetch(org.init_sheet_id, org.init_tab or DEFAULT_TAB))
    except Exception as exc:                              # noqa: BLE001
        db.rollback()
        org.init_error = _why(exc)
        db.commit()
        return {"ok": False, "why": org.init_error}
    users = list(db.scalars(select(User).where(User.org_id == org.id)).all())
    al = aliases_of(db, org.id)
    db.execute(delete(Initiative).where(Initiative.org_id == org.id))
    db.add_all([Initiative(org_id=org.id, user_id=match_name(r["name_raw"], users, al), **r)
                for r in recs])
    org.init_read_at = clock.now()
    org.init_error = None
    db.commit()
    return {"ok": True, "rows": len(recs)}


def sync_all(db: Session) -> int:
    done = 0
    for org in db.scalars(select(Organization).where(
            Organization.init_sheet_id.isnot(None))).all():
        if sync(db, org).get("ok"):
            done += 1
    return done


# --------------------------------------------------------------- scoring ---
def taken_by(db: Session, org_id: int, start, end) -> set[int] | None:
    """Who has an initiative dated inside [start, end]. None = rule not live."""
    org = db.get(Organization, org_id)
    if not active(org):
        return None
    s = start.date() if isinstance(start, datetime) else start
    e = end.date() if isinstance(end, datetime) else end
    return set(db.scalars(select(Initiative.user_id).where(
        Initiative.org_id == org_id, Initiative.user_id.isnot(None),
        Initiative.done_on >= s, Initiative.done_on <= e).distinct()).all())


def penalty_for(taken: set[int] | None, user_id: int) -> float:
    if taken is None or user_id in taken:
        return 0.0
    return PENALTY


def unmatched(db: Session, org_id: int) -> list[tuple[str, int]]:
    seen: dict[str, int] = {}
    for r in db.scalars(select(Initiative).where(
            Initiative.org_id == org_id, Initiative.user_id.is_(None))).all():
        seen[r.name_raw] = seen.get(r.name_raw, 0) + 1
    return sorted(seen.items())
