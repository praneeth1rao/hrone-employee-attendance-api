"""Shared pytest fixtures.

The test suite uses an in-memory MongoDB (mongomock) by default, so the suite
can run fast and without a live server. A real MongoDB is used only when the
`TEST_MODE=real` environment variable is set.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Generator

import pytest

from fastapi.testclient import TestClient  # type: ignore

from app.config import get_settings, Settings
from app.main import app


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------
def _settings_from_env(overrides: Dict[str, Any]) -> Settings:
    """Build a Settings instance from environment + overrides (test only)."""
    base = os.environ.copy()
    base.update(overrides)
    # Temporarily apply env then read; pydantic-settings reads os.environ at
    # construction time, so we rebuild per-test configuration.
    os.environ.update(overrides)
    return Settings()


@pytest.fixture()
def settings():
    """Settings configured for an in-memory MongoDB (mongomock)."""
    return _settings_from_env({
        "TEST_MODE": "true",
        "MONGO_DB": "hrone_test",
        "MONGO_HOST": "localhost",
        "MONGO_PORT": "27017",
        "MONGO_USER": "",
        "MONGO_PASSWORD": "",
        "USE_MONGOMOCK": "true",
    })


@pytest.fixture()
def db(settings):
    """Moroccan-free db handle: a MongomockStorage (in-memory) database."""
    from app.database import get_storage_backend
    client = get_storage_backend(settings)
    db = client[settings.mongo_db]
    # Create indexes idempotently.
    from app.database import create_indexes
    create_indexes(db)
    return db


@pytest.fixture()
def client(db, settings):
    """FastAPI TestClient wired to the in-memory MongoDB."""
    # Attach the db handle to the app state (mirrors startup event).
    app.state.db = db
    app.state.settings = settings
    # Make `get_settings()` (used by main.py startup) resolve to the
    # in-memory mongomock configuration so the startup event does not try
    # to reach a live MongoDB server.
    from app.config import get_settings as _get_settings
    _get_settings.cache_clear()  # clear the lru_cache singleton
    os.environ.update({
        "TEST_MODE": "true",
        "USE_MONGOMOCK": "true",
        "MONGO_DB": "hrone_test",
        "MONGO_HOST": "localhost",
        "MONGO_PORT": "27017",
        "MONGO_USER": "",
        "MONGO_PASSWORD": "",
    })
    _get_settings()  # rebuild the singleton from the env
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Sample data (mirrors a HROne-style employee + attendance seed)
# ---------------------------------------------------------------------------
def _epoch_ms(year, month, day, hour, minute, second):
    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000) // 1000 * 1000


def sample_employee(emp_code="EMP001"):
    return {
        "emp_code": emp_code,
        "name": "Aarav Nair",
        "department": "Engineering",
        "designation": "Software Engineer",
        "email": "aarav.nair@example.com",
        "phone": "91-98765-43210",
        "join_date": "2026-01-15",
        "status": "ACTIVE",
        "total_work_hours": 0.0,
        "created_at": _epoch_ms(2026, 1, 15, 9, 0, 0),
        "updated_at": _epoch_ms(2026, 1, 15, 9, 0, 0),
    }


def _punch(emp_code, day, year, month, day_num, hour=9, minute=0, pm=False):
    # Convert 12h to 24h.
    h = hour % 12
    if pm and hour != 12:
        h += 12
    return {
        "emp_code": emp_code,
        "date": f"{year}-{month:02d}-{day_num:02d}",
        "punch_in": _epoch_ms(year, month, day_num, h, minute, 0),
        "punch_out": _epoch_ms(year, month, day_num, h + 8, minute + 30, 0),
    }


SAMPLE_EMPLOYEES = [sample_employee("EMP001"), sample_employee("EMP002")]
SAMPLE_JANUARY_2026 = {
    "EMP001": {
        "2026-01-01": (9, 0, False),  # Friday earlier
        "2026-01-02": (9, 0, False),
        "2026-01-03": (9, 11, False),  # late by 11 min (>10 grace)
        "2026-01-04": (9, 0, False),
        "2026-01-05": (9, 0, False),
        "2026-01-06": (9, 0, False),  # Saturday - weekend
        "2026-01-07": (9, 0, False),  # Sunday - weekend
        "2026-01-08": (8, 30, False),  # midnight-soon
    },
}
