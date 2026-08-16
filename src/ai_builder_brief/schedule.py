"""Deterministic mapping from GitHub's UTC cron slots to Pacific attempts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


PACIFIC = ZoneInfo("America/Los_Angeles")
PACIFIC_ATTEMPT_HOURS = frozenset({6, 8, 10})
PREPUBLICATION_HOUR = 6


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    """The run decision for one identifiable UTC cron invocation."""

    episode_date: date
    pacific_hour: int
    run: bool
    shadow: bool


def cron_utc_hour(schedule: str) -> int:
    """Parse an individual ``0 H * * *`` GitHub cron expression."""

    fields = schedule.split()
    if len(fields) != 5 or fields[0] != "0" or fields[2:] != ["*", "*", "*"]:
        raise ValueError("schedule must be an individual UTC hourly cron expression")
    try:
        hour = int(fields[1])
    except ValueError as error:
        raise ValueError("schedule must contain one UTC hour") from error
    if not 0 <= hour <= 23:
        raise ValueError("schedule UTC hour must be between 0 and 23")
    return hour


def intended_pacific_slot(schedule_hour_utc: int, schedule_date: date) -> datetime:
    """Convert the intended UTC cron instant to Pacific local time.

    ``schedule_date`` is the calendar date represented by GitHub's scheduled
    event.  This deliberately does not inspect the runner's current clock, so a
    queued self-hosted job still evaluates the slot that triggered it.
    """

    if not 0 <= schedule_hour_utc <= 23:
        raise ValueError("schedule UTC hour must be between 0 and 23")
    scheduled = datetime(
        schedule_date.year,
        schedule_date.month,
        schedule_date.day,
        schedule_hour_utc,
        tzinfo=UTC,
    )
    return scheduled.astimezone(PACIFIC)


def scheduled_attempt(
    schedule: str,
    schedule_date: date,
    *,
    publication_enabled: bool,
) -> ScheduleDecision:
    """Return whether an individual scheduled event should run.

    Before publication, only the Pacific 6 AM slot is admitted and marked as a
    private shadow.  Once publication is enabled, the 6/8/10 AM slots are all
    admitted as public attempts.
    """

    utc_hour = cron_utc_hour(schedule)
    local = intended_pacific_slot(utc_hour, schedule_date)
    allowed_hour = local.hour in PACIFIC_ATTEMPT_HOURS if publication_enabled else local.hour == PREPUBLICATION_HOUR
    return ScheduleDecision(
        episode_date=local.date(),
        pacific_hour=local.hour,
        run=allowed_hour,
        shadow=allowed_hour and not publication_enabled,
    )
