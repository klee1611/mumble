"""Seed a demo week: eight voice memos from Monday to Saturday, voiced by macOS
`say`, backdated, and left *pending* so the real nap pipeline (Whisper, Gemma)
and the weekly review (Ultramarine) process them like any other memos.

    uv run python scripts/demo_week.py            # into DATABASE_URL
    uv run python scripts/demo_week.py --wipe     # remove the demo week again

Demo memos are tagged with mime 'audio/webm; demo' so --wipe only removes them.
"""

import base64
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).parent.parent))
from mumble import config, core  # noqa: E402

TZ = ZoneInfo("America/Toronto")
DEMO_MIME = "audio/webm; demo"

# (local time, what was mumbled)
WEEK = [
    ("2026-10-19 08:10", "Okay so this week, I really need to call the dentist, that back tooth is acting up "
                         "again. Also renew my passport, I think it expires in January. And remind me to send "
                         "Priya the hackathon idea doc."),
    ("2026-10-19 21:30", "Idea. What if the sloth thing was about batch jobs, like doing the work at night when "
                         "nobody is watching. That could be a talk."),
    ("2026-10-20 12:40", "Lunch thought. The gym membership renews on November first. I should cancel it, I "
                         "haven't gone since August."),
    ("2026-10-21 07:55", "Tooth still hurts. I'll call the dentist tomorrow morning, for real this time. Also pick "
                         "up mom's birthday gift before Saturday, she wanted that blue scarf."),
    ("2026-10-21 18:20", "Priya says the meetup is Thursday at 6 at the library. Bring the laptop charger."),
    ("2026-10-22 22:05", "Didn't call the dentist. Again. Ugh. Friday then. Also I need a passport photo for the "
                         "renewal, the pharmacy on King Street does them."),
    ("2026-10-23 09:15", "Note for the talk. Slow and steady: the 3 AM job is cheaper because nothing is waiting "
                         "for it. Maybe open with the sloth fact, they only come down from the trees once a week."),
    ("2026-10-24 08:00", "Got the scarf, mom's sorted. The gym, I still haven't cancelled it, and it renews "
                         "next Sunday."),
]


def speak(text: str) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        aiff, webm = Path(d) / "m.aiff", Path(d) / "m.webm"
        subprocess.run(["say", "-v", "Samantha", "-r", "185", "-o", str(aiff), text], check=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-c:a", "libopus",
                        "-b:a", "16k", str(webm)], check=True)
        return webm.read_bytes()


def main() -> None:
    with psycopg.connect(config.DATABASE_URL, row_factory=dict_row, autocommit=True) as conn:
        if "--wipe" in sys.argv:
            n = conn.execute("DELETE FROM memos WHERE mime = %s", (DEMO_MIME,)).rowcount
            print(f"removed {n} demo memos (and their items)")
            return
        if conn.execute("SELECT 1 FROM memos WHERE mime = %s LIMIT 1", (DEMO_MIME,)).fetchone():
            sys.exit("demo week already seeded; run with --wipe first")
        for when, text in WEEK:
            audio = speak(text)
            memo = core.add_memo(conn, "audio/webm", base64.b64encode(audio).decode())
            at = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            conn.execute("UPDATE memos SET created_at = %s, mime = %s WHERE id = %s",
                         (at, DEMO_MIME, memo["id"]))
            print(f"memo {memo['id']}: {when}  {len(audio) // 1024} KB")


if __name__ == "__main__":
    main()
