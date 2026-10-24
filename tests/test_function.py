"""Calls the DigitalOcean Function handler the way DO does (raw web args),
against the local docker-compose Postgres. Skipped if that isn't running."""

import base64
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import psycopg
import pytest

from mumble import config, db

ROOT = Path(__file__).parent.parent
FN_DIR = ROOT / "functions/packages/mumble/api"
TOKEN = "test-token-that-is-long-enough-0123"


@pytest.fixture(scope="module")
def fn(monkeypatch_module):
    try:
        psycopg.connect(config.DATABASE_URL, connect_timeout=2).close()
    except psycopg.OperationalError:
        pytest.skip("local Postgres isn't running (docker compose up -d)")
    db.init()
    db.close()
    shutil.copy(ROOT / "mumble/core.py", FN_DIR / "core.py")  # what deploy-functions.sh does
    sys.path.insert(0, str(FN_DIR))
    monkeypatch_module.setenv("MUMBLE_TOKEN", TOKEN)
    monkeypatch_module.setenv("DATABASE_URL", config.DATABASE_URL)
    spec = importlib.util.spec_from_file_location("mumble_fn", FN_DIR / "__main__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.path.remove(str(FN_DIR))


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def call(fn, method, path, body=None, token=TOKEN, query=""):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    res = fn.main({"http": {"method": method, "path": path, "headers": headers, "queryString": query,
                            "body": json.dumps(body) if body is not None else ""}})
    out = res["body"]
    if res["headers"].get("Content-Type") == "application/json":
        out = json.loads(out)
    return res["statusCode"], out


def test_requires_token(fn):
    assert call(fn, "GET", "/digest", token="nope")[0] == 401


def test_preflight(fn):
    status = fn.main({"http": {"method": "OPTIONS", "path": "/memos", "headers": {}}})["statusCode"]
    assert status == 204


def test_upload_and_list(fn):
    audio = base64.b64encode(b"\x1aE\xdf\xa3 fake webm").decode()
    status, memo = call(fn, "POST", "/api/memos", {"mime": "audio/webm;codecs=opus", "audio": audio})
    assert status == 200 and memo["status"] == "pending"
    status, memos = call(fn, "GET", "/memos", query="limit=5")
    assert status == 200 and memos[0]["id"] == memo["id"]
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM memos WHERE id = %s", (memo["id"],))


def test_rejects_non_audio_and_huge(fn):
    assert call(fn, "POST", "/memos", {"mime": "text/html", "audio": "aGk="})[0] == 415
    big = base64.b64encode(b"x" * (800 * 1024)).decode()
    assert call(fn, "POST", "/memos", {"mime": "audio/webm", "audio": big})[0] == 413


def test_digest_search_patch_ics(fn):
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        memo_id = conn.execute("INSERT INTO memos (mime, status) VALUES ('audio/webm', 'done') RETURNING id").fetchone()[0]
        item_id = conn.execute(
            "INSERT INTO items (memo_id, kind, title, detail, when_text, due_at) VALUES "
            "(%s, 'event', 'Meeting with Priya', 'about the hackathon', 'Tuesday at 10am', "
            "'2026-10-27 14:00+00') RETURNING id", (memo_id,)).fetchone()[0]

    status, d = call(fn, "GET", "/digest")
    assert status == 200 and any(i["id"] == item_id for i in d["items"])

    status, hits = call(fn, "GET", "/search", query="q=priya")
    assert status == 200 and hits[0]["id"] == item_id

    link = next(i["ics"] for i in d["items"] if i["id"] == item_id)
    status, ics = call(fn, "GET", f"/items/{item_id}/event.ics", token=None, query=link)
    assert status == 200 and "DTSTART:20261027T140000Z" in ics
    # The API token must not work in the URL, and a link only opens its own event.
    assert call(fn, "GET", f"/items/{item_id}/event.ics", token=None, query=f"t={TOKEN}")[0] == 401
    assert call(fn, "GET", f"/items/{item_id + 1}/event.ics", token=None, query=link)[0] == 401
    assert call(fn, "GET", f"/items/{item_id}/event.ics", token=None, query="exp=1&sig=x")[0] == 401

    status, row = call(fn, "PATCH", f"/items/{item_id}", {"status": "dismissed"})
    assert status == 200 and row["status"] == "dismissed"
    assert call(fn, "PATCH", f"/items/{item_id}", {"status": "lol"})[0] == 422
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM memos WHERE id = %s", (memo_id,))


def test_unknown_route(fn):
    assert call(fn, "GET", "/nope")[0] == 404


def test_accepts_video_labelled_recordings(fn):
    audio = base64.b64encode(b"fake").decode()
    status, memo = call(fn, "POST", "/memos", {"mime": "video/webm", "audio": audio})
    assert status == 200
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        assert conn.execute("SELECT mime FROM memos WHERE id=%s", (memo["id"],)).fetchone()[0] == "audio/webm"
        conn.execute("DELETE FROM memos WHERE id=%s", (memo["id"],))


# --- security regressions (from the audit) ---

def test_non_ascii_token_is_refused_not_crashed(fn):
    assert call(fn, "GET", "/digest", token="é€ünïcode")[0] == 401


def test_short_placeholder_token_never_authorizes(fn, monkeypatch_module):
    monkeypatch_module.setenv("MUMBLE_TOKEN", "change-me")
    try:
        assert call(fn, "GET", "/digest", token="change-me")[0] == 401
    finally:
        monkeypatch_module.setenv("MUMBLE_TOKEN", TOKEN)


def test_negative_and_junk_limits(fn):
    assert call(fn, "GET", "/memos", query="limit=-5")[0] == 200
    assert call(fn, "GET", "/memos", query="limit=lots")[0] == 200


def test_bad_bodies(fn):
    res = fn.main({"http": {"method": "POST", "path": "/memos", "body": "[1,2]",
                            "headers": {"Authorization": f"Bearer {TOKEN}"}}})
    assert res["statusCode"] == 400
    res = fn.main({"http": {"method": "POST", "path": "/memos", "body": "{nope",
                            "headers": {"Authorization": f"Bearer {TOKEN}"}}})
    assert res["statusCode"] == 400


def test_database_errors_do_not_leak(fn, monkeypatch_module):
    monkeypatch_module.setenv("DATABASE_URL", "postgresql://nobody:secret@127.0.0.1:1/nowhere")
    try:
        status, body = call(fn, "GET", "/digest")
    finally:
        monkeypatch_module.setenv("DATABASE_URL", config.DATABASE_URL)
    assert status == 500 and body == {"error": "something went wrong"}


def test_security_headers(fn):
    res = fn.main({"http": {"method": "GET", "path": "/digest", "headers": {}}})
    assert res["headers"]["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in res["headers"]["Content-Security-Policy"]


def test_ics_cannot_be_injected(fn):
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        memo_id = conn.execute("INSERT INTO memos (mime, status) VALUES ('audio/webm', 'done') RETURNING id").fetchone()[0]
        item_id = conn.execute(
            "INSERT INTO items (memo_id, kind, title, detail, due_at) VALUES "
            "(%s, 'event', %s, 'x', '2026-10-27 14:00+00') RETURNING id",
            (memo_id, "Lunch\r\nATTENDEE:mailto:evil@example.com\rX:1")).fetchone()[0]
    d = call(fn, "GET", "/digest")[1]
    link = next(i["ics"] for i in d["items"] if i["id"] == item_id)
    ics = call(fn, "GET", f"/items/{item_id}/event.ics", token=None, query=link)[1]
    assert not any(line.startswith(("ATTENDEE", "X:")) for line in ics.split("\r\n"))
    assert "SUMMARY:Lunch\\nATTENDEE:mailto:evil@example.com\\nX:1" in ics
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM memos WHERE id = %s", (memo_id,))


def test_like_wildcards_are_literal(fn):
    assert call(fn, "GET", "/search", query="q=%25%25")[1] == []
    assert call(fn, "GET", "/search", query="q=__")[1] == []
