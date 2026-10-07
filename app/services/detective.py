"""Task Detective — a second pair of eyes on finished work.

The problem it exists for: somebody marks a task complete, attaches a
screenshot of something else entirely, or types "done" and nothing more, and
nobody notices until the auditor opens it three days later — if they open it
at all. One person checking a hundred finished tasks a week will not look
hard at every screenshot. A machine will look at all of them.

What it does NOT do, deliberately:

  * it does not close, reopen, approve or reject anything
  * it does not touch a score, a benchmark or a false mark
  * it does not stop a submission, or slow one down

It reads what was handed in, writes down what it saw, and says whether the
proof looks like proof. A person still decides. An AI quietly costing
somebody marks is how a scoring system loses the trust that makes it worth
having — so this one is advisory, in writing, and reversible by ignoring it.

It also must never break the thing it is watching. Every call is wrapped: a
missing key, a dead network, a rate limit or a reply in the wrong shape all
end as a stored "could not check", never as an error on the page the person
was using.
"""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import date, datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..config import (AI_KEY, AI_MODEL, AI_ENABLED, AI_MAX_FILES,
                      AI_MAX_FILE_MB, AI_TIMEOUT, AI_SOURCES, AI_SINCE,
                      AI_DAILY_LIMIT, UPLOAD_DIR)
from ..models import AiAudit, AiVerdict, Attachment, Task, TaskSource

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# What the model is told to do. Written as instructions to a careful clerk
# rather than a prompt full of adjectives: the useful output here is a
# specific observation ("the screenshot is a WhatsApp chat about a gym
# membership, the task is about a bus GPS") and not a score out of ten.
SYSTEM = """You check whether the proof attached to a finished work task
actually shows that the task was done.

You are given the task as it was assigned, what the person wrote when they
marked it complete, and the files they attached. Images are included as
images — look at them properly and describe what is actually in them.

Judge only one thing: does this evidence show THIS task being done?

Answer with one of these verdicts:
  ok         the files or the note show the work described
  weak       something was handed in, but it would not convince anyone
             — a blurry photo, a note with no detail, a file that could
             belong to any task
  unrelated  the proof is clearly of something else
  no_proof   nothing was attached and the note says nothing useful
             (for example just "done", "ok", "completed")

Rules you must follow:
  * Do not guess at what a file might contain. If an image is unreadable,
    say so and answer weak.
  * A task that never required proof, closed with a sensible note, is ok.
  * Do not comment on whether the work was late, or on the person. You are
    looking at the evidence, not running an appraisal.
  * Be concrete. "The screenshot shows a bank statement for a different
    account" is useful. "The proof seems insufficient" is not.
  * Write the remark for the manager who will read it. One or two plain
    sentences, no jargon, no preamble.

Reply with JSON only, in exactly this shape:
{"verdict": "ok|weak|unrelated|no_proof",
 "confidence": 0-100,
 "remark": "one or two plain sentences",
 "looked_at": "what you actually saw, e.g. '1 screenshot of a payment receipt'"}"""

# Files we can hand over as text rather than as an image.
TEXTY = ("text/plain", "text/csv", "application/csv", "text/markdown")


def available() -> bool:
    """Whether the detective can run at all."""
    return bool(AI_ENABLED and AI_KEY)


# ------------------------------------------------------------- what it sees -
# The detective does not look at everything, and the limits live here so the
# page, the backfill, the retry and the hook that fires on submission all
# agree about what is in and what is out. Two of them disagreeing is how a
# page ends up saying "308 never checked" about work nothing will ever check.
def _since() -> date | None:
    try:
        return date.fromisoformat(AI_SINCE) if AI_SINCE else None
    except ValueError:
        return None


WATCHED = tuple(s for s in TaskSource if s.value in AI_SOURCES)


def scope_words() -> str:
    """What it is watching, in words, for the page to print."""
    names = {TaskSource.DELEGATION: "Delegation",
             TaskSource.RECURRING: "Checklist", TaskSource.FLOW: "FMS"}
    kinds = " and ".join(names.get(s, s.value) for s in WATCHED) or "nothing"
    start = _since()
    return (f"{kinds} work finished on or after {start:%d %b %Y}"
            if start else f"{kinds} work")


def watches(task: Task) -> bool:
    """Is this one task the detective's business?"""
    if task.source not in WATCHED:
        return False
    start = _since()
    if start and (task.submitted_at is None or task.submitted_at.date() < start):
        return False
    return True


def in_scope(q):
    """The same rule as a SQL filter, for a query over tasks."""
    q = q.where(Task.source.in_(WATCHED)) if WATCHED else q.where(False)
    start = _since()
    if start:
        q = q.where(Task.submitted_at >= datetime.combine(
            start, datetime.min.time()))
    return q


# ---------------------------------------------------------- the day's budget -
# Google's free allowance is about twenty requests a day. Past it, every call
# comes back 429 and gets stored as "could not check" — a page full of
# failures that reads as "the AI looked and had nothing to say". So the
# software counts, stops at the line, and leaves the rest for tomorrow.
#
# A task skipped for budget is NOT given a row. It stays in "never checked",
# which is the honest state and the thing the daily top-up looks for.
_quota_hit_on: date | None = None     # the day Google said no more


def spent_today(db: Session, org_id: int) -> int:
    start = datetime.combine(clock.today(), datetime.min.time())
    return db.scalar(
        select(func.count()).select_from(AiAudit)
        .where(AiAudit.org_id == org_id, AiAudit.created_at >= start)) or 0


def budget_left(db: Session, org_id: int) -> int:
    """How many more checks may be made today. A large number means no cap."""
    if _quota_hit_on == clock.today():
        return 0                       # Google has already said no for today
    if not AI_DAILY_LIMIT:
        return 1_000_000
    return max(0, AI_DAILY_LIMIT - spent_today(db, org_id))


def budget_words(db: Session, org_id: int) -> str:
    """One sentence for the page about where the day's allowance stands."""
    if not AI_DAILY_LIMIT:
        return ""
    used = spent_today(db, org_id)
    left = budget_left(db, org_id)
    if left:
        return (f"{used} of {AI_DAILY_LIMIT} checks used today. "
                f"{left} left — the rest of the queue is picked up tomorrow, "
                "oldest first.")
    return (f"Today's {AI_DAILY_LIMIT} checks are used up. Checking starts "
            "again after midnight, oldest first — nothing is lost.")


def why_not() -> str:
    """One plain sentence for the page when it cannot run."""
    if not AI_ENABLED:
        return ("Task Detective is switched off. Remove the AI_AUDIT setting "
                "on the server, or set it to on, to switch it back.")
    if not AI_KEY:
        return ("Task Detective needs a Gemini API key. Add one as "
                "GEMINI_API_KEY in the server's environment settings and it "
                "starts working on the next task that is marked complete. "
                "Until then nothing is sent anywhere.")
    return ""


# ------------------------------------------------------------ the prompt ---
def _task_brief(task: Task) -> str:
    bits = [f"TASK: {task.title}"]
    if task.details:
        bits.append(f"WHAT IT ASKED FOR: {task.details}")
    bits.append(f"KIND OF WORK: {task.source.value}")
    bits.append(f"PLANNED FOR: {task.due_at:%d %b %Y, %I:%M %p}")
    bits.append("PROOF WAS: " + ("required" if task.requires_attachment
                                 else "not required"))
    note = (task.completion_note or "").strip()
    bits.append("WHAT THEY WROTE WHEN MARKING IT DONE: "
                + (note if note else "(nothing)"))
    return "\n".join(bits)


def _bytes_of(att: Attachment) -> bytes | None:
    """The actual file, wherever it was put.

    On the live site attachments live in the database, so att.data is right
    there. On a machine with a disk they are files, and reading only att.data
    would hand the AI nothing but a file name — which is exactly the case
    this feature exists to catch, so it would fail silently at being useful.
    S3 is left alone on purpose: fetching from a bucket belongs behind the
    same signed-URL path everything else uses, and is not worth doing badly
    here.
    """
    if att.data:
        return att.data
    if (att.storage or "") == "local" and att.stored_name:
        try:
            path = UPLOAD_DIR / att.stored_name
            if path.is_file():
                return path.read_bytes()
        except OSError:
            return None
    return None


def _parts_for(task: Task, files: list[Attachment]) -> tuple[list[dict], int, int]:
    """The task, then each file — images as images, text as text.

    A file too large to send, or of a kind nothing can read, is still
    MENTIONED by name and type. Staying silent about it would let a person
    attach a 40MB video and get "no proof attached" back, which is both
    wrong and the sort of wrong that destroys confidence in the whole thing.
    """
    parts: list[dict] = [{"text": _task_brief(task)}]
    cap = int(AI_MAX_FILE_MB * 1024 * 1024)
    images = seen = 0

    for att in files[:AI_MAX_FILES]:
        seen += 1
        blob = _bytes_of(att)
        label = f"ATTACHED FILE: {att.filename} ({att.content_type or 'unknown type'}, {att.size or 0} bytes)"
        if not blob:
            parts.append({"text": label + " — the file itself could not be "
                                  "loaded, so judge it by its name only."})
            continue
        if len(blob) > cap:
            parts.append({"text": label + " — too large to open here, so "
                                  "judge it by its name only."})
            continue
        if att.is_image:
            parts.append({"text": label})
            parts.append({"inline_data": {
                "mime_type": att.content_type,
                "data": base64.b64encode(blob).decode("ascii")}})
            images += 1
        elif (att.content_type or "") in TEXTY:
            try:
                text = blob.decode("utf-8", "replace")[:4000]
            except Exception:
                text = ""
            parts.append({"text": label + "\nITS CONTENTS:\n" + text})
        else:
            parts.append({"text": label + " — this kind of file cannot be "
                                  "opened here. Say so rather than guessing "
                                  "what is in it."})

    extra = len(files) - min(len(files), AI_MAX_FILES)
    if extra > 0:
        parts.append({"text": f"({extra} further file(s) were attached and "
                              f"not shown here.)"})
    if not files:
        parts.append({"text": "NO FILES WERE ATTACHED."})
    return parts, seen, images


# ----------------------------------------------------------- the answer ----
def _read_reply(payload: dict) -> dict:
    """Pull our four fields out of whatever came back.

    Models wrap JSON in prose and in ``` fences however firmly you ask them
    not to, so this digs the object out rather than trusting the shape.
    """
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        raise ValueError("the reply had no text in it")

    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-z]*\s*|\s*```$", "", body, flags=re.I | re.S)
    try:
        data = json.loads(body)
    except Exception:
        match = re.search(r"\{.*\}", body, re.S)
        if not match:
            raise ValueError("the reply was not JSON")
        data = json.loads(match.group(0))

    raw = str(data.get("verdict", "")).strip().lower()
    try:
        verdict = AiVerdict(raw)
    except ValueError:
        raise ValueError(f"unknown verdict {raw!r}")
    if verdict == AiVerdict.ERROR:
        raise ValueError("the model may not return 'error' as a verdict")

    try:
        confidence = max(0, min(100, int(float(data.get("confidence", 0)))))
    except Exception:
        confidence = 0
    return {
        "verdict": verdict,
        "confidence": confidence,
        "remark": str(data.get("remark", "")).strip()[:2000],
        "looked_at": str(data.get("looked_at", "")).strip()[:500],
    }


# The model actually in use. It starts as whatever was configured and moves
# only when Google tells us the configured one is gone — see _call. Module
# level on purpose: once one check has been told the new name, every later
# check uses it, instead of every single one paying for the same 404.
_live_model = AI_MODEL

# "models/gemini-2.5-flash is no longer available ... use models/gemini-3.8-flash"
_REPLACEMENT = re.compile(r"models/([A-Za-z0-9.\-_]+)")


def current_model() -> str:
    return _live_model


def _retirement(status: int, text: str) -> str | None:
    """The model Google says to use instead, if that is what went wrong.

    Google retires a model and the old name answers 404 with the new name in
    the message. Reading it means a retirement costs one failed check rather
    than every check until somebody notices — which is exactly what happened
    the first time: seven hundred stored "could not check" rows, all the same
    404, while the page looked like the AI simply had no opinion.
    """
    if status != 404:
        return None
    low = text.lower()
    if "no longer available" not in low and "not found" not in low:
        return None
    names = [n for n in _REPLACEMENT.findall(text) if n != _live_model]
    return names[-1] if names else None


def _call(parts: list[dict], client: httpx.Client | None = None) -> dict:
    global _live_model
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 600,
                             "responseMimeType": "application/json"},
    }
    owned = client is None
    client = client or httpx.Client(timeout=AI_TIMEOUT)
    try:
        for attempt in (1, 2):
            r = client.post(API.format(model=_live_model), json=body,
                            headers={"x-goog-api-key": AI_KEY,
                                     "content-type": "application/json"})
            if r.status_code == 200:
                return r.json()
            moved = _retirement(r.status_code, r.text) if attempt == 1 else None
            if moved:
                # Say it in the log, once, so whoever reads it knows the name
                # in the settings is out of date even though nothing broke.
                print(f"Task Detective: {_live_model} is retired, "
                      f"using {moved} instead")
                _live_model = moved
                continue
            # The message is kept because a rate limit, a bad key and a dead
            # model need different answers from whoever reads it, and "it
            # failed" tells them none of the three.
            raise ValueError(f"the AI service answered {r.status_code}: "
                             f"{r.text[:200]}")
    finally:
        if owned:
            client.close()


# -------------------------------------------------------------- the run ----
def review(db: Session, task: Task,
           client: httpx.Client | None = None) -> AiAudit | None:
    """Check one finished task and store what was found.

    Returns a row even when the check failed — a stored "could not check" is
    a fact somebody can act on, while silence looks identical to "nothing
    wrong here".

    Returns None, and stores NOTHING, when the day's allowance is gone. That
    is not a failed check and must not be filed as one: the task stays in
    "never checked", which is both the truth and what tomorrow's top-up
    looks for.
    """
    if budget_left(db, task.org_id) <= 0:
        return None

    started = time.monotonic()
    files = list(db.scalars(
        select(Attachment).where(Attachment.task_id == task.id)
        .order_by(Attachment.id)).all())

    row = AiAudit(org_id=task.org_id, task_id=task.id, model=current_model(),
                  files_seen=len(files))
    try:
        if not available():
            raise ValueError(why_not())
        parts, seen, images = _parts_for(task, files)
        row.files_seen, row.images_seen = seen, images
        found = _read_reply(_call(parts, client))
        row.model = current_model()       # it may have moved mid-call
        row.verdict = found["verdict"]
        row.confidence = found["confidence"]
        row.remark = found["remark"]
        row.looked_at = found["looked_at"]
    except Exception as e:
        # Google saying "you are over quota" is about the day, not about this
        # task. Stop for the day rather than spending the next hundred tasks
        # collecting the same message.
        global _quota_hit_on
        if "429" in str(e):
            _quota_hit_on = clock.today()
        row.verdict = AiVerdict.ERROR
        row.confidence = 0
        row.remark = str(e)[:2000]
        row.looked_at = ""
    row.took_ms = int((time.monotonic() - started) * 1000)
    db.add(row)
    db.commit()
    return row


def review_quietly(task_id: int) -> None:
    """Run a check in the background, and never let it reach the person.

    Called after somebody marks a task complete. It opens its own database
    session because the request's one is closed by the time this runs, and
    it swallows everything: a slow AI service must never be the reason a
    doer's submission appears to fail.
    """
    from ..db import SessionLocal
    try:
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            # Checked here as well as by every caller: this is the last gate
            # before somebody's free allowance is spent, and a caller that
            # forgets the rule should waste nothing.
            if task is not None and watches(task):
                review(db, task)
    except Exception:
        pass


def latest_for(db: Session, task_id: int) -> AiAudit | None:
    return db.scalar(select(AiAudit).where(AiAudit.task_id == task_id)
                     .order_by(AiAudit.created_at.desc(), AiAudit.id.desc())
                     .limit(1))


def latest_map(db: Session, task_ids: list[int]) -> dict[int, AiAudit]:
    """The newest check for each of these tasks, in one query.

    One query rather than one per row: a list of five hundred tasks each
    asking the database its own question is how a page that was fast becomes
    a page nobody opens.
    """
    if not task_ids:
        return {}
    out: dict[int, AiAudit] = {}
    rows = db.scalars(
        select(AiAudit).where(AiAudit.task_id.in_(task_ids))
        .order_by(AiAudit.created_at.asc(), AiAudit.id.asc())).all()
    for r in rows:
        out[r.task_id] = r          # later rows overwrite earlier ones
    return out


def pending_ids(db: Session, org_id: int, limit: int = 25) -> list[int]:
    """Finished work in scope that has never been checked, oldest first.

    Oldest first on purpose: a backlog worked newest-first leaves the oldest
    tasks permanently last in the queue and never checked at all.
    """
    checked = select(AiAudit.task_id).where(AiAudit.org_id == org_id)
    return list(db.scalars(in_scope(
        select(Task.id).where(Task.org_id == org_id,
                              Task.submitted_at.is_not(None),
                              Task.id.not_in(checked)))
        .order_by(Task.submitted_at.asc()).limit(limit)).all())


def top_up(batch: int = 5) -> int:
    """Spend a little of today's allowance on the oldest unchecked work.

    Called every few minutes by the background loop. With a free allowance
    of twenty a day, a backlog clears itself over a week and new work is
    looked at within a day — without anybody having to remember to press a
    button, which is the only way a queue like this ever actually empties.

    Deliberately a few at a time rather than the whole day's allowance at
    once: a task finished at nine in the morning should not find the day
    already spent on last week's backlog.
    """
    from ..db import SessionLocal
    from ..models import Organization

    if not available():
        return 0
    done = 0
    try:
        with SessionLocal() as db:
            for org_id in db.scalars(select(Organization.id)).all():
                left = budget_left(db, org_id)
                if left <= 0:
                    continue
                for task_id in pending_ids(db, org_id, min(batch, left)):
                    task = db.get(Task, task_id)
                    if task is None or not watches(task):
                        continue
                    if review(db, task) is None:
                        break           # allowance ran out mid-batch
                    done += 1
    except Exception as exc:
        # Never let this kill the loop it runs in.
        print(f"Task Detective top-up failed: {exc!r}")
    return done
