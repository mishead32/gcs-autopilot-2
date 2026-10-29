"""Overdue work climbs the ladder, one rung a day, until it is closed.

This is the approved escalation process, in code. The shape of it, from the
document:

    Day 1 overdue   the HOD of that company
    Day 2 overdue   the EA to the CMD
    Day 3 overdue   the CMD

and each message carries the task's own ID — DEL-341, CL-07, FMS-12 — so the
reply names the work rather than describing it.

Four decisions are worth reading before the code, because each of them was
a choice between two defensible answers.


ONE MESSAGE A DAY PER TASK, NOT A CATCH-UP OF EVERY RUNG IT HAS PASSED

A task that is five days overdue the first morning this runs — which is
every old task, the morning it is switched on — does not generate five
emails. It generates the one for the rung it has reached. Escalation exists
to put a thing in front of the person who can move it; delivering four
days of history to the CMD at once buries today's news in it. The rungs
that were never sent are simply never sent, and the log says so by not
having a row for them.


NEVER TWICE FOR THE SAME RUNG

The promise in the approved document, and it is enforced by the database
rather than by the code remembering: unique on (task, level, repeat).
Two web processes, a restart mid-run, somebody pressing "Run the check
now" an hour after the morning run — none of them can send the HOD the
same escalation twice. The second insert loses on the constraint and is
rolled back to a skip.


WHAT COUNTS AS A DAY OVERDUE

By default, a day somebody was in. The offices keep Sunday off, the gym
and the spa do not, and that is already recorded per company. Counting
calendar days means a task due on Saturday reaches the CMD on Tuesday,
having spent one of its three days on a Sunday when nobody could have
done it, and the first person to notice that is the doer being asked to
explain it. Switchable, because it is a judgement rather than a fact.


WHICH WORK ESCALATES

By default, exactly the work the follow-up desk is expected to chase —
`chasing.needs_chasing`, the one rule in the one file. So a low-priority
daily checklist job does not reach the CMD because somebody did not tick
it on a Tuesday, and an FMS step whose turn has not come round is never
escalated at all. Those are the same two exclusions the desk already
makes, and having escalation disagree with the desk about what is owed
would mean two answers to one question.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from html import escape as html_escape

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import clock
from ..models import (
    Branch, Escalation, EscalationMail, EscalationRule, Organization,
    Priority, Task, TaskStatus, User, PARKED_STATES,
)
from . import chasing, holidays, mail

# The states in which the work is still owed by the doer.
OPEN = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS,
        TaskStatus.REJECTED, TaskStatus.REOPENED)

# Never count further back than this. A task overdue by more than a quarter
# is a data problem rather than a late job, and walking a company's closed
# days one at a time for years is a slow query somebody is waiting on.
MAX_DAYS = 120

# How many reminders to send after the top rung, when the setting says to
# repeat. Thirty working days of "still open" is already past the point
# where another email is what fixes it.
MAX_REPEATS = 30


# ------------------------------------------------------------- the ladder --

def ladder(db: Session, org_id: int, branch_id: int | None) -> dict[int, EscalationRule]:
    """This company's rungs by level, falling back to the group's.

    A company with its own rung 1 and nothing else gets its own HOD and the
    group's EA and CMD, which is exactly the real arrangement: the HOD
    differs per company, the two above them do not.
    """
    rows = db.scalars(
        select(EscalationRule).where(EscalationRule.org_id == org_id)
    ).all()
    out: dict[int, EscalationRule] = {}
    for r in rows:                                 # group-wide first …
        if r.branch_id is None:
            out[r.level] = r
    for r in rows:                                 # … then overridden
        if branch_id is not None and r.branch_id == branch_id:
            out[r.level] = r
    # A rung with nobody to send to is not a rung. Left in the table on
    # purpose — somebody half-filling the form should see their row again,
    # not lose it — but it cannot be escalated to.
    return {lvl: r for lvl, r in out.items() if r.email or r.person is not None}


def top_level(rungs: dict[int, EscalationRule]) -> int:
    return max(rungs) if rungs else 0


def missing_ladders(db: Session, org_id: int) -> list[str]:
    """Companies that would escalate to nobody. Shown on the Setup page."""
    out = []
    for b in db.scalars(select(Branch).where(Branch.org_id == org_id)
                        .order_by(Branch.name)).all():
        if not ladder(db, org_id, b.id):
            out.append(b.name)
    return out


# --------------------------------------------------------- days overdue  ---

def days_overdue(db: Session, task: Task, on: date | None = None,
                 working_days: bool = True) -> int:
    """How many days late this task is, as of `on` (today by default).

    0 means not overdue yet. On working days, a day the company was shut
    does not count — so a Saturday deadline is one day overdue on Monday,
    not two.
    """
    on = on or clock.today()
    due = task.due_at.date()
    if on <= due:
        return 0
    if not working_days:
        return min((on - due).days, MAX_DAYS)

    count = 0
    day = due + timedelta(days=1)
    while day <= on and count < MAX_DAYS:
        if not holidays.is_closed(db, task.org_id, day, task.branch_id):
            count += 1
        day += timedelta(days=1)
    return count


# ------------------------------------------------------------ which work ---

def in_scope(task: Task, scope: str) -> bool:
    """Does this kind of work escalate at all?"""
    if task.status in PARKED_STATES or task.status not in OPEN:
        return False
    if task.submitted_at is not None:
        # Handed in and sitting with an auditor. The doer has done their
        # part; escalating it reports them for the audit queue's backlog.
        return False
    if task.flow_waiting:
        # A planned FMS step whose turn has not come. Nobody can be late
        # for work the run has not reached.
        return False
    if scope == "all":
        return True
    if scope == "high":
        return task.priority == Priority.HIGH
    return chasing.needs_chasing(task)


def overdue_tasks(db: Session, org_id: int, on: date | None = None,
                  working_days: bool = True, scope: str = "chased"
                  ) -> list[tuple[Task, int]]:
    """Every task that is late today, with how late, newest overdue first."""
    on = on or clock.today()
    # Due before `on` began. A task due later today is not overdue, and a
    # task due earlier today is one the doer still has the day for — the
    # first morning it can be called late is the morning after its date.
    rows = db.scalars(
        select(Task).where(
            Task.org_id == org_id,
            Task.status.in_(OPEN),
            Task.submitted_at.is_(None),
            Task.due_at < datetime.combine(on, time.min),
        )
    ).all()
    out = []
    for t in rows:
        if not in_scope(t, scope):
            continue
        n = days_overdue(db, t, on, working_days)
        if n >= 1:
            out.append((t, n))
    out.sort(key=lambda p: (-p[1], p[0].id))
    return out


# ------------------------------------------------------ what to send next --

def already(db: Session, task_id: int) -> list[Escalation]:
    return db.scalars(
        select(Escalation).where(Escalation.task_id == task_id)
        .order_by(Escalation.level, Escalation.repeat_n)
    ).all()


def next_step(sent: list[Escalation], late: int, rungs: dict[int, EscalationRule],
              after_top: str, today: date) -> tuple[int, int] | None:
    """Which (level, repeat) is owed right now, or None.

    `sent` is what has already gone out for this task.
    """
    if not rungs:
        return None
    top = top_level(rungs)
    # One a day, whatever else is true. Two runs in one morning, or a run
    # after somebody pressed the button by hand, must not stack.
    if any(e.day == today for e in sent):
        return None

    level = min(late, top)
    if level < min(rungs):
        return None
    # The nearest rung at or below the days overdue. A ladder of 1, 2, 5
    # leaves day 3 and day 4 on rung 2, which is what somebody who typed
    # 5 meant: the CMD on the fifth day.
    level = max(l for l in rungs if l <= level)

    done = {(e.level, e.repeat_n) for e in sent}
    if (level, 0) not in done:
        return level, 0

    if level < top or after_top != "repeat":
        return None
    # The top rung, already sent, and the task is still open. Another
    # reminder to the same person, numbered so the log reads honestly.
    used = [e.repeat_n for e in sent if e.level == top]
    nxt = max(used) + 1
    return (top, nxt) if nxt <= MAX_REPEATS else None


# ------------------------------------------------------------ the message --

# ------------------------------------------------------------- the table --
# The list inside one email, built twice: as an HTML table, which is what
# anybody actually reads, and as fixed-width text for the plain part of the
# message. Both say the same thing.
#
# The HTML is written the way email HTML has to be written, which is not the
# way web pages are written any more:
#
#   * Every style is inline. Gmail strips a <style> block in some views and
#     Outlook ignores large parts of it, so a stylesheet is a table that
#     arrives unformatted at exactly the wrong moment.
#   * Layout is tables, not flex or grid. Outlook on Windows renders through
#     Word, which has no idea what either of those is.
#   * Row colours are set per row rather than with :nth-child, for the same
#     reason — and because the whole point of striping is that it survives.
#   * Nothing is loaded from anywhere. No images, no fonts, no tracking
#     pixel: an email that needs the network to be legible is an email that
#     shows up blank behind a corporate mail gateway.
#
# And every single value out of the database goes through _esc(). Task
# titles and people's names are typed by hand by twenty staff; a task called
# `<b>URGENT` would otherwise reformat the rest of the email, and worse is
# possible.

COLS = [("Task ID", 16), ("Doer", 20), ("Planned", 18),
        ("Priority", 8), ("Overdue", 7), ("Task", 38)]


def _cell(text: str, width: int) -> str:
    """Pad to width, or cut with an ellipsis so columns never slip."""
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= width:
        return text.ljust(width)
    return text[:width - 1] + "…"


def sort_key(pair: tuple[Task, int]):
    """Doer name A to Z, then the oldest planned date first.

    Sorting by doer is what makes the list usable: it puts one person's work
    together, so a HOD reads it as four conversations rather than fourteen
    unrelated lines. Case-folded, or 'amit' would sort after 'Zoya'.
    """
    task, _late = pair
    name = (task.doer.name if task.doer else "").strip().lower()
    return (name, task.due_at, task.id)


def by_company(rows: list[tuple[Task, int]]) -> list[tuple[str, list]]:
    """The rows split company by company, each block sorted doer A to Z.

    Level 1 is a different person at each company, so their email is about
    one company and needs none of this. Levels 2 and 3 are the SAME two
    people for the whole group, so their email is every company at once —
    and thirty rows from five companies, told apart only by a grey line
    under each task, is a list nobody can act on. Blocked by company it
    reads as five short lists, which is how the EA and the CMD actually
    work: company by company.

    Companies alphabetically, anything with no company last — it is the odd
    case, and leading with it reads as though it were the main one.
    """
    blocks: dict[str, list] = {}
    for pair in rows:
        task = pair[0]
        blocks.setdefault(task.branch.name if task.branch else "",
                          []).append(pair)
    named = sorted((k for k in blocks if k), key=str.lower)
    out = [(k, sorted(blocks[k], key=sort_key)) for k in named]
    if "" in blocks:
        out.append(("No company", sorted(blocks[""], key=sort_key)))
    return out


def table(rows: list[tuple[Task, int]], with_doer: bool = True) -> str:
    """The task list, sorted by doer, as fixed-width plain text.

    `with_doer` is False on a doer's own copy: every row would carry the
    same name, which is a column of noise where the task's own words could
    be instead.
    """
    cols = COLS if with_doer else [c for c in COLS if c[0] != "Doer"]
    rule = "  ".join("-" * w for _h, w in cols)
    head = "  ".join(_cell(h, w) for h, w in cols).rstrip()
    blocks = by_company(rows)
    # A heading over a single block is furniture: the whole email is about
    # that company, and saying so twice reads as though there were another.
    banded = len(blocks) > 1
    out = [] if banded else [head, rule]

    for company, pairs in blocks:
        if banded:
            out += ["", f"{company.upper()} — {len(pairs)} task"
                        + ("s" if len(pairs) != 1 else ""), head, rule]
        out += [_text_row(task, late, cols) for task, late in pairs]
    return "\n".join(out)


def _text_row(task: Task, late: int, cols) -> str:
    """One line of the plain-text table."""
    values = {
        # ref_label, not ref: an FMS step shares its submission's number
        # with the other steps of that run, so the reference on its own
        # does not say which row of this table is which — and an email is
        # read where nobody can click to find out.
        "Task ID": task.ref_label,
        "Doer": task.doer.name if task.doer else "—",
        "Planned": task.due_at.strftime("%d %b, %I:%M %p"),
        "Priority": task.priority.value.upper(),
        "Overdue": f"{late} day" + ("s" if late != 1 else ""),
        "Task": task.title,
    }
    return "  ".join(_cell(values[h], w) for h, w in cols).rstrip()


# --------------------------------------------------------- the HTML table --

INK = "#1f2937"          # body text
MUTED = "#6b7280"        # captions
LINE = "#dde3ea"         # rules between rows
BRAND = "#0f4c81"        # the header bar
URGENT = "#b91c1c"       # the header bar at the top rung
STRIPE = "#f4f7fa"       # every other row
PAPER = "#ffffff"
SHELL = "#eef2f6"        # outside the card

# A priority reads faster as a colour than as a word, and the three colours
# are the ones the dashboard already uses — somebody who knows what a red
# chip means on the screen knows what it means in the email.
PILL = {
    "high": ("#fee2e2", "#991b1b"),
    "medium": ("#e0f2fe", "#075985"),
    "low": ("#eef2f7", "#475569"),
}

_FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
         "'Helvetica Neue',Arial,sans-serif")


def _esc(value) -> str:
    """Everything from the database goes through here. No exceptions."""
    return html_escape("" if value is None else str(value), quote=True)


def _pill(text: str, bg: str, fg: str) -> str:
    return (f'<span style="display:inline-block;padding:2px 9px;'
            f'border-radius:11px;background:{bg};color:{fg};font-size:11px;'
            f'font-weight:700;letter-spacing:.03em;white-space:nowrap">'
            f'{_esc(text)}</span>')


def html_table(rows: list[tuple[Task, int]], site: str = "",
               with_doer: bool = True) -> str:
    """The task list as a striped HTML table."""
    th = (f'style="padding:9px 10px;text-align:left;font-size:11px;'
          f'font-weight:700;letter-spacing:.06em;text-transform:uppercase;'
          f'color:#ffffff;background:{BRAND};white-space:nowrap"')
    out = [
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'border="0" width="100%" style="width:100%;border-collapse:collapse;'
        f'font-family:{_FONT};font-size:13px;color:{INK}">',
        "<thead><tr>",
        f'<th {th}>Task&nbsp;ID</th>',
        (f'<th {th}>Doer</th>' if with_doer else ""),
        f'<th {th}>Planned date</th>',
        f'<th {th}>Priority</th>',
        f'<th {th} align="right">Overdue</th>',
        f'<th {th}>Task</th>',
        "</tr></thead><tbody>",
    ]

    blocks = by_company(rows)
    # One company needs no band: the whole email is about it, and a heading
    # over a single block reads as though a second one were coming.
    banded = len(blocks) > 1
    span = 6 if with_doer else 5
    i = 0
    for company, pairs in blocks:
        if banded:
            out.append(
                f'<tr><td colspan="{span}" style="padding:14px 10px 7px;'
                f'border-bottom:2px solid {BRAND};background:{PAPER};'
                f'font-size:12px;font-weight:800;letter-spacing:.06em;'
                f'text-transform:uppercase;color:{BRAND}">'
                f'{_esc(company)}'
                f'<span style="font-weight:600;color:{MUTED};'
                f'letter-spacing:0;text-transform:none"> &middot; '
                f'{len(pairs)} task{"s" if len(pairs) != 1 else ""}</span>'
                "</td></tr>")
            i = 0                       # stripes restart under each band
        for task, late in pairs:
            out += _html_row(task, late, i, with_doer, site, banded)
            i += 1

    out.append("</tbody></table>")
    return "".join(out)


def _html_row(task: Task, late: int, i: int, with_doer: bool, site: str,
              banded: bool) -> list:
    """One row of the HTML table.

    `banded` drops the company caption under the task: it is already the
    heading this row sits under, and printing it on every line is the noise
    the bands were added to remove.
    """
    out: list = []
    bg = STRIPE if i % 2 else PAPER
    # Built as the style VALUE, so a cell that needs an extra rule adds
    # to it rather than carrying a second style attribute — which is
    # invalid, and which Outlook resolves by ignoring one of them.
    cell = (f"padding:10px;border-bottom:1px solid {LINE};"
            f"background:{bg};vertical-align:top")
    td = f'style="{cell}"'
    mono = ('style="' + cell + ";font-family:ui-monospace,SFMono-Regular,"
            'Menlo,Consolas,monospace;white-space:nowrap"')
    ref = _esc(task.ref or task.id)
    step = task.step_label
    # The reference links to the task when the site's address is known.
    # It is the one link in the table, and it is the thing somebody
    # clicks: "open DEL-341" is the whole action the email is asking for.
    if site:
        ref = (f'<a href="{_esc(site.rstrip("/"))}/tasks/{task.id}" '
               f'style="color:{BRAND};font-weight:700;'
               f'text-decoration:none">{ref}</a>')
    else:
        ref = f'<strong style="color:{BRAND}">{ref}</strong>'

    bgc, fgc = PILL.get(task.priority.value, PILL["low"])
    days = f"{late} day" + ("s" if late != 1 else "")
    out += [
        "<tr>",
        f"<td {mono}>{ref}"
        + (f'<div style="color:{MUTED};font-size:11px;margin-top:2px;'
           f'font-family:{_FONT}">{_esc(step)}</div>' if step else "")
        + "</td>",
        (f'<td {td}>{_esc(task.doer.name if task.doer else "—")}</td>'
         if with_doer else ""),
        f'<td {td} nowrap>'
        f'{_esc(task.due_at.strftime("%d %b %Y"))}'
        f'<div style="color:{MUTED};font-size:11.5px;margin-top:2px">'
        f'{_esc(task.due_at.strftime("%I:%M %p"))}</div></td>',
        f'<td {td}>{_pill(task.priority.value.upper(), bgc, fgc)}</td>',
        f'<td {td} align="right" nowrap>'
        f'<span style="font-weight:700;color:'
        f'{URGENT if late >= 3 else INK}">{_esc(days)}</span></td>',
        f'<td {td}>{_esc(task.title)}'
        + (f'<div style="color:{MUTED};font-size:11.5px;margin-top:2px">'
           f'{_esc(task.branch.name)}</div>'
           if task.branch and not banded else "")
        + "</td>",
        "</tr>",
    ]
    return out


def html_message(rows: list[tuple[Task, int]], heading: str, greeting: str,
                 said: str, level: int, urgent: bool, site: str = "",
                 footnote: str = "", with_doer: bool = True) -> str:
    """One whole email as HTML — the bar, the sentence, the table, the note."""
    bar = URGENT if urgent else BRAND
    count = len(rows)
    worst = max((late for _t, late in rows), default=0)
    return "".join([
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(heading)}</title></head>",
        f'<body style="margin:0;padding:0;background:{SHELL}">',
        # An outer table rather than a div: Outlook needs one to centre
        # anything, and a background colour on <body> alone is ignored by
        # several clients.
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        f'border="0" width="100%" style="background:{SHELL};padding:20px 0">'
        '<tr><td align="center">',
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'border="0" width="860" style="width:860px;max-width:96%;'
        f'background:{PAPER};border:1px solid {LINE};border-radius:10px;'
        'overflow:hidden">',

        # The bar. Level and scale, so it is triaged before it is read.
        f'<tr><td style="background:{bar};padding:14px 18px;'
        f'font-family:{_FONT}">'
        f'<div style="color:#ffffff;font-size:15px;font-weight:700;'
        f'letter-spacing:.01em">{_esc(heading)}</div>'
        f'<div style="color:#ffffff;opacity:.85;font-size:12px;'
        f'margin-top:3px">{count} overdue '
        f'{"task" if count == 1 else "tasks"}'
        + (f" · up to {worst} day" + ("s" if worst != 1 else "")
           if worst else "")
        + f' · Level {level}</div></td></tr>',

        # The sentence.
        f'<tr><td style="padding:18px 18px 4px;font-family:{_FONT};'
        f'font-size:13.5px;line-height:1.6;color:{INK}">'
        f'<p style="margin:0 0 10px"><strong>{_esc(greeting)}</strong></p>'
        f'<p style="margin:0">{_esc(said)}</p></td></tr>',

        # The list.
        '<tr><td style="padding:14px 18px 6px">'
        + html_table(rows, site, with_doer) + "</td></tr>",

        # The note under it.
        f'<tr><td style="padding:10px 18px 18px;font-family:{_FONT};'
        f'font-size:12px;line-height:1.6;color:{MUTED}">'
        + (f'<p style="margin:0 0 8px">{_esc(footnote)}</p>'
           if footnote else "")
        + (f'<p style="margin:0 0 8px"><a href="{_esc(site.rstrip("/"))}'
           f'/tasks?scope=all&amp;status=pending" style="color:{BRAND}">'
           f'Open the full list in GCS Autopilot</a></p>' if site else "")
        + f'<p style="margin:10px 0 0;padding-top:10px;'
          f'border-top:1px solid {LINE}">Regards,<br>'
          f'<strong style="color:{INK}">GCS Task Escalation System</strong>'
          f'<br>This message was generated automatically. Closing the task '
          f'in GCS Autopilot stops it being escalated further.</p>'
          "</td></tr>",

        "</table></td></tr></table></body></html>",
    ])


def _subject(level: int, repeat_n: int, urgent: bool, count: int,
             worst: int, who: str) -> str:
    """The subject line. It has to say the three things somebody triages on.

    How bad (the level), how much (the count) and how long (the worst of
    them). The Task ID no longer fits, because one email now carries many —
    the IDs are the first column of the table, which is where they belong
    once there is more than one.
    """
    tag = "URGENT Escalation" if urgent else "Escalation"
    head = f"[{tag} L{level}"
    if repeat_n:
        head += f" · reminder {repeat_n}"
    head += "]"
    what = "task" if count == 1 else "tasks"
    return (f"{head} {count} overdue {what}, up to {worst} day(s) — {who}")


def compose(rows: list[tuple[Task, int]], level: int, repeat_n: int,
            rule: EscalationRule, rungs: dict[int, EscalationRule],
            site: str = "", below_sent: bool = False, who: str = "",
            for_doer: str = "") -> tuple[str, str, str, list[str]]:
    """One email to one person. Returns (subject, text, html, cc).

    `rows` is [(task, days overdue)]. `for_doer` is set when this is a
    doer's own copy of their own work rather than the list going up the
    ladder — see `_doer_copies`.

    Both bodies are built here from the same three sentences, so the HTML
    and the plain text can never drift into saying different things.
    """
    top = top_level(rungs)
    count = len(rows)
    worst = max((late for _t, late in rows), default=0)
    who = who or rule.name

    if for_doer:
        subject = (f"[Escalation L{level}] Your {count} overdue "
                   f"{'task' if count == 1 else 'tasks'} — "
                   f"reported to {rule.name}")
        # The whole name, not the first word of it. Half the names on this
        # system are roles rather than people — "Bodyzone Front Desk", "EA
        # Karan", "HO Accounts Sunil" — and a first-word greeting turns
        # those into "Dear Bodyzone," and "Dear EA,".
        opening = f"Dear {for_doer.strip()},"
        said = (f"The {'task' if count == 1 else 'tasks'} below "
                f"{'is' if count == 1 else 'are'} overdue and "
                f"{'has' if count == 1 else 'have'} been reported to "
                f"{rule.name} this morning. This copy is so you know what "
                f"was said about your work. Please close "
                f"{'it' if count == 1 else 'them'} or say where "
                f"{'it stands' if count == 1 else 'they stand'}.")
    else:
        subject = _subject(level, repeat_n, rule.urgent, count, worst, who)
        opening = rule.greeting + ","
        said = (rule.message or "").strip() or \
            _digest_message(level, top, below_sent, count)

    # The plain-text part, for whatever cannot show the table.
    parts = [opening, "", said, "", table(rows, with_doer=not for_doer)]
    if count == 1 and site:
        parts += ["", f"Open the task: {site.rstrip('/')}/tasks/{rows[0][0].id}"]
    elif site:
        parts += ["", f"Open the full list: {site.rstrip('/')}/tasks"
                      "?scope=all&status=pending"]
    parts += ["", "Regards,", "GCS Task Escalation System (auto-generated)",
              "Closing the task in GCS Autopilot stops it being escalated "
              "further."]

    # The heading on the coloured bar. Not the subject line: a subject is
    # read in a crowded list and needs the counts, while the bar is read
    # with the email already open and should name the thing instead.
    if for_doer:
        heading = f"Your overdue work — reported to {rule.name}"
    else:
        heading = f"Escalation to {who}"
        if repeat_n:
            heading += f" — reminder {repeat_n}"
    body_html = html_message(
        rows, heading, opening, said, level,
        rule.urgent and not for_doer, site,
        footnote=("This copy lists only your own work. The full list went "
                  f"to {rule.name}." if for_doer else ""),
        with_doer=not for_doer)

    cc: list[str] = []
    if not for_doer and rule.cc_earlier:
        # Everybody already escalated to below this rung, so the level above
        # replying reaches the ones who were asked first. These are ladder
        # people rather than doers, so copying them leaks nothing: they are
        # answerable for this work already.
        for lvl in sorted(l for l in rungs if l < level):
            cc.append(rungs[lvl].email)
    cc = [a for a in mail.clean(cc) if a.lower() != (rule.email or "").lower()]
    return subject, "\n".join(parts), body_html, cc


def _digest_message(level: int, top: int, below_sent: bool, count: int) -> str:
    """The sentence above the list, in the approved wording.

    `below_sent` is whether any lower rung was ACTUALLY escalated to. It is
    not always true of work that has reached rung 3, and the difference
    matters: tasks already sixteen days overdue the first morning this runs
    — which is every old task, that morning — reach the top rung at once,
    having never been escalated below it. The approved wording says "still
    open after escalation to the HOD and the EA", and sending that to the
    CMD about tasks neither of them was ever told about is a false
    statement in writing, over the group's name, to the one person least
    able to check it. So the sentence follows what the log says happened
    rather than what the level implies.

    The pronouns are spelled out rather than patched together per sentence.
    One email says "task", the next says "7 tasks", and a subject pronoun is
    not an object pronoun — getting that wrong produces "please see that
    them are closed", which is the sort of thing that makes a whole system
    look like it was not worth the trouble.
    """
    one = count == 1
    subj = "it" if one else "they"           # IT is overdue
    obj = "it" if one else "them"            # review IT
    be = "is" if one else "are"
    have = "has" if one else "have"
    was = "was" if one else "were"
    doers = "the doer" if one else "the doers"
    these = "The following task" if one else f"The following {count} tasks"

    if level <= 1:
        return (f"{these} {be} overdue and {have} not been closed. Please "
                f"review {obj} with {doers} and ensure {subj} {be} completed "
                f"today.")
    if not below_sent:
        return (f"{these} {be} overdue and {have} not been closed. "
                f"{subj.capitalize()} {have} reached this level by the "
                f"number of days overdue rather than through the levels "
                f"below. Please see that {subj} {be} closed.")
    if level < top:
        return (f"{these} {be} now overdue. {subj.capitalize()} {was} "
                f"escalated a level below yesterday and {be} still open. "
                f"Please follow up so that {subj} {be} closed at the "
                f"earliest.")
    return (f"{these} {be} still open after escalation at every level below. "
            f"{subj.capitalize()} {be} being brought to your notice for "
            f"direction.")


# ---------------------------------------------------------------- the run --

def plan(db: Session, org: Organization, today: date) -> tuple[list, list, int]:
    """Who is owed what this morning, worked out but not written down.

    Returns (groups, skipped, no_ladder). A group is one email waiting to
    happen: everything escalated to one person at one level today. This is
    the whole decision, and it is deliberately separate from writing and
    sending — the preview page runs exactly this and stops, which is the
    only honest way to show somebody what would be sent.
    """
    rows = overdue_tasks(db, org.id, today, org.esc_working_days,
                         org.esc_scope)
    groups: dict[tuple, dict] = {}
    skipped: list[tuple[Task, int, str]] = []
    no_ladder = 0

    for task, late in rows:
        rungs = ladder(db, org.id, task.branch_id)
        if not rungs:
            no_ladder += 1
            skipped.append((task, late, "No level is set for that company."))
            continue
        sent = already(db, task.id)
        step = next_step(sent, late, rungs, org.esc_after_top, today)
        if step is None:
            if any(e.day == today for e in sent):
                why = "Already escalated today."
            elif sent:
                why = (f"Level {max(e.level for e in sent)} is the top of "
                       "that ladder and has been sent.")
            else:
                why = "Not yet at the first level."
            skipped.append((task, late, why))
            continue

        level, repeat_n = step
        rule = rungs[level]
        # Grouped by WHO and WHICH RUNG, not by company. One person holding
        # the same rung at three companies gets one email, because they read
        # one inbox — and the company is on every row of the table anyway.
        key = ((rule.email or "").lower(), level, repeat_n)
        g = groups.setdefault(key, {
            "rule": rule, "rungs": rungs, "level": level,
            "repeat_n": repeat_n, "rows": [], "below_sent": False,
        })
        g["rows"].append((task, late))
        if any(e.level < level for e in sent):
            g["below_sent"] = True

    return list(groups.values()), skipped, no_ladder


def _doer_copies(group: dict) -> list[tuple[User, list]]:
    """Each doer's own rows, for their own copy. Sorted A to Z like the list.

    A digest sent up the ladder lists everybody's overdue work. Copying
    every doer on it would show each of them all the others' — a person's
    standing with the CMD is not something to hand round the office by
    accident, and nothing in the approved document asks for it. But the
    principle behind that tick is still right: nobody should be reported
    over their head without seeing it. So each doer gets a copy containing
    only their own rows.
    """
    by_doer: dict[int, list] = {}
    people: dict[int, User] = {}
    for task, late in group["rows"]:
        if task.doer is None or not (task.doer.email or "").strip():
            continue
        by_doer.setdefault(task.doer_id, []).append((task, late))
        people[task.doer_id] = task.doer
    return sorted(
        ((people[i], rows) for i, rows in by_doer.items()),
        key=lambda p: (p[0].name or "").strip().lower())


def _deliver(db: Session, m: EscalationMail, send: bool) -> None:
    """Try to send one written-down email and record what happened."""
    if not send:
        return
    ok, why = mail.send([m.to_email], m.subject, m.body, m.cc_list,
                        html=m.body_html or "")
    m.status = "sent" if ok else ("failed" if mail.enabled() else "queued")
    m.error = None if ok else (why or None)
    m.sent_at = clock.now() if ok else None
    db.commit()


def raise_group(db: Session, org: Organization, group: dict, today: date,
                send: bool = True, site: str = "") -> EscalationMail | None:
    """Write one morning's escalation to one person down, then send it.

    The rows first, committed, and only then the email. If the process dies
    between the two the worst case is a morning recorded as queued that
    nobody received — recoverable, and visible on the log. The other order
    risks sending the CMD the same email every morning because nothing was
    ever written down.

    Any task whose row loses the unique index — another web process got
    there first, or the button was pressed twice — drops out of the email
    rather than failing the lot. That is the constraint doing its job.
    """
    rule = group["rule"]
    m = EscalationMail(
        org_id=org.id, day=today, level=group["level"],
        repeat_n=group["repeat_n"], to_name=rule.name, to_email=rule.email,
    )
    db.add(m)
    db.flush()

    kept: list[tuple[Task, int]] = []
    for task, late in group["rows"]:
        esc = Escalation(
            org_id=org.id, task_id=task.id, level=group["level"],
            repeat_n=group["repeat_n"], day=today, days_overdue=late,
            to_name=rule.name, to_email=rule.email, mail_id=m.id,
        )
        db.add(esc)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            # The rollback threw away the mail row too, so start it again
            # with the tasks that are left. Rare enough to be worth the
            # second pass, and far better than a half-written morning.
            return _retry_without_clashes(db, org, group, today, send, site)
        kept.append((task, late))

    if not kept:
        db.rollback()
        return None

    m.task_count = len(kept)
    m.subject, m.body, m.body_html, cc = compose(
        kept, group["level"], group["repeat_n"], rule, group["rungs"],
        site, group["below_sent"])
    m.cc_emails = ",".join(cc) or None
    db.commit()

    _deliver(db, m, send)

    # And each doer their own copy of their own rows, if this rung says so.
    if rule.cc_doer:
        for person, rows in _doer_copies({**group, "rows": kept}):
            copy = EscalationMail(
                org_id=org.id, day=today, level=group["level"],
                repeat_n=group["repeat_n"], to_name=person.name,
                to_email=(person.email or "").strip(),
                task_count=len(rows), to_doer=True,
            )
            copy.subject, copy.body, copy.body_html, _cc = compose(
                rows, group["level"], group["repeat_n"], rule,
                group["rungs"], site, group["below_sent"],
                for_doer=person.name)
            db.add(copy)
            db.commit()
            _deliver(db, copy, send)

    return m


def _retry_without_clashes(db: Session, org: Organization, group: dict,
                           today: date, send: bool, site: str
                           ) -> EscalationMail | None:
    """Rebuild the group without the tasks already escalated at this rung."""
    level, repeat_n = group["level"], group["repeat_n"]
    ids = [t.id for t, _l in group["rows"]]
    taken = {e.task_id for e in db.scalars(
        select(Escalation).where(
            Escalation.task_id.in_(ids),
            Escalation.level == level,
            Escalation.repeat_n == repeat_n)).all()}
    left = [(t, l) for t, l in group["rows"] if t.id not in taken]
    if not left:
        return None
    return raise_group(db, org, {**group, "rows": left}, today, send, site)


def run(db: Session, org: Organization, on: date | None = None,
        send: bool = True, site: str = "", force: bool = False) -> dict:
    """The morning check. Safe to call again — see the module docstring."""
    today = on or clock.today()
    out = {"day": today, "looked": 0, "raised": 0, "emails": 0, "sent": 0,
           "failed": 0, "queued": 0, "skipped_no_ladder": 0, "ran": False,
           "why": ""}

    if not org.esc_on and not force:
        out["why"] = "Escalation is switched off."
        return out

    groups, skipped, no_ladder = plan(db, org, today)
    out["looked"] = len(groups) + len(skipped)
    out["skipped_no_ladder"] = no_ladder
    out["ran"] = True

    for group in groups:
        m = raise_group(db, org, group, today, send, site)
        if m is None:
            continue
        out["raised"] += m.task_count
        out["emails"] += 1
        out[{"sent": "sent", "failed": "failed"}.get(m.status, "queued")] += 1

    org.esc_last_run = today
    db.commit()
    return out


def due_today(db: Session, org: Organization, on: date | None = None) -> bool:
    """Has this morning's check still to happen?

    The date is the trigger rather than a timer: a restart, a second web
    process or a host that sleeps all night cannot make the check run twice
    in a day, and cannot make it skip one either.
    """
    today = on or clock.today()
    if not org.esc_on:
        return False
    if org.esc_last_run == today:
        return False
    return clock.now().hour >= max(0, min(23, org.esc_hour or 0))


def run_daily(db: Session, site: str = "") -> dict | None:
    """Called by the background loop. None when there was nothing to do.

    `site` is a fallback from the environment. The address recorded on the
    organisation wins, because it was learned from a real request to this
    site rather than typed into a setting that can go stale after a rename.
    """
    org = db.scalar(select(Organization))
    if org is None or not due_today(db, org):
        return None
    return run(db, org, site=(org.site_url or site or "").strip())


# ------------------------------------------------------------- the ladder --
# What a brand-new organisation starts with, so the page is not an empty
# form somebody has to guess the shape of. The names are left blank: who
# the HOD is differs per company and typing a wrong one in is worse than
# typing none.

DEFAULT_RUNGS = [
    (1, "The HOD of that company", False, None),
    (2, "The EA to the CMD", False, None),
    (3, "The CMD", True, "Respected CMD Sir"),
]


def seed(db: Session, org_id: int) -> int:
    """Lay out the three group-wide rungs, empty, if there are none at all.

    The wording is deliberately left blank rather than pre-filled with the
    approved sentences. Empty means the software writes the sentence itself,
    and its sentence is the approved one in the ordinary case and an honest
    variant in the one case the approved wording would be untrue — see
    `_digest_message`. Typing the approved text into the box would freeze
    the untrue version in, which is the opposite of what approving it meant.
    """
    if db.scalar(select(EscalationRule).where(EscalationRule.org_id == org_id)):
        return 0
    for level, _who, urgent, salutation in DEFAULT_RUNGS:
        db.add(EscalationRule(
            org_id=org_id, branch_id=None, level=level, to_name=None,
            salutation=salutation, message=None, urgent=urgent,
            cc_doer=True, cc_earlier=level > 1,
        ))
    db.commit()
    return len(DEFAULT_RUNGS)
