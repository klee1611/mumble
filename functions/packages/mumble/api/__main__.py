"""Mumble's API as a single DigitalOcean web function (raw HTTP).

The logic lives in core.py, copied here from mumble/core.py at deploy time so
the local server and the function can't drift apart.
"""

import base64
import json
import os
import re
import urllib.parse
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

import core

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, PATCH, OPTIONS",
}

# Works whether the function is called directly (/memos) or through an
# App Platform route (/api/memos): only the tail of the path matters.
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


def main(args: dict) -> dict:
    http = args.get("http", {})
    method = http.get("method", "GET").upper()
    headers = {k.lower(): v for k, v in (http.get("headers") or {}).items()}
    query = dict(p.split("=", 1) if "=" in p else (p, "") for p in (http.get("queryString") or "").split("&") if p)
    query = {k: urllib.parse.unquote_plus(v) for k, v in query.items()}

    if method == "OPTIONS":
        return respond(204, "")

    m = ROUTE.search(http.get("path") or "")
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

    body = http.get("body") or ""
    if body and http.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
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
    except (ValueError, json.JSONDecodeError):
        return respond(400, {"error": "bad request"})
    except Exception as e:  # never hand internals (SQL, hostnames) to the caller
        print(f"api error: {type(e).__name__}: {e}")  # goes to the function's logs
        return respond(500, {"error": "something went wrong"})
