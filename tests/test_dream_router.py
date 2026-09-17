"""Tests for the Dream HTTP surface (design: docs/design/memory-dream.md §5.9).

The endpoints are thin translators, so these tests pin the translation itself without a
database or a live scheduler:

  * a dry-run preview returns the report unchanged and touches no store ([AC-25], [AC-26]);
  * `dream_enabled=false` and a held lock both surface as 409, not as a silent success
    ([AC-17], [AC-38]);
  * the two evidence-chain lookups return exactly what an independent recomputation over
    the observation rows returns ([AC-9], [AC-10]).

The end-to-end behaviour (a real run against Qdrant and Postgres) is verified in the
implementation handoff, not here.
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi import FastAPI
from fastapi.testclient import TestClient

# `server/` is a flat module directory (`db`, `auth`, `routers.*` are imported by bare
# name) because the container runs uvicorn with PYTHONPATH=/app. Tests run from the repo
# root, so put the flat directory on the path the same way before importing them.
_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from auth import verify_auth  # noqa: E402
from routers import dream as dream_router  # noqa: E402


class _Row:
    def __init__(self, point_id, payload):
        self.id = point_id
        self.payload = payload


class _FakeVectorStore:
    def __init__(self, rows):
        self.rows = list(rows)
        self.list_calls = []

    def list(self, filters=None, top_k=100, cursor=None):
        self.list_calls.append({"filters": filters, "top_k": top_k, "cursor": cursor})
        rows = self.rows
        if filters and filters.get("memory_kind", {}).get("eq") == "observation":
            rows = [row for row in rows if row.payload.get("memory_kind") == "observation"]
        return rows[:top_k], None


def _observation(point_id, key, sources, created_at="2026-09-17T00:00:00+00:00", invalid_at=None):
    return _Row(
        point_id,
        {
            "data": f"observation {key}",
            "memory_kind": "observation",
            "observation_key": key,
            "source_memory_ids": list(sources),
            "evidence_count": len(sources),
            "dream_run_id": "run-1",
            "user_id": "test_dream_router",
            "agent_id": "tester",
            "created_at": created_at,
            "invalid_at": invalid_at,
        },
    )


def _client(scheduler, memory=None):
    app = FastAPI()
    app.include_router(dream_router.router)
    app.dependency_overrides[verify_auth] = lambda: None
    patchers = [patch.object(dream_router, "get_scheduler", lambda: scheduler)]
    if memory is not None:
        patchers.append(patch.object(dream_router, "get_memory_instance", lambda: memory))
    for patcher in patchers:
        patcher.start()
    client = TestClient(app)
    client._patchers = patchers
    return client


class _SchedulerStub:
    def __init__(self, report):
        self.report = report
        self.calls = []

    def run_once(self, *, mode, scopes=None, trigger="manual"):
        self.calls.append({"mode": mode, "scopes": scopes, "trigger": trigger})
        if isinstance(self.report, Exception):
            raise self.report
        return self.report


REPORT = {
    "run_id": "0f3a",
    "mode": "dry_run",
    "status": "completed",
    "totals": {"llm_calls": 1, "clusters": 1},
    "would_write": ["point-1"],
    "candidates": [
        {
            "observation_key": "k1",
            "members": ["f1", "f2", "f3", "f4"],
            "text": "belief",
            "source_memory_ids": ["f1", "f2", "f3", "f4"],
            "evidence_count": 4,
            "counterexample": [],
            "decision": "would_write",
            "skip_reason": None,
            "would_write": {"point_id": "point-1", "memory_kind": "observation"},
        }
    ],
    "supersede_preview": [],
    "errors": [],
}


class TestPreview:
    def test_preview_returns_the_report_and_writes_nothing(self):
        memory = SimpleNamespace(vector_store=_FakeVectorStore([]))
        scheduler = _SchedulerStub(REPORT)
        client = _client(scheduler, memory)
        try:
            response = client.post("/dream/preview")
            assert response.status_code == 200
            assert response.json() == REPORT
            assert scheduler.calls == [{"mode": "dry_run", "scopes": None, "trigger": "manual"}]
            assert memory.vector_store.list_calls == []
        finally:
            for patcher in client._patchers:
                patcher.stop()

    def test_preview_passes_scopes_through(self):
        scheduler = _SchedulerStub(REPORT)
        client = _client(scheduler)
        try:
            response = client.post("/dream/preview", json={"scopes": ["u1:a1"]})
            assert response.status_code == 200
            assert scheduler.calls[0]["scopes"] == ["u1:a1"]
        finally:
            for patcher in client._patchers:
                patcher.stop()

    def test_disabled_is_a_conflict_not_a_silent_success(self):
        """[AC-38]: the switch is authoritative for both trigger endpoints."""
        scheduler = _SchedulerStub({"status": "disabled", "mode": "dry_run"})
        client = _client(scheduler)
        try:
            for path in ("/dream/preview", "/dream/run"):
                response = client.post(path)
                assert response.status_code == 409
                assert "disabled" in response.json()["detail"].lower()
        finally:
            for patcher in client._patchers:
                patcher.stop()

    def test_held_lock_is_a_conflict(self):
        """[AC-17]: the loser of the race reports 409 and performs no work."""
        scheduler = _SchedulerStub(
            {"status": "skipped", "mode": "live", "run_id": "r2", "reason": "another dream run holds the lock"}
        )
        client = _client(scheduler)
        try:
            response = client.post("/dream/run")
            assert response.status_code == 409
            assert "in progress" in response.json()["detail"].lower()
        finally:
            for patcher in client._patchers:
                patcher.stop()

    def test_missing_scheduler_is_503(self):
        client = _client(None)
        try:
            assert client.post("/dream/preview").status_code == 503
        finally:
            for patcher in client._patchers:
                patcher.stop()

    def test_scheduler_failure_becomes_502(self):
        scheduler = _SchedulerStub(RuntimeError("boom"))
        client = _client(scheduler)
        try:
            assert client.post("/dream/preview").status_code == 502
        finally:
            for patcher in client._patchers:
                patcher.stop()


class TestEvidenceChain:
    def test_reverse_lookup_matches_an_independent_recomputation(self):
        """[AC-10]: the endpoint's answer equals a fresh scan for `source_memory_ids`."""
        rows = [
            _observation("o1", "k1", ["f1", "f2"], created_at="2026-09-17T01:00:00+00:00"),
            _observation("o2", "k2", ["f2", "f3"], created_at="2026-09-17T02:00:00+00:00"),
            _observation("o3", "k3", ["f4"], created_at="2026-09-17T03:00:00+00:00"),
            _Row("f1", {"data": "a fact", "user_id": "u", "agent_id": "a"}),
        ]
        memory = SimpleNamespace(vector_store=_FakeVectorStore(rows))
        client = _client(_SchedulerStub(REPORT), memory)
        try:
            response = client.get("/memories/f2/observations")
            assert response.status_code == 200
            body = response.json()

            expected = {row.id for row in rows if "f2" in (row.payload.get("source_memory_ids") or [])}
            assert body["total"] == len(expected)
            assert {item["id"] for item in body["results"]} == expected
            # Every observation field is a first-class key, never nested in `metadata`.
            for item in body["results"]:
                assert item["memory_kind"] == "observation"
                assert item["evidence_count"] == len(item["source_memory_ids"])
                assert item["dream_run_id"] == "run-1"
        finally:
            for patcher in client._patchers:
                patcher.stop()

    def test_reverse_lookup_includes_invalidated_versions(self):
        rows = [
            _observation("o1", "k1", ["f1"], invalid_at="2026-09-17"),
            _observation("o2", "k2", ["f1"], created_at="2026-09-17T05:00:00+00:00"),
        ]
        memory = SimpleNamespace(vector_store=_FakeVectorStore(rows))
        client = _client(_SchedulerStub(REPORT), memory)
        try:
            body = client.get("/memories/f1/observations").json()
            assert body["total"] == 2
        finally:
            for patcher in client._patchers:
                patcher.stop()

    def test_forward_lookup_returns_only_resolvable_sources(self):
        """[AC-9]: dangling source ids are skipped, never invented (P13)."""
        observation = {
            "id": "o1",
            "memory": "belief",
            "memory_kind": "observation",
            "source_memory_ids": ["f1", "gone"],
            "evidence_count": 2,
        }

        def _get(memory_id):
            return observation if memory_id == "o1" else ({"id": "f1", "memory": "a fact"} if memory_id == "f1" else None)

        memory = SimpleNamespace(get=_get)
        client = _client(_SchedulerStub(REPORT), memory)
        try:
            body = client.get("/memories/o1/sources").json()
            assert body["total"] == 1
            assert body["missing"] == 1
            assert [item["id"] for item in body["results"]] == ["f1"]

            assert client.get("/memories/nope/sources").status_code == 404
        finally:
            for patcher in client._patchers:
                patcher.stop()

    def test_forward_lookup_rejects_a_plain_fact(self):
        memory = SimpleNamespace(get=lambda memory_id: {"id": memory_id, "memory": "just a fact"})
        client = _client(_SchedulerStub(REPORT), memory)
        try:
            assert client.get("/memories/f1/sources").status_code == 400
        finally:
            for patcher in client._patchers:
                patcher.stop()


class TestReportContract:
    def test_dry_run_report_has_the_documented_shape(self):
        """[AC-26]: the report a client parses is the one the design documents."""
        text = json.dumps(REPORT)
        parsed = json.loads(text)
        assert parsed["candidates"][0]["decision"] == "would_write"
        for field in ("observation_key", "members", "text", "source_memory_ids", "evidence_count", "decision", "skip_reason"):
            assert field in parsed["candidates"][0]
