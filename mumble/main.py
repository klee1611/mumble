"""Local development server. In production the API runs as a Vercel Function
(api/index.py, via mumble/api.py) and the nap runs on GitHub Actions.

Sign-in works like production when AUTH0_* is set. Without it, MUMBLE_DEV=1
signs every request from this machine in as one local account, so you can
hack offline. That shortcut never exists in the cloud API.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, core, db
from .structure import embed

WEB = Path(__file__).parent.parent / "web"
DEV_ACCOUNT = {"sub": "local|dev"}
ICS_SECRET = os.environ.get("MUMBLE_ICS_SECRET", "")

app = FastAPI(title="Mumble")
_nap_lock = threading.Lock()


def _dev_mode() -> bool:
    return os.environ.get("MUMBLE_DEV") == "1" and not all(auth.settings().values())


@app.on_event("startup")
def _startup() -> None:
    db.init()


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    res = await call_next(request)
    res.headers.update(core.SECURITY_HEADERS)
    return res


@app.exception_handler(core.ApiError)
def _api_error(_: Request, e: core.ApiError):
    return JSONResponse({"error": str(e)}, status_code=e.status)


def account(request: Request, authorization: str = Header(default="")) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    if _dev_mode():
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(401, "dev mode only answers this machine")
        claims, lookup = DEV_ACCOUNT, lambda: {"email": "dev@localhost", "email_verified": True}
    else:
        try:
            claims = auth.verify(token)
        except auth.AuthError as e:
            raise HTTPException(401, str(e))
        lookup = lambda: auth.userinfo(token)  # noqa: E731
    with db.connect() as conn:
        return core.open_account(conn, claims, lookup)


class NewMemo(BaseModel):
    mime: str
    audio: str  # base64


class ItemPatch(BaseModel):
    status: str


@app.get("/config.js")
def config_js():
    # Same shape as web/config.js, filled from this machine's environment.
    s = auth.settings()
    cfg = {"api": "", "dev": _dev_mode(), "auth0": {k: s[k] for k in ("domain", "client_id", "audience")}}
    return Response(f"window.MUMBLE = {json.dumps(cfg)};\n", media_type="text/javascript")


@app.get("/api/me")
def me(acct: dict = Depends(account)):
    with db.connect() as conn:
        return core.me(conn, acct)


@app.delete("/api/account")
def delete_account(acct: dict = Depends(account)):
    with db.connect() as conn:
        return core.delete_account(conn, acct["id"])


@app.post("/api/memos")
def add_memo(memo: NewMemo, acct: dict = Depends(account)):
    with db.connect() as conn:
        return core.add_memo(conn, acct, memo.mime, memo.audio)


@app.get("/api/memos")
def list_memos(limit: int = 50, acct: dict = Depends(account)):
    with db.connect() as conn:
        return core.list_memos(conn, acct["id"], limit)


@app.get("/api/memos/{memo_id}/audio")
def memo_audio(memo_id: int, acct: dict = Depends(account)):
    with db.connect() as conn:
        return core.memo_audio(conn, acct["id"], memo_id)


def _run_nap() -> None:
    try:
        # Separate process, like the GitHub Actions job, so Whisper's memory is freed afterwards.
        subprocess.run([sys.executable, "-m", "mumble.nap"], check=False)
    finally:
        _nap_lock.release()


@app.post("/api/nap")
def nap_now(acct: dict = Depends(account)):
    # A nap sorts everyone's memos and keeps the CPU busy for minutes, so only
    # the person at this machine (dev mode) may start one. Signed-in accounts wait for 3 AM.
    if not _dev_mode():
        return {"started": False, "reason": "naps run at 3 AM"}
    if not _nap_lock.acquire(blocking=False):
        return {"started": False, "reason": "already napping"}
    threading.Thread(target=_run_nap, daemon=True).start()
    return {"started": True}


@app.get("/api/nap")
def nap_status(acct: dict = Depends(account)):
    with db.connect() as conn:
        status = core.nap_status(conn, acct["id"], can_nap_now=_dev_mode())
    return {**status, "napping": status["napping"] or _nap_lock.locked()}


@app.get("/api/digest")
def digest(acct: dict = Depends(account)):
    with db.connect() as conn:
        return core.digest(conn, acct["id"], ICS_SECRET)


@app.get("/api/review")
def review(acct: dict = Depends(account)):
    with db.connect() as conn:
        return core.latest_review(conn, acct["id"])


@app.patch("/api/items/{item_id}")
def update_item(item_id: int, patch: ItemPatch, acct: dict = Depends(account)):
    with db.connect() as conn:
        return core.set_item_status(conn, acct["id"], item_id, patch.status)


@app.get("/api/items/{item_id}/event.ics")
def item_ics(item_id: int, exp: str = "", sig: str = ""):
    # Calendar apps open this link directly, so it carries a signature that
    # only unlocks this one event.
    if not core.check_ics(ICS_SECRET, item_id, exp, sig):
        raise HTTPException(401, "bad or expired link")
    with db.connect() as conn:
        name, body = core.item_ics(conn, item_id)
    return PlainTextResponse(body, media_type="text/calendar",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/search")
def search(q: str = Query(min_length=2, max_length=300), limit: int = 10, acct: dict = Depends(account)):
    # Locally Ollama is right here, so search by meaning as well as by keyword.
    try:
        with httpx.Client() as client:
            vector = embed([q], client)[0]
    except httpx.HTTPError:
        vector = None
    with db.connect() as conn:
        return core.search(conn, acct["id"], q, limit, vector)


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
