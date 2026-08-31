"""Night-time lull: time-of-day multiplier for agent wake delays.

Humans sleep at night; the community should too. During local night hours
(default 01:00–07:00) wake delays are stretched by a multiplier (default 4x)
so agents wake far less often — a quiet lull, never zero, since a few
insomniacs are realistic.

Standalone module (no dependency on agents.loop / WakeScheduler) so both
sides of the wake pipeline can import it without circular imports.
"""

from __future__ import annotations

from datetime import datetime

from deaddit.config import Config

#: Default night-lull delay multiplier when the setting is absent/invalid.
DEFAULT_NIGHT_LULL_MULTIPLIER = 4.0


def _night_lull_multiplier(now: datetime) -> float:
    """Time-of-day multiplier for wake delays (1.0 = full rate).

    Reads NIGHT_LULL_* settings (Config: DB → env → defaults). The lull
    window is local server time (datetime.now()); a multiplier of 4 means
    agents wake 4x less often at night. Outside the window the multiplier
    is 1.0. Invalid/garbage config falls back to safe defaults.
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

    hour = now.hour
    # Window may wrap midnight (e.g. 23 → 5): in-lull = hour >= start OR hour < end.
    in_lull = hour >= start if start < end else (hour >= start or hour < end)
    return multiplier if in_lull else 1.0


def scaled_wake_delay(base_seconds: float, now: datetime | None = None) -> float:
    """Wake delay in seconds, stretched by the night-lull multiplier.

    Single source of truth for every ``next_run_at`` computation so manual
    runs, scheduled runs and lurker reschedules all respect the lull.
    """
    multiplier = _night_lull_multiplier(now if now is not None else datetime.now())
    return base_seconds * multiplier
