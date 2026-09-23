"""First-run setup — create the first admin without a shell.

Render's free plan has no Shell tab (that is a paid feature), so the original
"run fresh_start.py after deploying" instruction is impossible there. Instead:

  * If ADMIN_EMAIL and ADMIN_PW are set as environment variables, the first
    admin is created automatically when the app starts. Nothing to click.

  * Otherwise the site shows a one-time setup page. It exists only while the
    organisation has no users at all, and disappears the moment one exists —
    so it cannot be used later to mint an account on a live system.
"""
import os

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Organization, User, Role, DEFAULT_RIGHTS
from ..security import hash_password
from ..templating import templates

router = APIRouter()


def needs_setup(db: Session) -> bool:
    """True only while nobody can log in at all."""
    return (db.scalar(select(func.count()).select_from(User)) or 0) == 0


def ensure_org(db: Session) -> Organization:
    org = db.scalar(select(Organization))
    if org is None:
        org = Organization(name=os.getenv("ORG_NAME", "GCS Group"), slug="gcs")
        db.add(org)
        db.flush()
    return org


def create_admin(db: Session, name: str, email: str, password: str) -> User:
    org = ensure_org(db)
    u = User(org_id=org.id, name=name.strip(), email=email.strip().lower(),
             password_hash=hash_password(password), role=Role.ADMIN)
    u.set_rights(DEFAULT_RIGHTS[Role.ADMIN])
    u.set_benchmarks(60, 20, 20)
    db.add(u)
    db.commit()
    return u


@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, db: Session = Depends(get_db)):
    if not needs_setup(db):
        raise HTTPException(404, "Setup is already done — sign in instead.")
    return templates.TemplateResponse(request, "setup.html",
                                      {"user": None, "error": None})


@router.post("/setup", response_class=HTMLResponse)
def do_setup(request: Request, name: str = Form(...), email: str = Form(...),
             password: str = Form(...), password2: str = Form(...),
             db: Session = Depends(get_db)):
    # Re-checked on the POST, not just the GET: two people opening the page at
    # once must not both be able to create an admin.
    if not needs_setup(db):
        raise HTTPException(404, "Setup is already done — sign in instead.")

    def fail(msg):
        return templates.TemplateResponse(
            request, "setup.html", {"user": None, "error": msg}, status_code=400)

    if len(password) < 8:
        return fail("Use a password of at least 8 characters.")
    if password != password2:
        return fail("The two passwords do not match.")
    if "@" not in email:
        return fail("That does not look like an email address.")
    if not name.strip():
        return fail("Enter your name.")

    create_admin(db, name, email, password)
    return RedirectResponse("/login", status_code=303)
