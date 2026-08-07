"""Newsletter sign-up — an address and a timestamp, nothing else.

Mount on whatever serves the public page:

    from fastapi import FastAPI
    from server.newsletter import router, init_db

    app = FastAPI()
    init_db()
    app.include_router(router)

**The subscriber list lives in its own SQLite file, never in the registry.**
That separation is load-bearing, not tidiness:

  * `create_backup` snapshots the registry and pushes it to a git remote.
    Addresses in that file would be published with every snapshot and would
    then be unremovable from git history.
  * SQLite has a single writer. An unauthenticated public endpoint writing
    to the registry can block an agent's `log_result` under load; the thing
    lost in a flood would not be a subscription.
  * Nothing an anonymous caller submits should ever land in a file an agent
    reads. The registry's tool surface is supposed to be the only way in —
    a public form in the same file quietly makes that untrue.

No double opt-in, no tracking pixel, no IP address, no user agent.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

router = APIRouter(prefix="/api/newsletter", tags=["newsletter"])

# Separate from ECOM_DB_PATH by default, and deliberately so — see above.
DB_PATH = Path(
    os.environ.get("ECOM_SUBSCRIBERS_DB", "/var/lib/ecom/subscribers.sqlite")
)
API_TOKEN = os.environ.get("ECOM_API_TOKEN", "")

# One address per submission, a handful of submissions per source per hour.
# Not a defence against a determined flood — that belongs at the proxy — but
# enough that a stuck client cannot fill the table.
_RATE_WINDOW_SECONDS = 3600
_RATE_LIMIT = 5
_recent: dict[str, list[float]] = defaultdict(list)

_LOCAL_PART = re.compile(r"^[^@]{1,64}@")


class Subscription(BaseModel):
    email: EmailStr
    # Honeypot. A real browser leaves it empty because it is off-screen and
    # not focusable; a form-filling bot completes every field it can see.
    company: str = Field(default="", max_length=200)


def init_db() -> None:
    if DB_PATH.resolve() == Path(
        os.environ.get("ECOM_DB_PATH", "/var/lib/ecom/registry.sqlite")
    ).resolve():
        raise RuntimeError(
            "ECOM_SUBSCRIBERS_DB must not be the registry database — "
            "subscriber addresses would be published with every backup."
        )
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS newsletter_subscribers (
                id         INTEGER PRIMARY KEY,
                email      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.commit()


# --------------------------------------------------------------------------
# Unsubscribe tokens
# --------------------------------------------------------------------------

def unsubscribe_token(email: str) -> str:
    """Stable per-address token, derived rather than stored.

    Put `…/api/newsletter/unsubscribe?email=…&token=…` in every message, so
    removal is one click rather than a reply somebody has to read and act on.
    A promise that needs a human to keep it does not belong in an estate that
    claims to run unattended.
    """
    if not API_TOKEN:
        raise RuntimeError("ECOM_API_TOKEN must be set to issue unsubscribe links")
    return hmac.new(
        API_TOKEN.encode(), f"unsubscribe:{email.lower()}".encode(), hashlib.sha256
    ).hexdigest()[:32]


def _token_valid(email: str, token: str) -> bool:
    try:
        return hmac.compare_digest(unsubscribe_token(email), token)
    except RuntimeError:
        return False


def _rate_limited(request: Request) -> bool:
    client = request.client.host if request.client else "unknown"
    # The key is hashed so the table of recent callers holds no addresses
    # even in memory; it exists only to count.
    key = str(hash(client))
    now = time.monotonic()
    hits = [t for t in _recent[key] if now - t < _RATE_WINDOW_SECONDS]
    _recent[key] = hits + [now]
    return len(hits) >= _RATE_LIMIT


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@router.post("", status_code=204, response_class=Response)
def subscribe(subscription: Subscription, request: Request) -> Response:
    # A completed honeypot is answered exactly like a success. Telling a bot
    # it was caught only teaches it to try again without the field.
    if subscription.company.strip():
        return Response(status_code=204)

    if _rate_limited(request):
        raise HTTPException(status_code=429, detail="Too many sign-ups; try later.")

    email = subscription.email.strip()
    if not _LOCAL_PART.match(email):
        raise HTTPException(status_code=422, detail="That address looks wrong.")

    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO newsletter_subscribers (email, created_at) "
            "VALUES (?, ?)",
            (email, datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()

    # Signing up twice is not an error worth surfacing — it would tell an
    # enumerating caller whether an address is already on the list.
    return Response(status_code=204)


@router.get("/unsubscribe", response_class=Response)
def unsubscribe(email: str, token: str) -> Response:
    """One-click removal. Wrong token is answered like a success."""
    if _token_valid(email, token):
        with closing(sqlite3.connect(DB_PATH)) as connection:
            connection.execute(
                "DELETE FROM newsletter_subscribers WHERE email = ?", (email,)
            )
            connection.commit()

    return Response(
        content=(
            "<!doctype html><meta charset=utf-8>"
            "<title>Unsubscribed</title>"
            "<p style='font:16px/1.6 Georgia,serif;max-width:34em;margin:4rem auto;"
            "padding:0 1rem'>That address is no longer on the list. "
            "Nothing else was kept.</p>"
        ),
        media_type="text/html",
    )


@router.get("")
def export(authorization: str = Header(default="")) -> dict:
    """Return the list. Requires the registry token."""
    if not API_TOKEN or not hmac.compare_digest(authorization, f"Bearer {API_TOKEN}"):
        raise HTTPException(status_code=401, detail="Unauthorised")

    with closing(sqlite3.connect(DB_PATH)) as connection:
        rows = connection.execute(
            "SELECT email, created_at FROM newsletter_subscribers "
            "ORDER BY created_at"
        ).fetchall()

    return {
        "count": len(rows),
        "subscribers": [
            {
                "email": email,
                "created_at": created_at,
                "unsubscribe_token": unsubscribe_token(email),
            }
            for email, created_at in rows
        ],
    }


@router.delete("/{email}", status_code=204, response_class=Response)
def remove(email: str, authorization: str = Header(default="")) -> Response:
    if not API_TOKEN or not hmac.compare_digest(authorization, f"Bearer {API_TOKEN}"):
        raise HTTPException(status_code=401, detail="Unauthorised")

    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.execute(
            "DELETE FROM newsletter_subscribers WHERE email = ?", (email,)
        )
        connection.commit()
    return Response(status_code=204)
