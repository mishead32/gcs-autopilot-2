"""API keys for other programs. The key is shown once; only its hash is kept."""
from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import ApiKey, User

PREFIX = "gcs_"
SCOPES = {
    "scores": "Read scores and reports",
    "tasks_read": "Read tasks",
    "tasks_write": "Create tasks",
    "flows_write": "Start FMS runs",
}
LIMIT_PER_MINUTE = 120
_hits: dict[int, deque] = defaultdict(deque)


def digest(key: str) -> str:
    return hashlib.sha256((key or "").encode()).hexdigest()


def make(db: Session, org_id: int, name: str, scopes: list[str],
         acting: User, made_by: User) -> tuple[ApiKey, str]:
    """A new key. Returns (row, the key itself — the only time it exists)."""
    key = PREFIX + secrets.token_urlsafe(32)
    row = ApiKey(org_id=org_id, name=name.strip()[:80], prefix=key[:10],
                 key_hash=digest(key),
                 scopes=",".join(s for s in scopes if s in SCOPES),
                 acting_user_id=acting.id, created_by_id=made_by.id)
    db.add(row)
    db.commit()
    return row, key


def lookup(db: Session, raw: str) -> ApiKey | None:
    if not raw or not raw.startswith(PREFIX):
        return None
    row = db.scalar(select(ApiKey).where(ApiKey.key_hash == digest(raw)))
    if not row or row.revoked_at is not None:
        return None
    if not row.acting_user or not row.acting_user.active:
        return None
    return row


def too_many(key_id: int, now: float | None = None) -> bool:
    """More than LIMIT_PER_MINUTE calls in the last minute."""
    now = now if now is not None else time.monotonic()
    q = _hits[key_id]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= LIMIT_PER_MINUTE:
        return True
    q.append(now)
    return False


def scopes_of(row: ApiKey) -> set[str]:
    return {s for s in (row.scopes or "").split(",") if s}


def touch(db: Session, row: ApiKey) -> None:
    row.last_used_at = clock.now()
    row.use_count = (row.use_count or 0) + 1
    db.commit()
