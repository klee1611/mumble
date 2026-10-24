"""Mumble's API for serverless hosting (api/index.py on Vercel).

Plain HTTP in, plain HTTP out: no web framework, no models, only psycopg, so
the function stays small. The logic lives in core.py, shared with the local
FastAPI server.
"""

import json
import os
import re
import urllib.parse
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from . import core

# The app and /api share one origin, so no CORS by default.
# If the app is hosted elsewhere, set CORS_ORIGIN to that exact origin.
_origin = os.environ.get("CORS_ORIGIN", "")
CORS = {
    "Access-Control-Allow-Origin": _origin,
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, PATCH, OPTIONS",
    "Vary": "Origin",
} if _origin else {}

# Only the tail of the path matters: /api/memos and /memos are the same route.
ROUTE = re.compile(r"(?:^|/)(memos|nap|digest|review|search|items/(\d+)(/event\.ics)?)/?$")


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(type(o).__name__)


def respond(status: int, body, headers: dict | None = None) -> dict:
    if isinstance(body, str):
        payload = body
    else:
        payload = json.dumps(body, default=_json_default)
        headers = {"Content-Type": "application/json", **(headers or {})}
    return {"statusCode": status, "headers": {**CORS, **core.SECURITY_HEADERS, **(headers or {})},
            "body": payload}


def _authorized(token: str) -> bool:
    return core.token_ok(token, os.environ.get("MUMBLE_TOKEN", ""))


def handle(method: str, url: str, headers: dict, body: str = "") -> dict:
    """url is the request target, e.g. /api/search?q=dentist."""
    method = method.upper()
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    path, _, query_string = url.partition("?")
    query = dict(urllib.parse.parse_qsl(query_string, keep_blank_values=True))

    if method == "OPTIONS":
        return respond(204, "")

    m = ROUTE.search(path)
    if not m:
        return respond(404, {"error": "not found"})
    route, item_id, is_ics = m.group(1), m.group(2), m.group(3)

    secret = os.environ.get("MUMBLE_TOKEN", "")
    if is_ics:
        # Calendar apps open this link directly: it carries a signature that
        # only unlocks this one event, never the API token.
        if not core.check_ics(secret, int(item_id), query.get("exp", ""), query.get("sig", "")):
            return respond(401, {"error": "bad or expired link"})
    elif not _authorized(headers.get("authorization", "").removeprefix("Bearer ").strip()):
        return respond(401, {"error": "bad token"})

    try:
        data = json.loads(body) if body else {}
        if not isinstance(data, dict):
            raise ValueError("body must be an object")
        if route == "nap" and method == "POST":
            # The nap runs on GitHub's 3 AM schedule; the API holds no GitHub token.
            return respond(200, {"started": False, "reason": "naps run at 3 AM"})
        with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
            if route == "memos" and method == "POST":
                return respond(200, core.add_memo(conn, data.get("mime", ""), data.get("audio", "")))
            if route == "memos":
                return respond(200, core.list_memos(conn, query.get("limit", 50)))
            if route == "nap":
                return respond(200, core.nap_status(conn, can_nap_now=False))
            if route == "digest":
                return respond(200, core.digest(conn, secret))
            if route == "review":
                return respond(200, core.latest_review(conn))
            if route == "search":
                return respond(200, core.search(conn, query.get("q", ""), query.get("limit", 10)))
            if item_id and is_ics:
                name, ics = core.item_ics(conn, int(item_id))
                return respond(200, ics, {"Content-Type": "text/calendar",
                                          "Content-Disposition": f'attachment; filename="{name}"'})
            if item_id and method == "PATCH":
                return respond(200, core.set_item_status(conn, int(item_id), data.get("status", "")))
        return respond(405, {"error": "method not allowed"})
    except core.ApiError as e:
        return respond(e.status, {"error": str(e)})
    except (ValueError, UnicodeDecodeError):  # JSONDecodeError is a ValueError
        return respond(400, {"error": "bad request"})
    except Exception as e:  # never hand internals (SQL, hostnames) to the caller
        print(f"api error: {type(e).__name__}: {e}")  # goes to the function's logs
        return respond(500, {"error": "something went wrong"})
