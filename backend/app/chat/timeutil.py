"""Dates and times in the student's own time zone."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


class BadTime(ValueError):
    pass


def parse_when(value: str | None, zone: ZoneInfo, *, end_of_day: bool = False) -> datetime | None:
    """Parse what the model sends: "2026-11-06", "2026-11-06T17:00", or with an offset.

    A bare date means the start of that day (or the end, for range ends). Times without an
    offset are in the student's time zone. Returns an aware UTC datetime.
    """
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        if len(raw) == 10:
            day = date.fromisoformat(raw)
            local = datetime.combine(day, time.max if end_of_day else time.min, tzinfo=zone)
            return local.astimezone(timezone.utc)
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BadTime(
            f"Couldn't read the time {value!r}. Use ISO 8601, e.g. 2026-11-06T17:00."
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt.astimezone(timezone.utc)


def fmt(dt: datetime | None, zone: ZoneInfo, with_time: bool = True) -> str | None:
    """'Fri, Nov 6, 11:59 PM' in the student's zone (portable: no %-d, which Windows lacks)."""
    if dt is None:
        return None
    local = dt.astimezone(zone)
    text = f"{local:%a}, {local:%b} {local.day}"
    if with_time:
        hour = local.hour % 12 or 12
        text += f", {hour}:{local:%M} {local:%p}"
    return text


def local_iso(dt: datetime | None, zone: ZoneInfo) -> str | None:
    return dt.astimezone(zone).isoformat(timespec="minutes") if dt else None


def parse_clock(value: str | None, default: time) -> time:
    if not value:
        return default
    try:
        return time.fromisoformat(value.strip())
    except ValueError as exc:
        raise BadTime(f"Couldn't read the time of day {value!r}. Use HH:MM, e.g. 08:00.") from exc


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def free_slots(
    busy: list[tuple[datetime, datetime]],
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
    day_start: time,
    day_end: time,
    min_minutes: int,
) -> list[tuple[datetime, datetime]]:
    """Gaps of at least min_minutes inside each day's waking window, between start and end."""
    busy = merge_intervals([(s, e) for s, e in busy if e > start and s < end])
    slots: list[tuple[datetime, datetime]] = []
    day = start.astimezone(zone).date()
    last_day = end.astimezone(zone).date()
    while day <= last_day:
        window_start = max(datetime.combine(day, day_start, tzinfo=zone), start)
        window_end = min(datetime.combine(day, day_end, tzinfo=zone), end)
        cursor = window_start
        if window_end > window_start:
            for b_start, b_end in busy:
                if b_end <= cursor:
                    continue
                if b_start >= window_end:
                    break
                if b_start > cursor and b_start - cursor >= timedelta(minutes=min_minutes):
                    slots.append((cursor, b_start))
                cursor = max(cursor, b_end)
                if cursor >= window_end:
                    break
            if window_end - cursor >= timedelta(minutes=min_minutes):
                slots.append((cursor, window_end))
        day += timedelta(days=1)
    return slots
