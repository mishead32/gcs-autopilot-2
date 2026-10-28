"""Reading an answer to a start-form question, and saying what is wrong.

There are two ways to start a run — one by hand on the flow's page, and
hundreds at a time from a spreadsheet — and they have to agree about what a
valid answer is. If they do not, a file imports four hundred runs that the
form would have refused one of, and the difference is found weeks later by
somebody holding work that should never have been created.

So the rules live here, once, and both callers use them. The only thing
that differs is where the raw value came from: a form field or a cell.
"""
from __future__ import annotations

from datetime import datetime
from difflib import get_close_matches

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import User


def _norm(value) -> str:
    """Trimmed, single-spaced, for comparing what people typed."""
    return " ".join(str(value or "").split())


def staff(db: Session, org_id: int) -> list[User]:
    """Everybody a person question may name: active, and given work.

    Owners and admins hand work out and are never handed any, so offering
    them as the answer to "who is the PT trainer" would only ever be a
    mistake somebody made from a long dropdown.
    """
    rows = db.scalars(
        select(User).where(User.org_id == org_id, User.active.is_(True))
        .order_by(User.name)).all()
    return [u for u in rows if u.receives_work]


def find_person(db: Session, org_id: int, raw) -> tuple[User | None, str | None]:
    """Match a typed name or email to one member of staff.

    Returns (person, problem). A problem is a sentence fit to show somebody
    looking at row 17 of their spreadsheet, which is why it names the value
    and, where it can, the person they probably meant.

    An email wins over a name, because an email is unique and a name is not.
    Two people called the same thing and nothing to separate them is
    refused rather than guessed: a guess about who does the work is the one
    thing not worth saving somebody a click for.
    """
    want = _norm(raw)
    if not want:
        return None, None
    people = staff(db, org_id)
    low = want.lower()

    for u in people:
        if (u.email or "").strip().lower() == low:
            return u, None

    hits = [u for u in people if _norm(u.name).lower() == low]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, (f"“{want}” matches {len(hits)} people. Use their email "
                      "address instead: " +
                      ", ".join(u.email for u in hits if u.email))

    near = get_close_matches(low, [_norm(u.name).lower() for u in people],
                             n=1, cutoff=0.75)
    if near:
        match = next(u for u in people if _norm(u.name).lower() == near[0])
        return None, (f"“{want}” does not match anybody. Did you mean "
                      f"{match.name}?")
    return None, f"“{want}” does not match anybody on GCS Autopilot."


# What a date may be written as. ISO first, because that is what the form
# posts and what Excel hands back for a real date cell; then the two ways
# people type one by hand.
# Each format, and whether it carries a TIME. The difference matters: a
# question that asks for a date and time, answered with only a date, means
# "that working day" and not "one second past midnight" — so a bare answer
# has to be stored bare and left for the engine to read that way.
DATE_FORMATS = (("%Y-%m-%dT%H:%M:%S", True), ("%Y-%m-%dT%H:%M", True),
                ("%Y-%m-%d %H:%M:%S", True), ("%Y-%m-%d %H:%M", True),
                ("%d-%m-%Y %H:%M", True),
                ("%Y-%m-%d", False), ("%d-%m-%Y", False),
                ("%d-%b-%Y", False), ("%d %b %Y", False),
                ("%d %B %Y", False))


def _as_moment(value: str) -> tuple[datetime | None, bool]:
    """A typed date as (moment, was a time given). (None, False) if not one.

    Strict on purpose: "32/11/2026" has to come back as not-a-date rather
    than be rolled forward into December, because a deadline nobody meant
    is worse than one somebody has to retype.
    """
    raw = _norm(value).replace("/", "-")
    for fmt, has_time in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt), has_time
        except ValueError:
            continue
    return None, False


def check_answer(db: Session, org_id: int, field: dict, raw
                 ) -> tuple[str, User | None, str | None]:
    """One answer, checked. Returns (value to store, person, problem).

    `value to store` is what goes in the run's context — for a person
    question that is their NAME, because that is what belongs in the
    submissions register and in the run's own title. The person comes back
    separately so the caller can record WHICH one, which a name alone
    cannot always say.
    """
    label, kind = field["label"], field["type"]
    value = _norm(raw)

    if not value:
        if field["required"]:
            return "", None, f"“{label}” has to be filled in."
        return "", None, None

    if kind == "number":
        try:
            float(value.replace(",", ""))
        except ValueError:
            return "", None, f"“{label}” should be a number — got “{value}”."

    elif kind == "yesno":
        if value.lower() in ("yes", "y", "true", "1"):
            value = "Yes"
        elif value.lower() in ("no", "n", "false", "0"):
            value = "No"
        else:
            return "", None, f"“{label}” should be Yes or No — got “{value}”."

    elif kind in ("date", "datetime"):
        # The form uses a date picker, so the browser refuses a bad date
        # before it is ever posted. A spreadsheet cell holds anything —
        # "32/11/2026" reads as a date to a person skimming a column and is
        # not one — and the engine anchors step deadlines to these answers,
        # so one that cannot be read becomes a deadline nobody is given.
        when, had_time = _as_moment(value)
        if when is None:
            return "", None, (
                f"“{label}” should be a date"
                + (" and time" if kind == "datetime" else "")
                + f" — got “{value}”. Write it as "
                + ("2026-11-20 15:30" if kind == "datetime" else "2026-11-20")
                + " or 20/11/2026.")
        # Stored the way the form posts it, so both routes leave the same
        # string behind and everything downstream reads one format. A bare
        # answer stays bare even on a date-and-time question: writing it as
        # midnight would turn "that day" into a deadline nobody can meet,
        # and the engine already knows a bare date means the working day.
        value = (when.strftime("%Y-%m-%dT%H:%M")
                 if kind == "datetime" and had_time
                 else when.strftime("%Y-%m-%d"))

    elif kind == "select":
        match = next((o for o in field["options"]
                      if _norm(o).lower() == value.lower()), None)
        if match is None:
            return "", None, (f"“{value}” is not one of the choices for "
                              f"“{label}”. Pick one of: "
                              + ", ".join(field["options"]) + ".")
        value = match

    elif kind == "person":
        person, problem = find_person(db, org_id, value)
        if problem:
            return "", None, f"{label}: {problem}"
        if person is None:
            return "", None, f"“{label}” has to name somebody."
        return person.name, person, None

    return value, None, None
