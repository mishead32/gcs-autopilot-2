import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Postgres: set DATABASE_URL=postgresql+psycopg://user:pass@host/db
# Vercel/Neon hand you a "postgres://..." URL — normalise it for SQLAlchemy.
_raw = os.getenv("DATABASE_URL", "")
if _raw.startswith("postgres://"):
    _raw = _raw.replace("postgres://", "postgresql+psycopg://", 1)
elif _raw.startswith("postgresql://"):
    _raw = _raw.replace("postgresql://", "postgresql+psycopg://", 1)

DATABASE_URL = _raw or f"sqlite:///{BASE_DIR / 'midap.db'}"

SECRET_KEY = os.getenv("MIDAP_SECRET", "change-me-in-production-gcs-midap")
CRON_SECRET = os.getenv("CRON_SECRET", "")

# On Vercel (and most serverless hosts) the filesystem is read-only apart from
# /tmp, and /tmp is wiped between invocations. Attachments therefore cannot be
# stored on the instance — the UI hides the upload field when this is true.
SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
EPHEMERAL_STORAGE = SERVERLESS

# UPLOAD_DIR can be pointed anywhere — useful on a VM where attachments should
# live on a data volume rather than beside the code.
_upload_env = os.getenv("UPLOAD_DIR", "")
if _upload_env:
    UPLOAD_DIR = Path(_upload_env)
elif SERVERLESS:
    UPLOAD_DIR = Path("/tmp/midap-uploads")
else:
    UPLOAD_DIR = BASE_DIR / "uploads"
try:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

APP_NAME = os.getenv("APP_NAME", "GCS Autopilot")
TIMEZONE = "Asia/Kolkata"

# A banner shown at the top of every page, e.g. "Demo — data resets"
BANNER = os.getenv("MIDAP_BANNER", "")
