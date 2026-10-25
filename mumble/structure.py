"""Turn a rambling transcript into to-dos, events and notes.

Gemma decides *what* was said. It never computes a date: it copies the exact
time phrase from the transcript, and plain code turns that phrase into a
timestamp. A phrase the speaker never said is thrown away.
"""

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import dateparser
import httpx

from . import config

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["todo", "event", "note"]},
                    "title": {"type": "string"},
                    "when": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["kind", "title", "when", "detail"],
            },
        }
    },
    "required": ["items"],
}

PROMPT = """You tidy up voice memos for a very lazy person.
Below is a transcript of one memo they mumbled into their phone. Pull out every
distinct thing worth keeping.

- kind "todo": something they need to do.
- kind "event": something that happens at a specific time (appointment, call, meeting).
- kind "note": an idea, fact or thought worth remembering, including small
  facts they will want later (a password, a code, a name, a number, where
  something is). Put the fact itself in detail.
- title: short and imperative for todos ("Call the dentist"), under 8 words.
- when: COPY the exact words from the transcript that say when ("Tuesday at 10",
  "tomorrow morning"). Do not reword, do not invent, do not compute a date.
  Use "" if they did not say when.
- detail: useful specifics beyond the title (who, why, which one), or "".
  Never repeat the title, and never put the time words here: they go in when.

If they say they finished something ("got the scarf", "called mom"), add a
note titled "Done: ..." so it is not left on their list.
Skip filler, false starts and anything they took back ("actually never mind").
Go through every sentence: one memo often mixes unrelated things. If nothing is
worth keeping, return an empty list.

Example transcript: "uh book a haircut for Friday at 3, oh and the gate code at
mom's is 4471. returned the library books finally. also get batteries, no wait,
we have batteries."
Example items:
[{{"kind": "event", "title": "Haircut", "when": "Friday at 3", "detail": ""}},
 {{"kind": "note", "title": "Gate code at mom's", "when": "", "detail": "4471"}},
 {{"kind": "note", "title": "Done: returned the library books", "when": "", "detail": ""}}]

Transcript:
\"\"\"{transcript}\"\"\"
"""


def _said(phrase: str, transcript: str) -> bool:
    words = re.findall(r"[a-z0-9:]+", phrase.lower())
    hay = " ".join(re.findall(r"[a-z0-9:]+", transcript.lower()))
    return bool(words) and " ".join(words) in hay


# When someone says a day but no clock time, pick the hour they probably meant.
PART_OF_DAY = [
    (r"\bmorning\b", 9),
    (r"\b(noon|lunch)\b", 12),
    (r"\bafternoon\b", 14),
    (r"\b(after work|evening)\b", 18),
    (r"\b(tonight|night)\b", 20),
]
CLOCK = re.compile(r"\d\s*(am|pm|a\.m\.|p\.m\.|o'?clock)|\d:\d\d|\bat \d", re.I)
# "4471" or "555 0123" is a code or a phone number, not a time.
JUST_NUMBERS = re.compile(r"[\d\s#.\-]+")
MAX_YEARS_AHEAD = 2


def resolve_when(phrase: str, transcript: str, recorded_at: datetime) -> datetime | None:
    if not phrase or not _said(phrase, transcript) or JUST_NUMBERS.fullmatch(phrase):
        return None
    tz = ZoneInfo(config.TZ)
    base = recorded_at.astimezone(tz).replace(tzinfo=None)
    when = _parse(phrase, base)
    if when is None or when.year > base.year + MAX_YEARS_AHEAD:
        return None
    if CLOCK.search(phrase):
        return when.replace(tzinfo=tz)
    # "tomorrow" alone would otherwise inherit the time the memo was recorded.
    context = transcript.lower()
    hour = next((h for rx, h in PART_OF_DAY if re.search(rx, phrase.lower())), None)
    if hour is None:
        idx = context.find(phrase.lower())
        nearby = context[idx: idx + len(phrase) + 20] if idx >= 0 else ""
        hour = next((h for rx, h in PART_OF_DAY if re.search(rx, nearby)), 9)
    # Wall-clock time first, zone last: zoneinfo then picks EDT or EST for that
    # day (Nov 1 2026 is the switch), instead of reusing the offset of today.
    return when.replace(hour=hour, minute=0, second=0, microsecond=0, tzinfo=tz)


BARE_HOUR = re.compile(r"\b(at\s+)?(\d{1,2})(:\d\d)?\b(?!\s*(am|pm|a\.m\.|p\.m\.|o'?clock|:))", re.I)


def _with_meridiem(m: re.Match) -> str:
    # "at 6" means 6 PM to most people; dateparser reads it as the 6th of a month.
    hour = int(m.group(2))
    if not 1 <= hour <= 12:
        return m.group(0)
    half = "am" if 8 <= hour <= 11 else "pm"
    return f"{m.group(1) or ''}{hour}{m.group(3) or ':00'} {half}"


def _parse(phrase: str, base: datetime) -> datetime | None:
    phrase = re.sub(r"\b(morning|afternoon|evening|tonight|night|after work|lunch)\b", "", phrase, flags=re.I).strip()
    # "before Saturday" / "by Friday": the deadline is that day.
    phrase = re.sub(r"^(before|by|until|no later than)\s+", "", phrase, flags=re.I)
    # A bare month ("December") would borrow today's day of the month; mean the 1st.
    if re.fullmatch(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*", phrase, re.I):
        phrase = f"{phrase} 1st"
    if re.search(r"\b(at\s+)?\d", phrase) and not re.search(r"\d{3,}|\d+(st|nd|rd|th)\b", phrase):
        phrase = BARE_HOUR.sub(_with_meridiem, phrase)
    if not phrase:
        phrase = "today"
    return dateparser.parse(
        phrase,
        settings={"RELATIVE_BASE": base, "PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False},
    )


def extract(transcript: str, recorded_at: datetime, client: httpx.Client) -> list[dict]:
    if not transcript.strip():
        return []
    r = client.post(
        f"{config.OLLAMA_URL}/api/chat",
        json={
            "model": config.MODEL,
            "messages": [{"role": "user", "content": PROMPT.format(transcript=transcript)}],
            "format": SCHEMA,
            "stream": False,
            "think": False,
            "options": {"temperature": 0},
        },
        timeout=600,
    )
    r.raise_for_status()
    raw = json.loads(r.json()["message"]["content"])

    items = []
    for it in raw.get("items", []):
        title = (it.get("title") or "").strip()
        if not title or it.get("kind") not in ("todo", "event", "note"):
            continue
        when_text = (it.get("when") or "").strip()
        detail = (it.get("detail") or "").strip()
        if not _said(when_text, transcript):
            when_text = ""
        if when_text and JUST_NUMBERS.fullmatch(when_text):
            # Small models sometimes file a gate code under "when".
            detail = detail or when_text
            when_text = ""
        due = resolve_when(when_text, transcript, recorded_at)
        if due is None and len(detail.split()) <= 6:
            # Small models sometimes file "tomorrow after work" under detail.
            moved = resolve_when(detail, transcript, recorded_at)
            if moved is not None:
                when_text, detail, due = detail, "", moved
        kind = it["kind"]
        if kind == "event" and due is None:
            kind = "todo"  # an event with no real time is just a to-do
        items.append(
            {
                "kind": kind,
                "title": title[:200],
                "detail": detail[:1000],
                "when_text": when_text,
                "due_at": due,
            }
        )
    return items


def embed(texts: list[str], client: httpx.Client) -> list[list[float]]:
    if not texts:
        return []
    r = client.post(
        f"{config.OLLAMA_URL}/api/embed",
        json={"model": config.EMBED_MODEL, "input": texts},
        timeout=600,
    )
    r.raise_for_status()
    return r.json()["embeddings"]
