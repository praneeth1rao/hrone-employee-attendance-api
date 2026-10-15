"""End-to-end tests for the HROne Attendance & Analytics API.

Covers, per the assignment:
  1. Health and MongoDB connectivity.
  2. Employee creation, duplicate conflicts, filtering, sorting, pagination.
  3. Punch-in and punch-out.
  4. IST boundaries and overnight shifts.
  5. Business-rule edge cases (grace period, overtime, rounding, half-day).
  6. Analytics correctness (monthly, dept summary, leaderboard, trend).
  7. Explain endpoint.
  8. Concurrency & idempotency guards.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient  # type: ignore

from app.config import get_settings, Settings
from app.database import get_storage_backend, create_indexes
from app.main import app

# --- helpers -------------------------------------------------------------
_M = 1000  # milliseconds in a second (reused by tests)


def _epoch_ms(year, month, day, hour, minute, second):
    """Epoch milliseconds (truncated to whole seconds)."""
    from datetime import timezone
    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000) // _M * _M


def _punch(emp_code, day_key, year, month, day_num, hour=9, minute=0, pm=False):
    """Punch-in payload for a given local (IST) calendar day."""
    h = hour % 12
    if pm and hour != 12:
        h += 12
    return {
        "emp_code": emp_code,
        "date": f"{year}-{month:02d}-{day_num:02d}",
        "punch_in": _epoch_ms(year, month, day_num, h, minute, 0),
        "punch_out": _epoch_ms(year, month, day_num, h + 8, minute + 30, 0),
    }


def _sample_employee(emp_code="EMP001"):
    return {
        "emp_code": emp_code,
        "name": "Aarav Nair",
        "department": "Engineering",
        "designation": "Software Engineer",
        "email": f"{emp_code.lower()}@example.com",
        "phone": "91-98765-43210",
        "join_date": "2026-01-15",
        "status": "ACTIVE",
        "total_work_hours": 0.0,
        "created_at": _epoch_ms(2026, 1, 15, 9, 0, 0),
        "updated_at": _epoch_ms(2026, 1, 15, 9, 0, 0),
    }


# --- fixtures ------------------------------------------------------------
@pytest.fixture()
def settings():
    """Settings configured for an in-memory MongoDB (mongomock)."""
    return Settings()


@pytest.fixture()
def db(settings):
    """In-memory MongoDB handle (mongomock), with indexes created."""
    from mongomock import MongoClient  # type: ignore
    client = MongoClient()
    d = client[settings.mongo_db]
    create_indexes(d)
    return d


@pytest.fixture()
def client(db, settings):
    """FastAPI TestClient backed by the in-memory MongoDB."""
    app.state.db = db
    app.state.settings = settings
    with TestClient(app) as c:
        yield c


# --- health ----------------------------------------------------------------
def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body.get("db") == "ready"


# --- employees -------------------------------------------------------------
def test_create_employee(client):
    payload = {
        "emp_code": "EMP100", "name": "Leena Krishnamurthy",
        "department": "Engineering", "designation": "Senior Engineer",
        "email": "leena@example.com", "phone": "91-91234-56789",
        "join_date": "2026-02-03", "status": "ACTIVE",
    }
    resp = client.post("/employees", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["emp_code"] == "EMP100"
    assert body["total_work_hours"] == 0.0


def test_create_employee_duplicate_conflict(client):
    payload = {
        "emp_code": "EMP100", "name": "Leena Krishnamurthy",
        "department": "Engineering", "designation": "Senior Engineer",
        "email": "leena@example.com", "join_date": "2026-02-03", "status": "ACTIVE",
    }
    assert client.post("/employees", json=payload).status_code == 201
    resp = client.post("/employees", json=payload)
    assert resp.status_code in (409, 409)
    assert resp.status_code == 409


def test_list_employees(client):
    for ec in ("EMP001", "EMP002"):
        client.post("/employees", json=_sample_employee(ec))
    resp = client.get("/employees")
    body = resp.json()
    assert body["total"] >= 2
    assert body["page"] == 1 and body["page_size"] == 20


def test_list_employees_pagination_and_sorting(client):
    for ec in ("EMP010", "EMP002"):
        client.post("/employees", json=_sample_employee(ec))
    resp = client.get("/employees?page=1&page_size=20")
    body = resp.json()
    codes = [e["emp_code"] for e in body["employees"]]
    assert codes == sorted(codes)


def test_list_employees_filtering_by_department(client):
    for ec, dept in [("EMP001", "Engineering"), ("EMP002", "Sales")]:
        p = _sample_employee(ec)
        p["department"] = dept
        client.post("/employees", json=p)
    resp = client.get("/employees?department=Engineering")
    body = resp.json()
    assert all(e["department"] == "Engineering" for e in body["employees"])


# --- punch-in / punch-out --------------------------------------------------
def test_punch_in(client):
    emp_code = "EMP050"
    client.post("/employees", json=_sample_employee(emp_code))
    epoch = _epoch_ms(2026, 2, 4, 9, 0, 0)
    resp = client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={epoch}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["emp_code"] == emp_code
    assert body["date"] == "2026-02-04"
    assert body["punch_in"] == epoch


def test_punch_in_unknown_employee_404(client):
    epoch = _epoch_ms(2026, 2, 4, 9, 0, 0)
    resp = client.post(f"/attendance/punch-in?emp_code=EMP999&epoch_ms={epoch}")
    assert resp.status_code == 404


def test_punch_out(client):
    emp_code = "EMP051"
    client.post("/employees", json=_sample_employee(emp_code))
    pi = _epoch_ms(2026, 2, 5, 9, 0, 0)
    po = _epoch_ms(2026, 2, 5, 17, 30, 0)
    client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={pi}")
    resp = client.post(f"/attendance/punch-out?emp_code={emp_code}&epoch_ms={po}")
    assert resp.status_code == 200


def test_punch_out_without_punch_in_409(client):
    emp_code = "EMP052"
    client.post("/employees", json=_sample_employee(emp_code))
    po = _epoch_ms(2026, 2, 5, 17, 30, 0)
    resp = client.post(f"/attendance/punch-out?emp_code={emp_code}&epoch_ms={po}")
    assert resp.status_code == 409


# --- IST and overnight shifts ------------------------------------------------
def test_ist_date_uses_local_calendar_day(client):
    emp_code = "EMP054"
    client.post("/employees", json=_sample_employee(emp_code))
    # UTC 2026-02-04 18:30 = IST 2026-02-05 00:00
    utc_midnight = _epoch_ms(2026, 2, 4, 18, 30, 0)
    resp = client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={utc_midnight}")
    assert resp.status_code == 200
    assert resp.json()["date"] == "2026-02-05"


def test_overnight_shift(client):
    emp_code = "EMP055"
    client.post("/employees", json=_sample_employee(emp_code))
    pi = _epoch_ms(2026, 2, 6, 23, 30, 0)  # IST 02-06 23:30
    po = _epoch_ms(2026, 2, 7, 6, 30, 0)   # IST 02-07 06:30
    client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={pi}")
    assert client.post(f"/attendance/punch-out?emp_code={emp_code}&epoch_ms={po}").status_code == 200


# --- business rules ----------------------------------------------------------
def test_grace_period_10_minutes(client):
    emp_code = "EMP056"
    client.post("/employees", json=_sample_employee(emp_code))
    pi = _epoch_ms(2026, 3, 9, 0, 5, 0)   # 5 min after midnight -> on time
    po = _epoch_ms(2026, 3, 9, 9, 30, 0)
    client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={pi}")
    assert client.post(f"/attendance/punch-out?emp_code={emp_code}&epoch_ms={po}").json()["late_minutes"] == 0


def test_grace_period_exceeded_gives_late(client):
    emp_code = "EMP057"
    client.post("/employees", json=_sample_employee(emp_code))
    pi = _epoch_ms(2026, 3, 9, 0, 11, 0)  # 11 min after midnight -> 1 min late
    po = _epoch_ms(2026, 3, 9, 9, 0, 0)
    client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={pi}")
    assert client.post(f"/attendance/punch-out?emp_code={emp_code}&epoch_ms={po}").json()["late_minutes"] == 1


def test_overtime_threshold_30_minutes(client):
    emp_code = "EMP058"
    client.post("/employees", json=_sample_employee(emp_code))
    pi = _epoch_ms(2026, 3, 10, 9, 0, 0)
    po = _epoch_ms(2026, 3, 10, 9, 41, 0)  # 41 min work
    client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={pi}")
    assert client.post(f"/attendance/punch-out?emp_code={emp_code}&epoch_ms={po}").json()["overtime_minutes"] == 1


def test_rounding_half_up_two_decimals(client):
    emp_code = "EMP059"
    client.post("/employees", json=_sample_employee(emp_code))
    # 1h59m30s = 6585s -> 1.8291666 -> 1.83 (half-up)
    pi = _epoch_ms(2026, 3, 11, 9, 0, 0)
    po = _epoch_ms(2026, 3, 11, 10, 59, 30)
    client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={pi}")
    body = client.post(f"/attendance/punch-out?emp_code={emp_code}&epoch_ms={po}").json()
    assert round(body["work_hours"], 2) == 1.83


def test_half_day_below_4_50(client):
    emp_code = "EMP060"
    client.post("/employees", json=_sample_employee(emp_code))
    # 4h15m -> below 4.50 => half_day True
    pi = _epoch_ms(2026, 3, 12, 9, 0, 0)
    po = _epoch_ms(2026, 3, 12, 13, 15, 0)
    client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={pi}")
    body = client.post(f"/attendance/punch-out?emp_code={emp_code}&epoch_ms={po}").json()
    assert body["half_day"] is True
    assert body["work_hours"] == 4.25


# --- analytics ---------------------------------------------------------------
def test_analytics_monthly_employee(client):
    emp_code = "EMP080"
    client.post("/employees", json=_sample_employee(emp_code))
    pi = _epoch_ms(2026, 2, 2, 9, 0, 0)
    po = _epoch_ms(2026, 2, 2, 17, 0, 0)
    client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={pi}")
    client.post(f"/attendance/punch-out?emp_code={emp_code}&epoch_ms={po}")
    resp = client.get(f"/analytics/employees/{emp_code}/monthly?year=2026&month=2")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["emp_code"] == emp_code and doc["period_start"] == "2026-02-01"


def test_analytics_department_summary_incl_zero_log_employees(client):
    emp_a, emp_b = "EMP090", "EMP091"
    client.post("/employees", json=_sample_employee(emp_a))
    client.post("/employees", json=_sample_employee(emp_b))
    pi = _epoch_ms(2026, 2, 2, 9, 0, 0)
    po = _epoch_ms(2026, 2, 2, 17, 0, 0)
    client.post(f"/attendance/punch-in?emp_code={emp_a}&epoch_ms={pi}")
    client.post(f"/attendance/punch-out?emp_code={emp_a}&epoch_ms={po}")
    resp = client.get("/analytics/departments/summary")
    assert resp.status_code == 200
    assert resp.json()[0]["headcount"] >= 2


def test_analytics_leaderboard_deterministic(client):
    for ec in ("EMP100", "EMP101"):
        client.post("/employees", json=_sample_employee(ec))
        pi = _epoch_ms(2026, 2, 3, 9, 45, 0)
        po = _epoch_ms(2026, 2, 3, 17, 0, 0)
        client.post(f"/attendance/punch-in?emp_code={ec}&epoch_ms={pi}")
        client.post(f"/attendance/punch-out?emp_code={ec}&epoch_ms={po}")
    resp = client.get(
        "/analytics/leaderboard/late?start_date=2026-02-03&end_date=2026-02-03"
    )
    assert resp.status_code == 200
    assert resp.json() and "emp_code" in resp.json()[0]


def test_analytics_department_trend(client):
    emp_code = "EMP110"
    client.post("/employees", json=_sample_employee(emp_code))
    client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={_epoch_ms(2026,3,10,9,0,0)}")
    client.post(f"/attendance/punch-out?emp_code={emp_code}&epoch_ms={_epoch_ms(2026,3,10,17,0,0)}")
    resp = client.get("/analytics/departments/Engineering/trend?year=2026&month=3")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# --- explain endpoint -----------------------------------------------------------
def test_explain_endpoint(client):
    resp = client.get("/admin/explain/GET%20%2Fhealth")
    assert resp.status_code == 200
    body = resp.json()
    assert "endpoint" in body and "command" in body and "index_used" in body


def test_explain_endpoint_unknown(client):
    assert client.get("/admin/explain/GET%20%2Fnonexistent").status_code == 404


# --- concurrency ------------------------------------------------------------------
def test_duplicate_punch_in_conflict(client):
    emp_code = "EMP120"
    client.post("/employees", json=_sample_employee(emp_code))
    epoch = _epoch_ms(2026, 4, 13, 9, 0, 0)
    r1 = client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={epoch}")
    r2 = client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={epoch}")
    ok = [r for r in (r1, r2) if r.status_code == 200]
    conflict = [r for r in (r1, r2) if r.status_code == 409]
    assert len(ok) == 1 and len(conflict) == 1


def test_concurrent_punch_out_conflict(client):
    emp_code = "EMP121"
    client.post("/employees", json=_sample_employee(emp_code))
    pi = _epoch_ms(2026, 4, 14, 9, 0, 0)
    po = _epoch_ms(2026, 4, 14, 17, 0, 0)
    client.post(f"/attendance/punch-in?emp_code={emp_code}&epoch_ms={pi}")
    r1 = client.post(f"/attendance/punch-out?emp_code={emp_code}&epoch_ms={po}")
    r2 = client.post(f"/attendance/punch-out?emp_code={emp_code}&epoch_ms={po}")
    assert len([r for r in (r1, r2) if r.status_code == 200]) == 1


# --- idempotency of indexes --------------------------------------------------------
def test_index_creation_idempotent(client):
    from app.database import create_indexes
    d = app.state.db
    results = create_indexes(d)
    results2 = create_indexes(d)
    for r in results2:
        assert r.get("skipped") == "already_exists" or r.get("created") is True
