"""Mumble's API as a single DigitalOcean web function (raw HTTP).

The logic lives in core.py, copied here from mumble/core.py at deploy time so
the local server and the function can't drift apart.
"""

import base64
import hmac
import json
import os
import re
import urllib.parse
import urllib.request
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
ROUTE = re.compile(r"(?:^|/)(memos|nap|digest|search|items/(\d+)(/event\.ics)?)/?$")


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
    return {"statusCode": status, "headers": {**CORS, **(headers or {})}, "body": payload}


def _authorized(token: str) -> bool:
    expected = os.environ.get("MUMBLE_TOKEN", "")
    return bool(expected) and hmac.compare_digest(token, expected)


def _wake_github() -> dict:
    repo, token = os.environ.get("GH_REPO", ""), os.environ.get("GH_TOKEN", "")
    if not repo or not token:
        raise core.ApiError(503, "GitHub isn't configured")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/dispatches",
        data=json.dumps({"event_type": "nap"}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "mumble"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10):
        return {"started": True}


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
    data = json.loads(body) if body else {}

    try:
        if route == "nap" and method == "POST":
            return respond(200, _wake_github())
        with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
            if route == "memos" and method == "POST":
                return respond(200, core.add_memo(conn, data.get("mime", ""), data.get("audio", "")))
            if route == "memos":
                return respond(200, core.list_memos(conn, int(query.get("limit", 50))))
            if route == "nap":
                return respond(200, core.nap_status(conn))
            if route == "digest":
                return respond(200, core.digest(conn, secret))
            if route == "search":
                return respond(200, core.search(conn, query.get("q", ""), int(query.get("limit", 10))))
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
