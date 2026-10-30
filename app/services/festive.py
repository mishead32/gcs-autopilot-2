"""A few days of festival colour, taken from the holiday list already kept.

The office adds Diwali to the holidays so the software stops creating work
that day. That same line is enough to know the building will be decorated
the week before — so the software greets it rather than staying the same
grey it is in March.

Deliberately small, because a thing like this earns none of its weight
back:

  * One query a day. The answer is the same for every person at a company
    from one midnight to the next, so it is worked out once and kept in a
    dictionary. A page load after the first does no database work at all.
  * No pictures and nothing from the network. The motif is a dozen
    characters of inline SVG; a decorated page must not get slower or stop
    working behind a corporate proxy.
  * No animation by default beyond one slow fade, and none at all for
    somebody who has asked their computer for less motion.
  * Nothing moves, nothing covers anything, nothing has to be dismissed.
    A strip above the page and a warmer accent — that is all.

Matched on the holiday's NAME, so no second list to maintain: write
"Diwali" on the holidays page and this finds it. A festival it does not
recognise still gets a greeting, in the house colours.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from .. import clock
from . import holidays as hol

# How long before the day the colour appears. Ten, as asked: long enough
# to feel like the run-up, short enough that it is still an occasion.
LEAD_DAYS = 10


@dataclass(frozen=True)
class Theme:
    key: str
    greeting: str          # what the strip says
    ink: str               # text on the strip
    start: str             # the strip's gradient, left
    end: str               # and right
    motif: str             # inline SVG, no more than a few hundred bytes


# A diya, a flame, a star, a crescent, a flag, a tree. Each one a path or
# two — enough to be recognised at sixteen pixels, which is all a strip
# ever shows.
_DIYA = ('<path d="M2 11h12c0 2-2.7 3-6 3s-6-1-6-3z" fill="#c2410c"/>'
         '<path d="M8 3c1.6 1.6 2.4 2.8 2.4 3.9A2.4 2.4 0 0 1 8 9.3 2.4 '
         '2.4 0 0 1 5.6 6.9C5.6 5.8 6.4 4.6 8 3z" fill="#f59e0b"/>')
_FLAME = ('<path d="M8 2c2.4 2.6 3.6 4.3 3.6 6A3.6 3.6 0 0 1 8 11.6 3.6 '
          '3.6 0 0 1 4.4 8c0-1.7 1.2-3.4 3.6-6z" fill="#f59e0b"/>')
_STAR = ('<path d="M8 2l1.7 3.9L14 6.4l-3 2.9.8 4.2L8 11.6 4.2 13.5l.8-4.2'
         '-3-2.9 4.3-.5z" fill="#f59e0b"/>')
_MOON = ('<path d="M10.5 2a6 6 0 1 0 3.3 11 6.6 6.6 0 0 1-3.3-11z" '
         'fill="#e0f2fe"/><circle cx="12.4" cy="4.6" r="1.1" fill="#bae6fd"/>')
_FLAG = ('<path d="M3 2v12" stroke="#78350f" stroke-width="1.4"/>'
         '<path d="M4 3h8l-1.6 2L12 7H4z" fill="#f59e0b"/>')
_TREE = ('<path d="M8 2l3.4 5H4.6zM8 5.5L12 11H4zM7 11h2v3H7z" '
         'fill="#15803d"/>')
_COLOURS = ('<circle cx="5" cy="6" r="2.6" fill="#ec4899"/>'
            '<circle cx="10.5" cy="5" r="2.2" fill="#f59e0b"/>'
            '<circle cx="8" cy="10.5" r="2.4" fill="#22c55e"/>')

# Keyword -> theme. First match wins, so the longer names come first:
# "Diwali" would otherwise swallow nothing, but "New Year" has to be
# tried before "year" ever is.
THEMES: list[tuple[tuple[str, ...], Theme]] = [
    (("diwali", "deepavali", "deepawali"),
     Theme("diwali", "Happy Diwali", "#7c2d12", "#fff7ed", "#fde68a", _DIYA)),
    (("holi",),
     Theme("holi", "Happy Holi", "#831843", "#fdf2f8", "#fbcfe8", _COLOURS)),
    (("christmas", "xmas"),
     Theme("christmas", "Merry Christmas", "#14532d", "#f0fdf4", "#bbf7d0",
           _TREE)),
    (("eid", "ramadan", "ramzan"),
     Theme("eid", "Eid Mubarak", "#134e4a", "#f0fdfa", "#99f6e4", _MOON)),
    (("independence", "republic"),
     Theme("flag", "", "#7c2d12", "#fff7ed", "#fed7aa", _FLAG)),
    (("new year",),
     Theme("newyear", "Happy New Year", "#312e81", "#eef2ff", "#c7d2fe",
           _STAR)),
    (("lohri", "baisakhi", "vaisakhi", "pongal", "makar"),
     Theme("harvest", "", "#7c2d12", "#fffbeb", "#fde68a", _FLAME)),
    (("navratri", "durga", "dussehra", "dasara", "ganesh", "janmashtami",
      "raksha", "rakhi", "onam", "ugadi", "gudi"),
     Theme("festival", "", "#831843", "#fff1f2", "#fecdd3", _STAR)),
]

# Anything on the list that is not one of the above. A day off is still
# worth marking, and inventing a greeting for a name nobody recognised is
# how software ends up wishing people a happy strike.
DEFAULT = Theme("day", "", "#1e3a5f", "#eff6ff", "#dbeafe", _STAR)


def theme_for(name: str) -> Theme:
    low = " ".join(str(name or "").lower().split())
    for words, theme in THEMES:
        if any(w in low for w in words):
            return theme
    return DEFAULT


@dataclass(frozen=True)
class Coming:
    """The festival being counted down to, and how it should look."""
    name: str
    day: date
    days: int                       # 0 = today
    theme: Theme

    @property
    def when(self) -> str:
        if self.days == 0:
            return "today"
        if self.days == 1:
            return "tomorrow"
        return f"in {self.days} days"

    @property
    def greeting(self) -> str:
        """"Happy Diwali" where there is one, else the holiday's own name."""
        return self.theme.greeting or self.name


# {(org, branch, date): Coming or None}. One entry per company per day;
# the whole thing is a handful of objects and is thrown away on restart.
_SEEN: dict[tuple, object] = {}


def coming(db: Session, org_id: int, branch_id: int | None = None,
           on: date | None = None) -> Coming | None:
    """The next holiday within LEAD_DAYS, or None. One query a day.

    Cached by company and date rather than per request: the answer cannot
    change between two page loads on the same day, and a decoration that
    costs a query on every click is a decoration that should not exist.
    """
    today = on or clock.today()
    key = (org_id, branch_id, today)
    if key in _SEEN:
        return _SEEN[key]                              # type: ignore[return-value]

    found = hol.holidays_for(db, org_id, branch_id, today,
                             today + timedelta(days=LEAD_DAYS))
    out = None
    if found:
        day = min(found)
        out = Coming(name=found[day], day=day, days=(day - today).days,
                     theme=theme_for(found[day]))

    # Today's answers only. Without this the dictionary would keep every
    # day the process has been alive, which is a slow leak on a server that
    # runs for months.
    for stale in [k for k in _SEEN if k[2] != today]:
        _SEEN.pop(stale, None)
    _SEEN[key] = out
    return out


def forget() -> None:
    """Drop what is remembered — for the tests, and for a holiday just added."""
    _SEEN.clear()
