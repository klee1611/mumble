"""Local development server. In production the same API runs as a DigitalOcean
Function (functions/packages/mumble/api) and the nap runs on GitHub Actions."""

import secrets
import subprocess
import sys
import threading
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, core, db
from .structure import embed

WEB = Path(__file__).parent.parent / "web"

app = FastAPI(title="Mumble")
_nap_lock = threading.Lock()


@app.on_event("startup")
def _startup() -> None:
    db.init()


@app.exception_handler(core.ApiError)
def _api_error(_: Request, e: core.ApiError):
    return JSONResponse({"error": str(e)}, status_code=e.status)


def auth(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not config.TOKEN or not secrets.compare_digest(token, config.TOKEN):
        raise HTTPException(401, "bad token")


class NewMemo(BaseModel):
    mime: str
    audio: str  # base64


class ItemPatch(BaseModel):
    status: str


@app.post("/api/memos", dependencies=[Depends(auth)])
def add_memo(memo: NewMemo):
    with db.connect() as conn:
        return core.add_memo(conn, memo.mime, memo.audio)


@app.get("/api/memos", dependencies=[Depends(auth)])
def list_memos(limit: int = 50):
    with db.connect() as conn:
        return core.list_memos(conn, limit)


def _run_nap() -> None:
    try:
        # Separate process, like the GitHub Actions job, so Whisper's memory is freed afterwards.
        subprocess.run([sys.executable, "-m", "mumble.nap"], check=False)
    finally:
        _nap_lock.release()


@app.post("/api/nap", dependencies=[Depends(auth)])
def nap_now():
    if not _nap_lock.acquire(blocking=False):
        return {"started": False, "reason": "already napping"}
    threading.Thread(target=_run_nap, daemon=True).start()
    return {"started": True}


@app.get("/api/nap", dependencies=[Depends(auth)])
def nap_status():
    with db.connect() as conn:
        status = core.nap_status(conn)
    return {**status, "napping": status["napping"] or _nap_lock.locked()}


@app.get("/api/digest", dependencies=[Depends(auth)])
def digest():
    with db.connect() as conn:
        return core.digest(conn, config.TOKEN)


@app.patch("/api/items/{item_id}", dependencies=[Depends(auth)])
def update_item(item_id: int, patch: ItemPatch):
    with db.connect() as conn:
        return core.set_item_status(conn, item_id, patch.status)


@app.get("/api/items/{item_id}/event.ics")
def item_ics(item_id: int, exp: str = "", sig: str = ""):
    # Calendar apps open this link directly, so it carries a signature that
    # only unlocks this one event, never the API token.
    if not core.check_ics(config.TOKEN, item_id, exp, sig):
        raise HTTPException(401, "bad or expired link")
    with db.connect() as conn:
        name, body = core.item_ics(conn, item_id)
    return PlainTextResponse(body, media_type="text/calendar",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/search", dependencies=[Depends(auth)])
def search(q: str = Query(min_length=2, max_length=300), limit: int = 10):
    # Locally Ollama is right here, so search by meaning as well as by keyword.
    try:
        with httpx.Client() as client:
            vector = embed([q], client)[0]
    except httpx.HTTPError:
        vector = None
    with db.connect() as conn:
        return core.search(conn, q, limit, vector)


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
