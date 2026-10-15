"""Application configuration.

Every connection/string value is read from environment variables. A real
environment variable always wins over a `.env` value. Values fall back to
defaults so the app can boot in a throw-away environment.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000

    # --- MongoDB ---
    # Primary connection string. If unset, MONGO_HOST/PORT/DB/USER/PASSWORD
    # are combined into one. Env vars win over .env for every member.
    mongo_uri: Optional[str] = None
    mongo_host: str = "localhost"
    mongo_port: int = 27017
    mongo_db: str = "hrone_attendance"
    mongo_user: Optional[str] = None
    mongo_password: Optional[str] = None

    # --- MongoDB connection pool / eager readiness ---
    mongo_min_pool: int = 1
    mongo_max_pool: int = 50

    # Replica-set name (if the deployment uses one). Transactions need one.
    mongo_replica_set: Optional[str] = None

    # --- Test mode: when true, use an in-process MongoDB mock instead of a live server ---
    test_mode: bool = False
    use_mongomock: bool = False

    @property
    def mongo_connection_string(self) -> str:
        """Return the MongoDB connection string.

        A real environment variable always takes precedence over `.env`.
        """
        if self.mongo_uri:
            return self.mongo_uri

        user = self.mongo_user
        password = self.mongo_password
        auth_part = ""
        if user and password:
            # Percent-encode the user/password so special chars are safe.
            auth_part = f"{self._encode(user)}:{self._encode(password)}@"

        return f"mongodb://{auth_part}{self.mongo_host}:{self.mongo_port}/"

    @staticmethod
    def _encode(value: str) -> str:
        """Minimal URL-encoding for user/password (no extra deps needed)."""
        # Encode characters that are illegal or meaningful in a URI.
        safe = {" ": "%20", "!": "%21", "#": "%23", "$": "%24", "&": "%26",
                "'": "%27", "(": "%28", ")": "%29", "*": "%2A", "+": "%2B",
                ",": "%2C", "/": "%2F", ":": "%3A", ";": "%3B", "=": "%3D",
                "?": "%3F", "@": "%40", "[": "%5B", "]": "%5D"}
        return "".join(safe.get(ch, ch) for ch in value)

    @property
    def mongo_ready_connection_string(self) -> str:
        """Connection string with credentials in the host portion (used for
        driver-level auth when no single URI is supplied)."""
        return self.mongo_connection_string


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached, lazily-built settings singleton."""
    return Settings()


def get_settings_no_cache() -> Settings:
    """Return a fresh settings instance (used by tests that need to swap env)."""
    return Settings()
