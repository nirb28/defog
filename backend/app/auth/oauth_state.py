"""State for OAuth redirects (Google sign-in, Google Calendar, Canvas), kept in a signed cookie.

The cookie holds the random `state` (CSRF protection for the redirect), the PKCE verifier,
the OIDC nonce, and who started the flow. It lives 10 minutes and is cleared on return.
"""

import base64
import hashlib
import secrets
from datetime import timedelta

import jwt
from fastapi import Request, Response

from ..config import Settings
from ..models import utcnow

STATE_COOKIE = "defog_oauth"
LIFETIME = timedelta(minutes=10)


class OAuthStateError(Exception):
    pass


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


def begin(response: Response, settings: Settings, flow: str, **data: str | bool | None) -> str:
    """Store a new state for `flow` and return the `state` value to send to the provider."""
    state = secrets.token_urlsafe(24)
    payload = {"flow": flow, "state": state, "exp": utcnow() + LIFETIME, **data}
    token = jwt.encode(payload, settings.effective_secret_key, algorithm="HS256")
    response.set_cookie(
        STATE_COOKIE,
        token,
        max_age=int(LIFETIME.total_seconds()),
        httponly=True,
        secure=settings.cookie_secure,
        # Lax: the cookie must come back on the provider's top-level redirect to us.
        samesite="lax",
        path="/api",
    )
    return state


def finish(request: Request, response: Response, settings: Settings, flow: str) -> dict:
    """Validate the returning `state` against the cookie. Always clears the cookie."""
    response.delete_cookie(
        STATE_COOKIE, path="/api", httponly=True, secure=settings.cookie_secure, samesite="lax"
    )
    token = request.cookies.get(STATE_COOKIE)
    returned_state = request.query_params.get("state")
    if not token or not returned_state:
        raise OAuthStateError("missing state")
    try:
        payload = jwt.decode(token, settings.effective_secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise OAuthStateError(f"bad state cookie: {exc}") from exc
    if payload.get("flow") != flow or not secrets.compare_digest(
        str(payload.get("state")), returned_state
    ):
        raise OAuthStateError("state mismatch")
    return payload
