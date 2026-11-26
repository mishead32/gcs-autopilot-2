"""Where administrators make and revoke API keys."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock, flash
from ..db import get_db
from ..deps import require_right
from ..models import ApiKey, Right, User
from ..services import apikeys
from ..templating import templates

router = APIRouter()
manage = require_right(Right.MANAGE_USER)


@router.get("/api-keys", response_class=HTMLResponse)
def page(request: Request, user: User = Depends(manage), db: Session = Depends(get_db)):
    keys = list(db.scalars(select(ApiKey).where(ApiKey.org_id == user.org_id)
                           .order_by(ApiKey.id.desc())).all())
    people = list(db.scalars(select(User).where(
        User.org_id == user.org_id, User.active.is_(True)).order_by(User.name)).all())
    return templates.TemplateResponse(request, "api_keys.html", {
        "user": user, "keys": keys, "people": people, "scopes": apikeys.SCOPES,
        "new_key": request.session.pop("new_api_key", None),
        "base": str(request.base_url).rstrip("/"),
    })


@router.post("/api-keys/new")
def make(request: Request, name: str = Form(...), acting: int = Form(...),
         scope: list[str] = Form(default=[]),
         user: User = Depends(manage), db: Session = Depends(get_db)):
    who = db.get(User, acting)
    if not who or who.org_id != user.org_id or not who.active:
        raise HTTPException(400, "Choose who the key acts as.")
    if not name.strip():
        raise HTTPException(400, "Give the key a name, so you know what uses it.")
    chosen = [s for s in scope if s in apikeys.SCOPES]
    if not chosen:
        raise HTTPException(400, "Tick at least one thing the key may do.")
    row, key = apikeys.make(db, user.org_id, name, chosen, who, user)
    request.session["new_api_key"] = {"name": row.name, "key": key}
    return RedirectResponse("/api-keys", status_code=303)


@router.post("/api-keys/{kid}/revoke")
def revoke(kid: int, request: Request, user: User = Depends(manage),
           db: Session = Depends(get_db)):
    row = db.get(ApiKey, kid)
    if not row or row.org_id != user.org_id:
        raise HTTPException(404, "No such key.")
    row.revoked_at = clock.now()
    db.commit()
    flash.set(request, "saved", f"“{row.name}” is revoked. It stops working at once.")
    return RedirectResponse("/api-keys", status_code=303)
