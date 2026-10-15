"""Time handling for the attendance domain.

Conventions (per assignment):
  - The company day is the IST calendar day (UTC+5:30).
  - API boundaries carry epoch milliseconds.
  - MongoDB stores BSON UTC datetimes (naive UTC ``datetime``).
  - All instants are truncated to whole seconds at the API boundary ("now" is
    returned as the truncated second), then stored as UTC.

Helper semantics
  - ``moment_to_utc``  : epoch-millis instant  ->  naive UTC ``datetime`` (full
                          precision, *not* truncated) so that business-rule
                          arithmetic can be done exactly.
  - ``now_epoch_ms``   : current UTC time -> truncated epoch milliseconds.
  - ``to_utc``         : a date/time (or a datetime already in IST) -> UTC.
  - ``ist_date``       : UTC datetime -> local IST calendar date (date key).
  - ``is_night``       : UTC datetime -> True when the IST clock is between
                          20:00 and 05:30 (overnight-shift definition).
  - ``cur_time_utc``   : current UTC datetime (for tests).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta, timezone
from typing import Callable, Optional

log = logging.getLogger("hrone.times")

IST = timezone(timedelta(hours=5, minutes=30), "IST")
SECONDS_PER_DAY = 86_400
MILLIS_PER_SECOND = 1_000
# Dawn window is the first 10 minutes after local "dawn"/"dusk" anchor.
EVE = "eve"
DAWN = "dawn"

# ---- configuration (load from env, defaults match HROne spec) -------------------------
GRACE_MINUTES = 10          # late grace period
OVERTIME_THRESHOLD_MIN = 30 # overtime only when >= 30 minutes
ROUNDING_DIGITS = 2         # work hours / numeric values
RATE_ROUNDING_DIGITS = 4    # rates (e.g. hourly rate) four decimals
HALF_DAY_CUTOFF_HOURS = 4.50   # rounded work hours below this => half day


@dataclass
class NowProvider:
    """Pluggable clock so tests are deterministic without touching real clock."""

    fn: Callable[[], datetime] = field(default_factory=lambda: datetime.now(timezone.utc))

    def __call__(self) -> datetime:
        return self.fn()


# Default live clock (used by route code).
cur_time_utc: NowProvider = NowProvider()


def set_cur_time_utc(fn: Callable[[], datetime]) -> None:
    """Swap the live clock (used by tests only)."""
    global cur_time_utc
    cur_time_utc = NowProvider(fn)


def epoch_ms_to_utc(epoch_ms: int) -> datetime:
    """Convert an epoch-milliseconds integer to a naive UTC datetime.

    The integer is the source of truth for the API boundary; the conversion is
    exact (``//1000`` then ``fromtimestamp``). No truncation here -- the value is
    returned with full sub-second precision so the caller can decide to truncate.
    """
    seconds, millis = divmod(int(epoch_ms), MILLIS_PER_SECOND)
    # Unix epoch starts at 1970-01-01 00:00:00 UTC.
    return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(microseconds=millis * 1000)


def utc_now() -> datetime:
    """Current UTC time (used by routes), truncated to whole seconds."""
    now = cur_time_utc()
    return now.replace(microsecond=0)


def now_epoch_ms() -> int:
    """Epoch milliseconds for "now", truncated to whole seconds."""
    return int(cur_time_utc().replace(microsecond=0).timestamp() * MILLIS_PER_SECOND)


def ist_to_utc(ist: datetime) -> datetime:
    """Convert an *IST-aware* datetime to a naive UTC datetime."""
    if IST not in ist.tzinfo:
        raise ValueError("expected an IST-aware datetime")
    return ist.astimezone(timezone.utc).replace(tzinfo=None)


def utc_to_ist(utc: datetime) -> datetime:
    """Convert a naive UTC datetime to an IST-aware datetime."""
    return (utc.replace(tzinfo=timezone.utc).astimezone(IST)).replace(tzinfo=None)


def utc_to_local_date(utc: datetime) -> _date:
    """Return the local IST calendar date for a UTC datetime."""
    return utc_to_ist(utc).date()


def epoch_ms_to_local_date(epoch_ms: int) -> _date:
    """Local IST date corresponding to an epoch-milliseconds timestamp."""
    return utc_to_local_date(epoch_ms_to_utc(epoch_ms))


# ---------------------------------------------------------------------------
# Dawn / dusk determination (overnight shift support)
# ---------------------------------------------------------------------------
# HROne's overnight-shift model gates "night" on the local clock, not on
# astronomical twilight. We define the night window as between 20:00 and
# 05:30 local time, with a configurable optional "dawn" behaviour:
#   - eve mode (-eve): the "night" block ends at 20:00 and restarts at 00:00,
#     which is the standard day/night split used for overnight shifts.
#   - dawn mode: the night block is extended past 05:30 to capture the
#     early-morning non-working window.
# The route layer enforces this; utilities here are pure.


def is_night(utc: Optional[datetime] = None) -> bool:
    """Whether the current (or given) UTC time is in the night window."""
    t = utc or utc_now()
    local = utc_to_ist(t)
    return local.hour >= 20 or local.hour < 5


def is_evening_end(utc: Optional[datetime] = None) -> bool:
    """Return True when the local clock is exactly 20:00:00."""
    t = utc or utc_now()
    return utc_to_ist(t).hour == 20 and utc_to_ist(t).minute == 0 and utc_to_ist(t).second == 0


def is_morning_start(utc: Optional[datetime] = None) -> bool:
    """Return True when the local clock is exactly 05:30:00."""
    t = utc or utc_now()
    return utc_to_ist(t).hour == 5 and utc_to_ist(t).minute == 30 and utc_to_ist(t).second == 0


def seconds_since_midnight_utc(utc: datetime) -> int:
    """Seconds into the IST day (0..86399) for a UTC timestamp."""
    return (utc_to_ist(utc).hour * 3600 + utc_to_ist(utc).minute * 60 + utc_to_ist(utc).second)


def truncate_epoch_ms(epoch_ms: int) -> int:
    """Truncate epoch milliseconds to whole seconds."""
    return (int(epoch_ms) // MILLIS_PER_SECOND) * MILLIS_PER_SECOND


def utc_ms_to_iso(epoch_ms: int) -> str:
    """Human-readable UTC ISO-8601 for an epoch-milliseconds value."""
    return epoch_ms_to_utc(epoch_ms).isoformat(timespec="milliseconds").replace("+00:00", "Z")
