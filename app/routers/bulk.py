"""Bulk import screens.

Two steps on purpose: upload and see exactly what will be created, then
confirm. Nothing is written until the second click.
"""
import base64
import io

from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_right
from ..models import User, Right
from ..services import bulk
from ..templating import templates

router = APIRouter(prefix="/bulk")
KINDS = {"delegation": "Delegation tasks", "checklist": "Checklist rules"}
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000


@router.get("", response_class=HTMLResponse)
def page(request: Request, kind: str = "delegation",
         user: User = Depends(require_right(Right.CREATE_TASK)),
         db: Session = Depends(get_db)):
    if kind not in KINDS:
        kind = "delegation"
    return templates.TemplateResponse(request, "bulk.html", {
        "user": user, "kind": kind, "kinds": KINDS, "parsed": None, "payload": "",
    })


@router.get("/template/{kind}")
def download_template(kind: str,
                      user: User = Depends(require_right(Right.CREATE_TASK)),
                      db: Session = Depends(get_db)):
    if kind not in KINDS:
        raise HTTPException(404, "Unknown template")
    data = bulk.template(db, user.org_id, kind)
    name = f"GCS-{kind}-template.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/preview", response_class=HTMLResponse)
async def preview(request: Request, kind: str = Form("delegation"),
                  file: UploadFile = File(...),
                  user: User = Depends(require_right(Right.CREATE_TASK)),
                  db: Session = Depends(get_db)):
    if kind not in KINDS:
        raise HTTPException(400, "Unknown import type")

    blob = await file.read()
    if len(blob) > MAX_BYTES:
        raise HTTPException(400, "That file is over 5 MB — split it into two.")
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Please upload the .xlsx template.")

    try:
        parsed = bulk.parse(db, user.org_id, kind, blob)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if len(parsed.rows) > MAX_ROWS:
        raise HTTPException(
            400, f"{len(parsed.rows)} rows is too many in one go — "
                 f"the limit is {MAX_ROWS}. Split the file.")

    # hold the file in the form so the confirm step re-parses the same bytes,
    # rather than trusting a preview the browser could have edited
    return templates.TemplateResponse(request, "bulk.html", {
        "user": user, "kind": kind, "kinds": KINDS, "parsed": parsed,
        "payload": base64.b64encode(blob).decode(),
        "filename": file.filename,
    })


@router.post("/commit")
async def commit(request: Request, kind: str = Form(...), payload: str = Form(...),
                 user: User = Depends(require_right(Right.CREATE_TASK)),
                 db: Session = Depends(get_db)):
    if kind not in KINDS:
        raise HTTPException(400, "Unknown import type")
    try:
        blob = base64.b64decode(payload)
    except Exception:
        raise HTTPException(400, "The upload was lost — please choose the file again.")

    parsed = bulk.parse(db, user.org_id, kind, blob)
    made = bulk.commit(db, user.org_id, user, parsed)

    dest = ("/tasks?scope=assigned&status=open" if kind == "delegation"
            else "/recurring")
    return RedirectResponse(f"{dest}&imported={made}" if "?" in dest
                            else f"{dest}?imported={made}", status_code=303)
