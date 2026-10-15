"""Pydantic v2 schemas.

All timestamps cross the API boundary as epoch milliseconds. MongoDB stores
BSON UTC datetimes; the route layer does the conversion. Truncation to whole
seconds is applied at the boundary.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class EmployeeStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"

class Presence(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"

class Shift(str, Enum):
    DAY = "DAY"
    OVERNIGHT = "OVERNIGHT"
    FLEX = "FLEX"

# ---------------------------------------------------------------------------
# Employee request / response
# ---------------------------------------------------------------------------
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., min_length=1, max_length=40, description="Unique employee code")
    name: str = Field(..., min_length=1, max_length=120)
    department: str = Field(..., min_length=1, max_length=60)
    designation: str = Field(..., min_length=1, max_length=80)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: Optional[str] = Field(None, max_length=40)
    join_date: date = Field(..., description="IST local date the employee joined")
    status: EmployeeStatus = Field(default=EmployeeStatus.ACTIVE, alias="status")

    class Config:
        populate_by_name = True


class EmployeeUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    department: Optional[str] = Field(None, min_length=1, max_length=60)
    designation: Optional[str] = Field(None, min_length=1, max_length=80)
    email: Optional[str] = Field(None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: Optional[str] = Field(None, max_length=40)
    join_date: Optional[date] = None
    status: Optional[EmployeeStatus] = None


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    department: str
    designation: str
    email: str
    phone: Optional[str]
    join_date: date
    status: EmployeeStatus
    total_work_hours: float = Field(default=0.0, ge=0.0)
    created_at: Optional[int] = None
    updated_at: Optional[int] = None

    model_config = {"from_attributes": True}


class EmployeeListIn(BaseModel):
    department: Optional[str] = None
    name_contains: Optional[str] = None
    status: Optional[EmployeeStatus] = None
    page: int = Field(default=1, ge=1, description="Page number, 1-indexed")
    page_size: int = Field(default=20, ge=1, le=100, description="Page size, max 100")

    model_config = {"extra": "ignore"}


class EmployeeListOut(BaseModel):
    employees: list[EmployeeOut]
    total: int
    page: int
    page_size: int
    total_pages: int
    filtered_total: int

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Attendance request / response
# ---------------------------------------------------------------------------
class AttendancePunchIn(BaseModel):
    epoch_ms: int = Field(..., description="Punch-in instant as epoch milliseconds")
    shift: Optional[Shift] = None
    notes: Optional[str] = None

    @classmethod
    def validate_epoch_ms(cls, v: int) -> int:
        if v < 0:
            raise ValueError("epoch_ms must be >= 0")
        # Truncate to whole seconds at the boundary.
        return (v // 1000) * 1000

    model_config = {"validate_by_name": True}


class AttendancePunchOut(BaseModel):
    epoch_ms: int = Field(..., description="Punch-out instant as epoch milliseconds")
    notes: Optional[str] = None

    @classmethod
    def validate_epoch_ms(cls, v: int) -> int:
        if v < 0:
            raise ValueError("epoch_ms must be >= 0")
        return (v // 1000) * 1000

    model_config = {"validate_by_name": True}


class AttendancePatchIn(BaseModel):
    status: Presence
    punch_out: Optional[int] = None
    notes: Optional[str] = None
    shift: Optional[Shift] = None

    model_config = {"extra": "ignore"}


class AttendanceOut(BaseModel):
    emp_code: str
    date: date
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: float = 0.0
    status: Presence
    late_minutes: int = 0
    overtime_minutes: int = 0
    half_day: bool = False
    shift: Optional[Shift] = None
    notes: Optional[str] = None

    model_config = {"from_attributes": True}


class AttendanceListIn(BaseModel):
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    emp_code: Optional[str] = None
    status: Optional[Presence] = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    model_config = {"extra": "ignore"}


class AttendanceListOut(BaseModel):
    logs: list[AttendanceOut]
    total: int
    page: int
    page_size: int
    total_pages: int

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
class AnalyticsMonthlyOut(BaseModel):
    emp_code: str
    name: str
    department: str
    joined_date: Optional[date] = None
    period_start: date
    period_end: date
    total_days: int
    present_days: int
    absent_days: int
    leave_days: int
    wfh_days: int
    on_duty_days: int
    half_day_days: int
    avg_work_hours: float
    late_minutes_total: int
    overtime_minutes_total: int
    monthly_work_hours: float
    monthly_rate: float

    model_config = {"from_attributes": True}


class DepartmentSummaryOut(BaseModel):
    department: str
    headcount: int
    total_work_hours: float
    avg_work_hours: float
    late_minutes_total: int
    overtime_minutes_total: int
    half_day_count: int
    present_count: int
    absent_count: int
    leave_count: int
    wfh_count: int
    on_duty_count: int

    model_config = {"from_attributes": True}


class LeaderboardOut(BaseModel):
    emp_code: str
    name: str
    department: str
    late_minutes_total: int
    log_count: int
    avg_late_minutes: float

    model_config = {"from_attributes": True}


class DepartmentTrendOut(BaseModel):
    date: date
    headcount: int
    present_count: int
    absent_count: int
    wfh_count: int
    on_duty_count: int
    leave_count: int

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Explain endpoint
# ---------------------------------------------------------------------------
class ExplainOut(BaseModel):
    endpoint: str
    command: str
    index_used: Optional[str] = None
    n_returned: int
    execution_time_ms: float
    plan_summary: str
    error: Optional[str] = None
