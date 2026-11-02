"""Reading, refreshing and storing a connection's provider tokens (encrypted at rest)."""

from datetime import timedelta

from sqlalchemy.orm import Session as DB

from ..auth import google
from ..config import Settings
from ..models import Connection, utcnow
from ..security import get_cipher
from . import canvas

CANVAS = "canvas"
GOOGLE_CALENDAR = "google_calendar"
PROVIDER_NAMES = {CANVAS: "Canvas", GOOGLE_CALENDAR: "Google Calendar"}
REFRESH_MARGIN = timedelta(seconds=90)

ATTENTION = {
    (CANVAS, "token"): (
        "Canvas stopped accepting your access token. Create a new token in Canvas and reconnect."
    ),
    (CANVAS, "oauth"): "Your Canvas sign-in expired. Reconnect Canvas.",
    (GOOGLE_CALENDAR, "oauth"): (
        "Google Calendar access was removed or expired. Reconnect Google Calendar."
    ),
}


class NeedsAttention(Exception):
    """The connection can't be used until the student reconnects it. Already recorded."""


def store_tokens(conn: Connection, tokens: dict, expires_in: int | None) -> None:
    conn.encrypted_tokens = get_cipher().encrypt(tokens)
    conn.token_expires_at = utcnow() + timedelta(seconds=int(expires_in)) if expires_in else None


def read_tokens(conn: Connection) -> dict:
    return get_cipher().decrypt(conn.encrypted_tokens)


def mark_needs_attention(db: DB, conn: Connection, detail: str | None = None) -> None:
    conn.status = "needs_attention"
    conn.status_detail = detail or ATTENTION.get(
        (conn.provider, conn.auth_type), "Reconnect this source."
    )
    db.commit()


def access_token(db: DB, conn: Connection, settings: Settings, force_refresh: bool = False) -> str:
    """A usable access token, refreshing (and saving) it first if it's about to expire."""
    if conn.status == "needs_attention":
        raise NeedsAttention(conn.status_detail)
    try:
        tokens = read_tokens(conn)
    except ValueError:
        mark_needs_attention(db, conn)
        raise NeedsAttention(conn.status_detail)

    if conn.auth_type == "token":
        return tokens["access_token"]

    fresh = conn.token_expires_at is None or conn.token_expires_at > utcnow() + REFRESH_MARGIN
    if fresh and not force_refresh:
        return tokens["access_token"]
    if not tokens.get("refresh_token"):
        mark_needs_attention(db, conn)
        raise NeedsAttention(conn.status_detail)

    try:
        if conn.provider == CANVAS:
            data = canvas.refresh_token(settings, conn.base_url or "", tokens["refresh_token"])
        else:
            data = google.refresh_access_token(settings, tokens["refresh_token"])
    except (canvas.CanvasAuthError, google.GoogleTokenRevoked):
        mark_needs_attention(db, conn)
        raise NeedsAttention(conn.status_detail)

    tokens["access_token"] = data["access_token"]
    # Canvas keeps the same refresh token; Google may (rarely) send a new one.
    if data.get("refresh_token"):
        tokens["refresh_token"] = data["refresh_token"]
    store_tokens(conn, tokens, data.get("expires_in"))
    db.commit()
    return tokens["access_token"]


def revoke(conn: Connection) -> None:
    """Best-effort revocation with the provider, used on disconnect and account deletion."""
    try:
        tokens = read_tokens(conn)
    except ValueError:
        return
    if conn.provider == GOOGLE_CALENDAR:
        # Revoking the refresh token also revokes its access tokens.
        google.revoke(tokens.get("refresh_token") or tokens["access_token"])
    elif conn.provider == CANVAS and conn.auth_type == "oauth":
        token = tokens["access_token"]
        expired = conn.token_expires_at is not None and conn.token_expires_at <= utcnow()
        if expired and tokens.get("refresh_token"):
            # Canvas revokes the whole grant via a live access token, so refresh first.
            from ..config import get_settings

            try:
                token = canvas.refresh_token(
                    get_settings(), conn.base_url or "", tokens["refresh_token"]
                )["access_token"]
            except Exception:  # noqa: BLE001 - already revoked or unreachable
                return
        canvas.revoke(conn.base_url or "", token)
    # Personal access tokens can only be deleted by the student in Canvas settings.
