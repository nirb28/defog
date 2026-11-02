"""Google Calendar API calls."""

from datetime import datetime
from urllib.parse import quote

from .. import http

API = "https://www.googleapis.com/calendar/v3"
OAUTH_CALLBACK = "/api/connections/google-calendar/callback"

READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
MAX_PAGES = 20


class CalendarError(Exception):
    pass


class CalendarAuthError(CalendarError):
    """401: the access token is no longer accepted."""


class CalendarScopeError(CalendarError):
    """403 for a write: the student didn't grant the events scope."""


def scopes_for(write: bool) -> str:
    return f"{READ_SCOPE} {WRITE_SCOPE}" if write else READ_SCOPE


class CalendarAPI:
    def __init__(self, access_token: str) -> None:
        self._client = http.client()
        self._client.headers["Authorization"] = f"Bearer {access_token}"

    def __enter__(self) -> "CalendarAPI":
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def _check(self, res, what: str) -> dict:  # noqa: ANN001
        if res.status_code == 401:
            raise CalendarAuthError(f"{what}: 401")
        if res.status_code not in (200, 201):
            raise CalendarError(f"{what} failed ({res.status_code}): {res.text[:200]}")
        return res.json()

    def calendars(self) -> list[dict]:
        items: list[dict] = []
        params: dict = {"maxResults": 250}
        for _ in range(MAX_PAGES):
            data = self._check(
                self._client.get(f"{API}/users/me/calendarList", params=params), "calendarList"
            )
            items.extend(data.get("items", []))
            if not data.get("nextPageToken"):
                break
            params["pageToken"] = data["nextPageToken"]
        return items

    def events(self, calendar_id: str, start: datetime, end: datetime) -> list[dict] | None:
        """Events in [start, end), recurring events expanded. None if the calendar is gone."""
        params: dict = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        }
        items: list[dict] = []
        for _ in range(MAX_PAGES):
            res = self._client.get(
                f"{API}/calendars/{quote(calendar_id, safe='')}/events", params=params
            )
            if res.status_code == 404:
                return None  # unsubscribed, deleted, or no longer shared with the student
            # Anything else (including 403 rate limits) fails the sync, which keeps the
            # existing data and retries later, rather than silently dropping a calendar.
            data = self._check(res, "events")
            items.extend(data.get("items", []))
            if not data.get("nextPageToken"):
                break
            params["pageToken"] = data["nextPageToken"]
        return items

    def insert_event(self, calendar_id: str, body: dict) -> dict:
        res = self._client.post(f"{API}/calendars/{quote(calendar_id, safe='')}/events", json=body)
        if res.status_code == 403 and "insufficient" in res.text.lower():
            # Google's wording: "Request had insufficient authentication scopes" /
            # "Insufficient Permission". Other 403s (e.g. rate limits) are plain errors.
            raise CalendarScopeError(f"insert forbidden: {res.text[:200]}")
        return self._check(res, "insert event")
