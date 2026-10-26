"""Shared pytest fixtures: a fresh SQLite-backed store, API client and local callback receiver per test."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import ReminderStore
from tests.receiver import Receiver


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


# A test client for a fresh app. The `with` block runs the app's startup (creates
# the schema and starts the background sender). The sender's first check is an
# hour away, so it never touches these tests' reminders; test_dispatcher.py covers sending.
@pytest.fixture
def client(db_path):
    with TestClient(create_app(db_path, poll_interval=3600)) as c:
        yield c


# A local HTTP server on a random port that records the requests it gets.
@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.stop()
