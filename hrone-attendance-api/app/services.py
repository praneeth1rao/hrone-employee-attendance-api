"""Service layer: employees + attendance business logic.

Attendance records are created and updated with atomic MongoDB operations
(their unique index on (emp_code, date) is the concurrency guarantee). We
never implement a read-then-write handshake.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from pymongo import MongoClient  # type: ignore
from pymongo.errors import DuplicateKeyError, OperationFailure  # type: ignore

from .models import (
    AttendanceListIn,
    EmployeeListIn,
    EmployeeOut,
    AttendancePatchIn,
    AttendancePunchIn,
    AttendancePunchOut,
    AttendanceOut,
    EmployeeCreate,
    EmployeeUpdate,
    Presence,
    Shift,
)
from .rules import (
    PRESENCE_ABSENT,
    PRESENCE_LEAVE,
    PRESENCE_ON_DUTY,
    PRESENCE_PRESENT,
    PRESENCE_WFH,
    classify_day,
    is_weekday,
    days_in_month,
)
from .times import (
    epoch_ms_to_utc,
    utc_to_local_date,
    utc_now,
    now_epoch_ms,
    truncate_epoch_ms,
    SECONDS_PER_DAY,
)

log = logging.getLogger("hrone.services")


def build_employee(emp: Dict[str, Any]) -> EmployeeOut:
    return EmployeeOut(
        emp_code=emp["emp_code"],
        name=emp["name"],
        department=emp["department"],
        designation=emp.get("designation", ""),
        email=emp["email"],
        phone=emp.get("phone"),
        join_date=emp["join_date"],
        status=emp.get("status", "ACTIVE"),
        total_work_hours=float(emp.get("total_work_hours", 0.0)),
        created_at=emp.get("created_at"),
        updated_at=emp.get("updated_at"),
    )


# ==========================================================================
# EMPLOYEES
# ==========================================================================
def list_employees(db, q: EmployeeListIn) -> Dict[str, Any]:
    filter_ = {}
    if q.name_contains:
        filter_["name"] = {"$regex": q.name_contains, "$options": "i"}
    if q.department:
        filter_["department"] = q.department
    if q.status is not None:
        filter_["status"] = q.status.value

    total = db.employees.count_documents(filter_)
    skip = (q.page - 1) * q.page_size
    cursor = (db.employees.find(filter_)
              .sort("emp_code", 1)
              .skip(skip)
              .limit(q.page_size))
    employees = [build_employee(e) for e in cursor]
    total_pages = max(1, (total + q.page_size - 1) // q.page_size) if total else 1
    return {
        "employees": employees,
        "total": total,
        "page": q.page,
        "page_size": q.page_size,
        "total_pages": total_pages,
        "filtered_total": total,
    }


def create_employee(db, payload: EmployeeCreate) -> EmployeeOut:
    emp = {
        "emp_code": payload.emp_code,
        "name": payload.name,
        "department": payload.department,
        "designation": payload.designation,
        "email": payload.email,
        "phone": payload.phone,
        "join_date": payload.join_date.isoformat(),
        "status": payload.status.value,
        "total_work_hours": 0.0,
        "created_at": now_epoch_ms(),
        "updated_at": now_epoch_ms(),
    }
    try:
        db.employees.insert_one(emp)
    except DuplicateKeyError as exc:
        raise ValueError(f"duplicate employee code: {payload.emp_code}") from exc
    return build_employee(db.employees.find_one({"emp_code": payload.emp_code}))


def get_employee(db, emp_code: str) -> Optional[EmployeeOut]:
    emp = db.employees.find_one({"emp_code": emp_code})
    return build_employee(emp) if emp else None


def update_employee(db, emp_code: str, payload: EmployeeUpdate) -> Optional[EmployeeOut]:
    fixed = payload.model_dump(exclude_unset=True)
    if not fixed:
        return get_employee(db, emp_code)
    set_map = {}
    if payload.name is not None:
        set_map["name"] = payload.name
    if payload.department is not None:
        set_map["department"] = payload.department
    if payload.designation is not None:
        set_map["designation"] = payload.designation
    if payload.email is not None:
        set_map["email"] = payload.email
    if payload.phone is not None:
        set_map["phone"] = payload.phone
    if payload.join_date is not None:
        set_map["join_date"] = payload.join_date.isoformat()
    if payload.status is not None:
        set_map["status"] = payload.status.value
    set_map["updated_at"] = now_epoch_ms()
    db.employees.update_one({"emp_code": emp_code}, {"$set": set_map})
    return get_employee(db, emp_code)


# ==========================================================================
# ATTENDANCE (single employee/date record)
# ==========================================================================
def _today_iso() -> str:
    return utc_to_local_date(utc_now()).isoformat()


def _local_date_utc(midnight: Optional[datetime], target_local: Optional[date]) -> Optional[date]:
    return utc_to_local_date(utc_now())


def _open_record(db, emp_code: str, day: str) -> Optional[Dict[str, Any]]:
    return db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": day, "punch_out": {"$exists": False}})


def punch_in(db, payload: AttendancePunchIn, emp_code: str, when: int) -> Dict[str, Any]:
    utc = epoch_ms_to_utc(payload.epoch_ms)
    day = utc_to_local_date(utc).isoformat()
    return _upsert_open(db, emp_code, day, punch_in=utc, punch_out=None, when=when)


def punch_out(db, payload: AttendancePunchOut, emp_code: str, when: int) -> Dict[str, Any]:
    utc = epoch_ms_to_utc(payload.epoch_ms)
    day = utc_to_local_date(utc).isoformat()
    return _upsert_open(db, emp_code, day, punch_in=None, punch_out=utc, when=when)


def _compute_presence(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Dict[str, Any]:
    presence, work_hours, late, ot, half = classify_day(
        punch_in=punch_in, punch_out=punch_out)
    return {
        "presence": presence,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": ot,
        "half_day": half,
    }


def _upsert_open(db, emp_code: str, day: str, *, punch_in: Optional[datetime],
                 punch_out: Optional[datetime], when: int) -> Dict[str, Any]:
    """Atomically open (or close) a record for emp_code@day.

    The unique compound index enforces "one record per emp_code@day". If a
    concurrent request races here, the second insert raises DuplicateKeyError
    and we return the winner's record (the 409 logic lives in the route).
    """
    existing = _open_record(db, emp_code, day)
    now = now_epoch_ms()

    if punch_in is None and punch_out is None:
        # Nothing to do; return current state.
        return existing_to_out(existing)

    # Open-record "toggle": if punch_in is given but there's an open record,
    # treat this as an idle-state open that inserts a fresh record.
    if punch_in is not None:
        try:
            db.attendance_logs.insert_one({
                "emp_code": emp_code,
                "date": day,
                "punch_in": punch_in,
                "punch_out": {"$exists": False},
            })
        except DuplicateKeyError:
            # Someone won the race; return their record (route turns this into 409
            # when they also requested the expired/active comparison, otherwise
            # a 409 already_tokens is set).
            existing = _open_record(db, emp_code, day)
            return existing_to_out(existing)
        presence = _compute_presence(punch_in, None)
        return {
            "emp_code": emp_code,
            "date": day,
            "punch_in": punch_in,
            "punch_out": None,
            "status": presence["presence"],
            "present": "present" in presence["presence"] or presence["work_hours"] == 0.0,
            "work_hours": presence["work_hours"],
            "late_minutes": presence["late_minutes"],
            "overtime_minutes": presence["overtime_minutes"],
            "half_day": presence["half_day"],
            "shift": None,
            "notes": None,
            "created_at": now,
        }

    # Punch-out: close the open record (atomic update instead of insert).
    if existing is not None:
        # Merge and close atomically via find_one_and_update so only ONE
        # punch-out can succeed; a second punch-out finds nothing to close.
        updated = db.attendance_logs.find_one_and_update(
            {"emp_code": emp_code, "date": day, "punch_out": {"$exists": False}},
            {"$set": {
                "punch_out": punch_out,
                "status": "PRESENT",
                "notes": None,
            }},
            return_document=True,
        )
        if updated is None:
            # Someone else closed it first (or it was already closed); return
            # the record that exists now.
            existing = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
            return existing_to_out(existing)
        pi = updated.get("punch_in")
        presence = _compute_presence(pi, punch_out)
        return {
            "emp_code": emp_code,
            "date": day,
            "punch_in": pi,
            "punch_out": punch_out,
            "status": presence["presence"],
            "present": presence["presence"] == PRESENCE_PRESENT,
            "work_hours": presence["work_hours"],
            "late_minutes": presence["late_minutes"],
            "overtime_minutes": presence["overtime_minutes"],
            "half_day": presence["half_day"],
            "shift": None,
            "notes": None,
            "updated_at": now_epoch_ms(),
        }

    # Record has no open entry but punch_out was supplied; nothing to close.
    existing = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    return existing_to_out(existing)


def patch_attendance(db, emp_code: str, day: str, payload: AttendancePatchIn,
                     when: int) -> Dict[str, Any]:
    open_rec = _open_record(db, emp_code, day)
    now = now_epoch_ms()

    if payload.status == Presence.ABSENT:
        db.attendance_logs.update_one(
            {"emp_code": emp_code, "date": day},
            {"$set": {
                "status": "ABSENT",
                "punch_in": None,
                "punch_out": None,
                "work_hours": 0.0,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "notes": payload.notes,
            }},
        )
        return {"emp_code": emp_code, "date": day, "status": "ABSENT", "present": False,
                "work_hours": 0.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}

    if payload.status == Presence.LEAVE:
        db.attendance_logs.update_one(
            {"emp_code": emp_code, "date": day},
            {"$set": {
                "status": "LEAVE",
                "work_hours": 0.0,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "notes": payload.notes,
            }},
        )
        return {"emp_code": emp_code, "date": day, "status": "LEAVE", "present": True,
                "work_hours": 0.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}

    set_map = {"status": payload.status.value, "notes": payload.notes}
    if payload.shift is not None:
        set_map["shift"] = payload.shift.value

    if payload.punch_out is not None:
        utc_out = epoch_ms_to_utc(payload.punch_out)
        day = utc_to_local_date(utc_out).isoformat()
        updated = db.attendance_logs.find_one_and_update(
            {"emp_code": emp_code, "date": day, "punch_out": {"$exists": False}},
            {"$set": {"punch_out": utc_out, "status": "PRESENT"}},
            return_document=True,
        )
        if updated is None:
            existing = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
            return existing_to_out(existing)
        pi = updated.get("punch_in")
        presence = _compute_presence(pi, utc_out)
        return {
            "emp_code": emp_code, "date": day,
            "punch_in": pi, "punch_out": utc_out,
            "status": presence["presence"], "present": presence["presence"] == PRESENCE_PRESENT,
            "work_hours": presence["work_hours"], "late_minutes": presence["late_minutes"],
            "overtime_minutes": presence["overtime_minutes"], "half_day": presence["half_day"],
            "shift": None, "notes": None, "updated_at": now_epoch_ms(),
        }

    # Just change status on an open record (or upsert nothing).
    if open_rec is not None:
        db.attendance_logs.update_one(
            {"emp_code": emp_code, "date": day},
            {"$set": set_map},
        )
    return {"emp_code": emp_code, "date": day, "status": payload.status.value,
            "present": True, "notes": payload.notes, "updated_at": now}


def existing_to_out(doc: Optional[Dict[str, Any]], with_id: bool = False) -> Dict[str, Any]:
    if doc is None:
        return {"emp_code": "", "date": "", "punch_in": None, "punch_out": None,
                "status": "PRESENT", "present": False, "work_hours": 0.0,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
                "shift": None, "notes": None}
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "status": doc.get("status", "PRESENT"),
        "present": True,
        "work_hours": doc.get("work_hours", 0.0),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "shift": doc.get("shift"),
        "notes": doc.get("notes"),
    }


# ==========================================================================
# ATTENDANCE LIST
# ==========================================================================
def list_attendance(db, q: AttendanceListIn) -> Dict[str, Any]:
    filter_ = {}
    if q.emp_code:
        filter_["emp_code"] = q.emp_code
    if q.status:
        filter_["status"] = q.status.value
    if q.from_date or q.to_date:
        if q.from_date:
            filter_["date"] = filter_.get("date", {})
            filter_["date"]["$gte"] = q.from_date.isoformat()
        if q.to_date:
            filter_["date"]["$lte"] = q.to_date.isoformat()

    total = db.attendance_logs.count_documents(filter_)
    skip = (q.page - 1) * q.page_size
    cursor = (db.attendance_logs.find(filter_)
              .sort("date", 1)
              .sort("emp_code", 1)
              .skip(skip)
              .limit(q.page_size))
    logs = [existing_to_out(e) for e in cursor]
    total_pages = max(1, (total + q.page_size - 1) // q.page_size) if total else 1
    return {"logs": logs, "total": total, "page": q.page, "page_size": q.page_size,
            "total_pages": total_pages}
