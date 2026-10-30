from datetime import datetime

from fastapi.templating import Jinja2Templates

from . import clock
from .config import BASE_DIR, APP_NAME, EPHEMERAL_STORAGE, BANNER

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def dt(value, fmt="%d %b %Y, %I:%M %p"):
    if value is None:
        return "—"
    return value.strftime(fmt)


def relative_due(value):
    if value is None:
        return "—"
    delta = value - clock.now()
    mins = int(delta.total_seconds() // 60)
    if mins < 0:
        mins = -mins
        if mins < 60:
            return f"{mins}m overdue"
        if mins < 1440:
            return f"{mins // 60}h overdue"
        return f"{mins // 1440}d overdue"
    if mins < 60:
        return f"in {mins}m"
    if mins < 1440:
        return f"in {mins // 60}h"
    return f"in {mins // 1440}d"


def ago(value):
    """How long ago something happened, in the plainest words.

    Shown under an assigned date so "when was this given to me" can be
    answered at a glance. A task assigned eleven days ago and still open
    says something a date on its own does not.
    """
    if value is None:
        return ""
    mins = int((clock.now() - value).total_seconds() // 60)
    if mins < 0:
        return "just now"          # clock skew, or a date typed ahead
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    if mins < 1440:
        return f"{mins // 60}h ago"
    days = mins // 1440
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


templates.env.filters["ago"] = ago
templates.env.filters["dt"] = dt
templates.env.filters["due"] = relative_due
templates.env.globals["APP_NAME"] = APP_NAME
templates.env.globals["EPHEMERAL_STORAGE"] = EPHEMERAL_STORAGE

from .services import storage as _storage          # noqa: E402
templates.env.globals["UPLOADS_ON"] = _storage.uploads_available
templates.env.globals["UPLOAD_MODE"] = _storage.mode
templates.env.globals["MAX_UPLOAD_MB"] = _storage.MAX_UPLOAD_MB
templates.env.globals["UPLOAD_ACCEPT"] = _storage.ACCEPT_ATTR
templates.env.globals["BANNER"] = BANNER
templates.env.globals["now"] = datetime.utcnow

from .models import PRIORITY_WEIGHT as _PW          # noqa: E402
templates.env.globals["PRIORITY_WEIGHT"] = _PW

from . import flash as _flash                       # noqa: E402
templates.env.globals["pop_flash"] = _flash.pop


def _festive(user):
    """The festival strip for whoever is looking, or None.

    Takes the person the page already has rather than looking one up:
    every page that extends the base template is given `user`, and a
    second lookup per request to decorate the page would be a query
    nobody asked for.

    The holiday itself is cached by day inside the service, so this is a
    dictionary lookup after the first page of the morning — and it is
    wrapped, because decoration must never be the reason a page fails to
    load.
    """
    try:
        if user is None or not getattr(user, "org_id", None):
            return None
        from .db import SessionLocal
        from .services import festive as _f
        with SessionLocal() as db:
            return _f.coming(db, user.org_id, getattr(user, "branch_id", None))
    except Exception:                                   # noqa: BLE001
        return None


def _festive_public():
    """The festival for the login page, where nobody is signed in yet.

    The company's own holidays rather than a branch's: there is no person
    to ask which branch they are at, and a festival the whole group keeps
    is the one worth greeting a stranger with.
    """
    try:
        from sqlalchemy import select

        from .db import SessionLocal
        from .models import Organization
        from .services import festive as _f
        with SessionLocal() as db:
            org = db.scalar(select(Organization))
            return _f.coming(db, org.id, None) if org else None
    except Exception:                                   # noqa: BLE001
        return None


templates.env.globals["festive"] = _festive
templates.env.globals["festive_public"] = _festive_public


class _Counter:
    """1, 2, 3 … handed out as a page renders.

    Section numbers on the dashboard have to count the sections that are
    really there: an account that is never given work does not get "My work"
    or "My score", and a hard-coded 3 on the only remaining section reads as
    though two are missing.
    """
    def __init__(self):
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n


templates.env.globals["counter"] = _Counter

# So the menu can ask "may this person decide an audit?" without a role
# literal in the template.
from .models import Right as _Right
templates.env.globals["AUDIT_RIGHT"] = _Right.AUDIT_TASK
templates.env.globals["MOVE_DUE_RIGHT"] = _Right.CHANGE_DUE_DATE
templates.env.globals["EDIT_RIGHT"] = _Right.EDIT_TASK

# The three dates a window can mean, so the filter bar can offer them.
from .routers.reports import BASIS_CHOICES as _BASIS      # noqa: E402
templates.env.globals["BASIS_CHOICES"] = _BASIS

# "Back" means back to the filtered list they were actually on, not to an
# unfiltered one. See app/lastview.py.
from . import lastview as _lastview
templates.env.globals["back_url"] = _lastview.url
templates.env.globals["back_label"] = _lastview.label

# ------------------------------------------------------- clickable links ---
def linkify(text: str) -> "Markup":
    """Turn a pasted link into a link, and nothing else into anything.

    People write a step's instructions as "pull the sheet from <url>, check
    against <url>" — and a doer reading that on a phone cannot do anything
    with a web address they have to copy out by hand.

    Escaped FIRST, always, and only then are the links put back. The text is
    typed by whoever builds the flow, and treating it as HTML would let a
    pasted <script> run in every doer's browser. Nothing else is allowed
    through: no bold, no images, no raw tags.

    rel="noopener noreferrer" because target="_blank" without it hands the
    opened page a handle on ours.
    """
    import re as _re
    from markupsafe import Markup as _M, escape as _esc
    if not text:
        return _M("")
    safe = str(_esc(text))
    # http(s) only. A "javascript:" or "data:" link is not a link somebody
    # pasted from their browser bar, it is an attack.
    pattern = _re.compile(r"(https?://[^\s<>\"']+)")

    def _a(m):
        url = m.group(1).rstrip(".,;:!?)")
        tail = m.group(1)[len(url):]
        show = url if len(url) <= 60 else url[:57] + "..."
        return (f'<a href="{url}" target="_blank" rel="noopener noreferrer">'
                f'{show}</a>{tail}')

    return _M(pattern.sub(_a, safe))


templates.env.filters["linkify"] = linkify
