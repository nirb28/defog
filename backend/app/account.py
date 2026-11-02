"""The signed-in student's own account: profile, data export and deletion."""

import json

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from .auth.schemas import DeleteAccountRequest, ProfileUpdate, UserOut
from .auth.sessions import Auth, clear_session_cookie, require_auth
from .config import Settings, get_settings
from .connections.tokens import revoke
from .db import get_db
from .models import (
    Announcement,
    Assignment,
    CalendarEvent,
    Chat,
    Connection,
    Course,
    Message,
    utcnow,
)

router = APIRouter(prefix="/api/me", tags=["account"])


def _json_default(value: object) -> str:
    # datetimes and dates as ISO 8601; UUIDs and anything else as strings
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class MeOut(BaseModel):
    user: UserOut
    # Web sessions: send this back in the X-CSRF-Token header on POST/PATCH/DELETE.
    csrf_token: str | None = None


@router.get("", response_model=MeOut)
def me(auth: Auth = Depends(require_auth)) -> MeOut:
    return MeOut(user=UserOut.of(auth.user), csrf_token=auth.session.csrf_token)


@router.patch("", response_model=UserOut)
def update_me(
    body: ProfileUpdate, auth: Auth = Depends(require_auth), db: DB = Depends(get_db)
) -> UserOut:
    user = auth.user
    fields = body.model_fields_set
    if "name" in fields:
        if not body.name:
            raise HTTPException(status_code=422, detail="Name can't be empty.")
        user.name = body.name
    if "school" in fields:
        if not body.school:
            raise HTTPException(status_code=422, detail="School can't be empty.")
        user.school = body.school
    if "year" in fields:
        user.year = body.year
    if "major" in fields:
        user.major = body.major
    if "time_zone" in fields:
        if not body.time_zone:
            raise HTTPException(status_code=422, detail="Time zone can't be empty.")
        user.time_zone = body.time_zone
    if "accept_terms" in fields:
        if body.accept_terms is not True:
            raise HTTPException(
                status_code=422,
                detail="To stop using Defog under the terms, delete your account instead.",
            )
        if user.consent_at is None:
            user.consent_at = utcnow()
    db.commit()
    return UserOut.of(user)


@router.get("/export")
def export_me(auth: Auth = Depends(require_auth), db: DB = Depends(get_db)) -> Response:
    """Everything Defog stores about the student, as one JSON download (tokens excluded)."""
    user = auth.user

    def rows(model, *order) -> list[dict]:  # noqa: ANN001
        out = []
        for row in db.scalars(select(model).where(model.user_id == user.id).order_by(*order)):
            item = {}
            for column in model.__table__.columns:
                if column.name in ("encrypted_tokens", "user_id"):
                    continue
                item[column.name] = getattr(row, column.key)
            out.append(item)
        return out

    data = {
        "exported_at": utcnow().isoformat(),
        "profile": UserOut.of(user).model_dump(mode="json"),
        "connections": rows(Connection, Connection.created_at),
        "courses": rows(Course, Course.name),
        "assignments": rows(Assignment, Assignment.due_at),
        "announcements": rows(Announcement, Announcement.posted_at),
        "calendar_events": rows(CalendarEvent, CalendarEvent.start_at),
        "chats": rows(Chat, Chat.created_at),
        "messages": rows(Message, Message.seq),
    }
    return Response(
        content=json.dumps(data, indent=2, default=_json_default),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="defog-export.json"'},
    )


@router.delete("", status_code=204)
def delete_me(
    body: DeleteAccountRequest,
    auth: Auth = Depends(require_auth),
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    user = auth.user
    if body.confirm_email.strip().lower() != user.email:
        raise HTTPException(status_code=422, detail="Type your account's email address to confirm.")
    for conn in db.scalars(select(Connection).where(Connection.user_id == user.id)):
        revoke(conn)
    db.delete(user)  # every table cascades from users, so this removes all their data
    db.commit()
    response = Response(status_code=204)
    clear_session_cookie(response, settings)
    return response
