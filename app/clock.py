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
from datetime import date, datetime, timedelta, timezone

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
