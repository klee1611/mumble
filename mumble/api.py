"""Mumble's API for serverless hosting (api/index.py on Vercel).

Plain HTTP in, plain HTTP out: no web framework, no models, so the function
stays small. The logic lives in core.py, shared with the local FastAPI server.

Every request except a signed calendar link needs an Auth0 access token
(see auth.py). Settings: DATABASE_URL, AUTH0_DOMAIN, AUTH0_AUDIENCE,
AUTH0_CLIENT_ID, MUMBLE_ICS_SECRET.
"""

import json
import os
import re
import urllib.parse
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from . import auth, core

# The app and /api share one origin, so no CORS by default.
# If the app is hosted elsewhere, set CORS_ORIGIN to that exact origin.
_origin = os.environ.get("CORS_ORIGIN", "")
CORS = {
    "Access-Control-Allow-Origin": _origin,
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    "Vary": "Origin",
} if _origin else {}

# Only the tail of the path matters: /api/memos and /memos are the same route.
ROUTE = re.compile(r"(?:^|/)(me|account|memos|nap|digest|review|search|memos/(\d+)/audio|items/(\d+)(/event\.ics)?)/?$")


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
    route, memo_id, item_id, is_ics = m.group(1), m.group(2), m.group(3), m.group(4)

    secret = os.environ.get("MUMBLE_ICS_SECRET", "")
    claims = None
    if is_ics:
        # Calendar apps open this link directly: it carries a signature that
        # only unlocks this one event, and expires.
        if not core.check_ics(secret, int(item_id), query.get("exp", ""), query.get("sig", "")):
            return respond(401, {"error": "bad or expired link"})
    else:
        # Checked before touching the database, so junk tokens cost us nothing.
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
        try:
            claims = auth.verify(token)
        except auth.AuthError as e:
            return respond(401, {"error": str(e)})

    try:
        data = json.loads(body) if body else {}
        if not isinstance(data, dict):
            raise ValueError("body must be an object")
        with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
            if is_ics:
                name, ics = core.item_ics(conn, int(item_id))
                return respond(200, ics, {"Content-Type": "text/calendar",
                                          "Content-Disposition": f'attachment; filename="{name}"'})
            account = core.open_account(conn, claims, lambda: auth.userinfo(token))
            me = account["id"]
            if route == "me":
                return respond(200, core.me(conn, account))
            if route == "account" and method == "DELETE":
                return respond(200, core.delete_account(conn, me))
            if route == "nap" and method == "POST":
                # The nap runs on GitHub's 3 AM schedule; the API holds no GitHub token.
                return respond(200, {"started": False, "reason": "naps run at 3 AM"})
            if route == "memos" and method == "POST":
                return respond(200, core.add_memo(conn, account, data.get("mime", ""), data.get("audio", "")))
            if route == "memos" and method == "GET":
                return respond(200, core.list_memos(conn, me, query.get("limit", 50)))
            if memo_id and method == "GET":
                return respond(200, core.memo_audio(conn, me, int(memo_id)))
            if route == "nap":
                return respond(200, core.nap_status(conn, me, can_nap_now=False))
            if route == "digest":
                return respond(200, core.digest(conn, me, secret))
            if route == "review":
                return respond(200, core.latest_review(conn, me))
            if route == "search":
                return respond(200, core.search(conn, me, query.get("q", ""), query.get("limit", 10)))
            if item_id and method == "PATCH":
                return respond(200, core.set_item_status(conn, me, int(item_id), data.get("status", "")))
        return respond(405, {"error": "method not allowed"})
    except core.ApiError as e:
        return respond(e.status, {"error": str(e)})
    except auth.AuthError as e:
        return respond(401, {"error": str(e)})
    except (ValueError, UnicodeDecodeError):  # JSONDecodeError is a ValueError
        return respond(400, {"error": "bad request"})
    except Exception as e:  # never hand internals (SQL, hostnames) to the caller
        print(f"api error: {type(e).__name__}: {e}")  # goes to the function's logs
        return respond(500, {"error": "something went wrong"})
