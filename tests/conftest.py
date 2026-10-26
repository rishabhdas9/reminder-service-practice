"""Shared pytest fixtures: a fresh SQLite-backed store per test."""

import pytest

from app.store import ReminderStore


# A ReminderStore on a throwaway database file, with the schema created.
@pytest.fixture
def store(tmp_path):
    s = ReminderStore(str(tmp_path / "test.db"))
    s.init_schema()
    return s
