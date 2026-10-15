"""Business rules R1 - R10 for the HROne attendance domain.

Derived from the assignment statement:
  R1  Company day = IST calendar day (UTC+5:30).  An employee's log day is the
      IST date of their punch instant, not the UTC date.
  R2  API crosses at epoch milliseconds; MongoDB crosses at BSON UTC datetimes.
      Truncation to whole seconds applies at every API boundary.
  R3  Truncate instants to whole seconds.
  R4  10-minute grace period: punch in within the first 10 minutes of the IST
      day => not late. Late minutes derive from punch-in time. Whole-minute.
  R5  Overtime: credit only when the logged work window exceeds 30 minutes.
      Whole-minute.
  R6  Work hours rounded half-up to 2 decimal places.
  R7  Half-day when rounded work hours < 4.50.
  R8  Presence codes: PRESENT / WFH / ON_DUTY / ABSENT / LEAVE.
  R9  Weekdays only for working-day/monthly/trend counts; mid-month joiner
      prorated from their join day forward.
  R10 Numbers: two-decimal values half-up; four-decimal rates half-up.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple

from .times import (
    SECONDS_PER_DAY,
    GRACE_MINUTES,
    OVERTIME_THRESHOLD_MIN,
    HALF_DAY_CUTOFF_HOURS,
    utc_to_local_date,
)

log = logging.getLogger("hrone.rules")

DEC2 = Decimal("0.01")
DEC4 = Decimal("0.0001")
HALF_UP = ROUND_HALF_UP

PRESENCE_PRESENT = "PRESENT"
PRESENCE_WFH = "WFH"
PRESENCE_ON_DUTY = "ON_DUTY"
PRESENCE_ABSENT = "ABSENT"
PRESENCE_LEAVE = "LEAVE"


def round_half_up(value: float, digits: int = 2) -> float:
    d = Decimal(str(value))
    q = Decimal(1).scaleb(-digits)
    return float(d.quantize(q, rounding=HALF_UP))


def _local_minutes(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


# ---------------------------------------------------------------------------
# Core single-day classifier
# ---------------------------------------------------------------------------
def classify_day(
    *,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    override_status: Optional[str] = None,
) -> Tuple[str, float, int, int, bool]:
    """Return (presence, work_hours, late_minutes, overtime_minutes, half_day).

    ``punch_in``/``punch_out`` are naive-UTC datetimes.
    """
    if override_status is not None:
        return override_status, 0.0, 0, 0, False

    if punch_in is None:
        return PRESENCE_ABSENT, 0.0, 0, 0, False

    if punch_out is None:
        return PRESENCE_PRESENT, 0.0, 0, 0, False

    # ---- work window ----
    start = punch_in
    end = punch_out
    start_local = utc_to_local_date(start) if start else None
    end_local = utc_to_local_date(end) if end else None

    if start_local and end_local and end_local < start_local:
        # Overnight shift crosses midnight. Window = seconds from start to the
        # UTC midnight beginning of end_local + seconds of end to that midnight.
        midnight_of_end = datetime.combine(end_local, time.min)
        window = (midnight_of_end - start).total_seconds() + \
                 (end - midnight_of_end).total_seconds()
    else:
        window = (end - start).total_seconds()

    work_seconds = max(0, int(window))
    work_hours = round_half_up(work_seconds / 3600.0, 2)  # R6

    # ---- late minutes (10-min grace) ----
    late_minutes = 0
    if start_local:
        ms = _local_minutes(start)
        if ms > GRACE_MINUTES:
            late_minutes = ms - GRACE_MINUTES

    # ---- overtime ----
    overtime_minutes = 1 if work_seconds > OVERTIME_THRESHOLD_MIN * 60 else 0  # R5

    # ---- half day ----
    half_day = work_hours < HALF_DAY_CUTOFF_HOURS  # R7

    return PRESENCE_PRESENT, work_hours, late_minutes, overtime_minutes, half_day


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def weekday_count(year: int, month: int) -> int:
    start = date(year, month, 1)
    next_month = date(year, month + 1, 1)
    return sum(1 for _ in range((next_month - start).days + 1)
               if is_weekday(start + timedelta(days=_)))


def days_in_month(year: int, month: int) -> int:
    return (date(year, month + 1, 1) - date(year, month, 1)).days
