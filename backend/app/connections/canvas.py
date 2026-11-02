"""Canvas LMS: address checks, OAuth, and read-only API calls."""

import ipaddress
import socket
from datetime import datetime
from urllib.parse import urlencode, urlsplit

from .. import http
from ..config import Settings

OAUTH_CALLBACK = "/api/connections/canvas/callback"
MAX_PAGES = 50


class CanvasError(Exception):
    """A Canvas request failed. Message is for logs."""


class CanvasAuthError(CanvasError):
    """Canvas rejected the token (expired, revoked, or personal tokens disabled)."""


class InvalidCanvasURL(ValueError):
    """The address the student typed can't be a Canvas site we'll talk to."""


# --- Address validation (the server will make requests to this host) ---------------------


def normalize_base_url(raw: str, settings: Settings) -> str:
    """Turn what a student pastes ("canvas.upenn.edu", a course link...) into https://host."""
    raw = raw.strip()
    if not raw:
        raise InvalidCanvasURL("Enter your school's Canvas address, e.g. canvas.upenn.edu.")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise InvalidCanvasURL("That doesn't look like a web address.") from exc
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https":
        raise InvalidCanvasURL("The Canvas address must start with https://.")
    if parts.username or parts.password or port not in (None, 443):
        raise InvalidCanvasURL("That doesn't look like a Canvas address.")
    if not host or "." not in host or host == "localhost" or host.endswith(".localhost"):
        raise InvalidCanvasURL("That doesn't look like a Canvas address.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise InvalidCanvasURL("Use your school's Canvas web address, not an IP address.")
    if settings.canvas_allowed_hosts and host not in settings.canvas_allowed_hosts:
        raise InvalidCanvasURL("Defog doesn't support that school's Canvas yet.")
    return f"https://{host}"


def assert_public_host(base_url: str) -> None:
    """Refuse hosts that resolve to private or internal addresses (protects our network)."""
    host = urlsplit(base_url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise InvalidCanvasURL("We couldn't find that Canvas address. Check the spelling.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise InvalidCanvasURL("That address isn't a public Canvas site.")


# --- OAuth (needs a developer key from the school's Canvas admin) ------------------------


def authorization_url(settings: Settings, base_url: str, state: str) -> str:
    client = settings.canvas_client_for(base_url)
    assert client is not None
    params = {
        "client_id": client["client_id"],
        "response_type": "code",
        "redirect_uri": settings.redirect_uri(OAUTH_CALLBACK),
        "state": state,
    }
    if client.get("scopes"):
        params["scope"] = client["scopes"]
    return f"{base_url}/login/oauth2/auth?{urlencode(params)}"


def _token_request(settings: Settings, base_url: str, data: dict) -> dict:
    client = settings.canvas_client_for(base_url)
    if client is None:
        raise CanvasError(f"no developer key configured for {base_url}")
    with http.client() as c:
        res = c.post(
            f"{base_url}/login/oauth2/token",
            data={
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                **data,
            },
        )
    if res.status_code in (400, 401):
        raise CanvasAuthError(f"token endpoint refused ({res.status_code}): {res.text[:200]}")
    if res.status_code != 200:
        raise CanvasError(f"token endpoint failed ({res.status_code}): {res.text[:200]}")
    return res.json()


def exchange_code(settings: Settings, base_url: str, code: str) -> dict:
    return _token_request(
        settings,
        base_url,
        {
            "grant_type": "authorization_code",
            "redirect_uri": settings.redirect_uri(OAUTH_CALLBACK),
            "code": code,
        },
    )


def refresh_token(settings: Settings, base_url: str, refresh: str) -> dict:
    return _token_request(
        settings, base_url, {"grant_type": "refresh_token", "refresh_token": refresh}
    )


def revoke(base_url: str, access_token: str) -> None:
    """Best effort. Deletes the OAuth token on Canvas's side."""
    try:
        with http.client(timeout=10) as c:
            c.delete(
                f"{base_url}/login/oauth2/token",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except Exception:  # noqa: BLE001 - must not block disconnecting
        pass


# --- Read-only API --------------------------------------------------------------------------


class CanvasAPI:
    def __init__(self, base_url: str, access_token: str) -> None:
        self.base_url = base_url
        self._client = http.client()
        self._client.headers["Authorization"] = f"Bearer {access_token}"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CanvasAPI":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def self_profile(self) -> dict:
        """Checks the token works. A 401 here always means the token is bad."""
        res = self._client.get(f"{self.base_url}/api/v1/users/self")
        if res.status_code == 401:
            raise CanvasAuthError("Canvas rejected the access token")
        if res.status_code != 200:
            raise CanvasError(f"users/self failed ({res.status_code})")
        return res.json()

    def paginate(self, path: str, params: list[tuple[str, str]] | None = None) -> list[dict]:
        """GET every page of a list endpoint. Raises CanvasError on any non-200."""
        url: str | None = f"{self.base_url}{path}"
        query: list[tuple[str, str]] | None = [*(params or []), ("per_page", "100")]
        items: list[dict] = []
        for _ in range(MAX_PAGES):
            if url is None:
                break
            res = self._client.get(url, params=query)
            if res.status_code != 200:
                raise CanvasError(f"GET {path} failed ({res.status_code})")
            page = res.json()
            if not isinstance(page, list):
                raise CanvasError(f"GET {path} returned {type(page).__name__}, expected a list")
            items.extend(page)
            next_link = res.links.get("next", {}).get("url")
            # Only follow pagination links that stay on the same Canvas host.
            url = next_link if next_link and next_link.startswith(self.base_url + "/") else None
            query = None  # the next link already carries the query string
        return items

    def courses(self) -> list[dict]:
        return self.paginate(
            "/api/v1/courses",
            [
                ("enrollment_state", "active"),
                ("include[]", "total_scores"),
                ("include[]", "term"),
            ],
        )

    def assignments(self, course_id: str) -> list[dict]:
        return self.paginate(
            f"/api/v1/courses/{course_id}/assignments",
            [("include[]", "submission"), ("order_by", "due_at")],
        )

    def announcements(self, course_ids: list[str], start: datetime, end: datetime) -> list[dict]:
        return self.paginate(
            "/api/v1/announcements",
            [("context_codes[]", f"course_{cid}") for cid in course_ids]
            + [("start_date", start.isoformat()), ("end_date", end.isoformat())],
        )

    def calendar_events(self, course_ids: list[str], start: datetime, end: datetime) -> list[dict]:
        return self.paginate(
            "/api/v1/calendar_events",
            [("type", "event")]
            + [("context_codes[]", f"course_{cid}") for cid in course_ids]
            + [("start_date", start.isoformat()), ("end_date", end.isoformat())],
        )
