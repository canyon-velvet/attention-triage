"""`triage ui`: the review inbox and its API (SPEC.md §6) on 127.0.0.1."""

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel

from attention_triage import hook, review, store
from attention_triage.digest import digest

# Loopback only: the digest shows commands, paths and URLs from every project.
HOST = "127.0.0.1"
DEFAULT_PORT = 8765

app = FastAPI()
# Binding to loopback isn't enough: a web page can point its own name at 127.0.0.1 (DNS
# rebinding) and read the API as same-origin. Its requests still carry that name as the Host.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Any site the user visits can make the browser POST here, with our Host (CSRF), e.g. to
    dismiss flags unseen. Browsers send an Origin on such writes; only the inbox's own is
    accepted. A request without one (curl) isn't from another site's page."""
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD") and origin not in (
        None,
        f"http://{request.headers.get('host')}",
    ):
        return PlainTextResponse("Cross-origin write refused", status_code=403)
    return await call_next(request)


@app.get("/api/digest")
def get_digest() -> dict:
    with closing(store.connect()) as conn:
        return digest(conn)


class Dismiss(BaseModel):
    scope: Literal["once"]  # TODO(#15): "future" creates a suppression


@app.post("/api/flags/{flag_id}/dismiss")
def dismiss(flag_id: int, body: Dismiss) -> dict:
    return set_status(flag_id, "dismissed")


@app.post("/api/flags/{flag_id}/ack")
def acknowledge(flag_id: int) -> dict:
    return set_status(flag_id, "acknowledged")


def set_status(flag_id: int, status: str) -> dict:
    with closing(store.connect()) as conn:
        changed = review.set_status(conn, flag_id, status)
    if changed is None:
        raise HTTPException(404, "No such flag")
    if not changed:
        raise HTTPException(409, "Flag is not open")
    return {"id": flag_id, "status": status}


class MarkAll(BaseModel):
    """What the page showed: its flags, and the digest's `as_of`. Flags and actions that arrived
    after it stay for the next review."""

    flag_ids: list[int]
    as_of: AwareDatetime


@app.post("/api/review/mark-all")
def mark_all(body: MarkAll) -> dict:
    at = min(body.as_of, datetime.now(UTC))  # a client can't start the window in the future
    with closing(store.connect()) as conn:
        return {"dismissed": review.mark_all(conn, body.flag_ids, at)}


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


# The inbox page, built from web/ (`pnpm build`). Mounted last so the /api routes match first.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True))


def serve(port: int) -> None:
    """Store and flag spooled events first, so the digest isn't missing the ones the hook couldn't
    store."""
    with closing(store.connect()) as conn:
        hook.flag(conn, store.ingest_spool(conn))
    uvicorn.run(app, host=HOST, port=port)
