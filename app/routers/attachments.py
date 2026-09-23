"""Attachment upload and download.

The browser uploads straight to the bucket, so a 9 MB phone photo never has to
pass through the web server. Three steps:

  1. POST /tasks/{id}/attach/ticket   -> app validates and returns a signed URL
  2. PUT  <that signed URL>           -> browser sends the bytes to R2 directly
  3. POST /tasks/{id}/attach/confirm  -> app verifies the object and saves a row

Step 3 re-checks the stored size against the bucket, so a browser that lies
about the file size in step 1 still can't sneak an oversized file in.

Without S3 configured, /tasks/{id}/attach/local takes an ordinary multipart
upload and writes it to disk instead.
"""
from fastapi import APIRouter, Depends, Request, HTTPException, UploadFile, File
from fastapi.responses import (RedirectResponse, FileResponse, JSONResponse,
                               Response)
from sqlalchemy.orm import Session

from ..config import UPLOAD_DIR
from ..db import get_db
from ..deps import current_user, can_view_task
from ..models import Task, TaskStatus, Attachment, User, Right
from ..services import storage

router = APIRouter()


def _task_for_upload(db: Session, user: User, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task or task.org_id != user.org_id or not can_view_task(user, task):
        raise HTTPException(404, "Task not found")
    # the doer attaches proof; the assigner attaches briefing material
    if not (task.doer_id == user.id or task.assigner_id == user.id
            or user.has(Right.EDIT_TASK)):
        raise HTTPException(403, "You can't attach files to this task")
    if task.status == TaskStatus.CANCELLED:
        raise HTTPException(400, "This task is closed")
    return task


@router.get("/attach/config")
def attach_config(user: User = Depends(current_user)):
    """The browser asks what it's allowed to send before showing the picker."""
    return {
        "mode": storage.mode(),
        "enabled": storage.uploads_available(),
        "max_mb": storage.MAX_UPLOAD_MB,
        "accept": storage.ACCEPT_ATTR,
        "allowed": sorted(storage.ALLOWED_TYPES),
    }


@router.post("/tasks/{task_id}/attach/ticket")
async def attach_ticket(task_id: int, request: Request,
                        user: User = Depends(current_user),
                        db: Session = Depends(get_db)):
    task = _task_for_upload(db, user, task_id)
    if not storage.S3_ENABLED:
        raise HTTPException(400, "Direct upload is not configured on this server")

    body = await request.json()
    filename = (body.get("filename") or "").strip()
    content_type = (body.get("content_type") or "").strip()
    size = int(body.get("size") or 0)

    err = storage.check(filename, content_type, size)
    if err:
        raise HTTPException(400, err)

    ticket = storage.upload_ticket(task.id, filename, content_type)
    return {"url": ticket.url, "key": ticket.key,
            "method": ticket.method, "headers": ticket.headers}


@router.post("/tasks/{task_id}/attach/confirm")
async def attach_confirm(task_id: int, request: Request,
                         user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    task = _task_for_upload(db, user, task_id)
    body = await request.json()
    key = (body.get("key") or "").strip()
    filename = (body.get("filename") or "").strip()
    content_type = (body.get("content_type") or "").strip()

    if not key.startswith(f"tasks/{task.id}/"):
        raise HTTPException(400, "That upload doesn't belong to this task")
    if content_type not in storage.ALLOWED_TYPES:
        raise HTTPException(400, "File type not allowed")

    # trust the bucket, not the browser
    real_size = storage.head(key)
    if real_size is None:
        raise HTTPException(400, "The upload didn't arrive — please try again")
    if real_size > storage.MAX_UPLOAD_BYTES:
        storage.delete(key)
        raise HTTPException(400, f"That file is over {storage.MAX_UPLOAD_MB} MB")

    att = Attachment(task_id=task.id, uploaded_by_id=user.id,
                     filename=filename[:250], stored_name=key,
                     size=real_size, content_type=content_type, storage="s3")
    db.add(att)
    db.commit()
    return {"id": att.id, "filename": att.filename, "size": att.size,
            "size_label": att.size_label, "is_image": att.is_image}


@router.post("/tasks/{task_id}/attach/local")
async def attach_local(task_id: int, files: list[UploadFile] = File(default=[]),
                       user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    """Plain multipart upload, used when there is no bucket configured."""
    task = _task_for_upload(db, user, task_id)
    if storage.mode() not in ("local", "db"):
        raise HTTPException(400, "File uploads are not available on this server")

    saved = 0
    for f in files or []:
        if not f.filename:
            continue
        data = await f.read()
        err = storage.check(f.filename, f.content_type or "", len(data))
        if err:
            raise HTTPException(400, err)
        if storage.mode() == "db":
            db.add(Attachment(task_id=task.id, uploaded_by_id=user.id,
                              filename=f.filename[:250],
                              stored_name=storage.safe_key(task.id, f.filename),
                              size=len(data), content_type=f.content_type or "",
                              storage="db", data=data))
        else:
            key = storage.save_local(task.id, f.filename, data)
            db.add(Attachment(task_id=task.id, uploaded_by_id=user.id,
                              filename=f.filename[:250], stored_name=key,
                              size=len(data), content_type=f.content_type or "",
                              storage="local"))
        saved += 1
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/attach/upload")
async def attach_one(task_id: int, file: UploadFile = File(...),
                     user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    """One file, one JSON answer.

    This is what a pasted screenshot uses: a clipboard image is a Blob with no
    entry in a file input, so the plain form post can't carry it. The uploader
    sends it here instead and gets a result it can show in place.
    """
    task = _task_for_upload(db, user, task_id)
    if storage.mode() not in ("local", "db"):
        raise HTTPException(400, "This server uploads straight to storage instead")

    data = await file.read()
    name = (file.filename or "pasted.png").strip()
    err = storage.check(name, file.content_type or "", len(data))
    if err:
        raise HTTPException(400, err)

    if storage.mode() == "db":
        att = Attachment(task_id=task.id, uploaded_by_id=user.id,
                         filename=name[:250], stored_name=storage.safe_key(task.id, name),
                         size=len(data), content_type=file.content_type or "",
                         storage="db", data=data)
    else:
        key = storage.save_local(task.id, name, data)
        att = Attachment(task_id=task.id, uploaded_by_id=user.id,
                         filename=name[:250], stored_name=key, size=len(data),
                         content_type=file.content_type or "", storage="local")
    db.add(att)
    db.commit()
    return {"id": att.id, "filename": att.filename, "size": att.size,
            "size_label": att.size_label, "is_image": att.is_image}


def _from_db(att: Attachment, inline: bool) -> Response:
    """Serve a file that lives in the database row itself."""
    if att.data is None:
        raise HTTPException(404, "That file is no longer stored")
    disposition = "inline" if inline else "attachment"
    return Response(
        content=att.data,
        media_type=att.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'{disposition}; filename="{att.filename}"'},
    )


def _fetch(attachment_id: int, user: User, db: Session) -> Attachment:
    att = db.get(Attachment, attachment_id)
    if not att or not can_view_task(user, att.task):
        raise HTTPException(404, "Not found")
    return att


@router.get("/attachments/{attachment_id}")
def download(attachment_id: int, user: User = Depends(current_user),
             db: Session = Depends(get_db)):
    att = _fetch(attachment_id, user, db)
    if att.storage == "db":
        return _from_db(att, inline=False)
    if att.storage == "s3":
        url = storage.download_url(att.stored_name, att.filename)
        if not url:
            raise HTTPException(503, "File storage is not reachable right now")
        return RedirectResponse(url, status_code=307)
    path = UPLOAD_DIR / att.stored_name
    if not path.exists():
        raise HTTPException(404, "That file is no longer on the server")
    return FileResponse(path, filename=att.filename)


@router.get("/attachments/{attachment_id}/view")
def view(attachment_id: int, user: User = Depends(current_user),
         db: Session = Depends(get_db)):
    """Same file, shown in the browser instead of downloaded — for previews."""
    att = _fetch(attachment_id, user, db)
    if att.storage == "db":
        return _from_db(att, inline=True)
    if att.storage == "s3":
        url = storage.download_url(att.stored_name, att.filename, inline=True)
        if not url:
            raise HTTPException(503, "File storage is not reachable right now")
        return RedirectResponse(url, status_code=307)
    path = UPLOAD_DIR / att.stored_name
    if not path.exists():
        raise HTTPException(404, "That file is no longer on the server")
    return FileResponse(path, media_type=att.content_type or None)


@router.post("/attachments/{attachment_id}/delete")
def remove(attachment_id: int, user: User = Depends(current_user),
           db: Session = Depends(get_db)):
    att = _fetch(attachment_id, user, db)
    # only whoever uploaded it, or someone who can edit tasks
    if att.uploaded_by_id != user.id and not user.has(Right.EDIT_TASK):
        raise HTTPException(403, "You can only remove files you uploaded")
    if att.task.status in (TaskStatus.COMPLETED,) and not user.has(Right.EDIT_TASK):
        raise HTTPException(400, "This task is closed — the file is part of the record")

    task_id = att.task_id
    storage.delete(att.stored_name)
    db.delete(att)
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)
