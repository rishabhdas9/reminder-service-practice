"""Shared pytest fixtures: a fresh SQLite-backed store and API client per test."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import ReminderStore


# A ReminderStore on a throwaway database file, with the schema created.
@pytest.fixture
def store(tmp_path):
    s = ReminderStore(str(tmp_path / "test.db"))
    s.init_schema()
    return s


# Path of the throwaway database file the `client` fixture's app uses.
@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "api.db")


# A test client for a fresh app. The `with` block runs the app's startup (creates the schema).
@pytest.fixture
def client(db_path):
    with TestClient(create_app(db_path)) as c:
        yield c
