"""Calls the serverless API handler (mumble/api.py) against the local
docker-compose Postgres, with real signed JWTs from a throwaway RSA key
standing in for Auth0. Skipped if Postgres isn't running."""

import base64
import json
import threading
import time
import urllib.request
from http.server import HTTPServer

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mumble import auth, config, db

DOMAIN = "mumble-test.auth0.local"
AUDIENCE = "https://mumble.test/api"
CLIENT_ID = "test-spa-client"
ICS_SECRET = "test-ics-secret-that-is-long-enough"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
UNVERIFIED = "test|unverified"


def token(sub="test|alice", key=KEY, alg="RS256", **claims):
    now = int(time.time())
    body = {"iss": f"https://{DOMAIN}/", "aud": [AUDIENCE, f"https://{DOMAIN}/userinfo"],
            "azp": CLIENT_ID, "sub": sub, "iat": now, "exp": now + 3600, **claims}
    return jwt.encode(body, key, algorithm=alg)


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def _wipe_test_accounts():
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM accounts WHERE id LIKE 'test|%'")


@pytest.fixture(scope="module")
def fn(monkeypatch_module):
    try:
        psycopg.connect(config.DATABASE_URL, connect_timeout=2).close()
    except psycopg.OperationalError:
        pytest.skip("local Postgres isn't running (docker compose up -d)")
    db.init()
    db.close()
    mp = monkeypatch_module
    for k, v in {"AUTH0_DOMAIN": DOMAIN, "AUTH0_AUDIENCE": AUDIENCE, "AUTH0_CLIENT_ID": CLIENT_ID,
                 "MUMBLE_ICS_SECRET": ICS_SECRET, "DATABASE_URL": config.DATABASE_URL,
                 "MUMBLE_MAX_ACCOUNTS": "1000"}.items():
        mp.setenv(k, v)
    mp.delenv("MUMBLE_ALLOWED_EMAILS", raising=False)
    # Auth0's JWKS and /userinfo, faked: our public key, and an email per account.
    mp.setattr(auth, "_signing_key", lambda t: KEY.public_key())

    def userinfo(t):
        sub = jwt.decode(t, options={"verify_signature": False})["sub"]
        return {"email": f"{sub.split('|')[1]}@example.test", "email_verified": sub != UNVERIFIED}
    mp.setattr(auth, "userinfo", userinfo)
    _wipe_test_accounts()
    from mumble import api
    yield api
    _wipe_test_accounts()


def raw(fn, method, path, body="", headers=None):
    return fn.handle(method, path, headers or {}, body)


def call(fn, method, path, body=None, as_="test|alice", query="", tok=None):
    tok = tok if tok is not None else (token(as_) if as_ else "")
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    res = raw(fn, method, f"{path}?{query}" if query else path,
              json.dumps(body) if body is not None else "", headers)
    out = res["body"]
    if res["headers"].get("Content-Type") == "application/json":
        out = json.loads(out)
    return res["statusCode"], out


def memo_body(n=4):
    return {"mime": "audio/webm", "audio": base64.b64encode(b"x" * n).decode()}


def add_item(account_id, title="Meeting with Priya", kind="event"):
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        memo = conn.execute("INSERT INTO memos (account_id, mime, status) VALUES (%s, 'audio/webm', 'done') "
                            "RETURNING id", (account_id,)).fetchone()[0]
        return conn.execute(
            "INSERT INTO items (memo_id, account_id, kind, title, detail, when_text, due_at) VALUES "
            "(%s, %s, %s, %s, 'about the hackathon', 'Tuesday at 10am', '2026-10-27 14:00+00') RETURNING id",
            (memo, account_id, kind, title)).fetchone()[0]


def set_quota(account_id, **quota):
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute("UPDATE accounts SET quota = %s WHERE id = %s", (json.dumps(quota), account_id))


# --- sign-in ---

def test_requires_a_token(fn):
    assert call(fn, "GET", "/digest", as_=None)[0] == 401


@pytest.mark.parametrize("bad", [
    "garbage",
    "é€ünïcode",
    token(exp=int(time.time()) - 120),                       # expired
    token(aud="https://someone-else/api"),                     # another API
    token(iss="https://evil.auth0.com/"),                      # another tenant
    token(azp="some-other-app"),                               # another app on our tenant
    token(key=OTHER_KEY),                                      # forged signature
    jwt.encode({"sub": "test|alice", "aud": AUDIENCE, "iss": f"https://{DOMAIN}/", "azp": CLIENT_ID,
                "exp": int(time.time()) + 60, "iat": int(time.time())}, None, algorithm="none"),
])
def test_bad_tokens_are_refused(fn, bad):
    assert call(fn, "GET", "/digest", tok=bad)[0] == 401


def test_hs256_signed_with_the_public_key_is_refused(fn):
    # The classic algorithm-confusion trick: HMAC "signed" with the RSA public key.
    pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    now = int(time.time())
    forged = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": "test|alice", "aud": AUDIENCE, "iss": f"https://{DOMAIN}/", "azp": CLIENT_ID,
        "iat": now, "exp": now + 60}).encode()).rstrip(b"=").decode()
    import hashlib
    import hmac as _hmac
    sig = base64.urlsafe_b64encode(_hmac.new(pem, f"{forged}.{payload}".encode(), hashlib.sha256).digest())
    assert call(fn, "GET", "/digest", tok=f"{forged}.{payload}.{sig.rstrip(b'=').decode()}")[0] == 401


def test_sign_in_is_closed_when_auth0_is_not_configured(fn, monkeypatch):
    monkeypatch.setenv("AUTH0_DOMAIN", "")
    assert call(fn, "GET", "/digest")[0] == 401


def test_first_sign_in_creates_the_account(fn):
    status, me = call(fn, "GET", "/me", as_="test|newbie")
    assert status == 200 and me["email"] == "newbie@example.test"
    assert me["quota"]["memos_per_day"] == 20 and me["usage"]["memos_24h"] == 0


def test_unverified_email_gets_no_account(fn):
    assert call(fn, "GET", "/me", as_=UNVERIFIED)[0] == 403
    with psycopg.connect(config.DATABASE_URL) as conn:
        assert conn.execute("SELECT 1 FROM accounts WHERE id = %s", (UNVERIFIED,)).fetchone() is None


def test_invite_only(fn, monkeypatch):
    monkeypatch.setenv("MUMBLE_ALLOWED_EMAILS", "alice@example.test, someone@else.test")
    assert call(fn, "GET", "/me", as_="test|stranger")[0] == 403
    assert call(fn, "GET", "/me", as_="test|alice")[0] == 200


def test_sign_ups_stop_at_the_account_cap(fn, monkeypatch):
    call(fn, "GET", "/me", as_="test|alice")
    with psycopg.connect(config.DATABASE_URL) as conn:
        n = conn.execute("SELECT count(*) FROM accounts").fetchone()[0]
    monkeypatch.setenv("MUMBLE_MAX_ACCOUNTS", str(n))
    assert call(fn, "GET", "/me", as_="test|latecomer")[0] == 403
    assert call(fn, "GET", "/me", as_="test|alice")[0] == 200  # existing accounts still work


def test_disabled_account(fn):
    call(fn, "GET", "/me", as_="test|banned")
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute("UPDATE accounts SET disabled = true WHERE id = 'test|banned'")
    assert call(fn, "GET", "/me", as_="test|banned")[0] == 403


# --- every account only sees its own things ---

def test_accounts_cannot_see_or_touch_each_other(fn):
    call(fn, "GET", "/me", as_="test|alice")
    call(fn, "GET", "/me", as_="test|bob")
    status, memo = call(fn, "POST", "/api/memos", memo_body(), as_="test|alice")
    assert status == 200
    item = add_item("test|alice", "Alice's secret plan")

    assert any(m["id"] == memo["id"] for m in call(fn, "GET", "/memos", as_="test|alice")[1])
    assert all(m["id"] != memo["id"] for m in call(fn, "GET", "/memos", as_="test|bob")[1])
    assert all(i["id"] != item for i in call(fn, "GET", "/digest", as_="test|bob")[1]["items"])
    assert call(fn, "GET", "/search", as_="test|bob", query="q=secret")[1] == []
    assert call(fn, "GET", "/search", as_="test|alice", query="q=secret")[1][0]["id"] == item
    assert call(fn, "PATCH", f"/items/{item}", {"status": "dismissed"}, as_="test|bob")[0] == 404
    assert call(fn, "GET", "/nap", as_="test|bob")[1]["pending"] == 0
    # The nap's totals cover everyone, so they're not handed out.
    last = call(fn, "GET", "/nap", as_="test|bob")[1]["last"]
    assert last is None or set(last) == {"id", "started_at", "finished_at"}


def test_delete_account_removes_everything(fn):
    call(fn, "GET", "/me", as_="test|leaver")
    call(fn, "POST", "/memos", memo_body(), as_="test|leaver")
    add_item("test|leaver")
    assert call(fn, "DELETE", "/account", as_="test|leaver")[1] == {"deleted": True}
    with psycopg.connect(config.DATABASE_URL) as conn:
        assert conn.execute("SELECT count(*) FROM memos WHERE account_id = 'test|leaver'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM items WHERE account_id = 'test|leaver'").fetchone()[0] == 0


# --- quotas ---

def test_daily_memo_quota(fn):
    call(fn, "GET", "/me", as_="test|chatty")
    set_quota("test|chatty", memos_per_day=2)
    assert call(fn, "POST", "/memos", memo_body(), as_="test|chatty")[0] == 200
    assert call(fn, "POST", "/memos", memo_body(), as_="test|chatty")[0] == 200
    status, err = call(fn, "POST", "/memos", memo_body(), as_="test|chatty")
    assert status == 429 and "daily limit" in err["error"]
    assert call(fn, "GET", "/me", as_="test|chatty")[1]["usage"]["memos_24h"] == 2


def test_daily_audio_quota(fn):
    call(fn, "GET", "/me", as_="test|longwinded")
    set_quota("test|longwinded", audio_mb_per_day=1)
    assert call(fn, "POST", "/memos", memo_body(600_000), as_="test|longwinded")[0] == 200
    assert call(fn, "POST", "/memos", memo_body(600_000), as_="test|longwinded")[0] == 429


def test_pending_pile_quota(fn):
    call(fn, "GET", "/me", as_="test|hoarder")
    set_quota("test|hoarder", pending_max=1)
    assert call(fn, "POST", "/memos", memo_body(), as_="test|hoarder")[0] == 200
    assert call(fn, "POST", "/memos", memo_body(), as_="test|hoarder")[1]["error"].startswith("the pile is full")


def test_rate_limit(fn):
    call(fn, "GET", "/me", as_="test|spammer")
    set_quota("test|spammer", requests_per_minute=3)
    codes = [call(fn, "GET", "/me", as_="test|spammer")[0] for _ in range(5)]
    assert 429 in codes and codes[0] == 200


def test_junk_quota_overrides_fall_back_to_defaults(fn):
    call(fn, "GET", "/me", as_="test|weird")
    set_quota("test|weird", memos_per_day="lots", pending_max=True, unknown=5)
    q = call(fn, "GET", "/me", as_="test|weird")[1]["quota"]
    assert q["memos_per_day"] == 20 and q["pending_max"] == 40 and "unknown" not in q


# --- the rest of the API ---

def test_preflight(fn):
    assert raw(fn, "OPTIONS", "/memos")["statusCode"] == 204


def test_upload_and_list(fn):
    status, memo = call(fn, "POST", "/api/memos", {"mime": "audio/webm;codecs=opus",
                                                   "audio": base64.b64encode(b"\x1aE\xdf\xa3 fake").decode()})
    assert status == 200 and memo["status"] == "pending"
    assert call(fn, "GET", "/memos", query="limit=5")[1][0]["id"] == memo["id"]


def test_rejects_non_audio_and_huge(fn):
    assert call(fn, "POST", "/memos", {"mime": "text/html", "audio": "aGk="})[0] == 415
    assert call(fn, "POST", "/memos", memo_body(800 * 1024))[0] == 413


def test_accepts_video_labelled_recordings(fn):
    status, memo = call(fn, "POST", "/memos", {"mime": "video/webm", "audio": "ZmFrZQ=="})
    assert status == 200
    with psycopg.connect(config.DATABASE_URL) as conn:
        assert conn.execute("SELECT mime FROM memos WHERE id=%s", (memo["id"],)).fetchone()[0] == "audio/webm"


def test_digest_patch_and_calendar_link(fn):
    call(fn, "GET", "/me")
    item = add_item("test|alice")
    status, d = call(fn, "GET", "/digest")
    assert status == 200
    link = next(i["ics"] for i in d["items"] if i["id"] == item)
    status, ics = call(fn, "GET", f"/items/{item}/event.ics", as_=None, query=link)
    assert status == 200 and "DTSTART:20261027T140000Z" in ics
    # A link only opens its own event, and needs a real signature.
    assert call(fn, "GET", f"/items/{item + 1}/event.ics", as_=None, query=link)[0] == 401
    assert call(fn, "GET", f"/items/{item}/event.ics", as_=None, query="exp=1&sig=x")[0] == 401
    assert call(fn, "PATCH", f"/items/{item}", {"status": "dismissed"})[1]["status"] == "dismissed"
    assert call(fn, "PATCH", f"/items/{item}", {"status": "lol"})[0] == 422


def test_calendar_links_need_a_real_secret(fn, monkeypatch):
    monkeypatch.setenv("MUMBLE_ICS_SECRET", "short")
    item = add_item("test|alice")
    link = next(i["ics"] for i in call(fn, "GET", "/digest")[1]["items"] if i["id"] == item)
    assert call(fn, "GET", f"/items/{item}/event.ics", as_=None, query=link)[0] == 401


def test_ics_cannot_be_injected(fn):
    item = add_item("test|alice", "Lunch\r\nATTENDEE:mailto:evil@example.com\rX:1")
    link = next(i["ics"] for i in call(fn, "GET", "/digest")[1]["items"] if i["id"] == item)
    ics = call(fn, "GET", f"/items/{item}/event.ics", as_=None, query=link)[1]
    assert not any(line.startswith(("ATTENDEE", "X:")) for line in ics.split("\r\n"))
    assert "SUMMARY:Lunch\\nATTENDEE:mailto:evil@example.com\\nX:1" in ics


def test_unknown_route(fn):
    assert call(fn, "GET", "/nope")[0] == 404


def test_negative_and_junk_limits(fn):
    assert call(fn, "GET", "/memos", query="limit=-5")[0] == 200
    assert call(fn, "GET", "/memos", query="limit=lots")[0] == 200


def test_bad_bodies(fn):
    auth_header = {"Authorization": f"Bearer {token()}"}
    assert raw(fn, "POST", "/memos", "[1,2]", auth_header)["statusCode"] == 400
    assert raw(fn, "POST", "/memos", "{nope", auth_header)["statusCode"] == 400


def test_database_errors_do_not_leak(fn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:secret@127.0.0.1:1/nowhere")
    status, body = call(fn, "GET", "/digest")
    assert status == 500 and body == {"error": "something went wrong"}


def test_like_wildcards_are_literal(fn):
    assert call(fn, "GET", "/search", query="q=%25%25")[1] == []
    assert call(fn, "GET", "/search", query="q=__")[1] == []


def test_security_headers(fn):
    res = raw(fn, "GET", "/digest")
    assert res["headers"]["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in res["headers"]["Content-Security-Policy"]


def test_no_wildcard_cors_by_default(fn):
    assert "Access-Control-Allow-Origin" not in raw(fn, "GET", "/digest")["headers"]


def test_vercel_entrypoint_over_http(fn):
    # api/index.py exactly as Vercel runs it: a BaseHTTPRequestHandler class.
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("vercel_index", Path(__file__).parent.parent / "api/index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    server = HTTPServer(("127.0.0.1", 0), mod.handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    auth_header = {"Authorization": f"Bearer {token()}"}
    try:
        req = urllib.request.Request(f"{base}/api/memos", method="POST", data=json.dumps(memo_body()).encode(),
                                     headers={**auth_header, "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            memo = json.loads(r.read())
            assert r.headers["X-Frame-Options"] == "DENY"
        with urllib.request.urlopen(urllib.request.Request(f"{base}/api/memos?limit=1", headers=auth_header)) as r:
            assert json.loads(r.read())[0]["id"] == memo["id"]
        req = urllib.request.Request(f"{base}/api/account", method="DELETE",
                                     headers={"Authorization": f"Bearer {token('test|via-http')}"})
        with urllib.request.urlopen(req) as r:
            assert json.loads(r.read()) == {"deleted": True}
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/api/digest")
        assert e.value.code == 401
    finally:
        server.shutdown()
