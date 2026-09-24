"""One-shot confirmations — "Task assigned", "Audit recorded".

Kept in the signed session cookie rather than the URL, so a confirmation
cannot be produced by pasting a link, cannot survive a refresh, and never
leaks into a bookmark or a shared address.

set() is called just before a redirect; base.html pops whatever is waiting
and shows it once as a toast.
"""
from fastapi import Request

KEY = "flash"

# What each kind of confirmation looks like. Keeping the wording here rather
# than at every call site means "Marked complete" reads the same everywhere.
KINDS = {
    "assigned":  ("ok", "Task assigned"),
    "submitted": ("ok", "Sent for audit"),
    "completed": ("ok", "Marked complete"),
    "audited":   ("ok", "Audit recorded"),
    "reopened":  ("warn", "Sent back for rework"),
    "flagged":   ("bad", "Flagged as false marking"),
    "followed":  ("ok", "Follow-up recorded"),
    "unfollowed": ("warn", "Follow-up removed"),
    "saved":     ("ok", "Saved"),
    "welcome":   ("ok", "Signed in"),
}


def set(request: Request, kind: str, detail: str = "") -> None:
    """Queue a confirmation for the page the user is about to land on."""
    if kind not in KINDS:
        return
    try:
        request.session[KEY] = {"kind": kind, "detail": detail}
    except Exception:
        # No session (a background call, a test client without middleware).
        # A missing toast must never break the thing it was confirming.
        pass


def pop(request: Request) -> dict | None:
    """Read and clear. A confirmation is shown once, never twice."""
    try:
        data = request.session.pop(KEY, None)
    except Exception:
        return None
    if not data or data.get("kind") not in KINDS:
        return None
    tone, text = KINDS[data["kind"]]
    return {"tone": tone, "text": text, "detail": data.get("detail", "")}
