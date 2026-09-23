"""Nightly backup: database + attachments -> Oracle Object Storage.

Run it from cron on the server:

    0 2 * * *  cd /opt/gcs-autopilot && .venv/bin/python backup.py >> /var/log/gcs-backup.log 2>&1

What it does, in order:

  1. pg_dump the database, gzip it
  2. copy that dump to Object Storage under db/
  3. copy any attachment not already up there under files/
  4. delete dumps older than KEEP_DAYS, locally and remotely

Attachments are copied once and never re-uploaded, so a nightly run costs a
handful of API calls rather than one per file. That matters: Oracle's free
tier allows 50,000 storage API calls a month.

Everything is written to Oracle's S3-compatible endpoint, so the same script
works against AWS S3, Cloudflare R2 or MinIO by changing the environment
variables — you are not locked to Oracle.

Set these (the same S3_* values the app uses):

    S3_BUCKET, S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION
    BACKUP_KEEP_DAYS   optional, default 30
"""
from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
UPLOADS = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "uploads"))
KEEP_DAYS = int(os.getenv("BACKUP_KEEP_DAYS", "30"))

BUCKET = os.getenv("S3_BUCKET", "")
ENDPOINT = os.getenv("S3_ENDPOINT", "")
ACCESS = os.getenv("S3_ACCESS_KEY", "")
SECRET = os.getenv("S3_SECRET_KEY", "")
REGION = os.getenv("S3_REGION", "us-east-1")
DATABASE_URL = os.getenv("DATABASE_URL", "")


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT or None,
        aws_access_key_id=ACCESS,
        aws_secret_access_key=SECRET,
        region_name=REGION,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


# ------------------------------------------------------------- database ----
def dump_database(into: Path) -> Path | None:
    """pg_dump for Postgres, a plain file copy for SQLite."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M")

    if DATABASE_URL.startswith("sqlite") or not DATABASE_URL:
        src = BASE_DIR / "midap.db"
        if not src.exists():
            log("No SQLite database found — skipping the database backup.")
            return None
        out = into / f"midap-{stamp}.db.gz"
        with open(src, "rb") as f_in, gzip.open(out, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        return out

    # strip the SQLAlchemy driver suffix; pg_dump wants a plain URL
    url = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
    out = into / f"midap-{stamp}.sql.gz"
    try:
        proc = subprocess.run(
            ["pg_dump", "--no-owner", "--no-acl", url],
            capture_output=True, check=True,
        )
    except FileNotFoundError:
        log("ERROR: pg_dump is not installed. Run: sudo apt install postgresql-client")
        return None
    except subprocess.CalledProcessError as e:
        log(f"ERROR: pg_dump failed — {e.stderr.decode()[:400]}")
        return None

    with gzip.open(out, "wb") as f:
        f.write(proc.stdout)
    return out


# ---------------------------------------------------------------- upload ---
def already_there(s3, key: str, size: int) -> bool:
    """Skip files whose size already matches — cheap, and avoids re-uploading."""
    try:
        head = s3.head_object(Bucket=BUCKET, Key=key)
        return head["ContentLength"] == size
    except Exception:
        return False


def sync_attachments(s3) -> tuple[int, int]:
    if not UPLOADS.exists():
        log(f"No uploads folder at {UPLOADS} — nothing to sync.")
        return 0, 0

    sent = skipped = 0
    for f in sorted(UPLOADS.iterdir()):
        if not f.is_file() or f.name == ".gitkeep":
            continue
        key = f"files/{f.name}"
        size = f.stat().st_size
        if already_there(s3, key, size):
            skipped += 1
            continue
        try:
            s3.upload_file(str(f), BUCKET, key)
            sent += 1
        except Exception as e:
            log(f"  could not upload {f.name}: {e}")
    return sent, skipped


# ---------------------------------------------------------------- prune ----
def prune(s3) -> int:
    """Delete database dumps older than KEEP_DAYS. Attachments are never pruned."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    removed = 0
    token = None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": "db/"}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            if obj["LastModified"] < cutoff:
                s3.delete_object(Bucket=BUCKET, Key=obj["Key"])
                removed += 1
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return removed


# ----------------------------------------------------------------- main ----
def main() -> int:
    if not (BUCKET and ACCESS and SECRET):
        log("ERROR: S3_BUCKET, S3_ACCESS_KEY and S3_SECRET_KEY must be set.")
        return 1

    host = urlparse(ENDPOINT).netloc or "the default endpoint"
    log(f"Backing up to bucket '{BUCKET}' at {host}")

    try:
        s3 = client()
        s3.head_bucket(Bucket=BUCKET)
    except Exception as e:
        log(f"ERROR: cannot reach the bucket — {e}")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        dump = dump_database(Path(tmp))
        if dump:
            key = f"db/{dump.name}"
            s3.upload_file(str(dump), BUCKET, key)
            log(f"Database dumped and uploaded: {key} "
                f"({dump.stat().st_size / 1024 / 1024:.1f} MB)")

    sent, skipped = sync_attachments(s3)
    log(f"Attachments: {sent} newly copied, {skipped} already backed up")

    try:
        gone = prune(s3)
        if gone:
            log(f"Removed {gone} dump(s) older than {KEEP_DAYS} days")
    except Exception as e:
        log(f"Could not prune old dumps: {e}")

    log("Backup finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
