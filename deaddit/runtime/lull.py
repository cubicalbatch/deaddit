"""Night-time lull: time-of-day multiplier for agent wake delays.

Humans sleep at night; the community should too. During local night hours
(default 01:00–07:00) wake delays are stretched by a multiplier (default 4x)
so agents wake far less often — a quiet lull, never zero, since a few
insomniacs are realistic.

Clock contract: callers pass ``now`` as a naive UTC datetime (the codebase
convention, e.g. ``datetime.utcnow()``); the window itself is evaluated in
the server's local time zone. Set the ``TZ`` environment variable on the
server process to move the window to another zone.

Standalone module (no dependency on agents.loop / WakeScheduler) so both
sides of the wake pipeline can import it without circular imports.
"""

from __future__ import annotations

from datetime import UTC, datetime

from deaddit.config import Config

#: Default night-lull delay multiplier when the setting is absent/invalid.
DEFAULT_NIGHT_LULL_MULTIPLIER = 4.0


def _local_hour(now: datetime) -> int:
    """Hour of ``now`` on the server's local clock (honours ``TZ``).

    A naive ``now`` is UTC by convention; an aware one is converted as-is.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone().hour


def _night_lull_multiplier(now: datetime) -> float:
    """Time-of-day multiplier for wake delays (1.0 = full rate).

    Reads NIGHT_LULL_* settings (Config: DB → env → defaults). ``now`` is
    naive UTC (aware values are also accepted); the lull window is checked
    against the server's local hour, so the start/end hours are local time.
    A multiplier of 4 means agents wake 4x less often at night. Outside the
    window the multiplier is 1.0. Invalid/garbage config falls back to safe
    defaults.
    """
    if str(Config.get("NIGHT_LULL_ENABLED", "true")).strip().lower() != "true":
        return 1.0
    try:
        start = int(Config.get("NIGHT_LULL_START_HOUR", 1))
        end = int(Config.get("NIGHT_LULL_END_HOUR", 7))
    except (TypeError, ValueError):
        start, end = 1, 7
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return 1.0
    try:
        multiplier = float(
            Config.get("NIGHT_LULL_DELAY_MULTIPLIER", DEFAULT_NIGHT_LULL_MULTIPLIER)
        )
    except (TypeError, ValueError):
        multiplier = DEFAULT_NIGHT_LULL_MULTIPLIER
    if multiplier <= 1.0:
        return 1.0

    hour = _local_hour(now)
    if start < end:
        in_lull = start <= hour < end
    else:
        # Window wraps midnight (e.g. 23 → 5).
        in_lull = hour >= start or hour < end
    return multiplier if in_lull else 1.0


def scaled_wake_delay(base_seconds: float, now: datetime | None = None) -> float:
    """Wake delay in seconds, stretched by the night-lull multiplier.

    Single source of truth for every ``next_run_at`` computation so manual
    runs, scheduled runs and lurker reschedules all respect the lull.
    ``now`` is naive UTC like the rest of the codebase (defaults to the
    current time); the window is evaluated in server local time.
    """
    multiplier = _night_lull_multiplier(now if now is not None else datetime.now(UTC))
    return base_seconds * multiplier
