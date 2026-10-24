"""The weekly review: Ultramarine reads everything you mumbled this week.

Gemma sorts each memo on its own. Once a week, Ultramarine (Paper Instruments)
reads the whole pile at once and says what only shows up across memos: the
thing you keep saying you'll do, what's slipping, which notes belong together.

Every claim must cite the items it's based on. A claim that cites nothing, or
cites an item that doesn't exist, is thrown away. Runs Sundays from the nap
job, or `python -m mumble.review`.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from openai import OpenAI
from psycopg.types.json import Jsonb

from . import config, db

log = logging.getLogger("mumble.review")

BASE_URL = "https://api.paperinstruments.com/v1"
MODEL = "ultramarine-1-flash"
MAX_OUTPUT_TOKENS = 6000  # caps the reservation against the prepaid balance

# Per token, in nanodollars (from GET /v1/pricing). Used to report what each review cost.
PRICE_INPUT = 150
PRICE_CACHED_INPUT = 30
PRICE_OUTPUT = 500

CLAIM = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "item_ids": {"type": "array", "items": {"type": "integer"}}},
    "required": ["text", "item_ids"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "keep_saying": {"type": "array", "items": CLAIM},
        "slipping": {"type": "array", "items": CLAIM},
        "connections": {"type": "array", "items": CLAIM},
        "one_thing": CLAIM,
    },
    "required": ["headline", "keep_saying", "slipping", "connections", "one_thing"],
    "additionalProperties": False,
}

SYSTEM = """You review a very lazy person's week of voice memos. They mumble into
their phone; a small model already split each memo into items. You see every
item from the week at once, which the small model never did.

Find what only shows up across memos:
- keep_saying: things they mention again and again, or keep putting off.
- slipping: to-dos or events that are overdue, or untouched for days.
- connections: separate notes or ideas that belong together.
- one_thing: the single smallest next step that would help most. Be concrete.
- headline: one warm, short sentence about their week. No guilt.

Every entry must cite the ids of the items it is based on, in item_ids. Only
cite ids that appear below. If a section has nothing real, leave it empty:
an empty list beats a made-up pattern. Keep each text under 25 words."""


def _week(conn, now: datetime) -> list[dict]:
    since = now - timedelta(days=7)
    return conn.execute(
        # said_at is when the memo was recorded, not when the nap sorted it.
        """SELECT i.id, i.kind, i.title, i.detail, i.when_text, i.due_at, i.status,
                  m.created_at AS said_at
           FROM items i JOIN memos m ON m.id = i.memo_id
           WHERE m.created_at >= %s OR (i.status IN ('new', 'approved') AND m.created_at >= %s)
           ORDER BY m.created_at, i.id""",
        (since, now - timedelta(days=30)),
    ).fetchall()


def _describe(items: list[dict], now: datetime) -> str:
    tz = ZoneInfo(config.TZ)
    lines = [f"Today is {now.astimezone(tz):%A %B %d %Y, %H:%M}.", "", "Items:"]
    for it in items:
        due = f" | due {it['due_at'].astimezone(tz):%a %b %d %H:%M}" if it["due_at"] else ""
        said = f" | said “{it['when_text']}”" if it["when_text"] else ""
        lines.append(
            f"[{it['id']}] {it['kind']} | {it['status']} | mumbled {it['said_at'].astimezone(tz):%a %b %d %H:%M}"
            f"{due}{said} | {it['title']}" + (f" — {it['detail']}" if it["detail"] else "")
        )
    return "\n".join(lines)


def grounded(raw: dict, known_ids: set[int]) -> dict:
    """Keep only claims that cite items we actually showed the model."""
    def keep(claim):
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or not claim["text"].strip():
            return None
        cited = claim.get("item_ids")
        # type() is int, not isinstance: True and False are ints in Python.
        ids = sorted({i for i in cited if type(i) is int and i in known_ids}) if isinstance(cited, list) else []
        return {"text": claim["text"].strip()[:300], "item_ids": ids} if ids else None

    headline = raw.get("headline")
    out = {"headline": headline.strip()[:300] if isinstance(headline, str) else ""}
    for key in ("keep_saying", "slipping", "connections"):
        claims = raw.get(key)
        out[key] = [c for c in map(keep, claims if isinstance(claims, list) else []) if c][:5]
    out["one_thing"] = keep(raw.get("one_thing"))
    return out


def cost_usd(usage) -> float:
    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    nanodollars = ((usage.prompt_tokens - cached) * PRICE_INPUT + cached * PRICE_CACHED_INPUT
                   + usage.completion_tokens * PRICE_OUTPUT)
    return nanodollars / 1e9


def run(client: OpenAI | None = None, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    if client is None and not os.environ.get("PAPERINSTRUMENTS_API_KEY"):
        log.info("no PAPERINSTRUMENTS_API_KEY; skipping the weekly review")
        return None
    db.init()
    with db.connect() as conn:
        items = _week(conn, now)
    if len(items) < 2:
        log.info("only %d items this week; nothing to review", len(items))
        db.close()
        return None

    client = client or OpenAI(base_url=BASE_URL, api_key=os.environ["PAPERINSTRUMENTS_API_KEY"])
    res = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": _describe(items, now)}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "weekly_review", "schema": SCHEMA, "strict": True}},
        reasoning_effort="high",
        max_completion_tokens=MAX_OUTPUT_TOKENS,
    )
    review = grounded(json.loads(res.choices[0].message.content), {it["id"] for it in items})
    usage = res.usage
    row = {
        "items_read": len(items),
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "cost_usd": round(cost_usd(usage), 6),
    }
    with db.connect() as conn:
        rid = conn.execute(
            "INSERT INTO reviews (model, items_read, input_tokens, output_tokens, cost_usd, body) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (MODEL, row["items_read"], row["input_tokens"], row["output_tokens"], row["cost_usd"], Jsonb(review)),
        ).fetchone()["id"]
    db.close()
    log.info("review %s: %d items, %d in / %d out tokens, $%.6f",
             rid, row["items_read"], row["input_tokens"], row["output_tokens"], row["cost_usd"])
    return {**row, "id": rid, "review": review}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    result = run()
    print(json.dumps(result, default=str, indent=2) if result else "nothing to review")
    sys.exit(0)
