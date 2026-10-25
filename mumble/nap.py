"""The overnight job. Cron runs `python -m mumble.nap` at 3 AM.

Processes pending memos one at a time on a CPU, which is fine: nobody is awake
to wait for it. Each account gets at most MEMOS_PER_ACCOUNT per night, so one
busy account can't push everyone else's memos past breakfast.
"""

import logging
import sys
import tempfile
import time

import httpx

from . import config, db
from .structure import embed, extract
from .transcribe import transcribe

log = logging.getLogger("mumble.nap")

MEMOS_PER_ACCOUNT = 25  # per night; the rest wait for tomorrow


def _transcribe_memo(memo_id: int) -> tuple[str, float]:
    # Audio lives in Postgres until it's sorted; Whisper wants a file.
    with db.connect() as conn:
        audio = conn.execute("SELECT audio FROM memos WHERE id=%s", (memo_id,)).fetchone()["audio"]
    if not audio:
        raise ValueError("memo has no audio")
    with tempfile.NamedTemporaryFile() as f:
        f.write(audio)
        f.flush()
        return transcribe(f.name)


def run() -> dict:
    db.init()
    with db.connect() as conn:
        nap_id = conn.execute(
            "INSERT INTO naps (model) VALUES (%s) RETURNING id", (config.MODEL,)
        ).fetchone()["id"]
        pending = conn.execute(
            """SELECT id, account_id, mime, created_at FROM (
                   SELECT *, row_number() OVER (PARTITION BY account_id ORDER BY id) AS nth
                   FROM memos WHERE status = 'pending') m
               WHERE nth <= %s ORDER BY id""",
            (MEMOS_PER_ACCOUNT,),
        ).fetchall()
        # Rate-limit counters only matter for the current minute.
        conn.execute("DELETE FROM requests WHERE minute < now() - interval '1 hour'")

    stats = {"memos": 0, "items": 0, "audio_s": 0.0, "words": 0, "failed": 0}
    started = time.monotonic()
    with httpx.Client() as client:
        for memo in pending:
            try:
                text, duration = _transcribe_memo(memo["id"])
                items = extract(text, memo["created_at"], client)
                vectors = embed([f"{i['title']}. {i['detail']}" for i in items], client)
            except Exception as e:  # one bad memo shouldn't wake anyone up
                log.exception("memo %s failed", memo["id"])
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE memos SET status='failed', error=%s, processed_at=now() WHERE id=%s",
                        (str(e)[:500], memo["id"]),
                    )
                stats["failed"] += 1
                continue

            with db.connect() as conn, conn.transaction():
                conn.execute(
                    "UPDATE memos SET status='done', transcript=%s, duration_s=%s, "
                    "processed_at=now(), error=NULL, audio=NULL WHERE id=%s",
                    (text, duration, memo["id"]),
                )
                for item, vec in zip(items, vectors):
                    conn.execute(
                        "INSERT INTO items (memo_id, account_id, kind, title, detail, when_text, due_at, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
                        (memo["id"], memo["account_id"], item["kind"], item["title"], item["detail"],
                         item["when_text"], item["due_at"], vec),
                    )
            stats["memos"] += 1
            stats["items"] += len(items)
            stats["audio_s"] += duration
            stats["words"] += len(text.split())
            log.info("memo %s: %d words -> %d items", memo["id"], len(text.split()), len(items))

        # Hand the RAM back (matters on a laptop; the CI runner is thrown away anyway).
        for model in (config.MODEL, config.EMBED_MODEL):
            try:
                client.post(f"{config.OLLAMA_URL}/api/generate",
                            json={"model": model, "keep_alive": 0}, timeout=30)
            except httpx.HTTPError:
                pass

    with db.connect() as conn:
        conn.execute(
            "UPDATE naps SET finished_at=now(), memos=%(memos)s, items=%(items)s, "
            "audio_s=%(audio_s)s, words=%(words)s, failed=%(failed)s WHERE id=%(id)s",
            {**stats, "id": nap_id},
        )
    db.close()
    stats["seconds"] = round(time.monotonic() - started, 1)
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    result = run()
    print(result)
    sys.exit(1 if result["failed"] else 0)
