"""Recurring task engine.

`run_spawn(db)` is safe to call as often as you like — on app start and from
the daily loop — because a rule only ever produces one task per day it covers.

Two dates matter here and they are not always the same:

  covers_day   the day the job is FOR — the Sunday the report is due.
  due_at       when it has to be in by, which is when the task appears.

They differ whenever the job falls on a day nobody is in. Work planned for a
closed day is created on the last working day BEFORE it, so the Sunday report
is on somebody's desk on Saturday rather than being handed in late on Monday.

That applies to weekly, fortnightly, monthly, quarterly and yearly jobs — the
ones where a missed day means a missed week, month or year. It deliberately
does NOT apply to a daily job: bringing Sunday's copy back onto Saturday puts
two identical rows on one person's list, which reads as the same task twice,
and there is another one tomorrow anyway.
"""
import re
from datetime import datetime, date, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import clock
from ..models import (RecurringRule, Recurrence, MonthMode, Task, TaskComment,
                      TaskSource)
from . import notify, holidays

# How far ahead to look for closed days whose work has to be pulled back.
# Ten days covers a Sunday, a long festival run, and the two together.
LOOKAHEAD_DAYS = 10


def is_due_today(rule: RecurringRule, today: date) -> bool:
    """Does this rule's schedule land on that calendar day?

    Says nothing about whether anybody is in that day — that is decided
    separately, so the two questions cannot get tangled.
    """
    f = rule.frequency
    if f == Recurrence.DAILY:
        return True
    if f == Recurrence.WEEKDAYS:
        return today.weekday() < 5
    if f == Recurrence.WEEKLY:
        return today.weekday() in rule.weekday_list
    if f == Recurrence.FORTNIGHTLY:
        if today.weekday() not in rule.weekday_list:
            return False
        # Count whole weeks between the Monday of the anchor's week and the
        # Monday of this one. Comparing week starts rather than the dates
        # themselves is what makes a rule set on a Friday and read on the
        # Monday after land in the same fortnight, instead of flipping.
        anchor = rule.anchor_on or today
        a_week = anchor - timedelta(days=anchor.weekday())
        t_week = today - timedelta(days=today.weekday())
        return ((t_week - a_week).days // 7) % 2 == 0
    if f == Recurrence.MONTHLY:
        return _lands_in_month(rule, today)
    if f == Recurrence.QUARTERLY:
        if today.month not in rule.quarter_months:
            return False
        return _lands_in_month(rule, today)
    if f == Recurrence.YEARLY:
        # day_of holds the month and day together as MMDD — 417 is 17 April —
        # because a yearly rule needs both and there is only one column.
        want = rule.day_of or 101
        month, dom = divmod(want, 100)
        if not 1 <= month <= 12:
            return False
        return today.month == month and today.day == _month_day(dom, today)
    return False


def _lands_in_month(rule: RecurringRule, today: date) -> bool:
    """Within a month this rule runs in, is today the day?

    Two ways of saying it, and neither can be written as the other: "the 5th"
    moves around the week, "the first Saturday" moves around the dates.
    """
    if rule.month_mode == MonthMode.DATE:
        # Several dates are allowed — "the 15th and the 30th" is one job on
        # one rota, not two rules. Each is pulled back in a month too short
        # for it, and the set stops the 30th and the 31st both landing on
        # the 28th of February and creating the same task twice.
        wanted = {_month_day(d, today) for d in (rule.month_day_list or [1])}
        return today.day in wanted

    if today.weekday() not in rule.weekday_list:
        return False
    # Which occurrence of this weekday today is: the 1st to 7th of the month
    # hold the first of each weekday, the 8th to 14th the second, and so on.
    nth = (today.day - 1) // 7 + 1
    # And whether it is the last one — no day of the same weekday after it
    # this month. Not the same as the fourth: some months have five.
    is_last = (today + timedelta(days=7)).month != today.month
    wanted = rule.week_list or [1]
    return nth in wanted or (is_last and -1 in wanted)


def _month_day(target: int, today: date) -> int:
    """The day of the month to fire on, pulled back in a short month.

    A rule set for the 31st fires on the 30th in April and the 28th in
    February, rather than not firing at all.
    """
    nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    last_day = (nxt - timedelta(days=1)).day
    return min(max(target, 1), last_day)


def _already_made(db: Session, rule: RecurringRule, covers: date) -> bool:
    """Has this rule's job for that day already been created?

    Keyed on the day the task is FOR, not the day it was made, so bringing
    Sunday's job forward to Saturday cannot collide with Saturday's own.
    """
    return db.scalar(
        select(Task.id).where(Task.rule_id == rule.id,
                              Task.covers_day == covers).limit(1)) is not None


def days_to_spawn(db: Session, rule: RecurringRule, today: date) -> list[date]:
    """Which days' work this rule owes, if it is run on `today`.

    Today's own, when today is a working day — plus any closed day coming up
    whose last working day before it is today. That second part is what pulls
    a Sunday job back onto Saturday.
    """
    if holidays.is_closed(db, rule.org_id, today, rule.branch_id):
        # Nobody is in today, so nothing is created today. Whatever was due
        # today was already created on the last working day before it.
        return []

    out = []
    if is_due_today(rule, today):
        out.append(today)

    # A daily job is never brought forward. Pulling Sunday's copy back onto
    # Saturday puts two identical rows on one person's list — same title,
    # same deadline — and what they see is the same task twice. A daily job
    # that lands on a closed day is simply not done that day; there is
    # another one tomorrow. Everything that comes round less often IS
    # brought forward, because missing it means missing it for a week, a
    # month or a year.
    if rule.frequency == Recurrence.DAILY:
        return out

    for n in range(1, LOOKAHEAD_DAYS + 1):
        day = today + timedelta(days=n)
        if not holidays.is_closed(db, rule.org_id, day, rule.branch_id):
            break            # the run of closed days has ended; stop looking
        if not is_due_today(rule, day):
            continue
        # Only the working day immediately before the closed run creates it,
        # so a three-day festival does not have three days all making it.
        if holidays.previous_working_day(
                db, rule.org_id, day - timedelta(days=1), rule.branch_id) == today:
            out.append(day)
    return out


def run_spawn(db: Session, today: date | None = None) -> int:
    today = today or clock.today()
    created = 0
    rules = db.scalars(select(RecurringRule).where(RecurringRule.active.is_(True))).all()

    for rule in rules:
        for covers in days_to_spawn(db, rule, today):
            if _already_made(db, rule, covers):
                continue

            # Each task in its own savepoint. The check above is not enough
            # on its own: the midnight loop, the cron ping and somebody
            # pressing "Run spawner now" can all look at the same instant,
            # all see nothing, and all insert. The database refuses the
            # second one (uq_task_rule_day) and this turns that refusal into
            # "somebody else already made it" rather than a failed run that
            # abandons every rule after it.
            hh, mm = (int(x) for x in rule.due_time.split(":"))
            due_at = datetime.combine(today, datetime.min.time()).replace(
                hour=hh, minute=mm)

            try:
                with db.begin_nested():
                    task = Task(
                        org_id=rule.org_id,
                        branch_id=rule.branch_id,
                        title=rule.title,
                        details=rule.details,
                        assigner_id=rule.assigner_id,
                        doer_id=rule.doer_id,
                        priority=rule.priority,
                        source=TaskSource.RECURRING,
                        due_at=due_at,
                        covers_day=covers,
                        rule_id=rule.id,
                        requires_audit=rule.requires_audit,
                        requires_attachment=rule.requires_attachment,
                    )
                    db.add(task)
                    db.flush()

                    # Say why it turned up early, on the task itself, so
                    # nobody has to work out why Saturday has two of these.
                    if covers != today:
                        why = holidays.closed_reason(db, rule.org_id, covers,
                                                     rule.branch_id) or "a closed day"
                        db.add(TaskComment(
                            task_id=task.id, author_id=rule.assigner_id,
                            body=f"This is the {covers:%A %d %b} job, brought "
                                 f"forward because {covers:%d %b} is {why}."))

                    notify.queue_task_assigned(db, task)
            except IntegrityError:
                # Another run got there first. Not an error: the day's job
                # exists, which is all this was trying to achieve.
                continue

            rule.last_spawned_on = today
            created += 1

    db.commit()
    return created


# ==================================================== reading a schedule ====
# One reader, used by the Checklist form AND the bulk import, so a schedule
# typed into the page and the same schedule in a spreadsheet cannot end up
# meaning two different things.

WEEKDAY_WORDS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
NTH_WORDS = {"1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3,
             "third": 3, "4th": 4, "fourth": 4, "5th": 5, "fifth": 5,
             "last": -1, "final": -1}


def _weekday_numbers(raw) -> list[int]:
    """"Mon,Thu" or "0,3" or "Monday and Thursday" -> [0, 3]."""
    out = []
    for part in re.split(r"[,/&+]|\band\b", str(raw or ""), flags=re.I):
        part = part.strip().lower().rstrip(".")
        if not part:
            continue
        if part.isdigit() and 0 <= int(part) <= 6:
            out.append(int(part))
        elif part in WEEKDAY_WORDS:
            out.append(WEEKDAY_WORDS[part])
        else:
            raise ValueError(
                f"'{part}' is not a day — use Mon, Tue, Wed, Thu, Fri, Sat "
                "or Sun, and separate several with a comma")
    return sorted(set(out))


def read_schedule(frequency: str, values) -> dict:
    """Turn what somebody chose into the columns a rule stores.

    `values` is anything with .get / .getlist — a submitted form, or a small
    dict built from a spreadsheet row. Raises ValueError with a sentence the
    person can act on, never a silent default: a schedule guessed wrong puts
    work on somebody on the wrong day for months before anyone notices.
    """
    freq = (frequency or "").strip().lower()
    if freq not in {f.value for f in Recurrence}:
        raise ValueError(
            f"'{frequency}' is not a frequency — use daily, weekdays, weekly, "
            "fortnightly, monthly, quarterly or yearly")
    f = Recurrence(freq)
    out = {"frequency": f, "day_of": None, "weekdays": None, "month_days": None,
           "weeks_of_month": None, "start_month": None, "anchor_on": None}

    def one(name, default=""):
        got = values.get(name, default)
        return ("" if got is None else str(got)).strip()

    def many(name):
        if hasattr(values, "getlist"):
            return values.getlist(name)
        got = values.get(name) or ""
        return [got] if got else []

    if f == Recurrence.DAILY:
        return out

    if f == Recurrence.WEEKDAYS:
        # Still accepted from a spreadsheet, because people write it — but
        # saved as what it actually is, so it looks like every other weekly
        # rule and Saturday can be added to it later.
        out["frequency"] = Recurrence.WEEKLY
        out["weekdays"] = "0,1,2,3,4"
        return out

    if f in (Recurrence.WEEKLY, Recurrence.FORTNIGHTLY):
        raw_days = ",".join(str(v) for v in many("weekdays")) or one("day")
        try:
            days = _weekday_numbers(raw_days)
        except ValueError as bad_day:
            # Dates given where a weekday belongs is the commonest mix-up,
            # and worth its own sentence. Anything else keeps the message
            # that names what could not be read.
            if not _month_dates(raw_days):
                raise
            days = []
        if not days:
            # The commonest mix-up: dates given where a weekday belongs.
            # "the 15th and the 30th" is twice a month, not every two weeks —
            # one fires 24 times a year, the other 26, and they drift apart.
            looks_like_dates = _month_dates(raw_days)
            if looks_like_dates:
                joined = ",".join(str(d) for d in looks_like_dates)
                raise ValueError(
                    "That looks like dates in the month, not days of the "
                    f"week. {freq.title()} runs on a weekday — for the "
                    f"{joined.replace(',', 'th and the ')}th of every month, "
                    f"set the frequency to monthly and the day to “{joined}”.")
            raise ValueError(
                f"A {freq} rule needs at least one day of the week.")
        out["weekdays"] = ",".join(str(d) for d in days)
        if f == Recurrence.FORTNIGHTLY:
            anchor = one("anchor_on") or one("anchor")
            if anchor:
                try:
                    out["anchor_on"] = datetime.strptime(
                        anchor[:10], "%Y-%m-%d").date()
                except ValueError:
                    raise ValueError(
                        f"'{anchor}' is not a date — a fortnightly rule counts "
                        "from a date, written YYYY-MM-DD")
            else:
                # No anchor given: this week is an on week. Stated rather
                # than left to chance, because "every second Tuesday" with no
                # starting point is only half an instruction.
                out["anchor_on"] = clock.today()
        return out

    if f == Recurrence.YEARLY:
        raw = one("year_day") or one("day")
        if not raw:
            raise ValueError("A yearly rule needs the date it falls on, as DD/MM.")
        out["day_of"] = _year_day(raw)
        return out

    if f == Recurrence.QUARTERLY:
        # Quarterly asks for one thing: the date it first falls on. The other
        # three follow every third month from it, which is how anybody would
        # work them out on paper — 5 February means 5 May, 5 August, 5
        # November, and nobody has to think about which quarter that is.
        raw = (one("quarter_start") or one("start_date") or one("day")
               or one("day_of_month"))
        day, month = _first_date(raw, freq)
        out["day_of"] = day
        out["month_days"] = str(day)
        out["start_month"] = month
        return out

    # monthly

    mode = (one("month_mode") or "").lower()
    # A form sends one value per tick box; a spreadsheet sends "1,3" in one
    # cell. Flatten both to the same list rather than making the caller care.
    weeks = [w.strip() for raw in many("weeks_of_month")
             for w in str(raw).replace("&", ",").split(",") if w.strip()]
    if mode == "weekday" or (not mode and weeks):
        picked = []
        for w in weeks:
            w = str(w).strip().lower()
            if w in NTH_WORDS:
                picked.append(NTH_WORDS[w])
            elif w.lstrip("-").isdigit() and int(w) in (1, 2, 3, 4, 5, -1):
                picked.append(int(w))
            else:
                raise ValueError(
                    f"'{w}' is not a week — use first, second, third, fourth "
                    "or last")
        if not picked:
            raise ValueError(
                "Pick which week(s) of the month — first, third, last…")
        days = _weekday_numbers(",".join(str(v) for v in many("weekdays")))
        if not days:
            raise ValueError("Pick which day of the week, e.g. Saturday.")
        out["weeks_of_month"] = ",".join(str(w) for w in sorted(
            set(picked), key=lambda n: (n < 0, n)))
        out["weekdays"] = ",".join(str(d) for d in days)
        return out

    raw = one("day_of_month") or one("day")
    dates = _month_dates(raw)
    if not dates:
        raise ValueError(
            f"A {freq} rule needs a date 1-31 — several are fine, like "
            "“15,30” — or the week and day instead (“first Saturday”). "
            "Got '{}'.".format(raw or "nothing"))
    out["month_days"] = ",".join(str(d) for d in dates)
    out["day_of"] = dates[0]          # kept in step for anything reading it
    return out


def _month_dates(raw: str) -> list[int]:
    """"15" or "15,30" or "15th and 30th" -> [15, 30]."""
    out = []
    for part in re.split(r"[,/&+]|\band\b", str(raw or ""), flags=re.I):
        part = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", part.strip(), flags=re.I).strip()
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= 31:
            return []
        out.append(int(part))
    return sorted(set(out))


def _year_day(raw: str) -> int:
    """DD/MM (or a real date) stored as MMDD — 17/04 becomes 417."""
    text = str(raw).strip()
    if isinstance(raw, (datetime, date)):
        return raw.month * 100 + raw.day
    parts = [p for p in re.split(r"[/\-. ]", text) if p]
    if len(parts) < 2 or not all(p.isdigit() for p in parts[:2]):
        raise ValueError(f"'{text}' is not a date — yearly wants DD/MM, e.g. 17/04")
    dd, mm = int(parts[0]), int(parts[1])
    if dd > 31 and mm <= 31:              # somebody typed MM/DD
        dd, mm = mm, dd
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        raise ValueError(f"'{text}' is not a real date — yearly wants DD/MM")
    return mm * 100 + dd


def _first_date(raw: str, freq: str) -> tuple[int, int]:
    """The day and month a quarterly or yearly rule first falls on.

    Takes a real date from a date box, or DD/MM typed by hand.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError(
            f"A {freq} rule needs the date it first falls on — the three "
            "after it are every third month from there.")
    if isinstance(raw, (datetime, date)):
        return raw.day, raw.month
    parts = [p for p in re.split(r"[/\-. ]", text) if p]
    if len(parts) >= 3 and len(parts[0]) == 4:            # YYYY-MM-DD
        try:
            d = date(int(parts[0]), int(parts[1]), int(parts[2]))
            return d.day, d.month
        except ValueError:
            raise ValueError(f"'{text}' is not a real date.")
    if len(parts) < 2 or not all(p.isdigit() for p in parts[:2]):
        raise ValueError(
            f"'{text}' is not a date — write it as DD/MM, e.g. 05/02 for the "
            "5th of February.")
    dd, mm = int(parts[0]), int(parts[1])
    if dd > 31 and mm <= 31:
        dd, mm = mm, dd
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        raise ValueError(f"'{text}' is not a real date — write it as DD/MM.")
    return dd, mm
