"""File storage.

Two backends, chosen automatically:

  R2 / S3   when S3_BUCKET and credentials are set. Files never pass through
            the web server: the browser PUTs straight to the bucket using a
            short-lived signed URL, and downloads use a signed GET. This is
            what makes it work on Vercel, where a request body is capped at
            about 4.5 MB and a phone photo can easily exceed that.

  local     otherwise. Files land in ./uploads, same as before, so nothing
            changes when you run it on your own PC.

Cloudflare R2 is S3-compatible, so the same code works for R2, AWS S3,
Backblaze B2 or MinIO — only the endpoint changes.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..config import UPLOAD_DIR, EPHEMERAL_STORAGE

# ---------------------------------------------------------------- config ---
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "")          # R2: https://<account>.r2.cloudflarestorage.com
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "")
S3_REGION = os.getenv("S3_REGION", "auto")          # R2 uses "auto"

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "10"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
UPLOAD_URL_TTL = 600        # 10 minutes to finish an upload
DOWNLOAD_URL_TTL = 300      # 5 minutes on a view link

# What staff may attach. Anything not listed is refused, on both sides.
ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",          # iPhone photos
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "text/csv": ".csv",
    "text/plain": ".txt",
}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic"}

ACCEPT_ATTR = ".jpg,.jpeg,.png,.webp,.heic,.pdf,.xlsx,.xls,.docx,.doc,.csv,.txt"

# The S3_* settings are also used by backup.py, which copies local files into
# object storage nightly. Setting them must NOT silently move live attachments
# off the disk — on a VM that would spend object-storage API calls on every
# photo view for no benefit. STORAGE_BACKEND decides, explicitly:
#
#   local  -> always the disk, even when S3 keys are present (VM deployments)
#   s3     -> always object storage, if the keys are there
#   db     -> inside the database itself
#   unset  -> object storage when keys exist, otherwise disk
#
# "db" exists for free hosts that give you no permanent disk: the container's
# filesystem is wiped on every restart, so a photo attached on Monday is gone
# by Tuesday. Putting the bytes in the database keeps them, at the cost of
# database space — fine for a pilot, and one setting to change later.
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "").strip().lower()

_have_keys = bool(S3_BUCKET and S3_ACCESS_KEY and S3_SECRET_KEY)
DB_STORAGE = STORAGE_BACKEND == "db"
if STORAGE_BACKEND == "local":
    S3_ENABLED = False
elif STORAGE_BACKEND == "s3":
    S3_ENABLED = _have_keys
elif DB_STORAGE:
    S3_ENABLED = False
else:
    S3_ENABLED = _have_keys


@dataclass
class UploadTicket:
    """Everything the browser needs to send one file straight to the bucket."""
    url: str
    key: str
    method: str = "PUT"
    headers: dict | None = None


def _client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT or None,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


# ------------------------------------------------------------ validation ---
def check(filename: str, content_type: str, size: int) -> str | None:
    """Returns an error message, or None when the file is acceptable."""
    if not filename.strip():
        return "The file has no name."
    if size <= 0:
        return "That file is empty."
    if size > MAX_UPLOAD_BYTES:
        return (f"{filename} is {size / 1024 / 1024:.1f} MB. "
                f"The limit is {MAX_UPLOAD_MB} MB per file.")
    if content_type not in ALLOWED_TYPES:
        return (f"{filename} is not a type we accept. "
                "Attach a photo, PDF, Excel or Word file.")
    return None


def is_image(content_type: str) -> bool:
    return content_type in IMAGE_TYPES


def safe_key(task_id: int, filename: str) -> str:
    """A collision-proof object key that keeps the original extension."""
    ext = Path(filename).suffix.lower()[:10]
    return f"tasks/{task_id}/{uuid.uuid4().hex}{ext}"


# ---------------------------------------------------------------- upload ---
def upload_ticket(task_id: int, filename: str, content_type: str) -> UploadTicket:
    """A signed URL the browser can PUT the file to. S3/R2 only."""
    key = safe_key(task_id, filename)
    url = _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=UPLOAD_URL_TTL,
    )
    return UploadTicket(url=url, key=key, headers={"Content-Type": content_type})


def save_local(task_id: int, filename: str, data: bytes) -> str:
    """Fallback path: write to disk and return the stored name."""
    key = f"{uuid.uuid4().hex}_{Path(filename).name}"
    (UPLOAD_DIR / key).write_bytes(data)
    return key


# -------------------------------------------------------------- download ---
def download_url(key: str, filename: str, inline: bool = False) -> str | None:
    """A short-lived private link. None means 'serve it from disk instead'."""
    if not S3_ENABLED:
        return None
    disposition = "inline" if inline else f'attachment; filename="{filename}"'
    return _client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": S3_BUCKET,
            "Key": key,
            "ResponseContentDisposition": disposition,
        },
        ExpiresIn=DOWNLOAD_URL_TTL,
    )


def delete(key: str) -> None:
    if S3_ENABLED:
        try:
            _client().delete_object(Bucket=S3_BUCKET, Key=key)
        except Exception:
            pass          # a missing object is not worth failing a delete over
    else:
        try:
            (UPLOAD_DIR / key).unlink(missing_ok=True)
        except OSError:
            pass


def head(key: str) -> int | None:
    """Actual stored size, used to verify what the browser really uploaded."""
    if not S3_ENABLED:
        p = UPLOAD_DIR / key
        return p.stat().st_size if p.exists() else None
    try:
        return _client().head_object(Bucket=S3_BUCKET, Key=key)["ContentLength"]
    except Exception:
        return None


# ----------------------------------------------------------------- state ---
def mode() -> str:
    if S3_ENABLED:
        return "s3"
    if DB_STORAGE:
        return "db"
    return "none" if EPHEMERAL_STORAGE else "local"


def uploads_available() -> bool:
    """False only when there is nowhere durable to put a file."""
    return mode() != "none"
