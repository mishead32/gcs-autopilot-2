"""Remember the filtered list a person was looking at.

The complaint this exists for: filter the task list down to one employee,
click into a task, come back — and the filter is gone. Do that thirty times
in an afternoon and you have re-typed the same three dropdowns thirty times.

So every time somebody opens a LIST page, the exact address of that page —
tabs, pickers, dates and all — is written into their session under one key.
The task page's back link reads it, and so does every action that finishes
by sending them back to a list. The filter therefore survives opening a
task, submitting it, auditing it, deleting it, and the browser's own back
button, and it is replaced only when they open a different list.

Deliberately ONE remembered page rather than one per kind of list: people
arrive at a task from the task list, from a report, from the follow-up desk
and from the dashboard, and "take me back where I was" has to mean the place
they actually came from, not the last place of a matching type.

Nothing here is trusted on the way out. The stored address is checked again
before it is used, the same way a redirect target arriving in a form field
is checked, because a session cookie is still something that leaves the
building and comes back.
"""
from __future__ import annotations

from starlette.requests import Request

KEY = "lastview"

# The pages worth remembering, longest prefix first, with what to call them
# on a back link. A page not listed here never overwrites the memory — so
# opening a task from a filtered list and then wandering to Settings still
# leaves the list as the place to go back to.
LIST_PAGES = [
    # The dashboard counts: its EM score section carries a period now, and
    # losing that on the way back is the same annoyance in miniature.
    ("/", "the dashboard"),
    ("/tasks", "the task list"),
    ("/followups", "Follow-ups"),
    ("/recurring", "Checklist"),
    ("/flows", "FMS"),
    ("/help", "Help Desk"),
    ("/reports/tasks", "the report"),
    ("/reports/audit", "the audit report"),
    ("/reports/score", "the EM score report"),
    ("/reports/followups", "the follow-up report"),
    ("/stats", "Performance"),
    ("/outbox", "Outbox"),
    ("/admin/users", "Users"),
    ("/admin/branches", "Branches"),
    ("/admin/departments", "Departments"),
    ("/admin/holidays", "Holidays"),
]


def label_for(path: str) -> str | None:
    """What to call this page on a back link, or None if it is not a list.

    Matched on the exact path, not a prefix: /tasks is a list, /tasks/41 is
    one task inside it, and /tasks/new is a form. Treating those as lists
    would make "go back" mean "go back to the thing you just left".
    """
    for prefix, label in LIST_PAGES:
        if path == prefix:
            return label
    return None


def safe(dest: str) -> bool:
    """A plain path on this site, and nothing else.

    "//evil.com" is a complete URL to a browser, and a newline in a Location
    header splits the response. Both are refused rather than sanitised.
    """
    if not dest or not dest.startswith("/"):
        return False
    if dest.startswith("//") or dest.startswith("/\\"):
        return False
    return "\n" not in dest and "\r" not in dest


def remember_scope(scope) -> None:
    """Called from the middleware, with the raw ASGI scope.

    Works on the scope rather than a Request because it runs while the
    response is on its way out, and the session dict on the scope is the one
    the session middleware is about to serialise.
    """
    label = label_for(scope.get("path") or "")
    if label is None:
        return
    session = scope.get("session")
    if session is None:
        return                      # signed out, or no session on this route
    dest = scope["path"]
    query = (scope.get("query_string") or b"").decode("latin-1")
    if query:
        dest += "?" + query
    if not safe(dest):
        return
    session[KEY] = {"url": dest, "label": label}


def remember(request: Request) -> None:
    """The same thing, from inside a route. Kept for direct calls."""
    remember_scope(request.scope)


def get(request: Request) -> dict | None:
    try:
        saved = request.session.get(KEY)
    except Exception:
        return None
    if not isinstance(saved, dict):
        return None
    url = saved.get("url") or ""
    if not safe(url):
        return None
    return {"url": url, "label": saved.get("label") or "the list"}


def url(request: Request, fallback: str = "/tasks") -> str:
    """Where 'back' goes. The remembered list, or a sensible default."""
    saved = get(request)
    return saved["url"] if saved else fallback


def label(request: Request, fallback: str = "All tasks") -> str:
    saved = get(request)
    return saved["label"] if saved else fallback


def clear(request: Request) -> None:
    try:
        request.session.pop(KEY, None)
    except Exception:
        pass
