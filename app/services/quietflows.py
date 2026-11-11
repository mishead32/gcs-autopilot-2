"""Flows nobody is starting: the quiet-FMS report.

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
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import clock
from ..models import (Branch, EscalationMail, Flow, FlowInstance,
                      Organization)
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


@dataclass
class Gap:
    """One flow, at one company, and how long since anybody started it."""

    flow: Flow
    branch: Branch | None
    last_at: datetime | None
    days: int

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


def last_entries(db: Session, org_id: int) -> dict[tuple[int, int | None],
                                                   datetime]:
    """{(flow id, company id): when it was last started} — in one query.

    One query rather than one per pair: twenty flows across four companies
    is eighty lookups, and eighty round trips to write one email is how a
    nightly job becomes something that times out on a free host.
    """
    rows = db.execute(
        select(FlowInstance.flow_id, FlowInstance.branch_id,
               func.max(FlowInstance.started_at))
        .where(FlowInstance.org_id == org_id)
        .group_by(FlowInstance.flow_id, FlowInstance.branch_id)).all()
    return {(f, b): when for f, b, when in rows if when is not None}


def gaps(db: Session, org: Organization, today: date | None = None,
         threshold: int | None = None) -> list[Gap]:
    """Every pair quiet for longer than the threshold, worst first.

    Sorted by flow name and then by the gap, because the email is read
    flow by flow: somebody looking at "Bill to payment" wants its companies
    together, with the worst of them at the top of that block.
    """
    today = today or clock.today()
    limit = org.quiet_gap if threshold is None else threshold
    seen = last_entries(db, org.id)
    # The calendar once, in memory. Counting working days per flow per
    # company used to re-read the holiday table for every pair — twenty
    # flows across five companies is a hundred round trips to answer one
    # question about a calendar that does not change while the page is
    # being built.
    cal = holidays.calendar(db, org.id)

    out: list[Gap] = []
    for flow, branch in covered(db, org.id):
        bid = branch.id if branch else None
        when = seen.get((flow.id, bid))
        # Never started here: counted from the day the flow was made. The
        # alternative — leaving it out — hides the flow written in March
        # that nobody has used since, which is the thing most worth seeing.
        from_day = (when or flow.created_at).date()
        days = cal.open_days(from_day, today, bid, holidays.MAX_GAP)
        if days >= limit:
            out.append(Gap(flow=flow, branch=branch, last_at=when, days=days))
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


# ------------------------------------------------------------- the email --
# Same discipline as the escalation mail: inline styles, table layout,
# nothing fetched from the network. A mail client is not a browser, and the
# half of them that strip <style> blocks would otherwise show a heap of
# unformatted text to the one person who most needs to read it.

def text_report(rows: list[Gap], today: date, limit: int) -> str:
    """The plain-text version. Says everything the HTML one does."""
    if not rows:
        return (f"Every FMS has had an entry within the last {limit} "
                f"working day(s), as at {today.strftime('%d/%m/%Y')}.\n")
    width = max(len(g.flow.name or "") for g in rows)
    width = min(max(width, 20), 40)
    wide = max((len(g.company) for g in rows), default=18)
    wide = min(max(wide, 18), 30)
    lines = [
        f"FMS with no new entry for {limit} working day(s) or more",
        f"As at {today.strftime('%d/%m/%Y')}", "",
        f"{'FMS name'.ljust(width)}  {'Company'.ljust(wide)}  "
        f"{'Days'.rjust(5)}  Last entry",
        f"{'-' * width}  {'-' * wide}  {'-' * 5}  {'-' * 18}",
    ]
    for g in rows:
        lines.append(
            f"{(g.flow.name or '')[:width].ljust(width)}  "
            f"{g.company[:wide].ljust(wide)}  "
            f"{g.days_label.rjust(5)}  {g.since}")
    lines += ["", "Working days only — each company's own Sundays and "
                  "holidays are not counted.",
              "A flow marked 'never started here' is counted from the day "
              "it was created.", "",
              "Regards,", "GCS Autopilot"]
    return "\n".join(lines) + "\n"


def _row_html(g: Gap, i: int) -> str:
    stripe = "#f4f7fa" if i % 2 else PAPER
    # Red once a gap is past a working week: by then it is not a slow week,
    # it is a process that has stopped.
    tone = "#b91c1c" if g.days >= 5 else INK
    return (
        f'<tr style="background:{stripe}">'
        f'<td style="padding:8px 10px;border-bottom:1px solid {LINE};'
        f'font-size:13px;color:{INK}">{_esc(g.flow.name)}</td>'
        f'<td style="padding:8px 10px;border-bottom:1px solid {LINE};'
        f'font-size:13px;color:{INK}">{_esc(g.company)}</td>'
        f'<td style="padding:8px 10px;border-bottom:1px solid {LINE};'
        f'font-size:13px;font-weight:700;color:{tone};white-space:nowrap">'
        f'{_esc(g.days_label)}</td>'
        f'<td style="padding:8px 10px;border-bottom:1px solid {LINE};'
        f'font-size:12px;color:{MUTED};white-space:nowrap">'
        f'{_esc(g.since)}</td></tr>')


def html_report(rows: list[Gap], today: date, limit: int,
                site: str = "") -> str:
    """The whole email as HTML."""
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
            for c in ("FMS name", "Company",
                      "Days with no entry", "Last entry"))
        + "</tr>",
    ]
    for i, g in enumerate(rows):
        table.append(_row_html(g, i))
    table.append("</table>")

    worst = max((g.days for g in rows), default=0)
    companies = len({g.company for g in rows})
    return "".join([
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>FMS with no new entries</title></head>",
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
        "FMS with no new entries</div>"
        '<div style="color:#ffffff;opacity:.85;font-size:12px;margin-top:3px">'
        f"{len(rows)} flow/company line(s)"
        + (f" across {companies} companies" if companies > 1 else "")
        + (f" · worst {worst} working days" if worst else "")
        + f" · {today.strftime('%d/%m/%Y')}</div></td></tr>",

        f'<tr><td style="padding:18px 18px 6px;font-family:{_FONT};'
        f'font-size:13.5px;line-height:1.6;color:{INK}">'
        f'<p style="margin:0">Nothing has been entered on the start form of '
        f'these flows for <strong>{limit} working day(s)</strong> or more, '
        "at the companies shown. The count is per flow and per company: an "
        "entry at one company does not clear another.</p></td></tr>",

        '<tr><td style="padding:12px 14px 6px">' + "".join(table)
        + "</td></tr>",

        f'<tr><td style="padding:10px 18px 18px;font-family:{_FONT};'
        f'font-size:12px;line-height:1.6;color:{MUTED}">'
        '<p style="margin:0 0 8px">Working days only — each company\'s own '
        "Sundays and holidays are not counted. A line reading "
        "<em>never started here</em> is counted from the day the flow was "
        "created.</p>"
        + (f'<p style="margin:0 0 8px"><a href="{_esc(site.rstrip("/"))}'
           f'/flows" style="color:{BRAND}">Open FMS in GCS Autopilot</a></p>'
           if site else "")
        + f'<p style="margin:10px 0 0;padding-top:10px;'
          f'border-top:1px solid {LINE}">Regards,<br>'
          f'<strong style="color:{INK}">GCS Autopilot</strong><br>'
          "This message was generated automatically. Starting a run of a "
          "flow at that company clears its line.</p></td></tr>",

        "</table></td></tr></table></body></html>",
    ])


def subject(rows: list[Gap], today: date) -> str:
    worst = max((g.days for g in rows), default=0)
    if not rows:
        return (f"[FMS] Every flow has had an entry — "
                f"{today.strftime('%d/%m/%Y')}")
    return (f"[FMS] {len(rows)} flow/company line(s) with no entry, "
            f"up to {worst} working day(s) — {today.strftime('%d/%m/%Y')}")


def compose(db: Session, org: Organization, today: date | None = None,
            site: str = "", threshold: int | None = None
            ) -> tuple[list[Gap], str, str, str]:
    """(rows, subject, text, html) — everything needed to send or preview."""
    today = today or clock.today()
    limit = org.quiet_gap if threshold is None else threshold
    rows = gaps(db, org, today, limit)
    base = (org.site_url or site or "").strip()
    return (rows, subject(rows, today), text_report(rows, today, limit),
            html_report(rows, today, limit, base))


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
                mark: bool = True) -> dict:
    """Write this morning's report down, then try to send it.

    Written down first and sent second, like every other email here: a
    record of what was reported survives a mail server that was down, and
    the log's Send-again button can then do its job. `mark` is False for a
    test, which must not persuade the scheduler that today is done.
    """
    today = today or clock.today()
    rows, subj, body, body_html = compose(db, org, today, site, threshold)
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
