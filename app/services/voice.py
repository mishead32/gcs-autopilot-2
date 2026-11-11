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
# Two services can do the listening, and which one is right is a question
# about ACCENT rather than about price. Whisper on Groq is free and fast
# and writes clean English; on an Indian office speaking Hindi and English
# in the same sentence it guesses, and guesses differently each time.
# Gemini can be TOLD how to write — keep the Hindi in Roman letters, spell
# a name the way this company spells it — and that instruction is most of
# the difference. So Gemini is the default wherever its key exists.
GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("VOICE_MODEL", "whisper-large-v3").strip()
GROQ_URL = os.getenv("VOICE_URL",
                     "https://api.groq.com/openai/v1/audio/transcriptions"
                     ).strip()

# The same key and the same model name Task Detective already uses, so
# there is one Google credential on this site and not two. Worth knowing:
# the free allowance is shared between them, and a morning of dictation
# comes out of the same bucket as the afternoon's proof-checking.
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = (os.getenv("VOICE_AI_MODEL", "").strip()
                or os.getenv("AI_MODEL", "").strip() or "gemini-3.8-flash")
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")

#: Set to "groq" or "gemini" to force one. Left blank, Gemini wins when
#: its key is there, because it is the one that can be told how to spell.
PROVIDER = os.getenv("VOICE_PROVIDER", "").strip().lower()

TIMEOUT = float(os.getenv("VOICE_TIMEOUT", "60") or 60)


def provider() -> str:
    """Which service is doing the listening, or "" when none can."""
    if PROVIDER == "groq":
        return "groq" if GROQ_KEY else ""
    if PROVIDER == "gemini":
        return "gemini" if GEMINI_KEY else ""
    if GEMINI_KEY:
        return "gemini"
    return "groq" if GROQ_KEY else ""


def _key() -> str:
    return GEMINI_KEY if provider() == "gemini" else GROQ_KEY


def _model() -> str:
    return GEMINI_MODEL if provider() == "gemini" else GROQ_MODEL

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
    return bool(provider())


def status() -> str:
    """One sentence for the page about whether dictation can work.

    Reports the key's LENGTH and never the key. A wrong paste — a trailing
    space, half a key, the quotes copied with it — is the common failure,
    and it is diagnosable from the length alone.
    """
    who = provider()
    if not who:
        return ("Dictation is off: no GEMINI_API_KEY (or GROQ_API_KEY) is "
                "set. Add one in Render's Environment tab and restart.")
    where = "Gemini" if who == "gemini" else "Groq"
    said = (f"Dictation is on — {where}, {_model()}, with a key of "
            f"{len(_key())} characters.")
    if who == "gemini":
        said += (" Its free allowance is shared with Task Detective, so "
                 "heavy dictation eats into the day's proof-checking.")
    return said


def _safe(text: str) -> str:
    """Never let a key reach a screen, however an error quotes it back.

    Both of them, not just the one in use: an error from one service can
    quote a header the other set, and a key on a screen is a key on a
    screen whichever one it is.
    """
    out = text or ""
    for secret in (GEMINI_KEY, GROQ_KEY):
        if secret and len(secret) > 6:
            out = out.replace(secret, "***")
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


def _instruction(language: str, names: list[str]) -> str:
    """What Gemini is told before it listens. This IS the quality.

    Whisper can only be handed a list of likely words. Gemini can be given
    a rule, and the rules that matter in this office are all about HOW to
    write rather than what was said:

      * Hindi in Roman letters, not Devanagari and not translated. An
        instruction that comes back as "Tell Rohit to settle the bill" has
        lost the person who said "bill clear kar de" — they read it, do
        not recognise it, and stop trusting the transcript.
      * Names spelled the way this company spells them. A name is the one
        part of a task that cannot be fixed by reading it back, because a
        name heard wrong does not look wrong.
      * Nothing added. No summary, no "the speaker says", no apology for
        poor audio — just the words.
    """
    how = {
        "en": "The speaker is talking in English.",
        "hi": "The speaker is talking in Hindi. Write it in Roman letters "
              "the way it sounds, not in Devanagari, and do not translate "
              "it into English.",
    }.get(language,
          "The speaker is in an Indian office and will usually mix Hindi "
          "and English in the same sentence. Keep both exactly as spoken: "
          "write the Hindi words in Roman letters the way they sound "
          "(\"bill clear kar de\", not \"settle the bill\" and not "
          "Devanagari), and leave the English words in English.")
    said = [
        "Write down, word for word, what is said in this short recording. "
        "It is a work instruction one colleague is giving another.",
        how,
        "Do not translate it. Do not summarise or tidy it. Do not add "
        "anything of your own — no speaker labels, no timestamps, no "
        "notes about the audio quality.",
    ]
    short = [n.strip() for n in (names or []) if n and n.strip()][:PROMPT_NAMES]
    if short:
        said.append(
            "If a person is named, it will be one of these colleagues, and "
            "you must spell the name exactly as it is written here: "
            + ", ".join(short) + ".")
    said.append("Reply with the transcript and nothing else. If nothing "
                "can be heard, reply with nothing at all.")
    return " ".join(said)


def _say_gemini(data: bytes, content_type: str, language: str,
                names: list[str]) -> tuple[str, str]:
    """Ask Gemini. (transcript, problem) — never raises."""
    import base64

    mime = (content_type or "audio/webm").split(";")[0].strip().lower()
    body = {
        "contents": [{"role": "user", "parts": [
            {"text": _instruction(language, names)},
            {"inline_data": {"mime_type": mime,
                             "data": base64.b64encode(data).decode("ascii")}},
        ]}],
        # Nothing creative is wanted here. Zero, so the same recording
        # gives the same words twice — people compare them.
        "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(
                GEMINI_URL.format(model=GEMINI_MODEL), json=body,
                headers={"x-goog-api-key": GEMINI_KEY,
                         "content-type": "application/json"})
            # Google's own documentation does not say in so many words
            # that a webm container is accepted, and a browser records
            # webm by default. Rather than find that out in somebody's
            # face, a refusal that is plainly about the FORMAT is tried
            # once more under the container the same codec usually ships
            # in. Once, and only for that error.
            if (r.status_code == 400 and mime == "audio/webm"
                    and "mime" in r.text.lower()):
                body["contents"][0]["parts"][1]["inline_data"]["mime_type"] \
                    = "audio/ogg"
                r = client.post(
                    GEMINI_URL.format(model=GEMINI_MODEL), json=body,
                    headers={"x-goog-api-key": GEMINI_KEY,
                             "content-type": "application/json"})
    except Exception as exc:                          # noqa: BLE001
        return "", _safe(f"Could not reach Gemini: {type(exc).__name__}. "
                         "Type the task out instead.")

    if r.status_code in (401, 403):
        return "", ("Google refused the key. Check GEMINI_API_KEY in "
                    "Render — a trailing space or a half-copied key looks "
                    "exactly like this.")
    if r.status_code == 429:
        return "", ("Google's free allowance is used up for today. It is "
                    "shared with Task Detective. It comes back after "
                    "midnight; type the task out meanwhile.")
    if r.status_code >= 400:
        return "", _safe(f"Gemini said no ({r.status_code}): "
                         f"{r.text[:200]}")
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        said = " ".join(p.get("text", "") for p in parts)
    except Exception:                                 # noqa: BLE001
        # A reply with no text is what a safety block looks like, and
        # "it failed" would send somebody hunting the wrong problem.
        return "", _safe("Gemini answered with no transcript in it: "
                         + r.text[:200])
    return " ".join((said or "").split()), ""


def _say_groq(data: bytes, filename: str, content_type: str, language: str,
              names: list[str]) -> tuple[str, str]:
    """Ask Groq's Whisper. (transcript, problem) — never raises."""
    fields = {"model": (None, GROQ_MODEL),
              "response_format": (None, "text"),
              "prompt": (None, prompt_for(names or []))}
    if language in ("en", "hi"):
        fields["language"] = (None, language)
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(
                GROQ_URL, headers={"Authorization": f"Bearer {GROQ_KEY}"},
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
    return " ".join((r.text or "").split()), ""


def transcribe(data: bytes, filename: str, content_type: str,
               language: str = "", names: list[str] | None = None
               ) -> tuple[str, str]:
    """(transcript, problem) — exactly one of the two is ever filled in.

    Never raises. This is called from a web request somebody is waiting on,
    and a provider that is down must produce a sentence they can read, not
    a traceback and an empty page.
    """
    who = provider()
    if not who:
        return "", status()
    if who == "gemini":
        said, problem = _say_gemini(data, content_type, language, names or [])
    else:
        said, problem = _say_groq(data, filename, content_type, language,
                                  names or [])
    if problem:
        return "", problem
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
    # A word with a digit in it is not somebody's name. Worth saying
    # because the transcripts here are full of them — "EP152008047IN" is
    # a Dak document number, and once the digits are stripped out to
    # compare letters it becomes "EPIN", which will happily match
    # somebody four letters long.
    keep = [w for w in said
            if _norm(w) not in STOP and len(_norm(w)) >= MIN_PART
            and not any(c.isdigit() for c in w)]
    spans = [(w, _norm(w)) for w in keep]
    spans += [(f"{a} {b}", _norm(a) + _norm(b))
              for a, b in zip(said, said[1:])
              if _norm(a) not in STOP and _norm(b) not in STOP
              and len(_norm(a)) >= 3 and len(_norm(b)) >= 3
              and not any(c.isdigit() for c in a + b)]

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

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7,
    "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))

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

    def add_date(day_n: int, month_n: int, year_s, phrase: str) -> None:
        """A date somebody named outright, made sense of.

        The year is the awkward part and is almost never said. Left out, it
        is this year — and if that has already gone, next year, because a
        deadline dictated today is not one that fell last March.
        """
        if not 1 <= day_n <= 31:
            return
        month = month_n or now.month
        if not 1 <= month <= 12:
            return
        try:
            year = int(year_s) if year_s else now.year
        except (TypeError, ValueError):
            year = now.year
        if year < 100:
            year += 2000
        if not now.year - 1 <= year <= now.year + 5:
            return
        for bump in (0, 1):
            m, y = month, year + bump
            if month_n == 0 and bump:        # "8 tarikh": next MONTH, not year
                m, y = (month % 12) + 1, year + (1 if month == 12 else 0)
            try:
                when = _at(datetime(y, m, day_n), hour, minute)
            except ValueError:
                return                        # 31 February and friends
            if when > now:
                stamp = when.strftime("%Y-%m-%dT%H:%M")
                if any(w.value == stamp for w in out):
                    return
                # The date, once. Every other chip here names a day in
                # words — "tomorrow", "Friday" — and puts the real date
                # after it; a chip for a date somebody said outright has
                # no such word, and printing 08/11/2026 twice reads as a
                # mistake rather than as emphasis.
                out.append(When(
                    label=((hour_word + " — ") if hour_word else "")
                          + f"{when:%a %d/%m/%Y %I:%M %p}",
                    value=stamp, phrase=phrase))
                return

    def add(day: datetime, label: str, phrase: str) -> None:
        when = _at(day, hour, minute)
        if when <= now:
            return                      # a deadline in the past helps nobody
        stamp = when.strftime("%Y-%m-%dT%H:%M")
        if any(w.value == stamp for w in out):
            return
        shown = label + (f", {hour_word}" if hour_word else "")
        out.append(When(label=f"{shown} — {when:%a %d/%m/%Y %I:%M %p}",
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

    # An actual date said out loud: "8th November", "November 8", "8 Nov
    # 2026". This was the gap people noticed first — "that is 8th November"
    # is how somebody dictating a deadline most often says it, and reading
    # only "tomorrow" and "Friday" meant the one clear case was the one
    # that produced nothing.
    for found in re.finditer(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + _MONTH_RE
            + r")\b\.?\s*(\d{4})?", low):
        add_date(int(found.group(1)), MONTHS[found.group(2)],
                 found.group(3), found.group(0).strip())
    for found in re.finditer(
            r"\b(" + _MONTH_RE + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b\.?"
            r"\s*(\d{4})?", low):
        add_date(int(found.group(2)), MONTHS[found.group(1)],
                 found.group(3), found.group(0).strip())
    # 8/11, 08-11-2026. Day first, as India writes it — and the chip spells
    # the month out in words, so a reader can see which way it was read.
    for found in re.finditer(
            r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", low):
        add_date(int(found.group(1)), int(found.group(2)),
                 found.group(3), found.group(0).strip())
    # "8 tarikh" — the 8th of this month, or next month if it has gone.
    for found in re.finditer(r"\b(\d{1,2})\s*(?:tarikh|tareekh)\b", low):
        add_date(int(found.group(1)), 0, None, found.group(0).strip())

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


# ----------------------------------------------------------- the urgency --

@dataclass
class Urgency:
    """How urgent the recording said it was, and the words that said so."""
    value: str          # high | medium | low, as the form's dropdown wants
    label: str
    phrase: str


#: Read in this order, highest first, so "not urgent" is not matched by
#: the word "urgent" sitting inside it. Each entry is (priority, pattern).
URGENCY = [
    ("low", r"\b(low priority|no rush|no hurry|koi jaldi nahi|"
            r"jaldi nahi|baad mein|when you (are |get )?free|"
            r"not urgent|whenever)\b"),
    ("high", r"\b(urgent|urgently|asap|immediately|right away|top priority|"
             r"high priority|critical|turant|abhi|jaldi|foran|emergency)\b"),
    ("medium", r"\b(normal priority|medium priority|normal hai)\b"),
]

URGENCY_WORDS = {"high": "High", "medium": "Medium", "low": "Low"}


def urgency_from(text: str) -> Urgency | None:
    """How urgent it sounded, or None when nothing said.

    None is the common answer and the right one: most instructions say
    nothing about priority, and reading urgency into a tone of voice is
    exactly the kind of guess that makes people stop trusting the rest.

    "not urgent" is checked before "urgent" on purpose — the low pattern
    runs first, and the first match wins.
    """
    low = " " + (text or "").lower() + " "
    for value, pattern in URGENCY:
        found = re.search(pattern, low)
        if found:
            return Urgency(value=value, label=URGENCY_WORDS[value],
                           phrase=found.group(0).strip())
    return None


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
