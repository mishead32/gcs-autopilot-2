"""Push the database into the Google Sheet. Run by the scheduler, or by hand.

    python sync_sheet.py
"""
import sys
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Organization
from app.services import sheets

if not sheets.enabled():
    print("Google Sheet is not configured — set GOOGLE_SHEET_ID and "
          "GOOGLE_SERVICE_ACCOUNT.")
    sys.exit(0)

with SessionLocal() as db:
    org = db.scalar(select(Organization))
    if org is None:
        print("No organisation in the database yet — nothing to sync.")
        sys.exit(0)

    result = sheets.sync(db, org.id)

if result.get("ok"):
    print(f"Synced {result['total']} rows at {result['at']}")
    for tab, n in result["counts"].items():
        print(f"  {tab:14} {n}")
else:
    print("Sync failed:", result.get("error") or result.get("errors"))
    sys.exit(1)
