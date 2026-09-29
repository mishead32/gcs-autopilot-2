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

# "Back" means back to the filtered list they were actually on, not to an
# unfiltered one. See app/lastview.py.
from . import lastview as _lastview
templates.env.globals["back_url"] = _lastview.url
templates.env.globals["back_label"] = _lastview.label
