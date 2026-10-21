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
    Branch, Escalation, EscalationRule, Organization, Right, Task, User,
)
from ..services import escalation as esc_svc
from ..services import mail
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
        select(Escalation).where(Escalation.org_id == org.id)
        .order_by(Escalation.created_at.desc()).limit(8)
    ).all()

    return templates.TemplateResponse(request, "escalation.html", {
        "user": user, "org": org, "branches": branches, "rows": rows,
        "branch_id": branch_id,
        "branch": db.get(Branch, branch_id) if branch_id else None,
        "staff": staff, "recent": recent,
        "effective": esc_svc.ladder(db, org.id, branch_id),
        "gaps": esc_svc.missing_ladders(db, org.id),
        "mail_on": mail.enabled(), "mail_status": mail.status(),
        "after_top": AFTER_TOP, "scopes": SCOPES,
        "next_level": (max((r.level for r in rows), default=0) + 1),
    })


@router.post("/settings")
def save_settings(request: Request, esc_on: str = Form(""),
                  working_days: str = Form(""), after_top: str = Form("stop"),
                  scope: str = Form("chased"), hour: str = Form("9"),
                  user: User = Depends(manage), db: Session = Depends(get_db)):
    org = _org(db, user)
    want_on = bool(esc_on)

    if want_on:
        # Switching it on with nobody on the first rung sends every overdue
        # task to nowhere and looks, from the outside, exactly like a system
        # that is working. Better to refuse and say which company is short.
        gaps = esc_svc.missing_ladders(db, org.id)
        if not esc_svc.ladder(db, org.id, None) and gaps:
            raise HTTPException(
                400, "Fill in at least one level before switching escalation "
                     "on — otherwise overdue work at " + ", ".join(gaps[:3]) +
                     " would be escalated to nobody.")

    if after_top not in AFTER_TOP:
        raise HTTPException(400, "Choose what happens after the top level.")
    if scope not in SCOPES:
        raise HTTPException(400, "Choose which work escalates.")

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
    rows = esc_svc.overdue_tasks(db, org.id, today, org.esc_working_days,
                                 org.esc_scope)
    out, skipped = [], []
    for task, late in rows:
        rungs = esc_svc.ladder(db, org.id, task.branch_id)
        if not rungs:
            skipped.append((task, late, "No level is set for that company."))
            continue
        sent = esc_svc.already(db, task.id)
        step = esc_svc.next_step(sent, late, rungs, org.esc_after_top, today)
        if step is None:
            if any(e.day == today for e in sent):
                why = "Already escalated today."
            elif sent:
                why = (f"Level {max(e.level for e in sent)} is the top of that "
                       "ladder and has been sent.")
            else:
                why = "Not yet at the first level."
            skipped.append((task, late, why))
            continue
        level, repeat_n = step
        rule = rungs[level]
        subject, body, cc = esc_svc.compose(db, task, late, level, repeat_n,
                                            rule, rungs, _site(request), sent)
        out.append({"task": task, "late": late, "level": level,
                    "repeat_n": repeat_n, "to": rule.name,
                    "email": rule.email, "cc": cc,
                    "subject": subject, "body": body})

    return templates.TemplateResponse(request, "escalation_preview.html", {
        "user": user, "org": org, "rows": out, "skipped": skipped,
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
    bits = [f"{result['raised']} raised"]
    if result["sent"]:
        bits.append(f"{result['sent']} emailed")
    if result["failed"]:
        bits.append(f"{result['failed']} could not be sent")
    if result["queued"]:
        bits.append(f"{result['queued']} recorded only")
    if result["skipped_no_ladder"]:
        bits.append(f"{result['skipped_no_ladder']} skipped for want of a ladder")
    flash.set(request, "saved",
              f"Checked {result['looked']} overdue task(s): " + ", ".join(bits))
    return RedirectResponse("/escalation/log", status_code=303)


# ---------------------------------------------------------------- the log --

@router.get("/log", response_class=HTMLResponse)
def log(request: Request, level: str = "", status: str = "", day: str = "",
        task: str = "", user: User = Depends(manage),
        db: Session = Depends(get_db)):
    org = _org(db, user)
    q = select(Escalation).where(Escalation.org_id == org.id)
    if level.isdigit():
        q = q.where(Escalation.level == int(level))
    if status in ("sent", "queued", "failed"):
        q = q.where(Escalation.status == status)
    if day:
        try:
            q = q.where(Escalation.day == date.fromisoformat(day))
        except ValueError:
            raise HTTPException(400, "That is not a valid date.")
    if task.strip():
        ref = task.strip().upper()
        hit = db.scalar(select(Task).where(Task.org_id == org.id,
                                           Task.ref == ref))
        q = q.where(Escalation.task_id == (hit.id if hit else -1))

    rows = db.scalars(q.order_by(Escalation.created_at.desc()).limit(400)).all()
    return templates.TemplateResponse(request, "escalation_log.html", {
        "user": user, "org": org, "rows": rows, "level": level,
        "status": status, "day": day, "task": task,
        "levels": sorted({e.level for e in db.scalars(
            select(Escalation).where(Escalation.org_id == org.id)).all()}),
    })


@router.get("/log/{esc_id}", response_class=HTMLResponse)
def one(esc_id: int, request: Request, user: User = Depends(manage),
        db: Session = Depends(get_db)):
    """The message exactly as it went out, kept for the record."""
    esc = db.get(Escalation, esc_id)
    if esc is None or esc.org_id != user.org_id:
        raise HTTPException(404, "That escalation is not on this system.")
    return templates.TemplateResponse(request, "escalation_one.html", {
        "user": user, "esc": esc,
    })
