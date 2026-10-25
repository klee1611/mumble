"""The API's logic, shared by the local FastAPI server and the serverless API (api.py).

Only needs a psycopg connection with dict rows: no models, no Ollama, so it fits
in a small serverless function.

Every function that reads or writes someone's data takes their account id and
filters by it. There is no query here that isn't scoped to one account.
"""

import base64
import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta, timezone

AUDIO_TYPES = {"audio/webm", "audio/mp4", "audio/x-m4a", "audio/mpeg", "audio/ogg",
               "audio/wav", "audio/x-wav", "audio/aac"}
MAX_AUDIO_BYTES = 750 * 1024  # a 5-minute memo at 16 kbps is ~600 KB
STATUSES = ("new", "approved", "done", "dismissed")

# Lazy-payoff estimate: what the same memos would have cost by hand.
TYPING_WPM = 40
SORT_SECONDS_PER_ITEM = 20


MIN_SECRET_LEN = 24

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; connect-src 'self' https://dev-l1bc0yw26epgfj00.us.auth0.com; media-src 'self' blob:; "
        "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    ),
}

# What each account gets. Override one account with SQL, e.g.
#   UPDATE accounts SET quota = '{"memos_per_day": 50}' WHERE email = 'me@example.com';
QUOTA = {
    "memos_per_day": 20,        # rolling 24 hours
    "audio_mb_per_day": 6,      # about 50 minutes at 16 kbps
    "pending_max": 40,          # unsorted memos waiting for the nap
    "requests_per_minute": 120,
}


def max_accounts() -> int:
    # Sign-ups stop here, so strangers can't fill the free database or the night shift.
    try:
        return int(os.environ.get("MUMBLE_MAX_ACCOUNTS", "50"))
    except ValueError:
        return 50


def allowed_emails() -> set[str]:
    # Optional: only these people may sign up (comma-separated). Empty = anyone, up to max_accounts().
    return {e.strip().lower() for e in os.environ.get("MUMBLE_ALLOWED_EMAILS", "").split(",") if e.strip()}


def _limit(value, default: int, most: int) -> int:
    try:
        return max(1, min(int(value), most))
    except (TypeError, ValueError):
        return default


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _quota(row: dict) -> dict:
    custom = row.get("quota") or {}
    return {k: custom[k] if type(custom.get(k)) in (int, float) else v for k, v in QUOTA.items()}


def open_account(conn, claims: dict, lookup_email) -> dict:
    """The caller's account, created on first sign-in. Also counts the request
    against the per-minute limit. lookup_email() is only called for new accounts."""
    sub = claims["sub"]
    row = conn.execute("SELECT * FROM accounts WHERE id = %s", (sub,)).fetchone()
    if row is None:
        info = lookup_email()
        email = str(info.get("email") or "").strip().lower()
        if not email or info.get("email_verified") is not True:
            raise ApiError(403, "verify your email address first, then sign in again")
        allowed = allowed_emails()
        if allowed and email not in allowed:
            raise ApiError(403, "Mumble is invite-only right now")
        with conn.transaction():
            # One sign-up at a time, so the account cap can't be raced past.
            conn.execute("SELECT pg_advisory_xact_lock(4471)")
            if conn.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"] >= max_accounts():
                raise ApiError(403, "Mumble is full right now, sorry")
            conn.execute("INSERT INTO accounts (id, email) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                         (sub, email))
        row = conn.execute("SELECT * FROM accounts WHERE id = %s", (sub,)).fetchone()
    if row["disabled"]:
        raise ApiError(403, "this account is switched off")
    quota = _quota(row)
    n = conn.execute(
        """INSERT INTO requests (account_id, minute) VALUES (%s, date_trunc('minute', now()))
           ON CONFLICT (account_id, minute) DO UPDATE SET n = requests.n + 1 RETURNING n""",
        (sub,),
    ).fetchone()["n"]
    if n > quota["requests_per_minute"]:
        raise ApiError(429, "slow down a little (too many requests this minute)")
    if n == 1:  # once a minute is plenty
        conn.execute("UPDATE accounts SET last_seen = now() WHERE id = %s", (sub,))
    return {"id": sub, "email": row["email"], "quota": quota}


def usage(conn, account_id: str) -> dict:
    row = conn.execute(
        """SELECT count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS memos_24h,
                  coalesce(sum(size_bytes) FILTER (WHERE created_at > now() - interval '24 hours'), 0)
                      AS bytes_24h,
                  count(*) FILTER (WHERE status = 'pending') AS pending
           FROM memos WHERE account_id = %s""",
        (account_id,),
    ).fetchone()
    return {"memos_24h": row["memos_24h"], "audio_mb_24h": round(row["bytes_24h"] / 1e6, 2),
            "pending": row["pending"]}


def me(conn, account: dict) -> dict:
    return {"email": account["email"], "quota": account["quota"], "usage": usage(conn, account["id"])}


def delete_account(conn, account_id: str) -> dict:
    # Cascades to memos, items, reviews and request counts.
    conn.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
    return {"deleted": True}


def add_memo(conn, account: dict, mime: str, audio_b64: str) -> dict:
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
    q = account["quota"]
    with conn.transaction():
        # Lock the account row so two uploads at once can't both squeeze under the quota.
        conn.execute("SELECT 1 FROM accounts WHERE id = %s FOR UPDATE", (account["id"],))
        u = usage(conn, account["id"])
        if u["memos_24h"] >= q["memos_per_day"]:
            raise ApiError(429, f"that's {q['memos_per_day']} memos today, the daily limit. Rest your voice.")
        if u["audio_mb_24h"] + len(audio) / 1e6 > q["audio_mb_per_day"]:
            raise ApiError(429, "that's enough mumbling for one day (daily audio limit)")
        if u["pending"] >= q["pending_max"]:
            raise ApiError(429, "the pile is full; let tonight's nap catch up")
        return conn.execute(
            "INSERT INTO memos (account_id, mime, audio, size_bytes) VALUES (%s, %s, %s, %s) "
            "RETURNING id, created_at, status",
            (account["id"], mime, audio, len(audio)),
        ).fetchone()


def list_memos(conn, account_id: str, limit=50) -> list[dict]:
    return conn.execute(
        "SELECT id, created_at, status, duration_s, transcript, error FROM memos "
        "WHERE account_id = %s ORDER BY id DESC LIMIT %s", (account_id, _limit(limit, 50, 200))
    ).fetchall()


def nap_status(conn, account_id: str, can_nap_now: bool) -> dict:
    pending = conn.execute("SELECT count(*) AS n FROM memos WHERE account_id = %s AND status='pending'",
                           (account_id,)).fetchone()["n"]
    # Only when the nap ran: its totals cover everyone's memos.
    last = conn.execute("SELECT id, started_at, finished_at FROM naps ORDER BY id DESC LIMIT 1").fetchone()
    napping = bool(last and last["finished_at"] is None
                   and last["started_at"] > datetime.now(timezone.utc) - timedelta(hours=2))
    return {"napping": napping, "pending": pending, "last": last, "can_nap_now": can_nap_now}


def _payoff(audio_s: float, words: int, items: int) -> dict:
    by_hand_s = words / TYPING_WPM * 60 + items * SORT_SECONDS_PER_ITEM
    return {"spoken_s": round(audio_s), "by_hand_s": round(by_hand_s),
            "saved_s": max(0, round(by_hand_s - audio_s))}


def _sorted_stats(conn, account_id: str, since=None, until=None) -> dict:
    """What one account's memos added up to, for memos sorted between since and until."""
    return conn.execute(
        r"""WITH m AS (SELECT id, duration_s, transcript FROM memos
                       WHERE account_id = %s AND status = 'done' AND processed_at BETWEEN coalesce(%s::timestamptz, '-infinity')
                                                                  AND coalesce(%s::timestamptz, 'infinity'))
            SELECT (SELECT count(*) FROM m) AS memos,
                   (SELECT coalesce(sum(duration_s), 0) FROM m)::float AS audio_s,
                   (SELECT coalesce(sum(cardinality(regexp_split_to_array(nullif(btrim(transcript), ''), '\s+'))), 0)
                    FROM m)::int AS words,
                   (SELECT count(*) FROM items WHERE memo_id IN (SELECT id FROM m)) AS items""",
        (account_id, since, until),
    ).fetchone()


def digest(conn, account_id: str, secret: str) -> dict:
    items = conn.execute(
        "SELECT id, memo_id, kind, title, detail, when_text, due_at, status, created_at "
        "FROM items WHERE account_id = %s AND status IN ('new', 'approved') "
        "ORDER BY (due_at IS NULL), due_at, id", (account_id,)
    ).fetchall()
    nap = conn.execute(
        "SELECT started_at, finished_at FROM naps WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last = None
    if nap:
        stats = _sorted_stats(conn, account_id, nap["started_at"], nap["finished_at"])
        if stats["memos"]:
            last = {**nap, **stats, "payoff": _payoff(stats["audio_s"], stats["words"], stats["items"])}
    totals = _sorted_stats(conn, account_id)
    for it in items:
        it["ics"] = ics_query(secret, it["id"]) if it["kind"] == "event" and it["due_at"] else None
    return {
        "items": items,
        "last_nap": last,
        "all_time": {**totals, "payoff": _payoff(totals["audio_s"], totals["words"], totals["items"])},
    }


def latest_review(conn, account_id: str) -> dict | None:
    """The newest weekly review, with the items each claim cites attached."""
    review = conn.execute("SELECT * FROM reviews WHERE account_id = %s ORDER BY id DESC LIMIT 1",
                          (account_id,)).fetchone()
    if not review:
        return None
    body = review["body"]
    claims = [c for k in ("keep_saying", "slipping", "connections") for c in body.get(k, [])]
    if body.get("one_thing"):
        claims.append(body["one_thing"])
    ids = sorted({i for c in claims for i in c["item_ids"]})
    titles = {r["id"]: r["title"] for r in conn.execute(
        "SELECT id, title FROM items WHERE account_id = %s AND id = ANY(%s)", (account_id, ids)).fetchall()}
    for c in claims:
        c["items"] = [{"id": i, "title": titles[i]} for i in c["item_ids"] if i in titles]
    return {**review, "body": body}


def set_item_status(conn, account_id: str, item_id: int, status: str) -> dict:
    if status not in STATUSES:
        raise ApiError(422, "bad status")
    row = conn.execute(
        "UPDATE items SET status=%s WHERE id=%s AND account_id=%s RETURNING id, status",
        (status, item_id, account_id),
    ).fetchone()
    if not row:
        raise ApiError(404, "no such item")
    return row


ICS_LINK_TTL_S = 30 * 24 * 3600


def _ics_sig(secret: str, item_id: int, exp: int) -> str:
    # Derived key: the secret itself never appears in a link.
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
    return (len(secret or "") >= MIN_SECRET_LEN and exp_i > time.time()
            and hmac.compare_digest((sig or "").encode(), _ics_sig(secret, item_id, exp_i).encode()))


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
    # Model-written text: a raw CR or LF would let it inject extra calendar fields.
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")


def search(conn, account_id: str, q: str, limit=10, vector: list[float] | None = None) -> list[dict]:
    """Keyword search, fused with semantic search when a query vector is available.

    The serverless API has no embedding model, so it searches by keyword; the
    local server embeds the query with Ollama and gets both.
    """
    q = (q or "").strip()
    if len(q) < 2:
        raise ApiError(422, "search for something")
    sem = (
        """SELECT id, row_number() OVER (ORDER BY embedding <=> %(v)s::vector) AS r
           FROM items WHERE account_id = %(a)s AND embedding IS NOT NULL
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
            WHERE account_id = %(a)s
              AND (tsv @@ query OR (title || ' ' || detail) ILIKE '%%' || %(like)s || '%%')
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
        {"a": account_id, "v": vector, "q": q, "limit": _limit(limit, 10, 50),
         "like": q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")},
    ).fetchall()
