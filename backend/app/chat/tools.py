"""The tools the chatbot can call. Each runs against Defog's synced copy of the student's data.

Security: every query is filtered by the user_id of the logged-in student, which comes from
the server-side session, never from the model. The model can't name another student.
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import func, literal, select, union_all
from sqlalchemy.orm import Session as DB

from ..config import Settings
from ..connections import gcal
from ..connections.sync import user_zone
from ..connections.tokens import CANVAS, GOOGLE_CALENDAR
from ..llm import ToolSpec
from ..models import (
    Announcement,
    Assignment,
    CalendarEvent,
    Connection,
    Course,
    PendingAction,
    User,
    utcnow,
)
from .timeutil import BadTime, fmt, free_slots, local_iso, parse_clock, parse_when

MAX_RESULT_CHARS = 14_000
ACTION_LIFETIME = timedelta(hours=24)


@dataclass
class ToolContext:
    db: DB
    user: User
    settings: Settings
    chat_id: uuid.UUID
    zone: ZoneInfo = field(init=False)
    actions: list[dict] = field(default_factory=list)  # confirmation cards to show

    def __post_init__(self) -> None:
        self.zone = user_zone(self.user)

    def connection(self, provider: str) -> Connection | None:
        return self.db.scalar(
            select(Connection).where(
                Connection.user_id == self.user.id, Connection.provider == provider
            )
        )


class ToolError(Exception):
    """Returned to the model as {"error": ...} so it can explain or retry."""


def _not_connected(ctx: ToolContext, provider: str) -> ToolError:
    name = "Canvas" if provider == CANVAS else "Google Calendar"
    return ToolError(
        f"{name} isn't connected, so there's no data for this. Tell the student they can "
        f"connect it on the Connections page: {ctx.settings.app_url}/connections"
    )


def _source_note(conn: Connection, zone: ZoneInfo) -> dict:
    note: dict[str, Any] = {"data_updated": fmt(conn.last_synced_at, zone)}
    if conn.last_synced_at is None:
        note["warning"] = "The first sync is still running, so this data may be incomplete."
    if conn.status == "needs_attention":
        note["warning"] = (
            f"{conn.status_detail} This data may be out of date until the student reconnects "
            "on the Connections page."
        )
    return note


def _require(ctx: ToolContext, provider: str) -> Connection:
    conn = ctx.connection(provider)
    if conn is None:
        raise _not_connected(ctx, provider)
    return conn


# --- Courses ------------------------------------------------------------------------------------


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _courses(ctx: ToolContext) -> list[Course]:
    return list(
        ctx.db.scalars(select(Course).where(Course.user_id == ctx.user.id).order_by(Course.name))
    )


def _match_courses(ctx: ToolContext, query: str | None) -> list[Course]:
    courses = _courses(ctx)
    if not query or not query.strip():
        return courses
    q = _squash(query)
    matches = [
        c for c in courses if q and (q in _squash(c.name) or (c.code and q in _squash(c.code)))
    ]
    if not matches:
        names = "; ".join(f"{c.code or ''} {c.name}".strip() for c in courses) or "none"
        raise ToolError(
            f"No course matches {query!r}. The student's courses are: {names}. "
            "Call again with one of these names or codes."
        )
    return matches


def _course_label(course: Course) -> str:
    return (
        f"{course.code} – {course.name}"
        if course.code and course.code not in course.name
        else course.name
    )


# --- Tool implementations ---------------------------------------------------------------------


def get_assignments(ctx: ToolContext, args: dict) -> dict:
    conn = _require(ctx, CANVAS)
    status = args.get("status") or "upcoming"
    now = utcnow()
    start = parse_when(args.get("from"), ctx.zone)
    end = parse_when(args.get("to"), ctx.zone, end_of_day=True)
    limit = max(1, min(int(args.get("limit") or 25), 50))
    courses = {c.id: c for c in _match_courses(ctx, args.get("course"))}

    q = select(Assignment).where(
        Assignment.user_id == ctx.user.id, Assignment.course_id.in_(list(courses))
    )
    if status == "upcoming":
        q = q.where(
            Assignment.due_at >= (start or now),
            Assignment.due_at <= (end or now + timedelta(days=14)),
        )
        q = q.where(Assignment.submission_status.notin_(["submitted", "graded", "excused"]))
    elif status == "overdue":
        q = q.where(
            Assignment.due_at < now, Assignment.submission_status.in_(["unsubmitted", "missing"])
        )
    elif status in ("missing", "submitted", "graded"):
        q = q.where(Assignment.submission_status == status)
    elif status != "all":
        raise ToolError(
            "status must be one of: upcoming, overdue, missing, submitted, graded, all."
        )
    if status != "upcoming":
        if start:
            q = q.where(Assignment.due_at >= start)
        if end:
            q = q.where(Assignment.due_at <= end)
    q = q.order_by(Assignment.due_at.asc().nulls_last(), Assignment.title)
    rows = list(ctx.db.scalars(q.limit(limit + 1)))

    items = [
        {
            "title": a.title,
            "course": _course_label(courses[a.course_id]),
            "due": fmt(a.due_at, ctx.zone) or "No due date",
            "due_iso": local_iso(a.due_at, ctx.zone),
            "points": a.points,
            "status": a.submission_status,
            "score": a.score,
            "url": a.html_url,
        }
        for a in rows[:limit]
    ]
    window = None
    if status == "upcoming":
        window = (
            f"{fmt(start or now, ctx.zone)} to {fmt(end or now + timedelta(days=14), ctx.zone)}"
        )
    return {
        **_source_note(conn, ctx.zone),
        "status_filter": status,
        "window": window,
        "count": len(items),
        "truncated": len(rows) > limit,
        "items": items,
    }


def get_grades(ctx: ToolContext, args: dict) -> dict:
    conn = _require(ctx, CANVAS)
    courses = _match_courses(ctx, args.get("course"))
    per_course = 15 if args.get("course") else 5
    out = []
    for c in courses:
        graded = list(
            ctx.db.scalars(
                select(Assignment)
                .where(
                    Assignment.user_id == ctx.user.id,
                    Assignment.course_id == c.id,
                    Assignment.score.is_not(None),
                )
                .order_by(Assignment.due_at.desc().nulls_last())
                .limit(per_course)
            )
        )
        out.append(
            {
                "name": _course_label(c),
                "term": c.term,
                "current_score": c.current_score,
                "grade": c.current_grade,
                "url": f"{c.html_url}/grades" if c.html_url else None,
                "recent_scores": [
                    {
                        "title": a.title,
                        "score": a.score,
                        "points": a.points,
                        "due": fmt(a.due_at, ctx.zone, with_time=False),
                        "url": a.html_url,
                    }
                    for a in graded
                ],
            }
        )
    return {
        **_source_note(conn, ctx.zone),
        "note": "current_score is Canvas's running percentage; it may leave out ungraded or unposted work.",
        "courses": out,
    }


def get_announcements(ctx: ToolContext, args: dict) -> dict:
    conn = _require(ctx, CANVAS)
    courses = {c.id: c for c in _match_courses(ctx, args.get("course"))}
    days = max(1, min(int(args.get("days") or 14), 60))
    limit = max(1, min(int(args.get("limit") or 10), 25))
    rows = ctx.db.scalars(
        select(Announcement)
        .where(
            Announcement.user_id == ctx.user.id,
            Announcement.course_id.in_(list(courses)),
            Announcement.posted_at >= utcnow() - timedelta(days=days),
        )
        .order_by(Announcement.posted_at.desc())
        .limit(limit)
    )
    items = [
        {
            "title": a.title,
            "course": _course_label(courses[a.course_id]),
            "posted": fmt(a.posted_at, ctx.zone),
            "author": a.author,
            "text": (a.body or "")[:1500],
            "url": a.html_url,
        }
        for a in rows
    ]
    return {**_source_note(conn, ctx.zone), "days": days, "count": len(items), "items": items}


def search_course_content(ctx: ToolContext, args: dict) -> dict:
    conn = _require(ctx, CANVAS)
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("Give a search query, e.g. 'midterm'.")
    courses = {c.id: c for c in _match_courses(ctx, args.get("course"))}
    tsquery = func.websearch_to_tsquery("english", query)
    # The expressions match the GIN indexes in migration 0001, so these searches use them.
    ann_doc = func.to_tsvector(
        "english",
        func.coalesce(Announcement.title, "") + " " + func.coalesce(Announcement.body, ""),
    )
    asg_doc = func.to_tsvector(
        "english",
        func.coalesce(Assignment.title, "") + " " + func.coalesce(Assignment.description, ""),
    )
    options = "MaxFragments=2, MaxWords=35, MinWords=12, StartSel=«, StopSel=»"
    announcements = select(
        literal("announcement").label("kind"),
        Announcement.title,
        Announcement.course_id,
        Announcement.posted_at.label("when"),
        Announcement.html_url,
        func.ts_headline("english", func.coalesce(Announcement.body, ""), tsquery, options).label(
            "snippet"
        ),
        func.ts_rank(ann_doc, tsquery).label("rank"),
    ).where(
        Announcement.user_id == ctx.user.id,
        Announcement.course_id.in_(list(courses)),
        ann_doc.op("@@")(tsquery),
    )
    assignments = select(
        literal("assignment").label("kind"),
        Assignment.title,
        Assignment.course_id,
        Assignment.due_at.label("when"),
        Assignment.html_url,
        func.ts_headline(
            "english", func.coalesce(Assignment.description, ""), tsquery, options
        ).label("snippet"),
        func.ts_rank(asg_doc, tsquery).label("rank"),
    ).where(
        Assignment.user_id == ctx.user.id,
        Assignment.course_id.in_(list(courses)),
        asg_doc.op("@@")(tsquery),
    )
    combined = union_all(announcements, assignments).subquery()
    rows = ctx.db.execute(select(combined).order_by(combined.c.rank.desc()).limit(8)).all()
    items = [
        {
            "type": r.kind,
            "title": r.title,
            "course": _course_label(courses[r.course_id]),
            ("posted" if r.kind == "announcement" else "due"): fmt(r.when, ctx.zone),
            "excerpt": r.snippet,
            "url": r.html_url,
        }
        for r in rows
    ]
    return {**_source_note(conn, ctx.zone), "query": query, "count": len(items), "items": items}


def _range(
    ctx: ToolContext, args: dict, default_days: int, from_now: bool
) -> tuple[datetime, datetime]:
    now = utcnow()
    if from_now:
        default_start = now
    else:
        default_start = datetime.combine(now.astimezone(ctx.zone).date(), time.min, tzinfo=ctx.zone)
    start = parse_when(args.get("from"), ctx.zone) or default_start
    end = parse_when(args.get("to"), ctx.zone, end_of_day=True) or start + timedelta(
        days=default_days
    )
    if end <= start:
        raise ToolError("'to' must be after 'from'.")
    if end - start > timedelta(days=62):
        raise ToolError("Ask for at most about two months at a time.")
    return start, end


def _events_in(ctx: ToolContext, start: datetime, end: datetime) -> list[CalendarEvent]:
    return list(
        ctx.db.scalars(
            select(CalendarEvent)
            .where(
                CalendarEvent.user_id == ctx.user.id,
                CalendarEvent.start_at < end,
                CalendarEvent.end_at > start,
            )
            .order_by(CalendarEvent.start_at)
        )
    )


def get_events(ctx: ToolContext, args: dict) -> dict:
    google_conn = ctx.connection(GOOGLE_CALENDAR)
    canvas_conn = ctx.connection(CANVAS)
    if google_conn is None and canvas_conn is None:
        raise _not_connected(ctx, GOOGLE_CALENDAR)
    start, end = _range(ctx, args, 7, from_now=False)
    events = _events_in(ctx, start, end)
    items = [
        {
            "title": e.title,
            "start": fmt(e.start_at, ctx.zone, with_time=not e.all_day),
            "end": fmt(e.end_at, ctx.zone, with_time=not e.all_day),
            "all_day": e.all_day,
            "calendar": e.calendar_name,
            "source": "Google Calendar" if e.source == GOOGLE_CALENDAR else "Canvas",
            "location": e.location,
            "url": e.html_url,
        }
        for e in events[:100]
    ]
    result: dict[str, Any] = {
        "from": fmt(start, ctx.zone),
        "to": fmt(end, ctx.zone),
        "count": len(items),
        "truncated": len(events) > 100,
        "items": items,
    }
    if google_conn is None:
        result["note"] = (
            "Only Canvas course events are included; Google Calendar isn't connected "
            f"(connect it at {ctx.settings.app_url}/connections)."
        )
    else:
        result.update(_source_note(google_conn, ctx.zone))
    return result


def find_free_time(ctx: ToolContext, args: dict) -> dict:
    google_conn = _require(ctx, GOOGLE_CALENDAR)
    start, end = _range(ctx, args, 3, from_now=True)
    start = max(start, utcnow())
    min_minutes = max(15, min(int(args.get("min_minutes") or 30), 8 * 60))
    day_start = parse_clock(args.get("day_start"), time(8, 0))
    day_end = parse_clock(args.get("day_end"), time(22, 0))
    if day_end <= day_start:
        raise ToolError("day_end must be later than day_start.")
    busy = [(e.start_at, e.end_at) for e in _events_in(ctx, start, end) if e.busy and not e.all_day]
    slots = free_slots(busy, start, end, ctx.zone, day_start, day_end, min_minutes)
    return {
        **_source_note(google_conn, ctx.zone),
        "from": fmt(start, ctx.zone),
        "to": fmt(end, ctx.zone),
        "counting_as_awake": f"{day_start:%H:%M}–{day_end:%H:%M} each day",
        "count": len(slots),
        "slots": [
            {
                "start": fmt(s, ctx.zone),
                "end": fmt(e, ctx.zone),
                "start_iso": local_iso(s, ctx.zone),
                "end_iso": local_iso(e, ctx.zone),
                "minutes": int((e - s).total_seconds() // 60),
            }
            for s, e in slots[:40]
        ],
    }


def create_event(ctx: ToolContext, args: dict) -> dict:
    conn = _require(ctx, GOOGLE_CALENDAR)
    if not (conn.settings or {}).get("allow_writes") or gcal.WRITE_SCOPE not in conn.scopes.split():
        raise ToolError(
            "Defog isn't allowed to add events yet. Tell the student to turn on "
            f"“Let Defog add events” on the Connections page: {ctx.settings.app_url}/connections"
        )
    title = " ".join(str(args.get("title") or "").split())[:200]
    if not title:
        raise ToolError("Give the event a title.")
    start = parse_when(args.get("start"), ctx.zone)
    end = parse_when(args.get("end"), ctx.zone)
    if start is None:
        raise ToolError("Give a start time.")
    if end is None:
        end = start + timedelta(hours=1)
    if end <= start:
        raise ToolError("The end must be after the start.")
    if end - start > timedelta(days=1):
        raise ToolError("Events can be at most 24 hours long.")
    description = str(args.get("description") or "")[:2000] or None

    action = PendingAction(
        user_id=ctx.user.id,
        chat_id=ctx.chat_id,
        kind="create_event",
        payload={
            "title": title,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "description": description,
            "time_zone": str(ctx.zone),
        },
        expires_at=utcnow() + ACTION_LIFETIME,
    )
    ctx.db.add(action)
    ctx.db.commit()
    card = action_card(action, ctx.zone)
    ctx.actions.append(card)
    return {
        "status": "awaiting_confirmation",
        "summary": card["summary"],
        "note": "Nothing has been added yet. The student must tap Confirm in the app.",
    }


def action_card(action: PendingAction, zone: ZoneInfo) -> dict:
    p = action.payload
    start = datetime.fromisoformat(p["start"])
    end = datetime.fromisoformat(p["end"])
    same_day = start.astimezone(zone).date() == end.astimezone(zone).date()
    end_text = fmt(end, zone).split(", ")[-1] if same_day else fmt(end, zone)
    status = action.status
    if status == "pending" and action.expires_at <= utcnow():
        status = "expired"
    return {
        "id": str(action.id),
        "kind": action.kind,
        "status": status,
        "summary": f"Add “{p['title']}” on {fmt(start, zone)}–{end_text}?",
        "title": p["title"],
        "start": local_iso(start, zone),
        "end": local_iso(end, zone),
        "result": action.result or {},
    }


# --- Registry -----------------------------------------------------------------------------------

DATE_HELP = (
    "ISO 8601 in the student's time zone: a date (2026-11-06) or a date and time "
    "(2026-11-06T17:00)."
)
COURSE_HELP = "Course name or code, e.g. 'BIOL 205' or 'Organic Chemistry'. Omit for all courses."

TOOLS: dict[str, tuple[ToolSpec, Callable[[ToolContext, dict], dict], str]] = {
    "get_assignments": (
        ToolSpec(
            "get_assignments",
            "List the student's Canvas assignments with due dates, points and submission status. "
            "Default: not-yet-submitted work due in the next 14 days.",
            {
                "type": "object",
                "properties": {
                    "course": {"type": "string", "description": COURSE_HELP},
                    "status": {
                        "type": "string",
                        "enum": ["upcoming", "overdue", "missing", "submitted", "graded", "all"],
                        "description": "upcoming = due in the window and not submitted (default).",
                    },
                    "from": {"type": "string", "description": "Window start. " + DATE_HELP},
                    "to": {"type": "string", "description": "Window end (inclusive). " + DATE_HELP},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "additionalProperties": False,
            },
        ),
        get_assignments,
        "Checking your Canvas assignments",
    ),
    "get_grades": (
        ToolSpec(
            "get_grades",
            "Current grade per course from Canvas, with recent assignment scores.",
            {
                "type": "object",
                "properties": {"course": {"type": "string", "description": COURSE_HELP}},
                "additionalProperties": False,
            },
        ),
        get_grades,
        "Looking up your grades",
    ),
    "get_announcements": (
        ToolSpec(
            "get_announcements",
            "Recent Canvas course announcements, newest first, with their text.",
            {
                "type": "object",
                "properties": {
                    "course": {"type": "string", "description": COURSE_HELP},
                    "days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 60,
                        "description": "How far back (default 14).",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                },
                "additionalProperties": False,
            },
        ),
        get_announcements,
        "Reading course announcements",
    ),
    "search_course_content": (
        ToolSpec(
            "search_course_content",
            "Full-text search of announcements and assignment descriptions, e.g. what a "
            "professor said about the midterm. Returns matching excerpts.",
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Words to search for, e.g. 'midterm review'.",
                    },
                    "course": {"type": "string", "description": COURSE_HELP},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        search_course_content,
        "Searching your course materials",
    ),
    "get_events": (
        ToolSpec(
            "get_events",
            "Events from the student's Google Calendar and Canvas course calendars. "
            "Default: today through the next 7 days.",
            {
                "type": "object",
                "properties": {
                    "from": {"type": "string", "description": DATE_HELP},
                    "to": {"type": "string", "description": "Inclusive. " + DATE_HELP},
                },
                "additionalProperties": False,
            },
        ),
        get_events,
        "Checking your calendar",
    ),
    "find_free_time": (
        ToolSpec(
            "find_free_time",
            "Free time slots between calendar events (Google Calendar plus Canvas course events), "
            "within waking hours. Default: now through the next 3 days, slots of 30+ minutes.",
            {
                "type": "object",
                "properties": {
                    "from": {"type": "string", "description": DATE_HELP},
                    "to": {"type": "string", "description": "Inclusive. " + DATE_HELP},
                    "min_minutes": {"type": "integer", "minimum": 15, "maximum": 480},
                    "day_start": {
                        "type": "string",
                        "description": "Earliest time each day, HH:MM (default 08:00).",
                    },
                    "day_end": {
                        "type": "string",
                        "description": "Latest time each day, HH:MM (default 22:00).",
                    },
                },
                "additionalProperties": False,
            },
        ),
        find_free_time,
        "Finding free time",
    ),
    "create_event": (
        ToolSpec(
            "create_event",
            "Propose adding an event (a deadline or study block) to the student's Google "
            "Calendar. Nothing is added until the student taps Confirm in the app.",
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": DATE_HELP},
                    "end": {
                        "type": "string",
                        "description": DATE_HELP + " Default: 1 hour after start.",
                    },
                    "description": {"type": "string"},
                },
                "required": ["title", "start"],
                "additionalProperties": False,
            },
        ),
        create_event,
        "Preparing a calendar event",
    ),
}

TOOL_SPECS = [spec for spec, _, _ in TOOLS.values()]
SOURCE_LABELS = {
    "get_assignments": "Canvas assignments",
    "get_grades": "Canvas grades",
    "get_announcements": "Canvas announcements",
    "search_course_content": "Canvas course content",
    "get_events": "Calendar",
    "find_free_time": "Calendar",
    "create_event": "Google Calendar",
}
# Which synced sources each tool reads, so stale ones can be refreshed before it runs.
READS = {
    "get_assignments": (CANVAS,),
    "get_grades": (CANVAS,),
    "get_announcements": (CANVAS,),
    "search_course_content": (CANVAS,),
    "get_events": (GOOGLE_CALENDAR, CANVAS),
    "find_free_time": (GOOGLE_CALENDAR, CANVAS),
    "create_event": (),
}


def run_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    entry = TOOLS.get(name)
    if entry is None:
        return {"error": f"Unknown tool {name!r}."}
    if "_invalid_arguments" in args:
        return {"error": "The tool arguments weren't valid JSON. Try again."}
    try:
        result = entry[1](ctx, args)
    except (ToolError, BadTime) as exc:
        return {"error": str(exc)}
    except (TypeError, ValueError) as exc:
        return {"error": f"Invalid arguments: {exc}"}
    return truncate(result)


def truncate(result: dict) -> dict:
    """Keep tool results within budget by dropping list items from the end."""
    text = json.dumps(result, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return result
    for key in ("items", "slots", "courses"):
        items = result.get(key)
        while (
            isinstance(items, list)
            and items
            and len(json.dumps(result, default=str)) > MAX_RESULT_CHARS
        ):
            items.pop()
            result["truncated"] = True
    return result


def tool_label(name: str) -> str:
    entry = TOOLS.get(name)
    return entry[2] if entry else "Working"
