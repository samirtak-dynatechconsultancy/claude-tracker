"""Tests for the FastAPI backend."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_TRACKER_API_KEY", "test-key")
    monkeypatch.setenv("CLAUDE_TRACKER_ADMIN_USER", "adm")
    monkeypatch.setenv("CLAUDE_TRACKER_ADMIN_PASS", "pw")
    monkeypatch.setenv(
        "CLAUDE_TRACKER_BACKEND_DB", str(tmp_path / "events.sqlite")
    )
    # Reimport to pick up env.
    import importlib
    from backend import main as backend_main
    importlib.reload(backend_main)
    # Also update module-level constants that captured env at import time.
    backend_main.ADMIN_USER = "adm"
    backend_main.ADMIN_PASS = "pw"
    return TestClient(backend_main.create_app())


def _basic(user: str, pw: str) -> dict:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_healthz_is_open(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_events_requires_api_key(client):
    r = client.post("/events", json={"events": []})
    assert r.status_code == 401


def test_events_accepts_batch(client):
    payload = {
        "events": [
            {
                "user": "alice", "hostname": "ws-1",
                "source": "code", "event_type": "message",
                "timestamp": 1_700_000_000.0,
                "message_id": "msg-1", "model": "claude-opus-4-7",
                "input_tokens": 100, "output_tokens": 250,
                "cache_creation_tokens": 10, "cache_read_tokens": 5,
            },
            {
                "user": "bob", "hostname": "ws-2",
                "source": "ai_web", "event_type": "message",
                "timestamp": 1_700_000_100.0,
                "message_id": "msg-2", "model": "claude-opus-4-7",
                "input_tokens": 50, "output_tokens": 75,
            },
        ]
    }
    r = client.post("/events", json=payload, headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    assert r.json()["accepted"] == 2


def test_summary_requires_admin_basic_auth(client):
    r = client.get("/api/summary")
    assert r.status_code == 401

    r = client.get("/api/summary", headers=_basic("adm", "wrong"))
    assert r.status_code == 401

    r = client.get("/api/summary", headers=_basic("adm", "pw"))
    assert r.status_code == 200


def test_summary_aggregates_by_user_and_source(client):
    # Ingest a small dataset.
    client.post(
        "/events",
        json={
            "events": [
                {
                    "user": "alice", "hostname": "h", "source": "code",
                    "event_type": "message", "timestamp": 1_700_000_000.0,
                    "input_tokens": 100, "output_tokens": 250,
                },
                {
                    "user": "alice", "hostname": "h", "source": "ai_web",
                    "event_type": "message", "timestamp": 1_700_000_500.0,
                    "input_tokens": 10, "output_tokens": 20,
                },
                {
                    "user": "bob", "hostname": "h", "source": "code",
                    "event_type": "message", "timestamp": 1_700_000_000.0,
                    "input_tokens": 1, "output_tokens": 2,
                },
            ]
        },
        headers={"X-API-Key": "test-key"},
    )
    r = client.get("/api/summary", headers=_basic("adm", "pw"))
    assert r.status_code == 200
    body = r.json()

    alice = body["per_user"]["alice"]
    bob = body["per_user"]["bob"]
    assert alice["by_source"]["code"]["output_tokens"] == 250
    assert alice["by_source"]["ai_web"]["output_tokens"] == 20
    assert alice["total"]["output_tokens"] == 270
    assert bob["by_source"]["code"]["output_tokens"] == 2
    assert bob["total"]["messages"] == 1

    # Leaderboard is sorted by output tokens desc.
    all_lb = body["leaderboard"]["all"]
    assert [r["user"] for r in all_lb] == ["alice", "bob"]


def test_dashboard_route_requires_auth(client):
    r = client.get("/")
    assert r.status_code == 401
    r = client.get("/", headers=_basic("adm", "pw"))
    # The HTML file exists in our repo; assert we got HTML or 500 if missing.
    # In the test environment the path is relative to backend/, which does
    # contain dashboard/index.html, so this should be 200.
    assert r.status_code == 200
    assert "Claude Usage Tracker" in r.text
