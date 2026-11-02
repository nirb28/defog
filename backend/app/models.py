"""Database tables. Every row of student data carries user_id so queries can always be scoped."""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _user_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())


# --- Accounts -------------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(String(100))
    # Stored lower-cased, so the unique constraint is case-insensitive.
    email: Mapped[str] = mapped_column(String(320), unique=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    password_hash: Mapped[str | None] = mapped_column(String(255))
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True)
    school: Mapped[str | None] = mapped_column(String(200))
    year: Mapped[str | None] = mapped_column(String(20))
    major: Mapped[str | None] = mapped_column(String(200))
    time_zone: Mapped[str | None] = mapped_column(String(64))
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    @property
    def profile_complete(self) -> bool:
        return bool(self.school and self.time_zone and self.consent_at)


class Session(Base):
    """A signed-in device. Web sessions use a cookie; mobile sessions use access + refresh tokens."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user_fk()
    kind: Mapped[str] = mapped_column(String(10))  # web | mobile
    # Only SHA-256 hashes of tokens are stored, so a database leak doesn't leak sessions.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    csrf_token: Mapped[str | None] = mapped_column(String(64))
    device: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created()
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthToken(Base):
    """Single-use emailed links and short hand-offs (verify email, reset password, link Google)."""

    __tablename__ = "auth_tokens"

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user_fk()
    purpose: Mapped[str] = mapped_column(String(30))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class LoginFailure(Base):
    """Failed logins per email, for the 5-per-15-minutes limit. Kept for unknown emails too."""

    __tablename__ = "login_failures"
    __table_args__ = (Index("ix_login_failures_email_created", "email", "created_at"),)

    id: Mapped[uuid.UUID] = _id()
    email: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = _created()


# --- Connections and synced data ---------------------------------------------------------


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_connections_user_provider"),)

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user_fk()
    provider: Mapped[str] = mapped_column(String(30))  # canvas | google_calendar
    auth_type: Mapped[str] = mapped_column(String(10))  # oauth | token
    base_url: Mapped[str | None] = mapped_column(String(255))
    # Fernet-encrypted JSON: {"access_token", "refresh_token"}. Never leaves the backend.
    encrypted_tokens: Mapped[str] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[str] = mapped_column(Text, default="", server_default="")
    # connected | needs_attention
    status: Mapped[str] = mapped_column(String(20), default="connected")
    status_detail: Mapped[str | None] = mapped_column(Text)
    # Google: {"calendar_ids": [...], "allow_writes": bool}. Canvas: {"user_name": ...}
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class Course(Base):
    __tablename__ = "courses"
    __table_args__ = (UniqueConstraint("connection_id", "canvas_id", name="uq_courses_canvas"),)

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user_fk()
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    canvas_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(300))
    code: Mapped[str | None] = mapped_column(String(100))
    term: Mapped[str | None] = mapped_column(String(100))
    current_score: Mapped[float | None] = mapped_column(Float)
    current_grade: Mapped[str | None] = mapped_column(String(20))
    html_url: Mapped[str | None] = mapped_column(String(500))


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("course_id", "canvas_id", name="uq_assignments_canvas"),
        Index("ix_assignments_user_due", "user_id", "due_at"),
        Index(
            "ix_assignments_fts",
            text("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))"),
            postgresql_using="gin",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user_fk()
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    canvas_id: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)  # plain text, HTML stripped
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    points: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float | None] = mapped_column(Float)
    grade: Mapped[str | None] = mapped_column(String(50))
    # unsubmitted | submitted | graded | pending_review | missing | late | excused
    submission_status: Mapped[str | None] = mapped_column(String(30))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    html_url: Mapped[str | None] = mapped_column(String(500))


class Announcement(Base):
    __tablename__ = "announcements"
    __table_args__ = (
        UniqueConstraint("course_id", "canvas_id", name="uq_announcements_canvas"),
        Index(
            "ix_announcements_fts",
            text("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(body, ''))"),
            postgresql_using="gin",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user_fk()
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    canvas_id: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text)  # plain text, HTML stripped
    author: Mapped[str | None] = mapped_column(String(200))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    html_url: Mapped[str | None] = mapped_column(String(500))


class CalendarEvent(Base):
    """Events from Google Calendar and Canvas course calendars."""

    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "calendar_id", "external_id", name="uq_calendar_events_external"
        ),
        Index("ix_calendar_events_user_start", "user_id", "start_at"),
    )

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user_fk()
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(20))  # google_calendar | canvas
    external_id: Mapped[str] = mapped_column(String(255))
    calendar_id: Mapped[str] = mapped_column(String(255))
    calendar_name: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    location: Mapped[str | None] = mapped_column(String(500))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    # False for events marked "free" or declined, so they don't block free time.
    busy: Mapped[bool] = mapped_column(Boolean, default=True)
    html_url: Mapped[str | None] = mapped_column(String(1000))


# --- Chat -------------------------------------------------------------------------------------


class Chat(Base):
    __tablename__ = "chats"
    __table_args__ = (Index("ix_chats_user_updated", "user_id", "updated_at"),)

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user_fk()
    title: Mapped[str] = mapped_column(String(200), default="New chat")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_chat_seq", "chat_id", "seq"),)

    id: Mapped[uuid.UUID] = _id()
    # Insertion order, so messages written in the same instant still sort correctly.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=False), unique=True)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chats.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = _user_fk()
    role: Mapped[str] = mapped_column(String(20))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    # Tools the assistant used for this answer, and the sources it drew on.
    tool_calls: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    sources: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = _created()


class PendingAction(Base):
    """A write the assistant proposed (e.g. a calendar event). Runs only after the student confirms."""

    __tablename__ = "pending_actions"

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user_fk()
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(30))  # create_event
    payload: Mapped[dict] = mapped_column(JSONB)
    # pending | done | cancelled | failed
    status: Mapped[str] = mapped_column(String(20), default="pending")
    result: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LLMUsage(Base):
    """Tokens used per student per day, for the daily cap."""

    __tablename__ = "llm_usage"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)


# Tables the migration locks down against Supabase's public Data API.
ALL_TABLES = [table.name for table in Base.metadata.sorted_tables]
