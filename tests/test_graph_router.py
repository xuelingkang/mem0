"""Tests for the graph observability router (design: docs/design/graph-memory.md §8).

`GET /graph/stats` is the read path that makes the graph dispatch counters judgeable
without the bridge access log as a proxy (the log shows nothing for the dispatches the
circuit breaker drops), so these tests pin three things without a live server:

  * the five counters plus the circuit / queue readings are passed through untouched;
  * a capability that was switched on but never dispatched reads as zeros;
  * reading the endpoint never materialises a dispatcher (that would build a bridge
    HTTP client as a side effect of looking at the numbers).
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# `server/` is a flat module directory (`auth`, `routers.*` are imported by bare name)
# because the container runs uvicorn with PYTHONPATH=/app. Tests run from the repo root,
# so put the flat directory on the path the same way before importing them.
_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from auth import verify_auth  # noqa: E402
from mem0.configs.base import GraphConfig  # noqa: E402
from routers import graph as graph_router  # noqa: E402

REQUIRED_COUNTERS = ("graph_dispatched", "graph_synced", "graph_dropped", "graph_failed")


class _SyncStub:
    """Counts reads so a test can prove the endpoint hit the dispatcher exactly once."""

    def __init__(self, **stats):
        self._stats = stats
        self.stats_calls = 0

    def stats(self):
        self.stats_calls += 1
        return dict(self._stats)


def _memory(enabled, sync=None, timeout_seconds=0.4):
    config = SimpleNamespace(graph=GraphConfig(enabled=enabled, timeout_seconds=timeout_seconds))
    memory = SimpleNamespace(config=config)
    if sync is not None:
        memory._graph_sync = sync
    return memory


def _client(memory):
    app = FastAPI()
    app.include_router(graph_router.router)
    app.dependency_overrides[verify_auth] = lambda: None
    patcher = patch.object(graph_router, "get_memory_instance", lambda: memory)
    patcher.start()
    client = TestClient(app)
    client._patcher = patcher
    return client


class TestGraphStats:
    def test_counters_are_passed_through(self):
        sync = _SyncStub(
            graph_dispatched=5,
            graph_synced=3,
            graph_already_synced=1,
            graph_failed=1,
            graph_dropped=2,
            consecutive_failures=1,
            queue_size=4,
            circuit_open=True,
        )
        client = _client(_memory(True, sync, timeout_seconds=0.8))
        try:
            response = client.get("/graph/stats")
            assert response.status_code == 200
            body = response.json()
            assert body["graph_dispatched"] == 5
            assert body["graph_synced"] == 3
            assert body["graph_already_synced"] == 1
            assert body["graph_failed"] == 1
            assert body["graph_dropped"] == 2
            assert body["circuit_open"] is True
            assert body["consecutive_failures"] == 1
            assert body["queue_size"] == 4
            assert body["enabled"] is True
            assert body["timeout_seconds"] == 0.8
            assert sync.stats_calls == 1
        finally:
            client._patcher.stop()

    def test_the_judged_counters_are_always_published(self):
        """[AC-9]/[AC-25] 的判定面：四个计数器在任何状态下都可读。"""
        client = _client(_memory(True, _SyncStub()))
        try:
            body = client.get("/graph/stats").json()
            assert all(key in body for key in REQUIRED_COUNTERS)
        finally:
            client._patcher.stop()

    def test_disabled_capability_reads_as_zeros(self):
        client = _client(_memory(False))
        try:
            body = client.get("/graph/stats").json()
            assert body["enabled"] is False
            assert all(body[key] == 0 for key in graph_router.COUNTER_KEYS)
            assert body["circuit_open"] is False
        finally:
            client._patcher.stop()

    def test_never_dispatched_reads_as_zeros_without_creating_a_dispatcher(self):
        memory = _memory(True)
        client = _client(memory)
        try:
            body = client.get("/graph/stats").json()
            assert body["enabled"] is True
            assert all(body[key] == 0 for key in graph_router.COUNTER_KEYS)
            assert getattr(memory, "_graph_sync", None) is None
        finally:
            client._patcher.stop()

    def test_uninitialized_runtime_is_503(self):
        app = FastAPI()
        app.include_router(graph_router.router)
        app.dependency_overrides[verify_auth] = lambda: None

        def _raise():
            raise RuntimeError("Mem0 runtime has not been initialized.")

        with patch.object(graph_router, "get_memory_instance", _raise):
            response = TestClient(app).get("/graph/stats")
        assert response.status_code == 503
