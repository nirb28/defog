"""Google OAuth 2.0 / OpenID Connect, shared by sign-in and the Calendar connection."""

from urllib.parse import urlencode

import jwt

from .. import http
from ..config import Settings
from ..models import utcnow

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}

LOGIN_SCOPES = "openid email profile"
LOGIN_CALLBACK = "/api/auth/google/callback"


class GoogleError(Exception):
    """Something went wrong talking to Google. The message is safe to log, not to show."""


class GoogleTokenRevoked(GoogleError):
    """The refresh token is no longer valid (revoked, expired or password changed)."""


def authorization_url(
    settings: Settings,
    *,
    redirect_path: str,
    scope: str,
    state: str,
    code_challenge: str,
    nonce: str | None = None,
    offline: bool = False,
    login_hint: str | None = None,
) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.redirect_uri(redirect_path),
        "response_type": "code",
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if nonce:
        params["nonce"] = nonce
        params["prompt"] = "select_account"
    if offline:
        # A refresh token is only returned with access_type=offline, and only reliably
        # on a fresh consent.
        params["access_type"] = "offline"
        params["prompt"] = "consent"
        params["include_granted_scopes"] = "true"
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(settings: Settings, *, code: str, redirect_path: str, code_verifier: str) -> dict:
    with http.client() as client:
        res = client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.redirect_uri(redirect_path),
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
    if res.status_code != 200:
        raise GoogleError(f"token exchange failed ({res.status_code}): {res.text[:300]}")
    return res.json()


def refresh_access_token(settings: Settings, refresh_token: str) -> dict:
    with http.client() as client:
        res = client.post(
            TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if res.status_code == 400 and "invalid_grant" in res.text:
        raise GoogleTokenRevoked("Google refused the refresh token (invalid_grant)")
    if res.status_code != 200:
        raise GoogleError(f"token refresh failed ({res.status_code}): {res.text[:300]}")
    return res.json()


def revoke(token: str) -> None:
    """Best effort: the student may already have revoked access from their Google account."""
    try:
        with http.client(timeout=10) as client:
            client.post(REVOKE_URL, data={"token": token})
    except Exception:  # noqa: BLE001 - revocation failure must not block disconnecting
        pass


def verify_id_token(settings: Settings, id_token: str, nonce: str) -> dict:
    """Validate the ID token's claims.

    The token came straight from Google's token endpoint over TLS in exchange for our
    authorization code, so per OpenID Connect Core 3.1.3.7 the TLS connection authenticates
    the issuer and the signature check may be skipped. Every claim is still checked.
    """
    try:
        claims = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise GoogleError(f"malformed id_token: {exc}") from exc

    if claims.get("iss") not in ISSUERS:
        raise GoogleError("id_token has the wrong issuer")
    aud = claims.get("aud")
    if aud != settings.google_client_id and not (
        isinstance(aud, list) and settings.google_client_id in aud
    ):
        raise GoogleError("id_token was issued for a different client")
    now = utcnow().timestamp()
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - 60:
        raise GoogleError("id_token has expired")
    if claims.get("nonce") != nonce:
        raise GoogleError("id_token nonce mismatch")
    if not claims.get("sub") or not claims.get("email"):
        raise GoogleError("id_token is missing sub or email")
    if claims.get("email_verified") not in (True, "true"):
        raise GoogleError("Google account email is not verified")
    return claims
