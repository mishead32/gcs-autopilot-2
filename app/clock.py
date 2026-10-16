"""One clock for the whole application: Indian Standard Time.

Every datetime in this database is stored NAIVE — a bare date and time with
no timezone attached. That is fine as long as everything agrees on which
timezone those bare numbers mean. It did not.

Deadlines come from a form where somebody in Chandigarh types "18:00", and
were stored exactly as typed. "Now", though, came from datetime.utcnow().
On a server running in UTC — which is every free host — that made every
deadline 5 hours 30 minutes too generous:

    due 23 Sep 18:00   (what the manager meant: 6pm IST)
    overdue at         23 Sep 18:00 UTC  =  23 Sep 23:30 IST

So a task nobody touched all day still counted as on time until half past
eleven at night, and the "not done on time" half of the EM score — half of
every benchmark — was wrong by the same margin, every single day.

The fix is one source of truth. now() is the current moment in IST with the
timezone stripped off again, so it lines up with the naive deadlines people
type. Deliberately NOT timezone-aware objects: every stored column is naive,
and comparing an aware datetime with a naive one raises TypeError at the
point of comparison — which in this app means inside a scoring run.

If the business ever spans timezones, this is the file to change: make the
columns timezone-aware and convert at the edges. Until then, one office, one
clock, one function.
"""
import os
from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
UTC_OFFSET = timedelta(hours=5, minutes=30)


def now() -> datetime:
    """The current moment in IST, as a naive datetime."""
    return datetime.now(IST).replace(tzinfo=None)


def today() -> date:
    """Today's date in IST.

    Not date.today(): on a UTC server that is still yesterday until half past
    five in the morning, which would spawn the day's checklist under the wrong
    date and make the follow-up desk open on the previous day.
    """
    return now().date()


def stamp(fmt: str = "%d %b %Y, %I:%M %p") -> str:
    """Current IST time, formatted — for 'last updated' lines."""
    return now().strftime(fmt)


# --------------------------------------------------------------- spans ----
# A turnaround time is not always a number of hours. "Submit within 2 days",
# "review every month", "call back in 30 minutes" are all natural ways to
# describe the same field, and forcing them into hours makes a monthly step
# read as 720 — which is both unreadable and wrong, because months are not
# all the same length.
SPAN_UNITS = {
    "minutes": "minute(s)",
    "hours": "hour(s)",
    "days": "day(s)",
    "weeks": "week(s)",
    "months": "month(s)",
}

# What each unit offers in its second box. Whole numbers people actually use,
# rather than a free-text field that invites "0" and "999".
SPAN_CHOICES = {
    "minutes": [5, 10, 15, 20, 30, 45],
    "hours": [1, 2, 3, 4, 6, 8, 12, 18, 24, 36, 48, 72],
    "days": [1, 2, 3, 4, 5, 6, 7, 10, 15, 20, 30],
    "weeks": [1, 2, 3, 4, 6, 8],
    "months": [1, 2, 3, 4, 6, 12],
}


# ------------------------------------------------- calendar deadlines -----
# A turnaround is not always a LENGTH. "Due on the 7th", "due every Monday",
# "due on the 1st and the 15th" are deadlines that come round, and forcing
# them into a span is impossible: "a month after the 7th" drifts off the 7th
# the first time somebody is a day late.
#
# So a step's TAT can name a date instead of a duration. It is still counted
# FROM the step's anchor — including a date somebody typed on the start form
# — so "joining date, then the next 15th" is one setting, not a calculation
# anybody has to do in their head.
CALENDAR_UNITS = {
    "monthday": "A date each month",
    "weekday": "A weekday each week",
    "fortnight": "The 1st and the 15th",
}
SPAN_UNITS.update(CALENDAR_UNITS)

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]

# 1-28 only. The 29th, 30th and 31st do not exist in every month, and a
# deadline that silently moves to the 28th in February is a deadline nobody
# can plan around. Somebody wanting month-end says so with a span.
SPAN_CHOICES["monthday"] = list(range(1, 29))
SPAN_CHOICES["weekday"] = list(range(7))
SPAN_CHOICES["fortnight"] = [0]

# The hour a calendar deadline falls at. "Due on the 15th" means by the end
# of the 15th, not one second past midnight on it.
CAL_DUE_HOUR = int(os.getenv("STEP_DUE_HOUR", "18"))


def unit_labels() -> dict:
    """How each TAT unit is written in the dropdown.

    A span is titled from its key — "Hours", "Weeks" — which reads fine. A
    calendar unit is not a unit of anything, so titling its key gives
    "Monthday" and "Fortnight", which mean nothing to the person choosing.
    Those get a sentence instead.
    """
    out = {}
    for key in SPAN_UNITS:
        out[key] = CALENDAR_UNITS.get(key) or key.title()
    return out


def span_options() -> dict:
    """Every unit's second box as [value, label] pairs.

    The old shape was a bare list of numbers, which is fine while every
    choice IS a number. A weekday is 0 and reads "Monday", so the label has
    to travel with the value.
    """
    out = {}
    for unit, values in SPAN_CHOICES.items():
        if unit == "weekday":
            out[unit] = [[v, WEEKDAY_NAMES[v]] for v in values]
        elif unit == "monthday":
            out[unit] = [[v, _ordinal(v)] for v in values]
        elif unit == "fortnight":
            out[unit] = [[0, "the 1st or the 15th, whichever is next"]]
        else:
            out[unit] = [[v, str(v)] for v in values]
    return out


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        tail = "th"
    else:
        tail = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"the {n}{tail}"


def next_occurrence(start: datetime, unit: str, value: int) -> datetime:
    """The next time this calendar rule comes round, at or after `start`.

    "At or after" on purpose: a joining date of the 15th with a deadline of
    "the 15th" is due THAT day, not a month later. Only a target already
    gone by moves on to the next one.
    """
    value = int(value or 0)
    day = start.date()

    if unit == "weekday":
        want = max(0, min(6, value))
        ahead = (want - day.weekday()) % 7
        target = day + timedelta(days=ahead)

    elif unit == "fortnight":
        # Whichever of the 1st or the 15th comes next. Both are real dates in
        # every month, which is why they are the pair people pick.
        if day.day <= 1:
            target = day.replace(day=1)
        elif day.day <= 15:
            target = day.replace(day=15)
        else:
            target = _first_of_next_month(day)

    elif unit == "monthday":
        want = max(1, min(28, value))
        if day.day <= want:
            target = day.replace(day=want)
        else:
            nxt = _first_of_next_month(day)
            target = nxt.replace(day=want)

    else:                                   # not a calendar unit after all
        return add_span(start, unit, value)

    return datetime.combine(target, time(hour=CAL_DUE_HOUR))


def _first_of_next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=7)).replace(day=1)


def add_months(start: datetime, months: int) -> datetime:
    """Calendar months, not 30-day blocks.

    31 Jan + 1 month is 28 Feb (29 in a leap year), not 3 March. Clamping to
    the last day of the shorter month is what every calendar does and what
    anybody setting a monthly deadline means.
    """
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    # last day of the destination month
    if month == 12:
        last = 31
    else:
        last = (date(year, month + 1, 1) - timedelta(days=1)).day
    return start.replace(year=year, month=month, day=min(start.day, last))


def add_span(start: datetime, unit: str, value: int) -> datetime:
    """start + value units. Unknown units fall back to hours."""
    value = max(0, int(value or 0))
    if unit == "months":
        return add_months(start, value)
    if unit == "weeks":
        return start + timedelta(weeks=value)
    if unit == "days":
        return start + timedelta(days=value)
    if unit == "minutes":
        return start + timedelta(minutes=value)
    return start + timedelta(hours=value)


def span_label(unit: str, value: int) -> str:
    """'2 days', '1 month' — for reading back on a page."""
    value = int(value or 0)
    word = (unit or "hours").rstrip("s")
    return f"{value} {word}" + ("" if value == 1 else "s")
