"""Saving a file against a task, wherever this install keeps its files.

There are three places an attachment can live — a bucket, the disk, or the
database — and the choice is made once, by an environment variable, for the
whole install. Every place that saves a file therefore has to ask the same
question and branch the same way, and until this module existed each of them
asked it slightly differently.

That is not a tidiness complaint. The new-delegation form asked
`if storage.mode() == "local"`, which is false on the live site — where
files live in the database — so every briefing document anybody attached
while handing work out was read, checked, and then silently dropped. The
form offered the field, the upload appeared to work, and the file was gone.
A single helper is the fix: one answer to "where does a file go", used by
everybody, so a backend the live site uses cannot be the one nobody handled.

S3 is deliberately not written to here. Uploading to a bucket goes through
the browser and a signed URL — see routers/attachments.py — precisely so a
nine-megabyte phone photo never passes through the web server, and quietly
doing it server-side here would undo that.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Attachment
from . import storage


def save(db: Session, task_id: int, uploaded_by_id: int, filename: str,
         content_type: str, data: bytes, *, briefing: bool = False,
         flow_instance_id: int | None = None,
         start_field: str | None = None) -> Attachment:
    """Put one file where this install keeps files, and record it.

    Added to the session but NOT committed: the caller is usually in the
    middle of creating the thing the file belongs to, and half a task with
    a file against it is worse than neither.

    `briefing` marks a file handed TO the doer rather than produced BY
    them — the bill to be paid, the voice note the task was dictated from.
    It keeps such a file out of `task.proof_files`, so nobody can close a
    task on the strength of the document they were given.
    """
    row = Attachment(
        task_id=task_id, uploaded_by_id=uploaded_by_id,
        filename=filename[:250], size=len(data),
        content_type=content_type or "", briefing=briefing,
        flow_instance_id=flow_instance_id,
        start_field=start_field[:160] if start_field else None,
    )
    if storage.mode() == "db":
        row.storage = "db"
        row.stored_name = storage.safe_key(task_id, filename)
        row.data = data
    else:
        # "local" and the no-durable-storage case both land here. The
        # second is a demo host with no disk worth keeping anything on,
        # and the callers check `uploads_available()` before offering the
        # field at all.
        row.storage = "local"
        row.stored_name = storage.save_local(task_id, filename, data)
    db.add(row)
    return row
