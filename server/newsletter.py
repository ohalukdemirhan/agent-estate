"""Newsletter sign-up — an address and a timestamp, nothing else.

Mount on whatever serves the public page:

    from fastapi import FastAPI
    from server.newsletter import router, init_db

    app = FastAPI()
    init_db()
    app.include_router(router)

Deliberately minimal. No double opt-in e-mail, no tracking pixel, no IP
address, no user agent — the list is a column of addresses and the time each
one arrived. Reading the list requires the registry token; writing to it does
not, because that is the entire point of a sign-up form.
"""

from __future__ import annotations

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

DB_PATH = Path(os.environ.get("ECOM_DB_PATH", "/var/lib/ecom/registry.sqlite"))
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


def _rate_limited(request: Request) -> bool:
    client = request.client.host if request.client else "unknown"
    # The key is hashed so the table of recent callers holds no addresses
    # even in memory; it exists only to count.
    key = str(hash(client))
    now = time.monotonic()
    hits = [t for t in _recent[key] if now - t < _RATE_WINDOW_SECONDS]
    _recent[key] = hits + [now]
    return len(hits) >= _RATE_LIMIT


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

    # Signing up twice is not an error worth surfacing — it tells an
    # enumerating caller whether an address is already on the list.
    return Response(status_code=204)


@router.get("")
def export(authorization: str = Header(default="")) -> dict:
    """Return the list. Requires the same bearer token as the registry."""
    if not API_TOKEN or authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorised")

    with closing(sqlite3.connect(DB_PATH)) as connection:
        rows = connection.execute(
            "SELECT email, created_at FROM newsletter_subscribers "
            "ORDER BY created_at"
        ).fetchall()

    return {
        "count": len(rows),
        "subscribers": [{"email": e, "created_at": t} for e, t in rows],
    }


@router.delete("/{email}", status_code=204, response_class=Response)
def unsubscribe(email: str, authorization: str = Header(default="")) -> Response:
    if not API_TOKEN or authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorised")

    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.execute(
            "DELETE FROM newsletter_subscribers WHERE email = ?", (email,)
        )
        connection.commit()
    return Response(status_code=204)
