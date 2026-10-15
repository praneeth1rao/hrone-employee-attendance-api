"""Analytics engine (v2, rewritten fresh after 0-byte file conflict).

All four analytics endpoints run MongoDB aggregation pipelines and never fetch
every attendance record into Python for analytics. Each pipeline reads the
stored derived metrics that the grade service persisted (work_hours,
late_minutes, overtime_minutes, half_day) and never recomputes them from raw
punch timestamps.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("hrone.analytics")

DEC2 = Decimal("0.01")
DEC4 = Decimal("0.0004")


def _iso(d: date) -> str:
    return d.isoformat()


def _round2(value: Any) -> Any:
    return {"$round": [value, 2]}


def _round4(value: Any) -> Any:
    return {"$round": [value, 4]}


# ---------------------------------------------------------------- monthly employee
def monthly_pipeline(emp_code: str, year: int, month: int) -> List[Dict[str, Any]]:
    """Single month-summary document for one employee.

    Aggregates the employee's attendance_logs within the month period and
    summarizes the stored metrics. Week/day counts derive from the date
    dimension of matched logs; employees with no logs yield zero aggregates.
    """
    start_iso = _iso(date(year, month, 1))
    if month == 12:
        end_iso = _iso(date(year + 1, 1, 1))
    else:
        end_iso = _iso(date(year, month + 1, 1))

    pipeline: List[Dict[str, Any]] = [
        # 1. Match this employee's logs inside the month.
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": start_iso, "$lte": end_iso},
        }},
        # 2. Resolve employee metadata.
        {"$lookup": {
            "from": "employees",
            "let": {"ec": emp_code},
            "pipeline": [
                {"$match": {"emp_code": "$$ec"}},
                {"$project": {"name": 1, "department": 1, "join_date": 1}},
            ],
            "as": "emp",
        }},
        # 3. Build the summary document.
        {"$project": {
            "emp_code": emp_code,
            "name": {"$ifNull": [{"$arrayElemAt": ["$emp.name", 0]}, ""]},
            "department": {"$ifNull": [{"$arrayElemAt": ["$emp.department", 0]}, ""]},
            "joined_date": {"$ifNull": [{"$arrayElemAt": ["$emp.join_date", 0]}, ""]},
            "period_start": start_iso,
            "period_end": end_iso,
            "total_days": 1,
            "work_hours_total": {"$sum": {"$ifNull": ["$logs.work_hours", 0]}},
            "late_minutes_total": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "overtime_minutes_total": {"$sum": {"$ifNull": ["$logs.overtime_minutes", 0]}},
            "half_day_days": {"$sum": {"$cond": ["$logs.half_day", 1, 0]}},
            "present_days": {"$sum": {"$cond": [
                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]}},
            "absent_days": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ABSENT"]}, 1, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "wfh_days": {"$sum": {"$cond": [{"$eq": ["$logs.status", "WFH"]}, 1, 0]}},
            "on_duty_days": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        # 4. Collapse into a single document and compute counts from the logs.
        {"$group": {
            "_id": None,
            "total_days": {"$sum": 1},
            "work_hours_total": {"$sum": "$work_hours_total"},
            "late_minutes_total": {"$sum": "$late_minutes_total"},
            "overtime_minutes_total": {"$sum": "$overtime_minutes_total"},
            "half_day_days": {"$sum": "$half_day_days"},
            "present_days": {"$sum": "$present_days"},
            "absent_days": {"$sum": "$absent_days"},
            "leave_days": {"$sum": "$leave_days"},
            "wfh_days": {"$sum": "$wfh_days"},
            "on_duty_days": {"$sum": "$on_duty_days"},
        }},
        {"$project": {
            "emp_code": 1, "name": 1, "department": 1, "joined_date": 1,
            "period_start": 1, "period_end": 1,
            "total_days": 1, "work_hours_total": 1, "late_minutes_total": 1,
            "overtime_minutes_total": 1, "half_day_days": 1,
            "present_days": 1, "absent_days": 1, "leave_days": 1,
            "wfh_days": 1, "on_duty_days": 1,
        }},
    ]
    return pipeline


# ---------------------------------------------------------------- department summary
def department_summary_pipeline() -> List[Dict[str, Any]]:
    """Group employees by department.

    Headcount counts all employees in the department -- including those with
    zero attendance logs -- because the employee lookup is never filtered by an
    attendance join. Zero-log employees remain eligible headcount and contribute
    zero metrics. An explicit ABSENT record is distinguished from a missing
    record through the per-log status field.
    """
    pipeline: List[Dict[str, Any]] = [
        # Enumerate employees (no attendance filter) so zero-log employees stay.
        {"$lookup": {
            "from": "employees",
            "let": {},
            "pipeline": [{"$project": {
                "_id": 0, "emp_code": 1, "name": 1, "department": 1, "designation": 1,
                "email": 1, "phone": 1, "join_date": 1, "status": 1,
                "total_work_hours": 1, "created_at": 1, "updated_at": 1,
            }}],
            "as": "employees",
        }},
        {"$unwind": {"path": "$employees", "preserveNullAndEmptyArrays": False}},
        {"$group": {
            "_id": "$employees.department",
            "dep": {"$push": "$employees"},
            "total_employees": {"$sum": 1},
        }},
        {"$project": {
            "department": "$_id",
            "employees": "$dep",
            "headcount": "$total_employees",
        }},
        # Enrich each employee with metrics; employees without logs yield no
        # metric doc, so we LEFT JOIN in the aggregation itself.
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ecs": "$employees.emp_code"},
            "pipeline": [
                {"$match": {"emp_code": {"$in": "$$ecs"}}},
                {"$group": {
                    "_id": "$emp_code",
                    "hours": {"$sum": "$work_hours"},
                    "late": {"$sum": "$late_minutes"},
                    "ot": {"$sum": "$overtime_minutes"},
                    "half": {"$sum": {"$cond": ["$half_day", 1, 0]}},
                    "present": {"$sum": {"$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]}},
                    "absent": {"$sum": {"$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]}},
                    "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "wfh": {"$sum": {"$cond": [{"$eq": ["$status", "WFH"]}, 1, 0]}},
                    "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "logs",
        }},
        {"$project": {
            "employees": 1,
            "metrics": {
                "$arrayElemAt": [
                    {"$filter": {
                        "input": "$logs",
                        "as": "m",
                        "cond": {"$ne": ["$$m", None]},
                    }},
                    0,
                ],
            },
        }},
        {"$unwind": {"path": "$metrics", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$first": "$headcount"},
            "total_work_hours": {"$sum": {"$ifNull": ["$metrics.hours", 0]}},
            "avg_work_hours": {"$avg": {"$ifNull": ["$metrics.hours", 0]}},
            "late_minutes_total": {"$sum": {"$ifNull": ["$metrics.late", 0]}},
            "overtime_minutes_total": {"$sum": {"$ifNull": ["$metrics.ot", 0]}},
            "half_day_count": {"$sum": {"$ifNull": ["$metrics.half", 0]}},
            "present_count": {"$sum": {"$ifNull": ["$metrics.present", 0]}},
            "absent_count": {"$sum": {"$ifNull": ["$metrics.absent", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$metrics.leave", 0]}},
            "wfh_count": {"$sum": {"$ifNull": ["$metrics.wfh", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$metrics.on_duty", 0]}},
        }},
    ]
    return pipeline


# ---------------------------------------------------------------- leaderboard late
def leaderboard_pipeline(
    start_date: date, end_date: date, page: int, page_size: int, top_k: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """Late-arrival leaderboard.

    Filters the date range (inclusive) and sums stored late_minutes per
    employee. Deterministic tie handling: employees with equal late_minutes are
    ordered by emp_code ascending (secondary sort) before the limit is applied,
    so the output is stable and reproducible at the boundary.
    """
    filter_ = {
        "late_minutes": {"$gt": 0},
        "date": {"$gte": _iso(start_date), "$lte": _iso(end_date)},
    }
    pipeline = [
        {"$match": filter_},
        {"$group": {
            "_id": "$emp_code",
            "late_minutes_total": {"$sum": "$late_minutes"},
            "log_count": {"$sum": 1},
        }},
        {"$sort": {"late_minutes_total": -1, "_id": 1}},
        {"$skip": (page - 1) * page_size},
        {"$limit": page_size},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "e",
        }},
        {"$unwind": {"path": "$e", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "emp_code": "$_id",
            "_id": 0,
            "name": "$e.name",
            "department": "$e.department",
            "late_minutes_total": 1,
            "log_count": 1,
            "avg_late_minutes": _round4({"$divide": [
                "$late_minutes_total", "$log_count"]}),
        }},
    ]
    return pipeline, 0


# ---------------------------------------------------------------- department trend
def department_trend_pipeline(year: int, month: int, department: str) -> List[Dict[str, Any]]:
    """One document per calendar day with headcount + per-day presence counts.

    Headcount for a date counts all employees in the department eligible for
    the period (including zero-log employees): we begin from the department's
    employees (unconditional on attendance), then LEFT JOIN attendance per date.
    """
    # Generate the calendar day dimension in Python for the month.
    days: List[Dict[str, str]] = []
    d = date(year, month, 1)
    last = date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)
    while d < last:
        days.append({"d": _iso(d), "_id": _iso(d)})
        d = d + __import__("datetime").timedelta(days=1)

    pipeline: List[Dict[str, Any]] = [
        # 1. Calendar dimension (dates for the month) as a standalone source.
        {"$documents": days},
        # 2. Headcount per date: count department employees (unconditional set).
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$d"},
            "pipeline": [
                {"$match": {"department": department}},
                {"$count": "n"},
            ],
            "as": "emp_rows",
        }},
        {"$project": {
            "date": "$d",
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$emp_rows.n", 0]}, 0]},
        }},
        # 3. LEFT JOIN attendance per date; presence counts come from the
        #    attendance_logs collection for that date.
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"date": "$$d"}},
                {"$group": {
                    "_id": "$status",
                    "n": {"$sum": 1},
                }},
            ],
            "as": "rows",
        }},
        {"$project": {
            "date": 1,
            "headcount": 1,
            "present_count": {"$sum": {"$cond": [{"$eq": ["$_id", "PRESENT"]}, 1, 0]}},
            "absent_count": {"$sum": {"$cond": [{"$eq": ["$_id", "ABSENT"]}, 1, 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$_id", "LEAVE"]}, 1, 0]}},
            "wfh_count": {"$sum": {"$cond": [{"$eq": ["$_id", "WFH"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$_id", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$sort": {"date": 1}},
    ]
    return pipeline
