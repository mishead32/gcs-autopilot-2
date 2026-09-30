"""One text box, one meaning, on every list in the app.

Until now the only thing a person could search for was a task's reference,
which is only useful if you already know it. What people actually have in
their head is a few words from the task — "vendor invoices", "face ID",
"Maybach" — so the box searches the words as well as the number.

Everything here is deliberately boring:

  * case does not matter
  * a bare word matches anywhere inside the text, not just at the start
  * % and _ are ordinary characters, not wildcards — somebody searching for
    "100%" is searching for "100%", and without escaping them a stray % in a
    task title turns the query into "match everything"
  * the same function is used by every list, so "search" cannot come to mean
    one thing on the task list and another on a report
"""
from __future__ import annotations

from sqlalchemy import or_

# Anything longer than this is a paste accident, not a search.
MAX_LEN = 120
ESCAPE = "\\"


def clean(raw: str | None) -> str:
    """The query as typed, trimmed and capped. Empty means 'no search'."""
    return " ".join((raw or "").split())[:MAX_LEN]


def _pattern(q: str) -> str:
    """A LIKE pattern that treats the person's text as plain text.

    LIKE gives % and _ special meaning. A task called "Hit 100% attendance"
    is a real title here, and searching for it without this would match every
    row in the table — which reads as "search is broken" rather than "your
    search matched everything".
    """
    safe = (q.replace(ESCAPE, ESCAPE + ESCAPE)
             .replace("%", ESCAPE + "%")
             .replace("_", ESCAPE + "_"))
    return f"%{safe}%"


def clause(columns, q: str):
    """Match any of these columns against the query. None if nothing to do.

    ilike() is portable: PostgreSQL has a real ILIKE, and on SQLite
    SQLAlchemy lowers both sides instead, so development and production
    agree about case.
    """
    q = clean(q)
    if not q or not columns:
        return None
    pat = _pattern(q)
    return or_(*[c.ilike(pat, escape=ESCAPE) for c in columns])


def apply(query, columns, q: str):
    """Narrow a select() by the search, or hand it back untouched."""
    where = clause(columns, q)
    return query if where is None else query.where(where)
