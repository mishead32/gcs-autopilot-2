"""Clear all demo data and leave a clean system to build on.

    python fresh_start.py

WHAT IT DELETES
    every task, note, attachment, flow, flow run, checklist rule and queued
    message — and every user except the one admin account below.

WHAT IT KEEPS
    the organisation, the five companies, their departments, and one admin
    login so you are never locked out.

It asks for confirmation before touching anything. Nothing is recoverable
afterwards, so take a copy of midap.db first if you want the demo data back.
"""
import shutil
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, delete

from app.config import BASE_DIR, UPLOAD_DIR
from app.db import Base, engine, SessionLocal
from app import migrate
from app.models import (
    Organization, Branch, Department, User, Role, Flow, FlowStep, FlowInstance,
    Task, TaskComment, Attachment, RecurringRule, OutboundMessage, HelpTicket,
    DEFAULT_RIGHTS,
)
from app.security import hash_password

ADMIN_NAME = os.getenv("ADMIN_NAME", "Rajinder Singh")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "mis@gcs.local")

# On your own PC a known password is convenient. On anything reachable from
# the internet it is a door left open, so unless ADMIN_PW is set we generate
# one and print it once.
_PW_FROM_ENV = bool(os.getenv("ADMIN_PW"))
ADMIN_PW = os.getenv("ADMIN_PW") or (
    "gcs1234" if not os.getenv("DATABASE_URL") else secrets.token_urlsafe(12)
)

COMPANIES = [
    ("Bodyzone Fitness & Spa", "Chandigarh"),
    ("Spa Kora", "Chandigarh"),
    ("BIPS School", "Chandigarh"),
    ("GCS Jharkhand", "Ranchi"),
    ("GCS HO", "Chandigarh"),
]


def backup() -> Path | None:
    db = BASE_DIR / "midap.db"
    if not db.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BASE_DIR / f"midap-backup-{stamp}.db"
    shutil.copy2(db, dest)
    return dest


def wipe():
    Base.metadata.create_all(engine)
    migrate.run()
    db = SessionLocal()

    # order matters: children before parents
    # OutboundMessage first: it references tasks, and Postgres enforces that
    for model in (OutboundMessage, HelpTicket, TaskComment, Attachment, Task,
                  FlowInstance, FlowStep, Flow, RecurringRule):
        db.execute(delete(model))
    db.commit()

    org = db.scalar(select(Organization))
    if org is None:
        org = Organization(name="GCS Group", slug="gcs")
        db.add(org)
        db.flush()

    # make sure the five companies exist, without duplicating any
    have = {b.name for b in db.scalars(
        select(Branch).where(Branch.org_id == org.id)).all()}
    for name, city in COMPANIES:
        if name not in have:
            db.add(Branch(org_id=org.id, name=name, city=city))
    db.commit()

    # one admin survives; everyone else goes
    admin = db.scalar(select(User).where(User.email == ADMIN_EMAIL))
    if admin is None:
        admin = User(org_id=org.id, name=ADMIN_NAME, email=ADMIN_EMAIL,
                     password_hash=hash_password(ADMIN_PW), role=Role.ADMIN)
        admin.set_rights(DEFAULT_RIGHTS[Role.ADMIN])
        admin.set_benchmarks(60, 20, 20)
        db.add(admin)
        db.flush()

    db.execute(delete(User).where(User.id != admin.id))
    db.commit()

    branches = db.scalars(select(Branch).where(Branch.org_id == org.id)
                          .order_by(Branch.name)).all()
    depts = db.scalars(select(Department).where(Department.org_id == org.id)).all()
    db.close()

    # clear uploaded attachment files too — nothing points at them any more
    removed = 0
    if not UPLOAD_DIR.exists():
        return branches, depts, removed     # hosted: no local disk to clean
    for f in UPLOAD_DIR.iterdir():
        if f.is_file() and f.name != ".gitkeep":
            f.unlink()
            removed += 1

    return branches, depts, removed


if __name__ == "__main__":
    print(__doc__)
    if "--yes" not in sys.argv:
        ans = input("Delete all users, tasks, flows and checklists? Type YES to confirm: ")
        if ans.strip() != "YES":
            print("Cancelled. Nothing was changed.")
            sys.exit(0)

    saved = backup()
    branches, depts, removed = wipe()

    print("\nDone — the system is empty and ready.\n")
    if saved:
        print(f"  A backup of the old database was saved as {saved.name}")
    print(f"  {len(branches)} companies kept: " + ", ".join(b.name for b in branches))
    print(f"  {len(depts)} departments kept")
    print(f"  {removed} orphaned attachment file(s) removed")
    print(f"\n  Sign in as {ADMIN_EMAIL} / {ADMIN_PW}")
    if not _PW_FROM_ENV and os.getenv("DATABASE_URL"):
        print("  ^ this password was generated just now and is shown ONCE.")
        print("    Copy it somewhere safe, then change it from the Users screen.")
    print("  Then go to Users to create your real staff, setting each person's")
    print("  rights and their Delegation / Checklist / FMS benchmark.")
    print("\n  Change that password from the Users screen before anyone else logs in.")
