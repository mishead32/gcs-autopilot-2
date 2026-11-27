"""FMS working status: which flows are not working.

(The module keeps its old name, quietflows, because the saved settings and
the escalation mail log refer to it. The report is called "FMS working
status" everywhere a person can see it.)

A flow is NOT WORKING when two things are both true: nobody has made a
first entry (started a run) for the set number of working days, AND at
least one of its steps is overdue and has not been marked done. Each line
shows both numbers. The page can also be asked for flows that meet either
condition on its own — see SHOWS.

The text below is the original description of the "no entry" half.

Flows nobody is starting: the quiet-FMS report.

Every other alarm in this system fires when somebody is LATE with work
they were given. This one fires when no work was given at all — which is
the failure that never raises its hand. A bill-to-payment flow that nobody
at Spa Kora has started for four days does not show up as an overdue task,
because no task was ever created; the process has simply stopped being
followed, and it stops quietly.

What the report says, one line per flow and company:

    FMS name           Company            Nothing started for
    Bill to payment    Bodyzone           3 working days
    Bill to payment    Spa Kora           2 working days
    Bill to payment    GCS Head Office    4 working days

A flow built for one company is one line. A group-wide flow is one line
per company, because "somebody started it at Bodyzone" says nothing about
whether the spa is following the same process — and rolling them into one
line is how a company that has stopped altogether hides behind one that
has not.

Three decisions worth knowing before reading a number:

  * **Working days, for that company.** Bodyzone and the spa work Sundays;
    the school does not. Counting calendar days would put every school flow
    two days worse each Monday, and the whole report would be noise by the
    second week.

  * **The count is per flow AND company.** An entry at Bodyzone resets
    Bodyzone's line and leaves the spa's alone. That is the point of the
    split.

  * **A flow nobody has ever used is counted from the day it was made**
    and says so. Usually that is the most useful line on the page: a flow
    written, agreed, and then never adopted anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import clock
from ..models import (Branch, EscalationMail, Flow, FlowInstance,
                      Organization, PARKED_STATES, Task, TaskStatus,
                      overdue_clause)
from . import holidays, mail
from .escalation import BRAND, INK, LINE, MUTED, PAPER, SHELL, _esc, _FONT

#: The level this report's record is filed under in the escalation mail
#: log. Negative on purpose: the ladder's own levels count up from 1, a
#: doer's morning list is 0, and this is not on the ladder at all — it is
#: not about a task, and no rung is ever spent by it.
QUIET_LEVEL = -1

#: Shown instead of a number once the gap is past counting. See
#: holidays.MAX_GAP.
LONG = f"{holidays.MAX_GAP}+"


#: What the report lists. "both" is the definition of NOT WORKING: no first
#: entry for the set days AND a step overdue and not marked. The other three
#: let a person look at each half alone, or at either.
SHOWS = {
    "both": "Not working: no entry AND steps overdue",
    "either": "Either: no entry OR steps overdue",
    "entry": "Only: no first entry for the days",
    "steps": "Only: steps overdue and not marked",
}
DEFAULT_SHOW = "both"


def clean_show(raw: str | None) -> str:
    return raw if raw in SHOWS else DEFAULT_SHOW


@dataclass
class Gap:
    """One flow, at one company: how long since anybody started it, and how
    many of its steps are overdue and still not marked."""

    flow: Flow
    branch: Branch | None
    last_at: datetime | None
    days: int
    overdue: int = 0

    @property
    def ever(self) -> bool:
        return self.last_at is not None

    @property
    def company(self) -> str:
        return self.branch.name if self.branch else "Group-wide"

    @property
    def days_label(self) -> str:
        if self.days >= holidays.MAX_GAP:
            return LONG
        return str(self.days)

    @property
    def since(self) -> str:
        """What the last-entry column says. Never a blank."""
        if self.last_at:
            return self.last_at.strftime("%d/%m/%Y")
        return "never started here"


def covered(db: Session, org_id: int) -> list[tuple[Flow, Branch | None]]:
    """Every flow-and-company pair this report has an opinion about.

    A flow with no steps is left out: it cannot be started, so saying
    nobody has started it is true and useless. A switched-off flow is left
    out for the same reason — it was retired on purpose, and a report that
    nags about retired work teaches people to ignore it.
    """
    # The steps come with the flows. Asking `flow.steps` inside the loop
    # below is one query per flow, and this runs on a page and in the
    # morning email.
    flows = db.scalars(
        select(Flow).where(Flow.org_id == org_id, Flow.active.is_(True))
        .options(selectinload(Flow.steps))
        .order_by(Flow.name)).all()
    branches = db.scalars(
        select(Branch).where(Branch.org_id == org_id)
        .order_by(Branch.name)).all()
    out: list[tuple[Flow, Branch | None]] = []
    for flow in flows:
        if not flow.steps:
            continue
        if flow.branch_id:
            here = [b for b in branches if b.id == flow.branch_id]
            out.extend((flow, b) for b in here)
        elif branches:
            out.extend((flow, b) for b in branches)
        else:
            # A system with no companies set up at all. One line, so the
            # flow is still visible rather than silently uncovered.
            out.append((flow, None))
    return out


def last_entries(db: Session, org_id: int, upto: date | None = None
                 ) -> dict[tuple[int, int | None], datetime]:
    """{(flow id, company id): when it was last started} — in one query.

    One query rather than one per pair: twenty flows across four companies
    is eighty lookups, and eighty round trips to write one email is how a
    nightly job becomes something that times out on a free host.

    `upto` makes the answer historical: runs started AFTER that day are
    ignored, so asking about last Tuesday gives the answer the report
    would have given on last Tuesday — not today's answer with an old
    date written at the top. A report nobody can reproduce is not a
    report, and this one is read in a meeting a week after the fact.
    """
    q = (select(FlowInstance.flow_id, FlowInstance.branch_id,
                func.max(FlowInstance.started_at))
         .where(FlowInstance.org_id == org_id))
    if upto is not None:
        q = q.where(FlowInstance.started_at
                    <= datetime.combine(upto, datetime.max.time()))
    rows = db.execute(
        q.group_by(FlowInstance.flow_id, FlowInstance.branch_id)).all()
    return {(f, b): when for f, b, when in rows if when is not None}


def overdue_steps(db: Session, org_id: int, upto: date | None = None
                  ) -> dict[tuple[int, int | None], int]:
    """{(flow id, company id): FMS steps overdue and not marked yet}.

    "Overdue and not marked" is the same test every other overdue number in
    the software uses (models.overdue_clause): still with the doer, past its
    date, not handed in, and not a step whose turn has not come. A paused or
    cancelled step is nobody's problem and is not counted.

    For a past day it is rebuilt from the dates on the tasks: made by then,
    due before the end of that day, not handed in or closed by then. A step
    that was waiting then and has opened since is judged by the date it now
    carries, so an old date is close to, not exactly, what was true.
    """
    q = (select(FlowInstance.flow_id, FlowInstance.branch_id,
                func.count(Task.id))
         .join(FlowInstance, FlowInstance.id == Task.flow_instance_id)
         .where(Task.org_id == org_id))
    if upto is None or upto >= clock.today():
        q = q.where(overdue_clause())
    else:
        end = datetime.combine(upto, time.max)
        q = q.where(Task.status.notin_(PARKED_STATES),
                    Task.flow_waiting.is_(False),
                    Task.created_at <= end, Task.due_at < end,
                    (Task.submitted_at.is_(None)) | (Task.submitted_at > end),
                    (Task.closed_at.is_(None)) | (Task.closed_at > end))
    rows = db.execute(q.group_by(FlowInstance.flow_id,
                                 FlowInstance.branch_id)).all()
    return {(f, b): n for f, b, n in rows}


def gaps(db: Session, org: Organization, today: date | None = None,
         threshold: int | None = None, show: str = DEFAULT_SHOW) -> list[Gap]:
    """Every flow-and-company line that meets `show`, worst first.

    Sorted by flow name and then by the gap, because the email is read
    flow by flow: somebody looking at "Bill to payment" wants its companies
    together, with the worst of them at the top of that block.
    """
    today = today or clock.today()
    limit = org.quiet_gap if threshold is None else threshold
    # Bounded by the day being asked about, so looking back at a Tuesday
    # shows what Tuesday looked like.
    seen = last_entries(db, org.id, today)
    late = overdue_steps(db, org.id, today)
    show = clean_show(show)
    # The calendar once, in memory. Counting working days per flow per
    # company used to re-read the holiday table for every pair — twenty
    # flows across five companies is a hundred round trips to answer one
    # question about a calendar that does not change while the page is
    # being built.
    cal = holidays.calendar(db, org.id)

    out: list[Gap] = []
    for flow, branch in covered(db, org.id):
        # A flow that did not exist yet on the day being asked about
        # cannot have been going quiet on it.
        if flow.created_at.date() > today:
            continue
        bid = branch.id if branch else None
        when = seen.get((flow.id, bid))
        # Never started here: counted from the day the flow was made. The
        # alternative — leaving it out — hides the flow written in March
        # that nobody has used since, which is the thing most worth seeing.
        from_day = (when or flow.created_at).date()
        days = cal.open_days(from_day, today, bid, holidays.MAX_GAP)
        od = late.get((flow.id, bid), 0)
        quiet = days >= limit
        keep = {"both": quiet and od > 0, "either": quiet or od > 0,
                "entry": quiet, "steps": od > 0}[show]
        if keep:
            out.append(Gap(flow=flow, branch=branch, last_at=when, days=days,
                           overdue=od))
    return sorted(out, key=lambda g: ((g.flow.name or "").lower(),
                                      -g.days, g.company.lower()))


def by_flow(rows: list[Gap]) -> list[tuple[str, list[Gap]]]:
    """The gaps grouped under their flow's name, in the order they came."""
    out: list[tuple[str, list[Gap]]] = []
    for g in rows:
        if out and out[-1][0] == g.flow.name:
            out[-1][1].append(g)
        else:
            out.append((g.flow.name, [g]))
    return out


def by_company(rows: list[Gap]) -> list[tuple[str, list[Gap]]]:
    """The same gaps, grouped under the company instead of the flow.

    The other way round matters because the two questions are different.
    "Which process has stopped" is answered flow by flow; "which office
    has stopped working" is answered company by company, and reading it
    out of seventeen one-line tables is not reading it at all. The worst
    company comes first, and inside it the longest silence comes first,
    so the top of the page is where to start.
    """
    buckets: dict[str, list[Gap]] = {}
    for g in rows:
        buckets.setdefault(g.company, []).append(g)
    for lines in buckets.values():
        lines.sort(key=lambda g: (-g.days, (g.flow.name or "").lower()))
    return sorted(buckets.items(),
                  key=lambda kv: (-max(g.days for g in kv[1]),
                                  -len(kv[1]), kv[0].lower()))


# ------------------------------------------------------------- the email --
# Same discipline as the escalation mail: inline styles, table layout,
# nothing fetched from the network. A mail client is not a browser, and the
# half of them that strip <style> blocks would otherwise show a heap of
# unformatted text to the one person who most needs to read it.

def _name(g: Gap) -> str:
    """FMS name, with the company after it when the flow runs in several."""
    return f"{g.flow.name or ''} ({g.company})"


def _explain(limit: int, show: str) -> str:
    if show == "both":
        return (f"Not working = no first entry for {limit} working day(s) or "
                "more AND at least one step overdue and not marked yet.")
    if show == "either":
        return (f"Listed when there is no first entry for {limit} working "
                "day(s) or more, OR a step is overdue and not marked yet.")
    if show == "entry":
        return f"Listed when there is no first entry for {limit} working day(s) or more."
    return "Listed when a step is overdue and not marked yet."


def text_report(rows: list[Gap], today: date, limit: int,
                show: str = DEFAULT_SHOW) -> str:
    """The plain-text version. Says everything the HTML one does."""
    show = clean_show(show)
    if not rows:
        return (f"No FMS is in the 'not working' list as at "
                f"{today.strftime('%d/%m/%Y')}.\n{_explain(limit, show)}\n")
    width = min(max(max(len(_name(g)) for g in rows), 20), 52)
    lines = [
        "FMS working status", f"As at {today.strftime('%d/%m/%Y')}",
        _explain(limit, show), "",
        f"{'Sno'.rjust(3)}  {'FMS name'.ljust(width)}  "
        f"{'Days no first entry'.rjust(19)}  {'Steps overdue, not marked'.rjust(25)}",
        f"{'-' * 3}  {'-' * width}  {'-' * 19}  {'-' * 25}",
    ]
    for i, g in enumerate(rows, 1):
        lines.append(f"{str(i).rjust(3)}  {_name(g)[:width].ljust(width)}  "
                     f"{g.days_label.rjust(19)}  {str(g.overdue).rjust(25)}")
    lines += ["", "Working days only — each company's own Sundays and "
                  "holidays are not counted.",
              "A flow never started at that company is counted from the day "
              "it was created.", "",
              "Regards,", "GCS Autopilot"]
    return "\n".join(lines) + "\n"


def _row_html(g: Gap, i: int) -> str:
    stripe = "#f4f7fa" if i % 2 else PAPER
    tone = "#b91c1c" if g.days >= 5 else INK
    od_tone = "#b91c1c" if g.overdue else INK
    cell = (f'padding:8px 10px;border-bottom:1px solid {LINE};font-size:13px;')
    return (
        f'<tr style="background:{stripe}">'
        f'<td style="{cell}color:{MUTED}">{i + 1}</td>'
        f'<td style="{cell}color:{INK}">{_esc(g.flow.name)}'
        f' <span style="color:{MUTED};font-size:12px">· {_esc(g.company)}</span></td>'
        f'<td style="{cell}font-weight:700;color:{tone};white-space:nowrap">'
        f'{_esc(g.days_label)}</td>'
        f'<td style="{cell}font-weight:700;color:{od_tone};white-space:nowrap">'
        f'{g.overdue}</td></tr>')


def html_report(rows: list[Gap], today: date, limit: int,
                site: str = "", show: str = DEFAULT_SHOW) -> str:
    """The whole email as HTML."""
    show = clean_show(show)
    table = [
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        f'border="0" width="100%" style="border-collapse:collapse;'
        f'font-family:{_FONT}">',
        "<tr>"
        + "".join(
            f'<th align="left" style="padding:8px 10px;'
            f'border-bottom:2px solid {LINE};font-size:11px;'
            f'text-transform:uppercase;letter-spacing:.04em;color:{MUTED}">'
            f"{c}</th>"
            for c in ("Sno", "FMS name", "No. of days first entry not made",
                      "No. of steps overdue but not marked"))
        + "</tr>",
    ]
    for i, g in enumerate(rows):
        table.append(_row_html(g, i))
    table.append("</table>")

    worst = max((g.days for g in rows), default=0)
    companies = len({g.company for g in rows})
    steps = sum(g.overdue for g in rows)
    return "".join([
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>FMS working status</title></head>",
        f'<body style="margin:0;padding:0;background:{SHELL}">',
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        f'border="0" width="100%" style="background:{SHELL};padding:20px 0">'
        '<tr><td align="center">',
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'border="0" width="760" style="width:760px;max-width:96%;'
        f'background:{PAPER};border:1px solid {LINE};border-radius:10px;'
        'overflow:hidden">',

        f'<tr><td style="background:{BRAND};padding:14px 18px;'
        f'font-family:{_FONT}">'
        '<div style="color:#ffffff;font-size:15px;font-weight:700">'
        "FMS working status</div>"
        '<div style="color:#ffffff;opacity:.85;font-size:12px;margin-top:3px">'
        f"{len(rows)} FMS line(s)"
        + (f" across {companies} companies" if companies > 1 else "")
        + (f" · {steps} step(s) overdue and not marked" if steps else "")
        + (f" · worst {worst} working days" if worst else "")
        + f" · {today.strftime('%d/%m/%Y')}</div></td></tr>",

        f'<tr><td style="padding:18px 18px 6px;font-family:{_FONT};'
        f'font-size:13.5px;line-height:1.6;color:{INK}">'
        f'<p style="margin:0">{_esc(_explain(limit, show))} The count is per '
        "flow and per company: an entry at one company does not clear "
        "another.</p></td></tr>",

        '<tr><td style="padding:12px 14px 6px">' + "".join(table)
        + "</td></tr>",

        f'<tr><td style="padding:10px 18px 18px;font-family:{_FONT};'
        f'font-size:12px;line-height:1.6;color:{MUTED}">'
        '<p style="margin:0 0 8px">Working days only — each company\'s own '
        "Sundays and holidays are not counted. A flow never started at that "
        "company is counted from the day the flow was created.</p>"
        + (f'<p style="margin:0 0 8px"><a href="{_esc(site.rstrip("/"))}'
           f'/flows" style="color:{BRAND}">Open FMS in GCS Autopilot</a></p>'
           if site else "")
        + f'<p style="margin:10px 0 0;padding-top:10px;'
          f'border-top:1px solid {LINE}">Regards,<br>'
          f'<strong style="color:{INK}">GCS Autopilot</strong><br>'
          "This message was generated automatically. Marking the overdue "
          "steps and starting a run of the flow clears its line.</p></td></tr>",

        "</table></td></tr></table></body></html>",
    ])


def subject(rows: list[Gap], today: date) -> str:
    worst = max((g.days for g in rows), default=0)
    if not rows:
        return (f"[FMS] Working status — nothing to report — "
                f"{today.strftime('%d/%m/%Y')}")
    steps = sum(g.overdue for g in rows)
    return (f"[FMS] Working status: {len(rows)} FMS line(s), "
            f"{steps} step(s) overdue, up to {worst} working day(s) with no "
            f"entry — {today.strftime('%d/%m/%Y')}")


def compose(db: Session, org: Organization, today: date | None = None,
            site: str = "", threshold: int | None = None,
            show: str = DEFAULT_SHOW
            ) -> tuple[list[Gap], str, str, str]:
    """(rows, subject, text, html) — everything needed to send or preview."""
    today = today or clock.today()
    limit = org.quiet_gap if threshold is None else threshold
    rows = gaps(db, org, today, limit, show)
    base = (org.site_url or site or "").strip()
    return (rows, subject(rows, today), text_report(rows, today, limit, show),
            html_report(rows, today, limit, base, show))


# ------------------------------------------------------------- sending ----

def recipients(org: Organization) -> tuple[list[str], list[str]]:
    """Who it goes to and who is copied, cleaned and de-duplicated."""
    to = mail.clean((org.quiet_to or "").replace(";", ",").split(","))
    cc = mail.clean((org.quiet_cc or "").replace(";", ",").split(","))
    cc = [a for a in cc if a.lower() not in {t.lower() for t in to}]
    return to, cc


def send_report(db: Session, org: Organization, today: date | None = None,
                send: bool = True, site: str = "",
                to: list[str] | None = None, cc: list[str] | None = None,
                threshold: int | None = None,
                mark: bool = True, show: str = DEFAULT_SHOW) -> dict:
    """Write this morning's report down, then try to send it.

    Written down first and sent second, like every other email here: a
    record of what was reported survives a mail server that was down, and
    the log's Send-again button can then do its job. `mark` is False for a
    test, which must not persuade the scheduler that today is done.
    """
    today = today or clock.today()
    rows, subj, body, body_html = compose(db, org, today, site, threshold, show)
    send_to, send_cc = recipients(org)
    if to is not None:
        send_to = mail.clean(to)
    if cc is not None:
        send_cc = mail.clean(cc)
    send_cc = [a for a in send_cc if a.lower() not in
               {t.lower() for t in send_to}]

    out = {"lines": len(rows), "sent": 0, "failed": 0, "to": send_to,
           "cc": send_cc, "why": ""}
    if not send_to:
        out["why"] = ("Nobody is listed to receive it. Add at least one "
                      "address on the escalation page.")
        return out

    m = EscalationMail(
        org_id=org.id, day=today, level=QUIET_LEVEL, repeat_n=0,
        to_name="FMS entry report", to_email=send_to[0],
        cc_emails=",".join(send_to[1:] + send_cc) or None,
        subject=subj, body=body, body_html=body_html,
        task_count=len(rows))
    db.add(m)
    if mark:
        org.quiet_last_run = today
    db.commit()

    if not send:
        return out
    ok, why = mail.send(send_to, subj, body, send_cc, html=body_html)
    m.status = "sent" if ok else ("failed" if mail.enabled() else "queued")
    m.error = None if ok else (why or None)
    m.sent_at = clock.now() if ok else None
    db.commit()
    out["sent" if ok else "failed"] = 1
    out["why"] = "" if ok else (why or "")
    return out


def due_today(org: Organization, on: date | None = None) -> bool:
    """Is this morning's report still owed?

    The date it last ran is the trigger, not a timer — so a restart, a
    second web process, or a host that slept through eleven o'clock cannot
    make it send twice, and cannot make it skip a day either.

    It borrows the escalation schedule's days rather than keeping a second
    list of its own: two sets of tickboxes that both mean "not on Sunday"
    is one of them being wrong eventually.
    """
    today = on or clock.today()
    if not org.quiet_on:
        return False
    if org.quiet_last_run == today:
        return False
    if today.weekday() not in org.esc_day_list:
        return False
    return clock.now().hour >= max(0, min(23, org.quiet_hour or 0))


def run_daily(db: Session, site: str = "") -> dict | None:
    """Called by the background loop. None when nothing was owed."""
    org = db.scalar(select(Organization))
    if org is None or not due_today(org):
        return None
    if not mail.enabled():
        # Unlike the ladder, nothing here is spent by a failed send — but
        # marking the day done with no mail server would still lose a
        # morning silently, so the day is left owed.
        return None
    return send_report(db, org, site=(org.site_url or site or "").strip())
