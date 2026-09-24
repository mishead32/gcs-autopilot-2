from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import flash
from ..db import get_db
from ..models import User
from ..security import verify_password
from ..templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db)):
    from .setup import needs_setup
    if needs_setup(db):
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or not user.active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password."}, status_code=401
        )
    request.session["uid"] = user.id
    flash.set(request, "welcome", f"Welcome back, {user.name.split()[0]}")
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
