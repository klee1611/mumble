"""The API's logic, shared by the local FastAPI server and the DigitalOcean Function.

Only needs a psycopg connection with dict rows: no models, no Ollama, so it fits
in a small serverless function.
"""

import base64
import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone

AUDIO_TYPES = {"audio/webm", "audio/mp4", "audio/x-m4a", "audio/mpeg", "audio/ogg",
               "audio/wav", "audio/x-wav", "audio/aac"}
MAX_AUDIO_BYTES = 750 * 1024  # stays under DigitalOcean Functions' request limit once base64'd
STATUSES = ("new", "approved", "done", "dismissed")

# Lazy-payoff estimate: what the same memos would have cost by hand.
TYPING_WPM = 40
SORT_SECONDS_PER_ITEM = 20


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def add_memo(conn, mime: str, audio_b64: str) -> dict:
    mime = (mime or "").split(";")[0].strip().lower()
    # Browsers call .webm/.mp4 voice recordings "video/..."; the audio is what we keep.
    if mime in ("video/webm", "video/mp4", "video/quicktime"):
        mime = "audio/mp4" if mime != "video/webm" else "audio/webm"
    if mime not in AUDIO_TYPES:
        raise ApiError(415, f"not audio: {mime}")
    try:
        audio = base64.b64decode(audio_b64, validate=True)
    except ValueError:
        raise ApiError(400, "audio must be base64")
    if not audio:
        raise ApiError(400, "empty memo")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ApiError(413, "memo too long; mumble less (about 5 minutes max)")
    return conn.execute(
        "INSERT INTO memos (mime, audio) VALUES (%s, %s) RETURNING id, created_at, status",
        (mime, audio),
    ).fetchone()


def list_memos(conn, limit: int = 50) -> list[dict]:
    return conn.execute(
        "SELECT id, created_at, status, duration_s, transcript, error FROM memos "
        "ORDER BY id DESC LIMIT %s", (min(int(limit), 200),)
    ).fetchall()


def nap_status(conn) -> dict:
    pending = conn.execute("SELECT count(*) AS n FROM memos WHERE status='pending'").fetchone()["n"]
    last = conn.execute("SELECT * FROM naps ORDER BY id DESC LIMIT 1").fetchone()
    napping = bool(last and last["finished_at"] is None
                   and last["started_at"] > datetime.now(timezone.utc) - timedelta(hours=2))
    return {"napping": napping, "pending": pending, "last": last}


def _payoff(audio_s: float, words: int, items: int) -> dict:
    by_hand_s = words / TYPING_WPM * 60 + items * SORT_SECONDS_PER_ITEM
    return {"spoken_s": round(audio_s), "by_hand_s": round(by_hand_s),
            "saved_s": max(0, round(by_hand_s - audio_s))}


def digest(conn, secret: str) -> dict:
    items = conn.execute(
        "SELECT id, memo_id, kind, title, detail, when_text, due_at, status, created_at "
        "FROM items WHERE status IN ('new', 'approved') "
        "ORDER BY (due_at IS NULL), due_at, id"
    ).fetchall()
    totals = conn.execute(
        "SELECT coalesce(sum(audio_s),0) AS audio_s, coalesce(sum(words),0) AS words, "
        "coalesce(sum(items),0) AS items, coalesce(sum(memos),0) AS memos FROM naps"
    ).fetchone()
    last = conn.execute(
        "SELECT * FROM naps WHERE finished_at IS NOT NULL AND memos > 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    for it in items:
        it["ics"] = ics_query(secret, it["id"]) if it["kind"] == "event" and it["due_at"] else None
    return {
        "items": items,
        "last_nap": last and {**last, "payoff": _payoff(last["audio_s"], last["words"], last["items"])},
        "all_time": {**totals, "payoff": _payoff(totals["audio_s"], totals["words"], totals["items"])},
    }


def set_item_status(conn, item_id: int, status: str) -> dict:
    if status not in STATUSES:
        raise ApiError(422, "bad status")
    row = conn.execute(
        "UPDATE items SET status=%s WHERE id=%s RETURNING id, status", (status, item_id)
    ).fetchone()
    if not row:
        raise ApiError(404, "no such item")
    return row


ICS_LINK_TTL_S = 30 * 24 * 3600


def _ics_sig(secret: str, item_id: int, exp: int) -> str:
    # Derived key, so the link can't be turned back into the API token.
    key = hmac.new(secret.encode(), b"mumble-ics", hashlib.sha256).digest()
    return hmac.new(key, f"{item_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def ics_query(secret: str, item_id: int) -> str:
    """Query string for a calendar link that only unlocks this one event, for 30 days."""
    exp = int(time.time()) + ICS_LINK_TTL_S
    return f"exp={exp}&sig={_ics_sig(secret, item_id, exp)}"


def check_ics(secret: str, item_id: int, exp: str, sig: str) -> bool:
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    return (bool(secret) and exp_i > time.time()
            and hmac.compare_digest(sig or "", _ics_sig(secret, item_id, exp_i)))


def item_ics(conn, item_id: int) -> tuple[str, str]:
    """Return (filename, iCalendar text) for an event."""
    it = conn.execute("SELECT * FROM items WHERE id=%s", (item_id,)).fetchone()
    if not it or not it["due_at"]:
        raise ApiError(404, "no such event")
    fmt = "%Y%m%dT%H%M%SZ"
    start = it["due_at"].astimezone(timezone.utc)
    body = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Mumble//EN", "BEGIN:VEVENT",
        f"UID:mumble-{it['id']}@mumble", f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}",
        f"DTSTART:{start.strftime(fmt)}", f"DTEND:{(start + timedelta(minutes=30)).strftime(fmt)}",
        f"SUMMARY:{_ics_escape(it['title'])}", f"DESCRIPTION:{_ics_escape(it['detail'])}",
        "END:VEVENT", "END:VCALENDAR", "",
    ])
    return f"mumble-{it['id']}.ics", body


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")


def search(conn, q: str, limit: int = 10, vector: list[float] | None = None) -> list[dict]:
    """Keyword search, fused with semantic search when a query vector is available.

    The serverless API has no embedding model, so it searches by keyword; the
    local server embeds the query with Ollama and gets both.
    """
    q = (q or "").strip()
    if len(q) < 2:
        raise ApiError(422, "search for something")
    sem = (
        """SELECT id, row_number() OVER (ORDER BY embedding <=> %(v)s::vector) AS r
           FROM items WHERE embedding IS NOT NULL
           ORDER BY embedding <=> %(v)s::vector LIMIT 50"""
        if vector is not None else "SELECT NULL::bigint AS id, NULL::bigint AS r WHERE false"
    )
    # Keyword side: full-text match, plus a plain substring match so short
    # words like "wifi" or names still hit.
    return conn.execute(
        f"""
        WITH sem AS ({sem}),
        kw AS (
            SELECT id, row_number() OVER (ORDER BY ts_rank(tsv, query) DESC, id DESC) AS r
            FROM items, websearch_to_tsquery('english', %(q)s) query
            WHERE tsv @@ query OR (title || ' ' || detail) ILIKE '%%' || %(q)s || '%%'
            LIMIT 50
        ), fused AS (
            SELECT id, sum(1.0 / (60 + r)) AS score
            FROM (SELECT * FROM sem UNION ALL SELECT * FROM kw) u GROUP BY id
        )
        SELECT i.id, i.kind, i.title, i.detail, i.when_text, i.due_at, i.status,
               m.created_at AS said_at, f.score
        FROM fused f JOIN items i USING (id) JOIN memos m ON m.id = i.memo_id
        ORDER BY f.score DESC LIMIT %(limit)s
        """,
        {"v": vector, "q": q, "limit": min(int(limit), 50)},
    ).fetchall()
