"""Pull a student's Canvas and Google Calendar data into Defog's database.

Each sync fetches everything first, then replaces that connection's rows in one transaction,
so the chatbot never sees a half-written sync. A Postgres advisory lock stops two syncs of
the same connection (scheduled + on-demand, or two servers) from running at once.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx2 as httpx
from sqlalchemy import delete, select, text

from ..config import get_settings
from ..db import get_engine, session_scope
from ..models import Announcement, Assignment, CalendarEvent, Connection, Course, User, utcnow
from . import canvas, gcal
from .textutil import html_to_text
from .tokens import CANVAS, GOOGLE_CALENDAR, NeedsAttention, access_token, mark_needs_attention

log = logging.getLogger(__name__)

EVENTS_PAST = timedelta(weeks=2)
EVENTS_FUTURE = timedelta(weeks=8)
ANNOUNCEMENTS_PAST = timedelta(days=60)
CANVAS_CONTEXT_BATCH = 10  # Canvas accepts at most 10 context codes per request


@dataclass
class SyncResult:
    ok: bool
    skipped: bool = False
    error: str | None = None
    counts: dict[str, int] = field(default_factory=dict)


def _lock_key(connection_id: uuid.UUID) -> int:
    return int.from_bytes(connection_id.bytes[:8], "big", signed=True)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def user_zone(user: User | None) -> ZoneInfo:
    try:
        return ZoneInfo(user.time_zone) if user and user.time_zone else ZoneInfo("UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def sync_connection(connection_id: uuid.UUID) -> SyncResult:
    """Sync one connection. Safe to call from a background thread."""
    with get_engine().connect() as lock_conn:
        got = lock_conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": _lock_key(connection_id)}
        ).scalar()
        lock_conn.commit()  # the lock is session-level; don't sit in an open transaction
        if not got:
            return SyncResult(ok=True, skipped=True)
        try:
            return _sync(connection_id)
        finally:
            lock_conn.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_key(connection_id)}
            )
            lock_conn.commit()


def _sync(connection_id: uuid.UUID) -> SyncResult:
    settings = get_settings()
    with session_scope() as db:
        conn = db.get(Connection, connection_id)
        if conn is None:
            return SyncResult(ok=False, error="Connection no longer exists.")
        user = db.get(User, conn.user_id)
        zone = user_zone(user)

        fetched: dict
        for attempt in range(2):
            try:
                token = access_token(db, conn, settings, force_refresh=attempt > 0)
                db.commit()  # close the read transaction before slow network calls
                if conn.provider == CANVAS:
                    fetched = _fetch_canvas(conn.base_url or "", token)
                else:
                    fetched = _fetch_google(token, conn.settings or {}, zone)
                break
            except NeedsAttention as exc:
                return SyncResult(ok=False, error=str(exc))
            except (canvas.CanvasAuthError, gcal.CalendarAuthError):
                if conn.auth_type == "oauth" and attempt == 0:
                    continue  # the access token may have just expired: refresh and retry once
                mark_needs_attention(db, conn)
                return SyncResult(ok=False, error=conn.status_detail)
            except (canvas.CanvasError, gcal.CalendarError, httpx.HTTPError) as exc:
                log.warning("Sync of %s %s failed: %s", conn.provider, conn.id, exc)
                conn.last_sync_error = "Couldn't reach the service. We'll retry automatically."
                db.commit()
                return SyncResult(ok=False, error=conn.last_sync_error)

        # Re-read and lock the row: the student may have disconnected while we were fetching.
        conn = db.scalar(select(Connection).where(Connection.id == connection_id).with_for_update())
        if conn is None:
            db.rollback()
            return SyncResult(ok=False, error="Connection no longer exists.")
        counts = (
            _write_canvas(db, conn, fetched)
            if conn.provider == CANVAS
            else _write_google(db, conn, fetched, zone)
        )
        conn.last_synced_at = utcnow()
        conn.last_sync_error = None
        conn.status = "connected"
        conn.status_detail = None
        db.commit()
        return SyncResult(ok=True, counts=counts)


# --- Canvas ----------------------------------------------------------------------------------


def _fetch_canvas(base_url: str, token: str) -> dict:
    now = utcnow()
    with canvas.CanvasAPI(base_url, token) as api:
        api.self_profile()  # a 401 here means the token is bad
        courses = [
            c
            for c in api.courses()
            if c.get("id") is not None and c.get("name") and not c.get("access_restricted_by_date")
        ]
        assignments: dict[str, list[dict]] = {}
        for course in courses:
            cid = str(course["id"])
            try:
                assignments[cid] = api.assignments(cid)
            except canvas.CanvasError as exc:
                # Some courses hide assignments from students; skip just that course.
                log.info("Skipping assignments for course %s: %s", cid, exc)
                assignments[cid] = []

        ids = [str(c["id"]) for c in courses]
        announcements: list[dict] = []
        events: list[dict] = []
        for i in range(0, len(ids), CANVAS_CONTEXT_BATCH):
            batch = ids[i : i + CANVAS_CONTEXT_BATCH]
            try:
                announcements += api.announcements(
                    batch, now - ANNOUNCEMENTS_PAST, now + timedelta(days=1)
                )
            except canvas.CanvasError as exc:
                log.info("Skipping announcements batch: %s", exc)
            try:
                events += api.calendar_events(batch, now - EVENTS_PAST, now + EVENTS_FUTURE)
            except canvas.CanvasError as exc:
                log.info("Skipping calendar events batch: %s", exc)
    return {
        "courses": courses,
        "assignments": assignments,
        "announcements": announcements,
        "events": events,
    }


def _student_enrollment(course: dict) -> dict:
    for enrollment in course.get("enrollments") or []:
        if enrollment.get("type") in ("student", "StudentEnrollment"):
            return enrollment
    return {}


def submission_status(sub: dict | None) -> str:
    if not sub:
        return "unsubmitted"
    if sub.get("excused"):
        return "excused"
    if sub.get("missing"):
        return "missing"
    state = sub.get("workflow_state")
    if state == "graded" and sub.get("score") is not None:
        return "graded"
    if sub.get("submitted_at") or state in ("submitted", "pending_review", "graded"):
        return "submitted"
    return "unsubmitted"


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _write_canvas(db, conn: Connection, data: dict) -> dict[str, int]:  # noqa: ANN001
    db.execute(delete(Course).where(Course.connection_id == conn.id))  # cascades to children
    db.execute(delete(CalendarEvent).where(CalendarEvent.connection_id == conn.id))

    course_ids: dict[str, uuid.UUID] = {}
    for c in data["courses"]:
        cid = str(c["id"])
        if cid in course_ids:
            continue
        enrollment = _student_enrollment(c)
        course = Course(
            id=uuid.uuid4(),
            user_id=conn.user_id,
            connection_id=conn.id,
            canvas_id=cid,
            name=str(c["name"])[:300],
            code=(c.get("course_code") or None) and str(c["course_code"])[:100],
            term=((c.get("term") or {}).get("name") or None),
            current_score=_num(enrollment.get("computed_current_score")),
            current_grade=enrollment.get("computed_current_grade"),
            html_url=f"{conn.base_url}/courses/{cid}",
        )
        course_ids[cid] = course.id
        db.add(course)
    db.flush()

    n_assignments = 0
    for cid, items in data["assignments"].items():
        seen: set[str] = set()
        for a in items:
            aid = str(a.get("id"))
            if not a.get("id") or aid in seen or cid not in course_ids:
                continue
            seen.add(aid)
            sub = a.get("submission") or None
            db.add(
                Assignment(
                    user_id=conn.user_id,
                    course_id=course_ids[cid],
                    canvas_id=aid,
                    title=str(a.get("name") or "Untitled assignment")[:500],
                    description=html_to_text(a.get("description")),
                    due_at=parse_dt(a.get("due_at")),
                    points=_num(a.get("points_possible")),
                    score=_num((sub or {}).get("score")),
                    grade=((sub or {}).get("grade") or None) and str(sub["grade"])[:50],
                    submission_status=submission_status(sub),
                    submitted_at=parse_dt((sub or {}).get("submitted_at")),
                    html_url=a.get("html_url"),
                )
            )
            n_assignments += 1

    n_announcements = 0
    seen_ann: set[tuple[str, str]] = set()
    for item in data["announcements"]:
        cid = str(item.get("context_code", "")).removeprefix("course_")
        key = (cid, str(item.get("id")))
        if cid not in course_ids or not item.get("id") or key in seen_ann:
            continue
        seen_ann.add(key)
        db.add(
            Announcement(
                user_id=conn.user_id,
                course_id=course_ids[cid],
                canvas_id=str(item["id"]),
                title=str(item.get("title") or "Announcement")[:500],
                body=html_to_text(item.get("message")),
                author=((item.get("author") or {}).get("display_name") or None),
                posted_at=parse_dt(item.get("posted_at")),
                html_url=item.get("html_url"),
            )
        )
        n_announcements += 1

    n_events = 0
    seen_ev: set[str] = set()
    course_names = {str(c["id"]): str(c["name"]) for c in data["courses"]}
    for ev in data["events"]:
        start = parse_dt(ev.get("start_at"))
        if not start or not ev.get("id") or ev.get("workflow_state") == "deleted":
            continue
        eid = str(ev["id"])
        if eid in seen_ev:
            continue
        seen_ev.add(eid)
        end = parse_dt(ev.get("end_at")) or start
        cid = str(ev.get("context_code", "")).removeprefix("course_")
        all_day = bool(ev.get("all_day"))
        db.add(
            CalendarEvent(
                user_id=conn.user_id,
                connection_id=conn.id,
                source=CANVAS,
                external_id=eid,
                calendar_id=f"course_{cid}",
                calendar_name=course_names.get(cid),
                title=str(ev.get("title") or "Course event")[:500],
                location=(ev.get("location_name") or None),
                start_at=start,
                end_at=max(end, start),
                all_day=all_day,
                busy=not all_day,
                html_url=ev.get("html_url"),
            )
        )
        n_events += 1

    return {
        "courses": len(course_ids),
        "assignments": n_assignments,
        "announcements": n_announcements,
        "events": n_events,
    }


# --- Google Calendar -------------------------------------------------------------------------


def _fetch_google(token: str, conn_settings: dict, zone: ZoneInfo) -> dict:
    now = utcnow()
    calendar_ids = conn_settings.get("calendar_ids") or ["primary"]
    with gcal.CalendarAPI(token) as api:
        calendars = api.calendars()
        names = {c["id"]: c.get("summaryOverride") or c.get("summary") for c in calendars}
        primary = next((c for c in calendars if c.get("primary")), None)
        if primary:
            names["primary"] = primary.get("summaryOverride") or primary.get("summary") or "Primary"
        events: list[tuple[str, dict]] = []
        for cal_id in calendar_ids:
            items = api.events(cal_id, now - EVENTS_PAST, now + EVENTS_FUTURE)
            if items is None:
                log.info("Calendar %s is no longer available; skipping", cal_id)
                continue
            events += [(cal_id, item) for item in items]
    return {"events": events, "names": names}


def _google_time(value: dict, zone: ZoneInfo) -> tuple[datetime | None, bool]:
    if value.get("dateTime"):
        return parse_dt(value["dateTime"]), False
    if value.get("date"):
        try:
            day = date.fromisoformat(value["date"])
        except ValueError:
            return None, True
        return datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc), True
    return None, False


def _declined(event: dict) -> bool:
    return any(
        a.get("self") and a.get("responseStatus") == "declined"
        for a in event.get("attendees") or []
    )


def _write_google(db, conn: Connection, data: dict, zone: ZoneInfo) -> dict[str, int]:  # noqa: ANN001
    db.execute(delete(CalendarEvent).where(CalendarEvent.connection_id == conn.id))
    seen: set[tuple[str, str]] = set()
    count = 0
    for cal_id, ev in data["events"]:
        if ev.get("status") == "cancelled" or not ev.get("id"):
            continue
        key = (cal_id, str(ev["id"]))
        if key in seen:
            continue
        start, all_day = _google_time(ev.get("start") or {}, zone)
        end, _ = _google_time(ev.get("end") or {}, zone)
        if start is None:
            continue
        seen.add(key)
        db.add(
            CalendarEvent(
                user_id=conn.user_id,
                connection_id=conn.id,
                source=GOOGLE_CALENDAR,
                external_id=str(ev["id"])[:255],
                calendar_id=cal_id[:255],
                calendar_name=data["names"].get(cal_id),
                title=str(ev.get("summary") or "(No title)")[:500],
                location=(ev.get("location") or None) and str(ev["location"])[:500],
                start_at=start,
                end_at=max(end or start, start),
                all_day=all_day,
                busy=ev.get("transparency") != "transparent" and not _declined(ev) and not all_day,
                html_url=ev.get("htmlLink"),
            )
        )
        count += 1
    return {"events": count}


# --- Scheduling --------------------------------------------------------------------------------


def due_connection_ids() -> list[uuid.UUID]:
    settings = get_settings()
    cutoff = utcnow() - timedelta(minutes=settings.sync_interval_minutes)
    with session_scope() as db:
        return list(
            db.scalars(
                select(Connection.id).where(
                    Connection.status == "connected",
                    (Connection.last_synced_at.is_(None)) | (Connection.last_synced_at < cutoff),
                )
            )
        )


def is_stale(conn: Connection, minutes: int) -> bool:
    return conn.last_synced_at is None or conn.last_synced_at < utcnow() - timedelta(
        minutes=minutes
    )
