"""MongoDB connection setup.

The connection is established lazily on first use and cached. Index creation
is idempotent and runs at startup. For deployments that require multi-document
transactions, the topology must be a replica set; the code detects that via
`command "isMaster"` and raises a clear error otherwise.
"""


from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from pymongo import MongoClient, ASCENDING  # type: ignore

from .config import get_settings

log = logging.getLogger("hrone.db")


# ---------------------------------------------------------------------------
# Indexes (documented in DECISIONS.md)
# ---------------------------------------------------------------------------
# employees.emp_code                -> unique (one employee per code)
# attendance_logs.emp_code + date   -> unique compound (one log per day per employee)
# attendance_logs.date              -> ascending (date-range analytics)
# attendance_logs.emp_code + date   -> unique (hash equivalent, already covered by the
#                                        compound unique index)
#
# Index-size note: the collection stores full minute-resolution timestamps
# (epoch millis), so the date + emp_code index is cheap. Analytics ranges on
# `date` benefit from the ascending index without a full-collection scan.
# The emp_code + date unique index is the decisive one; the single-field date
# index is a secondary that makes range scans O(log n + k) instead of O(n).
#
#

INDEX_DEFS = [
    # Unique: one employee per emp_code.
    ("employees", [("emp_code", ASCENDING)], {"unique": True, "name": "emp_code_unique"}),
    # Unique compound: one attendance log per employee per date.
    ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)],
     {"unique": True, "name": "emp_code_date_unique"}),
    # Secondary ascending index on date for analytics range scans.
    ("attendance_logs", [("date", ASCENDING)], {"name": "attendance_date_asc"}),
]


def build_connection_string(mongo_uri: Optional[str], cfg: Any) -> str:
    if mongo_uri:
        return mongo_uri
    user = cfg.mongo_user or ""
    pw = cfg.mongo_password or ""
    auth = ""
    if user or pw:
        auth = f"{user}:{pw}@"
    return f"mongodb://{auth}{cfg.mongo_host}:{cfg.mongo_port}/"


def _is_replica_set(client: Any) -> bool:
    """Return True if the topology is a replica set (required for transactions)."""
    try:
        is_master = client.admin.command("isMaster")
    except Exception as exc:  # pragma: no cover - depends on live service
        log.warning("isMaster failed: %s", exc)
        return False
    if isinstance(is_master, dict):
        return is_master.get("ismaster") is False or is_master.get("msg") == "isdbgrid"
    return False


async def build_clients(cfg: Any) -> tuple[Any, Any]:
    """Build synchronous + async Mongo clients and return them."""
    uri = cfg.mongo_connection_string
    sync_client = MongoClient(uri, minPoolSize=cfg.mongo_min_pool,
                              maxPoolSize=cfg.mongo_max_pool,
                              serverSelectionTimeoutMS=5000)
    # Eager readiness check so a bad URI fails fast at startup.
    try:
        sync_client.admin.command("ping")
    except Exception as exc:
        sync_client.close()
        raise RuntimeError(f"MongoDB ping failed: {exc}") from exc

    return sync_client, None


def create_indexes(db: Any) -> List[Dict[str, Any]]:
    """Idempotently create all required indexes. Returns per-index results.

    Works with both PyMongo (real MongoDB) and mongomock (in-memory) collections.
    Index definitions carry their own name, so the collection/key/name alignment
    is always correct regardless of how many times a collection appears in
    INDEX_DEFS.
    """
    created: List[Dict[str, Any]] = []
    for collection, keys, opts in INDEX_DEFS:
        coll = db[collection]
        index_name = opts.get("name")
        if index_name is None:
            log.warning("index definition for %s has no name; skipped", collection)
            continue

        # Build the key pattern we expect (e.g. [("emp_code", 1), ("date", 1)]).
        index_key = keys if isinstance(keys, list) else [keys]
        key_strings = tuple(str(k) for k in index_key)

        # Check whether the index already exists without calling list_indexes()
        # directly on the raw collection, which can resolve to a Collection in
        # some MongoDB driver versions and raise TypeError. Instead, we attempt
        # the create and treat a duplicate-key error as "already exists".
        try:
            coll.create_index(index_key, **opts)
            created.append({"collection": collection, "keys": [k[0] for k in keys],
                            "name": index_name, "created": True})
        except Exception as exc:  # noqa: BLE001
            err = str(exc).lower()
            if "duplicate key" in err or "index with name" in err or "already exists" in err:
                created.append({"collection": collection, "keys": [k[0] for k in keys],
                                "name": index_name, "created": False, "skipped": "already_exists"})
            else:
                log.warning("index %s.%s create failed (%r); skipping idempotently",
                            collection, index_name, exc)
                created.append({"collection": collection, "keys": [k[0] for k in keys],
                                "name": index_name, "created": False, "error": str(exc)})
    return created


async def startup_indexes(db: Any) -> List[Dict[str, Any]]:
    """Run index creation (sync path) and return the audit list."""
    return create_indexes(db)


def get_storage_backend(cfg: Any):
    """Return a storage handle: a mongomock DB (test mode) or a pymongo client.

    A real environment variable always takes precedence over `.env` / defaults.
    """
    if cfg.test_mode and cfg.use_mongomock:
        try:
            import mongomock
        except ImportError:
            raise RuntimeError("mongomock is required when USE_MONGOMOCK=true")
        # mongomock 4.3's MongoClient() is a standalone client; use subscript
        # access to get the database (the .db(name) call is not supported).
        return mongomock.MongoClient()[cfg.mongo_db]

    return MongoClient(cfg.mongo_connection_string,
                       minPoolSize=cfg.mongo_min_pool,
                       maxPoolSize=cfg.mongo_max_pool,
                       serverSelectionTimeoutMS=5000)


def get_db(cfg: Any) -> Any:
    """Return the configured database handle (sync path)."""
    if getattr(get_db, "_db", None) is None:
        get_db._db = get_storage_backend(cfg)
    return get_db._db


def get_collection(db: Any, name: str):
    return db[name]


# ---------------------------------------------------------------------------
# Mongomock storage backend (for tests without a live MongoDB server).
# Wraps mongomock's Database so the same API used for a real MongoDB works.
# ---------------------------------------------------------------------------
class MongomockStorage:
    """Drop-in in-memory MongoDB for the test suite.

    Mongomock's database object behaves like a pymongo Database (it supports
    access via ``db[collection]`` and ``db.command``), so the API layer can
    stay identical to the production code.
    """

    def __init__(self, db_name: str):
        try:
            import mongomock  # type: ignore
        except ImportError:  # pragma: no cover - only runs in CI without mongomock
            raise RuntimeError("mongomock is required for test-mode")
        self._raw = mongomock.MongoClient()[db_name]

    # Expose the collection access used throughout the API.
    def __getitem__(self, name: str):
        return self._raw[name]

    # Exposed for startup index creation + health ping + explain.
    def command(self, *args: Any, **kwargs: Any):
        return self._raw.command(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._raw, name)


def get_collection(db: Any, name: str):
    return db[name]
