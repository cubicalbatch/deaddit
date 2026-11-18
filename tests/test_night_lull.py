"""Night lull: window membership, local-clock evaluation and delay scaling.

The lull window is local server time while callers pass naive UTC, so every
test pins the process time zone. POSIX TZ strings ("UTC0", "CST6", ...) are
parsed by libc without the tz database, keeping results independent of the
host zone and of installed tzdata.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

import deaddit.runtime.lull as lull
from deaddit.models import Setting
from deaddit.runtime.lull import _night_lull_multiplier, scaled_wake_delay

#: Fixed UTC-6, no DST (local = UTC - 6h).
UTC_MINUS_6 = "CST6"
#: US Central with DST rules (CST UTC-6 in winter, CDT UTC-5 in summer).
US_CENTRAL_DST = "CST6CDT,M3.2.0,M11.1.0"


@pytest.fixture(autouse=True)
def pin_tz():
    """Pin TZ to UTC for each test; yields a setter for other zones.

    TZ is saved and restored by hand (not via monkeypatch) so time.tzset()
    can run after the original value is back.
    """
    original = os.environ.get("TZ")

    def _pin(tz: str) -> None:
        os.environ["TZ"] = tz
        time.tzset()

    _pin("UTC0")
    yield _pin
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def _configure(*, enabled="true", start=1, end=7, multiplier=4.0) -> None:
    Setting.set_value("NIGHT_LULL_ENABLED", str(enabled))
    Setting.set_value("NIGHT_LULL_START_HOUR", str(start))
    Setting.set_value("NIGHT_LULL_END_HOUR", str(end))
    Setting.set_value("NIGHT_LULL_DELAY_MULTIPLIER", str(multiplier))


@pytest.fixture(autouse=True)
def _default_window(db_session):
    """Window 01:00-07:00, multiplier 4 (explicit, so env vars cannot leak in)."""
    _configure()


def _utc(hour: int, minute: int = 0, *, month: int = 1) -> datetime:
    """Naive UTC datetime, the convention every caller uses."""
    return datetime(2026, month, 15, hour, minute)


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 0, 1.0),
        (0, 59, 1.0),
        (1, 0, 4.0),
        (6, 0, 4.0),
        (6, 59, 4.0),
        (7, 0, 1.0),
        (12, 0, 1.0),
        (23, 0, 1.0),
    ],
)
def test_window_is_start_inclusive_end_exclusive(hour, minute, expected):
    assert _night_lull_multiplier(_utc(hour, minute)) == expected


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(22, 1.0), (23, 4.0), (0, 4.0), (2, 4.0), (4, 4.0), (5, 1.0), (12, 1.0)],
)
def test_wrapping_window_spans_midnight(hour, expected):
    _configure(start=23, end=5)

    assert _night_lull_multiplier(_utc(hour)) == expected


@pytest.mark.parametrize(
    ("utc_hour", "expected"),
    [
        (3, 1.0),  # inside 1-7 on the UTC clock, but local 21:00
        (9, 4.0),  # outside 1-7 on the UTC clock, but local 03:00
    ],
)
def test_naive_utc_now_is_checked_against_local_hour(pin_tz, utc_hour, expected):
    pin_tz(UTC_MINUS_6)

    assert _night_lull_multiplier(_utc(utc_hour)) == expected


@pytest.mark.parametrize(
    "aware_now",
    [
        datetime(2026, 1, 15, 9, 0, tzinfo=UTC),
        # Same instant expressed at +09:00; its tzinfo must be honoured, not
        # replaced with UTC (which would read it as 18:00 UTC -> 12:00 local).
        datetime(2026, 1, 15, 18, 0, tzinfo=timezone(timedelta(hours=9))),
    ],
)
def test_aware_now_is_converted_not_relabelled(pin_tz, aware_now):
    pin_tz(UTC_MINUS_6)  # 09:00 UTC == 03:00 local, inside the window

    assert _night_lull_multiplier(aware_now) == 4.0


@pytest.mark.parametrize(
    ("month", "expected"),
    [
        (1, 4.0),  # CST: 12:30 UTC -> 06:30 local, inside
        (7, 1.0),  # CDT: 12:30 UTC -> 07:30 local, outside
    ],
)
def test_local_offset_follows_dst_for_the_given_instant(pin_tz, month, expected):
    pin_tz(US_CENTRAL_DST)

    assert _night_lull_multiplier(_utc(12, 30, month=month)) == expected


def test_default_now_is_current_utc_instant(pin_tz, monkeypatch):
    """With now omitted, the current instant is used, not local wall time
    mislabelled as UTC."""
    pin_tz(UTC_MINUS_6)
    instant = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)  # 03:00 local, inside

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.astimezone().replace(tzinfo=None)
            return instant.astimezone(tz)

        @classmethod
        def utcnow(cls):
            return instant.replace(tzinfo=None)

    monkeypatch.setattr(lull, "datetime", _FrozenDatetime)

    assert scaled_wake_delay(100.0) == 400.0


@pytest.mark.parametrize(
    ("hour", "multiplier", "expected"),
    [(3, 4.0, 400.0), (12, 4.0, 100.0), (3, 2.5, 250.0)],
)
def test_scaled_wake_delay_multiplies_base(hour, multiplier, expected):
    _configure(multiplier=multiplier)

    assert scaled_wake_delay(100.0, _utc(hour)) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"enabled": "false"},
        {"start": 3, "end": 3},
        {"multiplier": 1.0},
        {"multiplier": 0.5},
    ],
    ids=["disabled", "start-equals-end", "multiplier-one", "multiplier-below-one"],
)
def test_guards_return_full_rate(overrides):
    _configure(**overrides)

    assert _night_lull_multiplier(_utc(3)) == 1.0
    assert scaled_wake_delay(100.0, _utc(3)) == 100.0
