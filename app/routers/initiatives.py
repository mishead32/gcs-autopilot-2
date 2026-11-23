"""Self-Initiative — which sheet is read, who it was matched to, and by hand
who it was not. Nothing here writes to the sheet."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import flash
from ..db import get_db
from ..deps import manager_up, require_right
from ..models import Initiative, InitiativeAlias, Organization, Right, User
from ..services import initiatives
from ..templating import templates

router = APIRouter()


def _org(db: Session, user: User) -> Organization:
    return db.scalar(select(Organization).where(Organization.id == user.org_id))


@router.get("/initiatives", response_class=HTMLResponse)
def page(request: Request, user: User = Depends(manager_up),
         db: Session = Depends(get_db)):
    org = _org(db, user)
    rows = list(db.scalars(select(Initiative).where(Initiative.org_id == org.id)
                           .order_by(Initiative.done_on.desc().nulls_last(),
                                     Initiative.row_no.desc()).limit(200)).all())
    people = list(db.scalars(select(User).where(
        User.org_id == org.id, User.active.is_(True)).order_by(User.name)).all())
    names = {u.id: u.name for u in people}
    for r in rows:
        r.matched_name = names.get(r.user_id)
    return templates.TemplateResponse(request, "initiatives.html", {
        "user": user, "org": org, "rows": rows, "people": people,
        "unmatched": initiatives.unmatched(db, org.id),
        "enabled": initiatives.enabled(), "robot": initiatives.robot(),
        "configured": initiatives.configured(org),
        "live": initiatives.active(org),
        "default_tab": initiatives.DEFAULT_TAB,
        "can_set": user.has(Right.MANAGE_USER),
    })


@router.post("/initiatives/refresh")
def refresh(request: Request, user: User = Depends(manager_up),
            db: Session = Depends(get_db)):
    got = initiatives.sync(db, _org(db, user))
    if got.get("ok"):
        flash.set(request, "saved", f"Read {got['rows']} answer(s) from the form sheet.")
    else:
        flash.set(request, "error", got.get("why") or "Could not read it.")
    return RedirectResponse("/initiatives", status_code=303)


@router.post("/initiatives/settings")
def settings(request: Request, sheet: str = Form(""), tab: str = Form(""),
             user: User = Depends(require_right(Right.MANAGE_USER)),
             db: Session = Depends(get_db)):
    org = _org(db, user)
    sheet_id = initiatives.sheet_id_from(sheet)
    if not sheet_id:
        raise HTTPException(
            400, "That does not look like a Google Sheet link. Open the sheet, "
                 "copy the whole address from the top of the browser, and paste it here.")
    org.init_sheet_id = sheet_id
    org.init_tab = (tab or "").strip() or initiatives.DEFAULT_TAB
    db.commit()
    got = initiatives.sync(db, org)
    if got.get("ok"):
        flash.set(request, "saved", f"Connected. Read {got['rows']} answer(s).")
    else:
        flash.set(request, "error",
                  "Saved, but the first read failed. " + (got.get("why") or ""))
    return RedirectResponse("/initiatives", status_code=303)


@router.post("/initiatives/match")
def match(request: Request, name: str = Form(""), person: int = Form(0),
          user: User = Depends(require_right(Right.MANAGE_USER)),
          db: Session = Depends(get_db)):
    """Tell it who a name on the form is, for good."""
    key = initiatives.name_key(name)
    target = db.get(User, person)
    if not key or not target or target.org_id != user.org_id:
        raise HTTPException(400, "Pick the person this name belongs to.")
    old = db.scalar(select(InitiativeAlias).where(
        InitiativeAlias.org_id == user.org_id, InitiativeAlias.name_key == key))
    if old:
        old.user_id = target.id
    else:
        db.add(InitiativeAlias(org_id=user.org_id, name_key=key, user_id=target.id))
    db.commit()
    initiatives.rematch(db, user.org_id)
    flash.set(request, "saved", f"“{name.strip()}” is now {target.name}.")
    return RedirectResponse("/initiatives", status_code=303)
