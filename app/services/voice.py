"""Dictating a delegated task: speech in, words out, names suggested.

Somebody walking the floor at the gym has a phone and no patience for a
form. They say "Rohit ko bolo ki Spa Kora ka bill kal tak clear kar de" and
the task should be most of the way written by the time they look at the
screen.

Three jobs, and only the first of them is the machine's:

  1. **The words.** Sent to Groq, which runs Whisper. Free, fast, and the
     one part that genuinely cannot be done here.

  2. **The name.** Whisper writes what it HEARS, phonetically, and it was
     trained mostly on English — so "Rohit Kumar" comes back as "Rohith",
     "Mohit", or "Rohit kumar" with a small k. Two things are done about
     that: the staff list is handed to Whisper as a prompt, which pulls its
     spelling towards names that exist, and what comes back is then matched
     against the real list allowing for near misses.

  3. **The date.** "kal tak", "by Friday", "do din mein" — picked out and
     offered as something to tap.

Both 2 and 3 are SUGGESTIONS and nothing else. The one failure worth
designing against is a task assigned to the wrong person, or due on the
wrong day, because a machine was confident — found three days later when
nothing has been done. So this module ranks and offers; a person confirms.
Nothing here ever fills the form in by itself.

The key lives in Render's environment and nowhere else — not the database,
not a template, not a log. Errors echoed back to the page are scrubbed, so
a provider that quotes the request back cannot put the key on a screen.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from .. import clock

# --------------------------------------------------------------- config --
KEY = os.getenv("GROQ_API_KEY", "").strip()
MODEL = os.getenv("VOICE_MODEL", "whisper-large-v3").strip()
URL = os.getenv("VOICE_URL",
                "https://api.groq.com/openai/v1/audio/transcriptions").strip()
TIMEOUT = float(os.getenv("VOICE_TIMEOUT", "60") or 60)

#: How the office actually speaks. Hindi and English in one sentence, so
#: no language is forced by default — forcing "en" makes Whisper translate
#: the Hindi half instead of writing it down, and forcing "hi" makes it
#: render the English half in Devanagari. Both are worse than letting it
#: decide, and a person who wants one or the other can say so on the form.
LANGUAGES = {
    "": "Hindi and English, as spoken",
    "en": "English only",
    "hi": "Hindi only",
}

#: Groq caps the prompt at about 224 tokens, so the staff list handed to
#: it has to stop somewhere. Names first, closest thing to useful.
PROMPT_NAMES = 60

#: Below this, a name is not a match — it is a coincidence of letters.
#: Tuned so "Rohith" finds Rohit Kumar and "Sunita" does not find Sunil.
NAME_FLOOR = 0.72

#: And a short word has to be almost exact. Four letters match four other
#: letters far too easily: "kar" in "kar de" found EA Karan, and a list of
#: people who were not mentioned is a list people stop reading.
SHORT = 5
SHORT_FLOOR = 0.9

#: The shortest run of letters worth comparing at all, on either side.
#: Below this a word is a preposition, not a name.
MIN_PART = 4

#: Words that turn up in every dictated instruction and are never the
#: person being named. Without these, ordinary Hinglish grammar matches
#: half the staff list.
STOP = {
    "kar", "karo", "karna", "kardo", "bolo", "bol", "bola", "dena", "dedo",
    "please", "pls", "kindly", "tell", "ask", "send", "check", "karke",
    "hai", "hain", "tak", "aur", "kal", "aaj", "liye", "wala", "wali",
    "kaam", "task", "bill", "file", "report", "the", "and", "for", "with",
    "that", "this", "from", "about", "today", "tomorrow",
}


def enabled() -> bool:
    return bool(KEY)


def status() -> str:
    """One sentence for the page about whether dictation can work.

    Reports the key's LENGTH and never the key. A wrong paste — a trailing
    space, half a key, the quotes copied with it — is the common failure,
    and it is diagnosable from the length alone.
    """
    if not KEY:
        return ("Dictation is off: no GROQ_API_KEY is set. Add it in "
                "Render's Environment tab and restart.")
    return (f"Dictation is on — {MODEL}, with a key of {len(KEY)} "
            "characters.")


def _safe(text: str) -> str:
    """Never let the key reach a screen, however an error quotes it back."""
    out = text or ""
    if KEY and len(KEY) > 6:
        out = out.replace(KEY, "***")
    return out[:400]


# ------------------------------------------------------------- the words --

def prompt_for(names: list[str]) -> str:
    """What to tell Whisper before it listens.

    Only the names. A prompt is a hint about what the audio is likely to
    contain, and a list of the people who might be mentioned is the single
    most useful thing to hint at — it is the one part of a task that cannot
    be fixed by reading the transcript, because a name heard wrong does not
    look wrong.
    """
    short = [n.strip() for n in names if n and n.strip()][:PROMPT_NAMES]
    if not short:
        return "A short work instruction for a colleague."
    return ("A spoken work instruction naming one of these colleagues: "
            + ", ".join(short) + ".")


def transcribe(data: bytes, filename: str, content_type: str,
               language: str = "", names: list[str] | None = None
               ) -> tuple[str, str]:
    """(transcript, problem) — exactly one of the two is ever filled in.

    Never raises. This is called from a web request somebody is waiting on,
    and a provider that is down must produce a sentence they can read, not
    a traceback and an empty page.
    """
    if not enabled():
        return "", ("Dictation is not set up on this site yet — there is no "
                    "GROQ_API_KEY in the environment.")
    fields = {"model": (None, MODEL),
              "response_format": (None, "text"),
              "prompt": (None, prompt_for(names or []))}
    if language in ("en", "hi"):
        fields["language"] = (None, language)
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(
                URL, headers={"Authorization": f"Bearer {KEY}"},
                files={"file": (filename, data,
                                (content_type or "application/octet-stream")
                                .split(";")[0]),
                       **fields})
    except Exception as exc:                          # noqa: BLE001
        return "", _safe(f"Could not reach the transcription service: "
                         f"{type(exc).__name__}. Type the task out instead.")

    if r.status_code == 401:
        return "", ("The transcription service refused the key. Check "
                    "GROQ_API_KEY in Render — a trailing space or a half-"
                    "copied key looks exactly like this.")
    if r.status_code == 429:
        return "", ("The free transcription allowance is used up for now. "
                    "It comes back shortly; type the task out meanwhile.")
    if r.status_code >= 400:
        return "", _safe(f"The transcription service said no "
                         f"({r.status_code}): {r.text[:200]}")

    # response_format=text gives the transcript as the whole body, which
    # is one less thing to parse and one less shape to be wrong about.
    said = " ".join((r.text or "").split())
    if not said:
        return "", ("Nothing was heard in that recording. Check the "
                    "microphone and try again.")
    return said, ""


# ------------------------------------------------------------- the names --

@dataclass
class Guess:
    """One person the recording might have named, and how sure that is."""
    user_id: int
    name: str
    heard: str
    score: float

    @property
    def sure(self) -> bool:
        """Close enough to put at the front — still not close enough to pick."""
        return self.score >= 0.92


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^\w']+", (text or "")) if w]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())


def find_people(text: str, staff: list, limit: int = 4) -> list[Guess]:
    """Who the recording seems to name, best first.

    `staff` is whatever has `.id` and `.name` — the same people the picker
    on the form offers, passed in rather than queried here so this stays
    testable without a database.

    Matched on first names as well as whole ones, because nobody dictating
    a task says "Rohit Kumar": they say "Rohit". A first name shared by two
    people therefore returns both, which is the honest answer — and the
    reason this suggests rather than selects.
    """
    said = _words(text)
    if not said:
        return []
    # Every one- and two-word run in the transcript, as a person's name
    # might be either. Ordinary grammar words are dropped first: "kar de"
    # is not somebody called Karan.
    keep = [w for w in said
            if _norm(w) not in STOP and len(_norm(w)) >= MIN_PART]
    spans = [(w, _norm(w)) for w in keep]
    spans += [(f"{a} {b}", _norm(a) + _norm(b))
              for a, b in zip(said, said[1:])
              if _norm(a) not in STOP and _norm(b) not in STOP
              and len(_norm(a)) >= 3 and len(_norm(b)) >= 3]

    best: dict[int, Guess] = {}
    for person in staff:
        full = (getattr(person, "name", "") or "").strip()
        if not full:
            continue
        targets = {_norm(full)}
        # A name PART has to be a real part. "Spa" and "EA" are words in
        # somebody's job title, not the thing anybody calls them.
        targets.update(_norm(p) for p in full.split()
                       if len(_norm(p)) >= MIN_PART and _norm(p) not in STOP)
        targets = {t for t in targets if t}
        for heard, key in spans:
            if len(key) < MIN_PART:
                continue
            score = max(difflib.SequenceMatcher(None, key, t).ratio()
                        for t in targets)
            # A whole name heard whole beats a first name heard whole.
            if " " in heard:
                score = min(1.0, score + 0.03)
            floor = SHORT_FLOOR if len(key) <= SHORT else NAME_FLOOR
            if score < floor:
                continue
            got = best.get(getattr(person, "id", 0))
            # On a near tie the LONGER thing heard wins. "Rohith kumar"
            # and "kumar" both find Rohit Kumar, and quoting the surname
            # alone back at somebody reads as if it matched the wrong
            # half of the name.
            better = got is None or (
                score > got.score + 0.05
                or (score > got.score - 0.05 and len(heard) > len(got.heard)))
            if better:
                best[getattr(person, "id", 0)] = Guess(
                    user_id=getattr(person, "id", 0), name=full,
                    heard=heard, score=round(score, 3))
    return sorted(best.values(), key=lambda g: (-g.score, g.name))[:limit]


# ------------------------------------------------------------- the dates --

@dataclass
class When:
    """A deadline the recording seems to ask for, and the words behind it."""
    label: str
    value: str          # YYYY-MM-DDTHH:MM, ready for the form's field
    phrase: str


WEEKDAYS = {
    "monday": 0, "somvar": 0, "tuesday": 1, "mangalvar": 1,
    "wednesday": 2, "budhvar": 2, "thursday": 3, "guruvar": 3,
    "friday": 4, "shukravar": 4, "saturday": 5, "shanivar": 5,
    "sunday": 6, "ravivar": 6,
}

#: Words that move the hour rather than the day.
HOURS = [
    (r"\b(morning|subah|subeh)\b", 11, "morning"),
    (r"\b(noon|dopahar|lunch)\b", 14, "afternoon"),
    (r"\b(evening|shaam|sham)\b", 18, "evening"),
    (r"\b(night|raat|eod|end of (the )?day)\b", 23, "end of day"),
]

DEFAULT_HOUR = 23
DEFAULT_MINUTE = 59


def _at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def when_from(text: str, now: datetime | None = None) -> list[When]:
    """Deadlines the words suggest, soonest first. Often empty, and that is fine.

    Deliberately narrow. It reads the handful of phrases an office actually
    uses — today, tomorrow, the day after, a named weekday, "in N days", "a
    week" — in English and in the Hindi words people mix into English, and
    it ignores everything else rather than guessing.

    "kal" is the awkward one: in Hindi it means both yesterday and tomorrow,
    and only the verb says which. Nobody delegates work into the past, so it
    is read as tomorrow — and the chip says "tomorrow" in words, so a person
    about to tap it can see what it decided.
    """
    now = now or clock.now()
    low = " " + (text or "").lower() + " "
    out: list[When] = []

    hour, hour_word = DEFAULT_HOUR, ""
    minute = DEFAULT_MINUTE
    for pattern, h, word in HOURS:
        if re.search(pattern, low):
            hour, hour_word, minute = h, word, 0 if h != 23 else 59
            break

    def add(day: datetime, label: str, phrase: str) -> None:
        when = _at(day, hour, minute)
        if when <= now:
            return                      # a deadline in the past helps nobody
        stamp = when.strftime("%Y-%m-%dT%H:%M")
        if any(w.value == stamp for w in out):
            return
        shown = label + (f", {hour_word}" if hour_word else "")
        out.append(When(label=f"{shown} — {when:%a %d %b, %I:%M %p}",
                        value=stamp, phrase=phrase))

    if re.search(r"\b(today|aaj|aj)\b", low):
        add(now, "today", "today")
    if re.search(r"\b(tomorrow|kal|kl)\b", low):
        add(now + timedelta(days=1), "tomorrow", "tomorrow (kal)")
    if re.search(r"\b(parso|parson|day after tomorrow)\b", low):
        add(now + timedelta(days=2), "the day after tomorrow", "parso")

    for word, index in WEEKDAYS.items():
        if not re.search(rf"\b{word}\b", low):
            continue
        ahead = (index - now.weekday()) % 7 or 7
        add(now + timedelta(days=ahead), word.title(), word)

    # "in 3 days", "3 din", "do din" — and the week and month versions.
    numbers = {"ek": 1, "do": 2, "teen": 3, "char": 4, "paanch": 5,
               "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
               "a": 1, "couple of": 2}
    units = {"day": 1, "days": 1, "din": 1,
             "week": 7, "weeks": 7, "hafte": 7, "hafta": 7,
             "month": 30, "months": 30, "mahine": 30, "maheena": 30}
    for found in re.finditer(
            r"\b(\d{1,2}|" + "|".join(numbers) + r")\s+("
            + "|".join(units) + r")\b", low):
        raw, unit = found.group(1), found.group(2)
        n = int(raw) if raw.isdigit() else numbers.get(raw, 0)
        if not 1 <= n <= 90:
            continue
        add(now + timedelta(days=n * units[unit]),
            f"in {n} {unit}", found.group(0).strip())

    return sorted(out, key=lambda w: w.value)


# ------------------------------------------------------------ the title ---

def title_from(text: str, cap: int = 120) -> str:
    """A first line for the Task box — the opening sentence, trimmed.

    Not a summary. Summarising would mean a second trip to a machine for
    something a person is about to read and edit anyway, and the first
    sentence of a dictated instruction is almost always the instruction.
    """
    said = " ".join((text or "").split())
    if not said:
        return ""
    first = re.split(r"(?<=[.!?।])\s+", said)[0]
    if len(first) <= cap:
        return first.rstrip(" .।")
    cut = first[:cap].rsplit(" ", 1)[0]
    return cut.rstrip(" ,.।") + "…"


# -------------------------------------------------------------- cleanup ---

#: An unused recording is one somebody made and then wandered off from.
#: Kept a day so a form left open over lunch still works, then swept.
KEEP_HOURS = 24


def sweep(db, now: datetime | None = None) -> int:
    """Delete recordings nobody turned into a task. Returns how many."""
    from sqlalchemy import delete, select

    from ..models import VoiceNote

    now = now or clock.now()
    cutoff = now - timedelta(hours=KEEP_HOURS)
    rows = db.scalars(select(VoiceNote.id).where(
        VoiceNote.used_at.is_(None),
        VoiceNote.created_at < cutoff)).all()
    if not rows:
        return 0
    db.execute(delete(VoiceNote).where(VoiceNote.id.in_(rows)))
    db.commit()
    return len(rows)
