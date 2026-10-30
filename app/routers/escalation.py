"""The escalation pages — set the ladder, read the trail.

Three screens, and the split between them is deliberate:

    /escalation          the ladders and the group-wide switches
    /escalation/log      every escalation ever raised, filterable
    /escalation/preview  what this morning WOULD send, sending nothing

The preview is the one that earns its place. The first question anybody
sensible asks before switching this on is "who is about to be emailed, and
about what" — and the only honest way to answer it is to run the whole
check with the sending switched off and show the result. Phase 5 of the
approved roadmap is exactly that, and doing it with real test emails means
the CMD's inbox is where the testing happens.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock, flash
from ..db import get_db
from ..deps import require_right
from ..models import (
    Branch, Escalation, EscalationMail, EscalationRule, Organization, Right,
    Task, User,
)
from ..services import escalation as esc_svc
from ..services import mail
from ..services import whatsapp
from ..templating import templates

router = APIRouter(prefix="/escalation")
manage = require_right(Right.MANAGE_USER)

AFTER_TOP = {
    "stop": "Stop — the CMD has been told once, and that is the record",
    "repeat": "Remind the top level again every day until it is closed",
}

SCOPES = {
    "chased": "The work the follow-up desk chases (recommended)",
    "high": "High priority only",
    "all": "Everything open and past its date",
}


def _org(db: Session, user: User) -> Organization:
    org = db.get(Organization, user.org_id)
    if org is None:
        raise HTTPException(404, "No organisation on this system yet.")
    return org


def _num(raw: str, what: str, lo: int, hi: int) -> int:
    try:
        n = int((raw or "").strip())
    except ValueError:
        raise HTTPException(400, f"'{raw}' is not a whole number for {what}.")
    if not lo <= n <= hi:
        raise HTTPException(400, f"{what} has to be between {lo} and {hi}.")
    return n


def _scope(db: Session, raw: str, user: User) -> int | None:
    """"" means the group-wide ladder; otherwise a company id."""
    if not (raw or "").strip():
        return None
    bid = _num(raw, "the company", 1, 10 ** 9)
    b = db.get(Branch, bid)
    if b is None or b.org_id != user.org_id:
        raise HTTPException(400, "Unknown company.")
    return bid


# -------------------------------------------------------------- the page ---

@router.get("", response_class=HTMLResponse)
def page(request: Request, scope: str = "",
         user: User = Depends(manage), db: Session = Depends(get_db)):
    org = _org(db, user)
    # Three empty rungs the first time somebody opens the page, so the form
    # arrives with the approved shape already in it rather than blank.
    esc_svc.seed(db, org.id)

    # And learn this site's own address from the request that got here, for
    # the links inside the nine o'clock emails — which are not a request and
    # cannot work it out for themselves. Learned once and then left alone,
    # so a deliberate change on this page is not overwritten by the next
    # person who opens it at a different address.
    if not (org.site_url or "").strip():
        org.site_url = _site(request)
        db.commit()

    branch_id = _scope(db, scope, user)
    branches = db.scalars(
        select(Branch).where(Branch.org_id == org.id).order_by(Branch.name)
    ).all()
    rows = db.scalars(
        select(EscalationRule).where(
            EscalationRule.org_id == org.id,
            EscalationRule.branch_id.is_(None) if branch_id is None
            else EscalationRule.branch_id == branch_id,
        ).order_by(EscalationRule.level)
    ).all()
    staff = db.scalars(
        select(User).where(User.org_id == org.id, User.active.is_(True))
        .order_by(User.name)
    ).all()
    recent = db.scalars(
        select(EscalationMail).where(EscalationMail.org_id == org.id)
        .order_by(EscalationMail.created_at.desc()).limit(8)
    ).all()

    return templates.TemplateResponse(request, "escalation.html", {
        "user": user, "org": org, "branches": branches, "rows": rows,
        "branch_id": branch_id,
        "branch": db.get(Branch, branch_id) if branch_id else None,
        "staff": staff, "recent": recent,
        "effective": esc_svc.ladder(db, org.id, branch_id),
        "gaps": esc_svc.missing_ladders(db, org.id),
        "mail_on": mail.enabled(), "mail_status": mail.status(),
        "wa_status": whatsapp.status(),
        "after_top": AFTER_TOP, "scopes": SCOPES,
        "next_level": (max((r.level for r in rows), default=0) + 1),
    })


@router.post("/test")
def send_test(request: Request, user: User = Depends(manage)):
    """One short email to whoever asked for it, and the plain truth back.

    Mail either works or it does not, and the only other way to find out
    was to force a real escalation run — which spends a rung on every
    overdue task to answer the question, and cannot be undone. This costs
    one email to the person pressing the button.
    """
    if not (user.email or "").strip():
        flash.set(request, "error",
                  "Your own account has no email address, so there is "
                  "nowhere to send the test.")
        return RedirectResponse("/escalation", status_code=303)
    if not mail.enabled():
        flash.set(request, "error", mail.status())
        return RedirectResponse("/escalation", status_code=303)

    ok, why = mail.send(
        [user.email], "GCS Autopilot — test email",
        "This is a test from GCS Autopilot's escalation settings.\n\n"
        "If you are reading it, escalation emails will reach people too.",
        html="<p style=\"font-family:system-ui,sans-serif\">This is a test "
             "from GCS Autopilot's escalation settings.</p><p "
             "style=\"font-family:system-ui,sans-serif\">If you are reading "
             "it, escalation emails will reach people too.</p>")
    if ok:
        flash.set(request, "saved",
                  f"Sent to {user.email}. If it is not there in a minute, "
                  "look in the spam folder — the first one often lands "
                  "there until somebody marks it as not spam.")
    else:
        flash.set(request, "error",
                  f"It did not send: {why}{mail.advice(why)}")
    return RedirectResponse("/escalation", status_code=303)


@router.post("/mail/{mail_id}/retry")
def retry_mail(mail_id: int, request: Request, user: User = Depends(manage),
               db: Session = Depends(get_db)):
    """Send a written-down email again, after the reason it failed is fixed.

    Nothing is re-decided and nothing new is recorded: the message is the
    one that was composed that morning, going to the person it was
    addressed to, covering the tasks it covered. A mail server that was
    unreachable for two days should not mean two days of escalations that
    nobody was ever told about — the record says they were escalated, and
    the record should be true.
    """
    m = db.get(EscalationMail, mail_id)
    if m is None or m.org_id != user.org_id:
        raise HTTPException(404, "No such email.")
    if m.status == "sent":
        flash.set(request, "error",
                  "That one was already delivered. Sending it again would "
                  "be a second copy of the same morning.")
        return RedirectResponse("/escalation/log", status_code=303)
    esc_svc._deliver(db, m, send=True)
    if m.status == "sent":
        flash.set(request, "saved", f"Sent to {m.to_email}.")
    else:
        flash.set(request, "error",
                  f"Still did not send: {m.error}{mail.advice(m.error or '')}")
    return RedirectResponse("/escalation/log", status_code=303)


@router.post("/mail/retry-day")
def retry_day(request: Request, day: str = Form(""),
              user: User = Depends(manage), db: Session = Depends(get_db)):
    """Every email of one day that did not get out, tried again."""
    try:
        want = date.fromisoformat(day.strip())
    except ValueError:
        raise HTTPException(400, "Pick a day first, as YYYY-MM-DD.")
    rows = db.scalars(
        select(EscalationMail).where(
            EscalationMail.org_id == user.org_id,
            EscalationMail.day == want,
            EscalationMail.status != "sent").order_by(EscalationMail.id)).all()
    sent = 0
    for m in rows:
        esc_svc._deliver(db, m, send=True)
        sent += 1 if m.status == "sent" else 0
    flash.set(request, "saved" if sent else "error",
              f"{sent} of {len(rows)} sent"
              + (f" — {rows[-1].error}" if rows and sent < len(rows) else ""))
    return RedirectResponse("/escalation/log", status_code=303)


@router.post("/mail/{mail_id}/discard")
def discard_mail(mail_id: int, request: Request, user: User = Depends(manage),
                 db: Session = Depends(get_db)):
    """Throw away an email that never left, and the rungs it claimed.

    Deleting the record alone would be the worst of both worlds: the log
    would go quiet while the escalations stayed, so every task it covered
    would still be marked as told at that level and would climb from there
    tomorrow — having told nobody anything. So the escalation rows go with
    it, and those tasks are owed their level again.

    Only ever for an email that was NOT delivered. One that was sent is
    history, and history is not edited here.
    """
    m = db.get(EscalationMail, mail_id)
    if m is None or m.org_id != user.org_id:
        raise HTTPException(404, "No such email.")
    if m.status == "sent":
        flash.set(request, "error",
                  "That one was delivered. The log is the record of what "
                  "people were actually told, so a sent email is not "
                  "removed from it.")
        return RedirectResponse("/escalation/log", status_code=303)
    freed = 0
    for row in db.scalars(select(Escalation).where(
            Escalation.mail_id == m.id)).all():
        db.delete(row)
        freed += 1
    db.delete(m)
    db.commit()
    flash.set(request, "saved",
              f"Discarded. {freed} task(s) are owed that level again, so "
              "the next run will tell somebody properly.")
    return RedirectResponse("/escalation/log", status_code=303)


@router.post("/settings")
async def save_settings(request: Request, esc_on: str = Form(""),
                  working_days: str = Form(""), after_top: str = Form("stop"),
                  scope: str = Form("chased"), hour: str = Form("9"),
                  site_url: str = Form(""), grace: str = Form("0"),
                  doer_daily: str = Form(""), doer_whatsapp: str = Form(""),
                  user: User = Depends(manage), db: Session = Depends(get_db)):
    org = _org(db, user)
    want_on = bool(esc_on)

    # Seven tickboxes posting one name, so they are read as a list. None
    # ticked is taken as every day rather than as "never run": a setting
    # that silently switches the whole thing off is not a setting, it is a
    # trap — and the tickbox above is where switching it off belongs.
    picked = sorted({int(d) for d in (await request.form()).getlist("esc_days")
                     if str(d).isdigit() and 0 <= int(d) <= 6})
    org.esc_days = ",".join(str(d) for d in picked) if picked else ""

    if want_on:
        # Switching it on with nobody on the first rung sends every overdue
        # task to nowhere and looks, from the outside, exactly like a system
        # that is working. Better to refuse and say which company is short.
        gaps = esc_svc.missing_ladders(db, org.id)
        if not esc_svc.ladder(db, org.id, None) and gaps:
            # "Fill in at least one level" was what this used to say, and it
            # was read by somebody who had just filled in three — for one
            # company. The levels they added were real; what is missing is
            # the ladder every OTHER company falls back on, and the message
            # has to name the tab rather than leave them adding the same
            # three levels to the company they already did.
            more = (f" and {len(gaps) - 3} other(s)" if len(gaps) > 3 else "")
            raise HTTPException(
                400, "Escalation is still off. " + ", ".join(gaps[:3]) + more
                     + " have no levels of their own, and the “Every other "
                       "company” ladder they would fall back on is empty — "
                       "so overdue work there would be escalated to nobody. "
                       "Add at least Level 1 under the “Every other company” "
                       "tab (the first one), then switch escalation on. Any "
                       "company with its own levels keeps using those.")

    try:
        # Clamped rather than refused: a number out of range is a typo, and
        # losing the rest of the form to it helps nobody.
        org.esc_grace = max(0, min(60, int(str(grace).strip() or 0)))
    except ValueError:
        raise HTTPException(
            400, "Days overdue before the first level has to be a whole "
                 "number — 0 for the first day a task is late, 3 to start "
                 "on the fourth day.")

    org.esc_doer_daily = bool(doer_daily)
    org.esc_doer_whatsapp = bool(doer_whatsapp)

    if after_top not in AFTER_TOP:
        raise HTTPException(400, "Choose what happens after the top level.")
    if scope not in SCOPES:
        raise HTTPException(400, "Choose which work escalates.")

    address = site_url.strip().rstrip("/")
    if address and not address.startswith(("http://", "https://")):
        raise HTTPException(
            400, "The web address has to start with https:// — otherwise "
                 "every link in every escalation email is a dead one.")
    org.site_url = address or None

    org.esc_on = want_on
    org.esc_working_days = bool(working_days)
    org.esc_after_top = after_top
    org.esc_scope = scope
    org.esc_hour = _num(hour, "the hour of the morning", 0, 23)
    db.commit()
    flash.set(request, "saved",
              "Escalation is on." if want_on else "Escalation is off.")
    return RedirectResponse("/escalation", status_code=303)


# ------------------------------------------------------------- the rungs ---

def _rule_from_form(db: Session, user: User, rule: EscalationRule, form) -> None:
    """Fill one rung from the posted form. Shared by add and edit."""
    who = (form.get("user_id") or "").strip()
    if who:
        person = db.get(User, int(who)) if who.isdigit() else None
        if person is None or person.org_id != user.org_id:
            raise HTTPException(400, "Unknown person.")
        rule.user_id = person.id
        rule.to_name = None
        rule.to_email = None
    else:
        rule.user_id = None
        rule.to_name = (form.get("to_name") or "").strip() or None
        address = (form.get("to_email") or "").strip() or None
        if address and not mail.valid(address):
            raise HTTPException(
                400, f"'{address}' does not look like an email address.")
        rule.to_email = address

    rule.salutation = (form.get("salutation") or "").strip() or None
    rule.message = (form.get("message") or "").strip() or None
    rule.urgent = bool(form.get("urgent"))
    rule.cc_doer = bool(form.get("cc_doer"))
    rule.cc_earlier = bool(form.get("cc_earlier"))
    rule.cc_assigner = bool(form.get("cc_assigner"))

    if not rule.email and rule.user_id is None:
        raise HTTPException(
            400, "A level needs somewhere to send to — pick somebody on the "
                 "system, or type a name and an email address.")


@router.post("/rungs")
async def add_rung(request: Request, user: User = Depends(manage),
                   db: Session = Depends(get_db)):
    org = _org(db, user)
    form = await request.form()
    branch_id = _scope(db, form.get("scope") or "", user)
    level = _num(form.get("level") or "", "the level", 1, 20)

    clash = db.scalar(select(EscalationRule).where(
        EscalationRule.org_id == org.id,
        EscalationRule.level == level,
        EscalationRule.branch_id.is_(None) if branch_id is None
        else EscalationRule.branch_id == branch_id))
    if clash:
        raise HTTPException(
            400, f"Level {level} already exists for that company. Edit it "
                 "rather than adding a second one.")

    rule = EscalationRule(org_id=org.id, branch_id=branch_id, level=level)
    _rule_from_form(db, user, rule, form)
    db.add(rule)
    db.commit()
    flash.set(request, "saved", f"Level {level} saved.")
    return RedirectResponse(_back(branch_id), status_code=303)


@router.post("/rungs/{rule_id}")
async def edit_rung(rule_id: int, request: Request, user: User = Depends(manage),
                    db: Session = Depends(get_db)):
    rule = db.get(EscalationRule, rule_id)
    if rule is None or rule.org_id != user.org_id:
        raise HTTPException(404, "That level is not on this system.")
    form = await request.form()
    _rule_from_form(db, user, rule, form)
    db.commit()
    flash.set(request, "saved", f"Level {rule.level} saved.")
    return RedirectResponse(_back(rule.branch_id), status_code=303)


@router.post("/rungs/{rule_id}/delete")
def delete_rung(rule_id: int, request: Request, user: User = Depends(manage),
                db: Session = Depends(get_db)):
    rule = db.get(EscalationRule, rule_id)
    if rule is None or rule.org_id != user.org_id:
        raise HTTPException(404, "That level is not on this system.")
    where, level = rule.branch_id, rule.level
    db.delete(rule)
    db.commit()
    flash.set(request, "saved", f"Level {level} removed.")
    return RedirectResponse(_back(where), status_code=303)


def _back(branch_id: int | None) -> str:
    return "/escalation" + (f"?scope={branch_id}" if branch_id else "")


# ------------------------------------------------------- preview and run ---

@router.get("/preview", response_class=HTMLResponse)
def preview(request: Request, user: User = Depends(manage),
            db: Session = Depends(get_db)):
    """What this morning would send, without sending any of it.

    The whole check runs — the same overdue list, the same ladder lookup,
    the same "what is owed next" question — and then stops short of both
    the database row and the email. So this is not a description of what
    would happen; it is what would happen, shown.
    """
    org = _org(db, user)
    today = clock.today()
    groups, skipped, _gap = esc_svc.plan(db, org, today)

    out = []
    for g in groups:
        rule = g["rule"]
        subject, body, body_html, cc = esc_svc.compose(
            g["rows"], g["level"], g["repeat_n"], rule, g["rungs"],
            _site(request), g["below_sent"])
        out.append({"to": rule.name, "email": rule.email, "cc": cc,
                    "level": g["level"], "repeat_n": g["repeat_n"],
                    "count": len(g["rows"]), "to_doer": False,
                    "subject": subject, "body": body, "html": body_html})
        # And each doer's own copy of their own rows — shown here too, so
        # nobody is surprised by an email they did not know went out.
        if rule.cc_doer:
            for person, rows in esc_svc._doer_copies(g):
                d_sub, d_body, d_html, _c = esc_svc.compose(
                    rows, g["level"], g["repeat_n"], rule, g["rungs"],
                    _site(request), g["below_sent"], for_doer=person.name)
                out.append({"to": person.name, "email": person.email,
                            "cc": [], "level": g["level"],
                            "repeat_n": g["repeat_n"], "count": len(rows),
                            "to_doer": True, "subject": d_sub,
                            "body": d_body, "html": d_html})

    return templates.TemplateResponse(request, "escalation_preview.html", {
        "user": user, "org": org, "rows": out, "skipped": skipped,
        "tasks": sum(g["count"] for g in out if not g["to_doer"]),
        "today": today, "mail_on": mail.enabled(),
        "mail_status": mail.status(),
    })


def _site(request: Request) -> str:
    """The address of this site, for the link in the email."""
    return str(request.base_url).rstrip("/")


@router.post("/run")
def run_now(request: Request, send: str = Form(""),
            user: User = Depends(manage), db: Session = Depends(get_db)):
    """Run the morning check by hand.

    Two buttons sit on this one route. "Record without sending" writes the
    escalations down and emails nobody, which is how a first day is tested;
    "Send now" does the real thing. Either way the rows are written, so a
    recorded-but-not-sent escalation cannot quietly be sent again tomorrow
    — which is the honest behaviour, and worth saying on the page.
    """
    org = _org(db, user)
    result = esc_svc.run(db, org, send=bool(send), site=_site(request),
                         force=True)
    bits = [f"{result['raised']} task(s) in {result['emails']} email(s)"]
    if result["sent"]:
        bits.append(f"{result['sent']} emailed")
    if result["failed"]:
        bits.append(f"{result['failed']} could not be sent")
    if result["queued"]:
        bits.append(f"{result['queued']} recorded only")
    if result["skipped_no_ladder"]:
        bits.append(f"{result['skipped_no_ladder']} skipped for want of a ladder")
    flash.set(request, "saved", "Escalated " + ", ".join(bits))
    return RedirectResponse("/escalation/log", status_code=303)


# ---------------------------------------------------------------- the log --

@router.get("/log", response_class=HTMLResponse)
def log(request: Request, level: str = "", status: str = "", day: str = "",
        task: str = "", view: str = "mails", user: User = Depends(manage),
        db: Session = Depends(get_db)):
    """The trail, read either way round.

    Two views of the same thing, because there are two questions. "What went
    out on Tuesday" is about emails; "has DEL-341 ever been escalated, and
    to whom" is about one task. One list cannot answer both well, so the log
    answers the first by default and switches to the second the moment
    somebody types a Task ID.
    """
    org = _org(db, user)
    if task.strip():
        view = "tasks"

    mails: list[EscalationMail] = []
    rows: list[Escalation] = []

    if view == "tasks":
        q = select(Escalation).where(Escalation.org_id == org.id)
        if level.isdigit():
            q = q.where(Escalation.level == int(level))
        if day:
            q = q.where(Escalation.day == _day(day))
        if task.strip():
            ref = task.strip().upper()
            hit = db.scalar(select(Task).where(Task.org_id == org.id,
                                               Task.ref == ref))
            q = q.where(Escalation.task_id == (hit.id if hit else -1))
        rows = db.scalars(
            q.order_by(Escalation.created_at.desc()).limit(400)).all()
        if status in ("sent", "queued", "failed"):
            rows = [e for e in rows if e.delivery == status]
    else:
        q = select(EscalationMail).where(EscalationMail.org_id == org.id)
        if level.isdigit():
            q = q.where(EscalationMail.level == int(level))
        if status in ("sent", "queued", "failed"):
            q = q.where(EscalationMail.status == status)
        if day:
            q = q.where(EscalationMail.day == _day(day))
        mails = db.scalars(
            q.order_by(EscalationMail.created_at.desc()).limit(300)).all()

    return templates.TemplateResponse(request, "escalation_log.html", {
        "user": user, "org": org, "mails": mails, "rows": rows,
        "view": view, "level": level, "status": status, "day": day,
        "task": task,
        "levels": sorted({m.level for m in db.scalars(
            select(EscalationMail).where(
                EscalationMail.org_id == org.id)).all()}
            | {e.level for e in db.scalars(
                select(Escalation).where(
                    Escalation.org_id == org.id)).all()}),
    })


def _day(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, "That is not a valid date.")


@router.get("/log/{esc_id}", response_class=HTMLResponse)
def one(esc_id: int, request: Request, user: User = Depends(manage),
        db: Session = Depends(get_db)):
    """The email that carried one task, exactly as it went out."""
    esc = db.get(Escalation, esc_id)
    if esc is None or esc.org_id != user.org_id:
        raise HTTPException(404, "That escalation is not on this system.")
    return templates.TemplateResponse(request, "escalation_one.html", {
        "user": user, "esc": esc, "mail": esc.mail, "task": esc.task,
    })


@router.get("/mail/{mail_id}", response_class=HTMLResponse)
def one_mail(mail_id: int, request: Request, user: User = Depends(manage),
             db: Session = Depends(get_db)):
    """One morning's email, with every task it carried."""
    m = db.get(EscalationMail, mail_id)
    if m is None or m.org_id != user.org_id:
        raise HTTPException(404, "That email is not on this system.")
    return templates.TemplateResponse(request, "escalation_mail.html", {
        "user": user, "mail": m,
        "items": sorted(m.items, key=lambda e: (
            (e.task.doer.name or "").strip().lower()
            if e.task and e.task.doer else "", e.id)),
    })
