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


# ---------------------------------------------------------- Task Detective --
# The AI second opinion on finished work. Off until a key is set, so the
# software runs exactly as before on a machine that has none.
#
# The key is read from the environment and never written to the database, a
# template or a log — same rule as every other secret here.
AI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Google retires a model and the old name starts answering 404 — which is
# what happened to gemini-2.5-flash. The name is settable here so a retirement
# can be fixed from Render without a deploy, and the detective also follows
# the replacement the error message names, so it keeps working either way.
AI_MODEL = os.getenv("AI_MODEL", "gemini-3.8-flash").strip()
AI_ENABLED = os.getenv("AI_AUDIT", "on").strip().lower() not in ("off", "0", "no")
# How many attached files to show it, and how large a file to bother sending.
# A phone photo is a couple of megabytes; six of them is a slow request and a
# big one, and the seventh almost never changes the verdict.
AI_MAX_FILES = int(os.getenv("AI_MAX_FILES", "6") or 6)
AI_MAX_FILE_MB = float(os.getenv("AI_MAX_FILE_MB", "4") or 4)
AI_TIMEOUT = float(os.getenv("AI_TIMEOUT", "45") or 45)
# WHICH work the detective looks at, and HOW FAR BACK.
#
# Delegation only to begin with: a checklist job is the same few words every
# day and the proof is the same screenshot, so a machine reading them adds
# little and spends the whole free allowance doing it. Delegation is where
# the proof is different every time and where a wrong attachment actually
# hides something. Widen it here when that stops being true — "delegation,
# checklist,fms" is accepted.
AI_SOURCES = tuple(
    s.strip().lower() for s in
    os.getenv("AI_SOURCES", "delegation").split(",") if s.strip())
# Nothing finished before this date is looked at. Years of old work would
# cost a fortune in allowance to check and tells nobody anything they can
# still act on. Written YYYY-MM-DD; blank means no limit.
AI_SINCE = os.getenv("AI_CHECK_FROM", "2026-10-03").strip()
