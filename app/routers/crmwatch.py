"""Pending-calls escalation: settings, a preview, a send-now, and the log."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock, flash
from ..db import get_db
from ..deps import manager_up, require_right
from ..models import CrmSend, CrmWatch, Organization, Right, User
from ..services import approvals, crmwatch, mail, whatsapp
from ..templating import templates

router = APIRouter()


def _org(db: Session, user: User) -> Organization:
    return db.scalar(select(Organization).where(Organization.id == user.org_id))


def _watch(db: Session, user: User, wid: int) -> CrmWatch:
    w = db.get(CrmWatch, wid)
    if not w or w.org_id != user.org_id:
        raise HTTPException(404, "No such company.")
    return w


@router.get("/pending-calls", response_class=HTMLResponse)
def page(request: Request, user: User = Depends(manager_up),
         db: Session = Depends(get_db)):
    org = _org(db, user)
    watches = crmwatch.ensure_watches(db, org.id)
    people = list(db.scalars(select(User).where(
        User.org_id == org.id, User.active.is_(True)).order_by(User.name)).all())
    log = list(db.scalars(select(CrmSend).where(
        CrmSend.org_id == org.id, CrmSend.level > 0)
        .order_by(CrmSend.id.desc()).limit(40)).all())
    names = {w.id: w.label for w in watches}
    return templates.TemplateResponse(request, "pending_calls.html", {
        "user": user, "org": org, "s": crmwatch.settings(org),
        "watches": watches, "people": people, "log": log, "names": names,
        "robot": approvals.robot(), "google": approvals.enabled(),
        "mail_status": mail.status(), "mail_on": mail.enabled(),
        "wa_status": whatsapp.status(), "wa_template": crmwatch.WA_TEMPLATE,
        "can_set": user.has(Right.MANAGE_USER), "now": clock.now(),
        "beat": crmwatch.HEARTBEAT,
    })


@router.post("/pending-calls/settings")
def save_settings(request: Request, l1: int = Form(0), l2: int = Form(0),
                  m1: int = Form(30), m2: int = Form(60),
                  h_from: int = Form(9), h_to: int = Form(18),
                  on: str = Form(""), email: str = Form(""), wa: str = Form(""),
                  user: User = Depends(require_right(Right.MANAGE_USER)),
                  db: Session = Depends(get_db)):
    org = _org(db, user)
    if not (0 < m1 <= m2 <= 600) or not (0 <= h_from <= h_to <= 23):
        raise HTTPException(400, "Check the minutes (first less than second) and "
                                 "the hours (from earlier than to).")
    for uid in (l1, l2):
        if uid and (db.get(User, uid) or User(org_id=-1)).org_id != org.id:
            raise HTTPException(400, "Pick people from the list.")
    org.crm_l1_user_id, org.crm_l2_user_id = (l1 or None), (l2 or None)
    org.crm_l1_min, org.crm_l2_min = m1, m2
    org.crm_from_hour, org.crm_to_hour = h_from, h_to
    org.crm_on, org.crm_email_on, org.crm_wa_on = bool(on), bool(email), bool(wa)
    db.commit()
    flash.set(request, "saved", "Saved.")
    return RedirectResponse("/pending-calls", status_code=303)


@router.post("/pending-calls/watch/{wid}")
def save_watch(wid: int, request: Request, sheet: str = Form(""), tab: str = Form(""),
               when_col: str = Form("C"), name_col: str = Form(""),
               stage_col: str = Form(""), pending_word: str = Form(""),
               date_order: str = Form("dmy"), aliases: str = Form(""),
               sunday_off: str = Form(""), active: str = Form(""),
               user: User = Depends(require_right(Right.MANAGE_USER)),
               db: Session = Depends(get_db)):
    w = _watch(db, user, wid)
    sid = approvals.sheet_id_from(sheet) if sheet.strip() else (w.sheet_id or "")
    if sheet.strip() and not sid:
        raise HTTPException(400, "That does not look like a Google Sheet link.")
    try:
        for c in (when_col, name_col, stage_col):
            crmwatch.col_index(c)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not pending_word.strip():
        raise HTTPException(400, "Say which Sales Stage word means the call is pending.")
    w.sheet_id, w.tab = sid or None, (tab.strip() or "Inquiry")
    w.when_col, w.name_col, w.stage_col = (when_col.strip().upper(),
                                           name_col.strip().upper(),
                                           stage_col.strip().upper())
    w.pending_word = pending_word.strip()
    w.date_order = "mdy" if date_order == "mdy" else "dmy"
    w.aliases = aliases.strip() or None
    w.sunday_off, w.active = bool(sunday_off), bool(active)
    db.commit()
    got = crmwatch.read_watch(db, w)
    flash.set(request, "saved" if got["ok"] else "error",
              f"{w.label}: saved and the sheet reads fine." if got["ok"]
              else f"{w.label}: saved, but reading failed. {got['why']}")
    return RedirectResponse("/pending-calls", status_code=303)


@router.post("/pending-calls/watch/{wid}/preview")
def preview(wid: int, request: Request, user: User = Depends(manager_up),
            db: Session = Depends(get_db)):
    """What would go out right now. Sends nothing."""
    org, w = _org(db, user), _watch(db, user, wid)
    res = crmwatch.run_watch(db, org, w, clock.now(), send=False, force=True)
    if not res["ok"]:
        flash.set(request, "error", f"{w.label}: {res.get('why')}")
    else:
        parts = []
        for lv in res["levels"]:
            top = ", ".join(f"{n} {c}" for n, c in crmwatch.lines(lv["counts"])) or "none"
            parts.append(f"over {lv['minutes']} min → {lv['to'] or 'nobody chosen'}: "
                         f"{lv['total']} pending ({top})")
        flash.set(request, "saved", f"{w.label} now — " + " | ".join(parts))
    return RedirectResponse("/pending-calls", status_code=303)


@router.post("/pending-calls/watch/{wid}/send")
def send_now(wid: int, request: Request,
             user: User = Depends(require_right(Right.MANAGE_USER)),
             db: Session = Depends(get_db)):
    """Send this company's escalation right now, whatever the hour."""
    org, w = _org(db, user), _watch(db, user, wid)
    res = crmwatch.run_watch(db, org, w, clock.now(), send=True, force=True)
    if not res["ok"]:
        flash.set(request, "error", f"{w.label}: {res.get('why')}")
    else:
        bits = []
        for lv in res["levels"]:
            for ch, ok, why in lv["sent"]:
                bits.append(f"{ch} to {lv['to']}: " + ("sent" if ok else f"FAILED ({why})"))
            if not lv["sent"]:
                bits.append(f"over {lv['minutes']} min: " + (
                    lv.get("why") or ("nothing pending" if not lv["total"] else "no channel on")))
        flash.set(request, "saved", f"{w.label} — " + "; ".join(bits))
    return RedirectResponse("/pending-calls", status_code=303)
