# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the building records seam in database.py."""

from contextlib import contextmanager
from decimal import Decimal

import database


def _split_top_level(clause: str) -> list:
    """Split a SQL list on commas that are not nested inside parentheses."""
    items, depth, current = [], 0, ""
    for char in clause:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        items.append(current.strip())
    return items


class _RecordingCursor:
    def __init__(self, rows=None, calls=None):
        self.rows = rows or []
        self.calls = calls if calls is not None else []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return None

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _RecordingConnection:
    def __init__(self, rows=None, calls=None):
        self.rows = rows or []
        self.calls = calls if calls is not None else []

    def cursor(self):
        return _RecordingCursor(self.rows, self.calls)

    def commit(self):
        return None


def _buildings_insert(calls):
    for query, params in calls:
        if "INSERT INTO buildings" in query:
            return query, params
    raise AssertionError("no INSERT INTO buildings was executed")


def test_save_building_binds_every_column_placeholder(monkeypatch):
    """The buildings INSERT must supply one placeholder per column."""
    calls = []

    @contextmanager
    def _fake_get_connection():
        yield _RecordingConnection(calls=calls)

    monkeypatch.setattr(database, "get_connection", _fake_get_connection)
    database.save_building(
        building_id="test-1",
        email="test@example.ch",
        profile={"address": "Hauptstrasse 1", "lat": 46.9, "lon": 7.4},
        consents={},
    )

    query, params = _buildings_insert(calls)
    columns = _split_top_level(
        query.split("INSERT INTO buildings (", 1)[1].split(") VALUES", 1)[0]
    )
    values = _split_top_level(
        query.split(") VALUES", 1)[1].split("(", 1)[1].rsplit(")", 1)[0]
    )

    assert len(values) == len(columns)
    assert len(params) == len(columns)


def test_save_building_wraps_verified_at_in_to_timestamp(monkeypatch):
    """verified_at is epoch seconds, so it needs to_timestamp like registered_at."""
    calls = []

    @contextmanager
    def _fake_get_connection():
        yield _RecordingConnection(calls=calls)

    monkeypatch.setattr(database, "get_connection", _fake_get_connection)
    database.save_building(
        building_id="test-1",
        email="test@example.ch",
        profile={"address": "Hauptstrasse 1", "lat": 46.9, "lon": 7.4},
        consents={},
    )

    query, _ = _buildings_insert(calls)
    columns = _split_top_level(
        query.split("INSERT INTO buildings (", 1)[1].split(") VALUES", 1)[0]
    )
    values = _split_top_level(
        query.split(") VALUES", 1)[1].split("(", 1)[1].rsplit(")", 1)[0]
    )

    for column in ("registered_at", "verified_at"):
        assert values[columns.index(column)] == "to_timestamp(%s)"


def test_get_all_building_profiles_coerces_decimals_to_float(monkeypatch):
    """NUMERIC columns arrive as Decimal, which numpy cannot mix with float."""

    @contextmanager
    def _fake_get_connection():
        yield _RecordingConnection(
            rows=[
                {
                    "building_id": "test-1",
                    "address": "Hauptstrasse 1",
                    "lat": Decimal("46.9380"),
                    "lon": Decimal("7.4474"),
                    "plz": "3150",
                    "building_type": "efh",
                    "annual_consumption_kwh": Decimal("4500"),
                    "potential_pv_kwp": Decimal("12.5"),
                    "user_type": "anonymous",
                }
            ]
        )

    monkeypatch.setattr(database, "get_connection", _fake_get_connection)
    profile = database.get_all_building_profiles()[0]

    for field in ("lat", "lon", "annual_consumption_kwh", "potential_pv_kwp"):
        assert type(profile[field]) is float, f"{field} is {type(profile[field])}"
    assert profile["lat"] == 46.9380
    assert profile["plz"] == "3150"


def test_get_all_building_profiles_keeps_missing_numerics_none(monkeypatch):
    """A NULL NUMERIC stays None rather than becoming 0.0."""

    @contextmanager
    def _fake_get_connection():
        yield _RecordingConnection(
            rows=[
                {
                    "building_id": "test-1",
                    "lat": Decimal("46.9380"),
                    "lon": Decimal("7.4474"),
                    "annual_consumption_kwh": None,
                    "potential_pv_kwp": None,
                }
            ]
        )

    monkeypatch.setattr(database, "get_connection", _fake_get_connection)
    profile = database.get_all_building_profiles()[0]

    assert profile["annual_consumption_kwh"] is None
    assert profile["potential_pv_kwp"] is None
