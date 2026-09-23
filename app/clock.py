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
