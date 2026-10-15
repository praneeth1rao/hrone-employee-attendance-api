"""HROne Employee Attendance & Analytics API.

FastAPI application built on PyMongo (MongoDB 6+).

Conventions:
  * API boundaries cross at epoch milliseconds (int). MongoDB stores BSON UTC
    datetimes (naive UTC ``datetime`` under PyMongo). The route layer converts
    and truncates every instant to whole seconds.
  * The company day is the IST calendar day (UTC+5:30) (R1).
  * All logic and routes live in this single entry-point module.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel as PydanticBase

# Third-party dependencies (version constraints in requirements.txt).
from pymongo import MongoClient  # type: ignore
from pymongo.errors import PyMongoError  # type: ignore

# Internal modules.
from .analytics import (
    department_summary_pipeline,
    department_trend_pipeline,
    leaderboard_pipeline,
    monthly_pipeline,
)
from .config import get_settings
from .database import create_indexes, get_storage_backend
from .models import (
    AttendanceListIn, AttendanceListOut,
    AttendanceOut,
    AttendancePatchIn,
    AttendancePunchIn,
    AttendancePunchOut,
    DepartmentSummaryOut,
    DepartmentTrendOut,
    EmployeeListIn,
    EmployeeListOut,
    EmployeeOut,
    EmployeeUpdate,
    EmployeeCreate,
    ExplainOut,
    LeaderboardOut,
    Presence,
    Shift,
)
from .rules import (
    GRACE_MINUTES,
    OVERTIME_THRESHOLD_MIN,
    HALF_DAY_CUTOFF_HOURS,
    PRESENCE_ABSENT,
    PRESENCE_LEAVE,
    PRESENCE_ON_DUTY,
    PRESENCE_PRESENT,
    PRESENCE_WFH,
    round_half_up,
)
from .services import (
    create_employee,
    get_employee,
    list_attendance,
    list_employees,
    punch_in,
    punch_out,
    patch_attendance,
)
from .times import (
    utc_to_local_date,
    utc_now,
    now_epoch_ms,
    epoch_ms_to_utc,
)

log = logging.getLogger("hrone.main")

app = FastAPI(
    title="HROne Employee Attendance & Analytics API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# --- database handle (set at startup) ---
def _get_db():
    if app.state.db is None:
        raise RuntimeError("database not initialised")
    return app.state.db


def _open_record(db, emp_code: str, day: str) -> Optional[Dict[str, Any]]:
    return db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": day, "punch_out": {"$exists": False}}
    )


def _utc_now() -> datetime:
    return utc_now()


# ---------------------------------------------------------------------------
# Startup: open DB + idempotently create indexes.
# With TEST_MODE/USE_MONGOMOCK set (tests) the backing storage is an
# in-process mongomock DB; otherwise it is a real MongoDB client.
# ---------------------------------------------------------------------------
@app.on_event("startup")
def startup_db_client():
    # Use a fresh settings instance so the in-memory mongomock storage (used
    # by the test-suite) or the real MongoDB (used in production) is picked
    # up correctly. get_settings() is lru_cache'd; the test suite clears the
    # cache and re-points os.environ before creating the TestClient.
    cfg = get_settings()
    try:
        db = get_storage_backend(cfg)
        created = create_indexes(db)
        log.info(
            "Indexes ensured: %s", "; ".join(
                f"{c['name']}={'OK' if c['created'] else 'exists'}" for c in created
            )
        )
        app.state.db = db
        app.state.db_instance = db
        # Keep a PyMongo client handle on shutdown only when NOT using
        # mongomock (real MongoDB is closed on shutdown).
        if not (cfg.test_mode and cfg.use_mongomock):
            app.state.client = get_storage_backend(cfg)
    except Exception as exc:  # noqa: BLE001
        log.error("Database startup failed: %s", exc)
        raise


@app.on_event("shutdown")
def shutdown_db_client():
    if app.state.client is not None:
        app.state.client.close()


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
# 1. GET /health
# ---------------------------------------------------------------------------
@app.get("/health", response_model=Dict[str, Any])
async def health():
    db = _get_db()
    try:
        db.command("ping")
        ready = True
    except PyMongoError:
        ready = False
    return {"status": "ok", "db": "ready" if ready else "unavailable"}


# 2. POST /employees
# ---------------------------------------------------------------------------
@app.post("/employees", response_model=EmployeeOut, status_code=status.HTTP_201_CREATED)
async def create_employee_route(payload: EmployeeCreate):
    return create_employee(_get_db(), payload)


# 3. GET /employees
# ---------------------------------------------------------------------------
@app.get("/employees", response_model=EmployeeListOut)
async def list_employees_route(q: EmployeeListIn = Query()):
    return EmployeeListOut(**list_employees(_get_db(), q))


# 4. POST /attendance/punch-in  (emp_code supplied as a query param)
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", response_model=AttendanceOut)
async def punch_in_route(
    emp_code: str = Query(..., description="Employee code"),
    epoch_ms: int = Query(..., alias="epoch_ms"),
):
    db = _get_db()
    if get_employee(db, emp_code) is None:
        raise HTTPException(status_code=404, detail="unknown employee")
    payload = AttendancePunchIn(epoch_ms=epoch_ms)
    return _punch_in(db, db, payload, emp_code) if False else _punch_in_core(db, payload, emp_code)


# 5. POST /attendance/punch-out  (emp_code supplied as a query param)
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-out", response_model=AttendanceOut)
async def punch_out_route(
    emp_code: str = Query(...),
    epoch_ms: int = Query(..., alias="epoch_ms"),
):
    db = _get_db()
    if get_employee(db, emp_code) is None:
        raise HTTPException(status_code=404, detail="unknown employee")
    payload = AttendancePunchOut(epoch_ms=epoch_ms)
    return _punch_out_core(db, payload, emp_code)


# 6. GET /attendance
# ---------------------------------------------------------------------------
@app.get("/attendance", response_model=AttendanceListOut)
async def list_attendance_route(q: AttendanceListIn = Query()):
    return AttendanceListOut(**list_attendance(_get_db(), q))


# 7. PATCH /attendance/{emp_code}/{date}
# ---------------------------------------------------------------------------
class PatchBody(PydanticBase):
    status: Presence
    punch_out: Optional[int] = None
    notes: Optional[str] = None
    shift: Optional[Shift] = None


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceOut)
async def patch_attendance_route(emp_code: str, date: str, body: PatchBody):
    db = _get_db()
    if get_employee(db, emp_code) is None:
        raise HTTPException(status_code=404, detail="unknown employee")
    try:
        day = date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid date; expected YYYY-MM-DD") from None

    if body.status == Presence.ABSENT:
        return patch_attendance(db, emp_code, day.isoformat(), body, now_epoch_ms())

    open_rec = _open_record(db, emp_code, day.isoformat())
    if open_rec is None:
        raise HTTPException(
            status_code=409,
            detail=f"no open record to patch for {emp_code} on {day.isoformat()}",
        )
    return patch_attendance(db, emp_code, day.isoformat(), body, now_epoch_ms())


# 8. GET /analytics/employees/{emp_code}/monthly
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly", response_model=List[Dict[str, Any]])
async def monthly_analytics_route(
    emp_code: str, year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
):
    db = _get_db()
    if get_employee(db, emp_code) is None:
        raise HTTPException(status_code=404, detail="unknown employee")
    pipeline = monthly_pipeline(emp_code, year, month)
    agg = db.attendance_logs.aggregate(pipeline)
    result = list(agg)
    return result or []


# 9. GET /analytics/departments/summary
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/summary", response_model=List[Dict[str, Any]])
async def departments_summary_route():
    db = _get_db()
    pipeline = department_summary_pipeline()
    agg = db.attendance_logs.aggregate(pipeline)
    return list(agg)


# 10. GET /analytics/leaderboard/late
# ---------------------------------------------------------------------------
@app.get("/analytics/leaderboard/late", response_model=List[LeaderboardOut])
async def leaderboard_route(
    start_date: date = Query(..., alias="start_date"),
    end_date: date = Query(..., alias="end_date"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(20, ge=1, le=100),
):
    if page < 1:
        page = 1
    if page_size > 100:
        page_size = 100
    if limit > 100:
        limit = 100
    pipeline, _total = leaderboard_pipeline(start_date, end_date, page, page_size, limit)
    db = _get_db()
    agg = db.attendance_logs.aggregate(pipeline)
    return list(agg)


# 11. GET /analytics/departments/{department}/trend
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend", response_model=List[Dict[str, Any]])
async def department_trend_route(department: str, year: int = Query(..., ge=2000, le=2100),
                                 month: int = Query(..., ge=1, le=12)):
    pipeline = department_trend_pipeline(year, month, department)
    db = _get_db()
    agg = db.attendance_logs.aggregate(pipeline)
    return list(agg)


# 12. GET /admin/explain/{endpoint}
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}", response_model=ExplainOut)
async def explain_endpoint(endpoint: str):
    db = _get_db()
    def _ping():
        return db.command("explain", {"ping": 1})
    def _employees():
        return db.command("explain", {"find": "employees", "filter": {},
                                      "sort": {"emp_code": 1}})
    def _punch_in():
        return db.command("explain", {"find": "attendance_logs", "filter": {
            "emp_code": "EMP001", "date": "2026-01-15", "punch_out": {"$exists": False}}})
    def _punch_out():
        return db.command("explain", {"find": "attendance_logs", "filter": {
            "emp_code": "EMP001", "date": "2026-01-15", "punch_out": {"$exists": False}}})
    def _attendance():
        return db.command("explain", {"find": "attendance_logs", "filter": {},
                                      "sort": {"date": 1, "emp_code": 1}})
    def _monthly():
        return db.command("explain", {"pipeline": ["attendance_logs",
                                                    monthly_pipeline("EMP001", 2026, 1)],
                                       "verbosity": "allPlansExecution"})
    def _dept_summary():
        return db.command("explain", {"pipeline": ["attendance_logs",
                                                  department_summary_pipeline()],
                                      "verbosity": "allPlansExecution"})
    def _leaderboard():
        pipe = leaderboard_pipeline(date(2026, 1, 1), date(2026, 1, 31), 1, 20, 20)[0]
        return db.command("explain", {"pipeline": ["attendance_logs", pipe],
                                      "verbosity": "allPlansExecution"})
    def _trend():
        pipe = list(department_trend_pipeline(2026, 1, "Engineering"))
        return db.command("explain", {"pipeline": ["attendance_logs", pipe],
                                      "verbosity": "allPlansExecution"})

    handlers = {
        "GET /health": _ping,
        "GET /employees": _employees,
        "POST /attendance/punch-in": _punch_in,
        "POST /attendance/punch-out": _punch_out,
        "GET /attendance": _attendance,
        "GET /analytics/employees/EMP001/monthly": _monthly,
        "GET /analytics/departments/summary": _dept_summary,
        "GET /analytics/leaderboard/late": _leaderboard,
        "GET /analytics/departments/Engineering/trend": _trend,
    }

    if endpoint not in handlers:
        raise HTTPException(status_code=404, detail=f"unknown endpoint: {endpoint}")

    try:
        explain = handlers[endpoint]()
    except PyMongoError as exc:
        return ExplainOut(
            endpoint=endpoint,
            command=handlers[endpoint].__name__,
            index_used=None,
            n_returned=0,
            execution_time_ms=0.0,
            plan_summary=str(exc),
            error=str(exc),
        )

    plan = explain.get("plan", {}) if isinstance(explain, dict) else {}
    wp = plan.get("queryPlanner", {}).get("winningPlan", {})
    index_used = wp.get("stage")
    exec_ms = float(plan.get("executionTimeMillis", 0.0))

    return ExplainOut(
        endpoint=endpoint,
        command=handlers[endpoint].__name__,
        index_used=index_used,
        n_returned=1,
        execution_time_ms=exec_ms,
        plan_summary=str(wp),
        error=None,
    )


# ---------------------------------------------------------------------------
# Internal attendance cores (hit the DB directly for concurrency control)
# ---------------------------------------------------------------------------
def _punch_in_core(db, payload: AttendancePunchIn, emp_code: str) -> Dict[str, Any]:
    """Open/open-or-duplicate punch-in with the unique compound index as the
    concurrency guarantee."""
    day = utc_to_local_date(epoch_ms_to_utc(payload.epoch_ms)).isoformat()
    now = now_epoch_ms()

    try:
        db.attendance_logs.insert_one({
            "emp_code": emp_code,
            "date": day,
            "punch_in": epoch_ms_to_utc(payload.epoch_ms),
            "punch_out": {"$exists": False},
        })
        presence, work, late, ot, half = _classify(payload.epoch_ms, None)
        return {
            "emp_code": emp_code, "date": day,
            "punch_in": epoch_ms_to_utc(payload.epoch_ms),
            "punch_out": None, "status": presence, "present": True,
            "work_hours": work, "late_minutes": late,
            "overtime_minutes": ot, "half_day": half,
            "shift": None, "notes": None, "created_at": now,
        }
    except Exception as exc:  # noqa: BLE001
        if "duplicate key" in str(exc).lower() or "duplicate" in str(exc).lower():
            # Another thread/process won the race for this same emp_code@day.
            existing = _open_record(db, emp_code, day)
            if existing:
                return _to_out(existing)
            raise HTTPException(status_code=409,
                                detail=f"punch-in already exists for {emp_code} on {day}") from None
        raise


def _punch_out_core(db, payload: AttendancePunchOut, emp_code: str) -> Dict[str, Any]:
    """Atomically close the eligible open record for this employee/date."""
    utc = epoch_ms_to_utc(payload.epoch_ms)
    day = utc_to_local_date(utc).isoformat()
    now = now_epoch_ms()

    # Atomically close the open record; only ONE punch-out can succeed.
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": emp_code, "date": day, "punch_out": {"$exists": False}},
        {"$set": {
            "punch_out": utc,
            "status": "PRESENT",
            "notes": None,
        }},
        return_document=True,
    )
    if updated is None:
        # No open record to close. Check whether an identical punch-out on a
        # different date already hit this employee -> 409; else return current.
        existing = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
        if existing is None:
            raise HTTPException(status_code=404,
                                detail=f"punch-out cannot close a qualifying open record for {emp_code} on {day}")
        return _to_out(existing)

    pi = updated.get("punch_in")
    presence, work, late, ot, half = _classify(payload.epoch_ms, pi)
    return {
        "emp_code": emp_code, "date": day,
        "punch_in": pi, "punch_out": utc,
        "status": presence, "present": True,
        "work_hours": work, "late_minutes": late,
        "overtime_minutes": ot, "half_day": half,
        "shift": None, "notes": None, "updated_at": now,
    }


def _classify(epoch_ms: int, punch_out: Optional[datetime]) -> tuple:
    punch_in = epoch_ms_to_utc(epoch_ms)
    presence, work, late, ot, half = _classify_punches(punch_in, punch_out)
    return presence, round_half_up(work, 2), late, ot, half


def _classify_punches(punch_in: datetime, punch_out: Optional[datetime]) -> tuple:
    if punch_in is None:
        return PRESENCE_ABSENT, 0.0, 0, 0, False
    if punch_out is None:
        return PRESENCE_PRESENT, 0.0, 0, 0, False
    from .times import SECONDS_PER_DAY, seconds_since_midnight_utc

    start_local = utc_to_local_date(punch_in)
    end_local = utc_to_local_date(punch_out)

    if end_local < start_local:
        midnight = datetime.combine(end_local, datetime.min.time())
        window = (midnight - punch_in).total_seconds() + (punch_out - midnight).total_seconds()
    else:
        window = (punch_out - punch_in).total_seconds()
    work_seconds = max(0, int(window))
    work_hours = round_half_up(work_seconds / 3600.0, 2)

    late_minutes = 0
    if punch_in:
        ms = punch_in.hour * 60 + punch_in.minute
        if ms > GRACE_MINUTES:
            late_minutes = ms - GRACE_MINUTES

    overtime_minutes = 1 if work_seconds > OVERTIME_THRESHOLD_MIN * 60 else 0
    half_day = work_hours < HALF_DAY_CUTOFF_HOURS
    return PRESENCE_PRESENT, work_hours, late_minutes, overtime_minutes, half_day


def _to_out(doc: Dict[str, Any]) -> Dict[str, Any]:
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
