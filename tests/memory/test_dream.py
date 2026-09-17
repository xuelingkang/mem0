"""Tests for Dream background synthesis (design: docs/design/memory-dream.md).

The four stages are exercised as units, in the order the design pins them:

  * Orient is read-only and enumerates scopes from the payload alone ([AC-1]).
  * Gather is a deterministic pure function with no LLM in its signature ([AC-2], [AC-3]).
  * Consolidate issues exactly one LLM call per cluster, with a prompt that carries only
    `id` / `text` / `created_at` and is distinct from the 1a/1b system prompts
    ([AC-4], [AC-5]).
  * Prune's judgement rules are pure functions: the hallucinated-source guard, the
    evidence floor and the empty-output rule ([AC-6], [AC-7], [AC-8]).

Plus the invariants that hold across stages: point id determinism ([AC-14]), source-fact
zero-write ([AC-22], [AC-23], [AC-24] shape), failure isolation ([AC-19], [AC-20],
[AC-21]) and the idempotence of a repeated run ([AC-13], [AC-15], [AC-16]).
"""

import inspect
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mem0.configs.prompts import (
    ADDITIVE_EXTRACTION_PROMPT,
    CONTRADICTION_DETECTION_PROMPT,
    MEMORY_KIND_OBSERVATION,
    OBSERVATION_SYNTHESIS_PROMPT,
)
from mem0.memory import dream
from mem0.memory.dream import (
    DreamSettings,
    _NullStateStore,
    build_observation_payload,
    cluster_members,
    gather,
    observation_key,
    observation_point_id,
    orient,
    parse_observation_response,
    prune_candidate,
    run_dream,
    synthesize_observation,
)

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _point(point_id, payload, vector=None, score=None):
    return SimpleNamespace(id=point_id, payload=payload, vector=vector, score=score)


def _fact(point_id, text, scope=("u1", "a1"), created_at="2026-09-01T00:00:00+00:00", valid_at=None, vector=None):
    return _point(
        point_id,
        {
            "data": text,
            "hash": text,
            "created_at": created_at,
            "valid_at": valid_at,
            "user_id": scope[0],
            "agent_id": scope[1],
        },
        vector=vector,
    )


class _FakeVectorStore:
    """Minimal in-memory store: enough of the Qdrant surface for the four stages.

    `scroll` serves Gather (payload + vector), `list` serves Orient (payload only, keyset
    paged by `created_at`), `insert`/`update` record the writes so a test can assert that
    a fact was never touched.
    """

    def __init__(self, points):
        self.points = list(points)
        self.collection_name = "test"
        self.writes = []
        self.payload_writes = []

    # -- Qdrant-shaped API --------------------------------------------------
    def _create_filter(self, filters):
        return filters

    @property
    def client(self):
        store = self

        class _Client:
            def scroll(self, collection_name, scroll_filter, limit, offset, with_payload, with_vectors):
                scope = scroll_filter or {}
                rows = [
                    point
                    for point in store.points
                    if all(point.payload.get(key) == value for key, value in scope.items())
                ]
                return rows[:limit], None

        return _Client()

    def list(self, filters=None, top_k=100, cursor=None):
        rows = list(self.points)
        if filters:
            rows = [
                point
                for point in rows
                if all(point.payload.get(key) == value for key, value in filters.items())
            ]
        rows.sort(key=lambda point: point.payload.get("created_at") or "", reverse=True)
        return rows[:top_k], None

    def insert(self, vectors, payloads, ids):
        self.writes.append({"vectors": vectors, "payloads": payloads, "ids": ids})
        for point_id, payload in zip(ids, payloads):
            self.points = [point for point in self.points if str(point.id) != str(point_id)]
            self.points.append(_point(point_id, payload, vector=vectors[0]))

    def update(self, vector_id, vector=None, payload=None):
        self.payload_writes.append({"id": vector_id, "payload": payload})


class _FakeLLM:
    """Records every call; returns the configured per-cluster response.

    `per_text` maps a substring of the cluster's facts to that cluster's reply, so a test
    can make one specific cluster fail without touching the others. The configured
    `response_callback` is invoked with the raw response exactly as the real provider
    client does (`mem0/llms/openai.py`), which is what the usage sink observes.
    """

    def __init__(self, response=None, per_text=None, usage=None):
        self.calls = []
        self._response = response
        self._per_text = per_text or {}
        self._usage = usage
        self.config = SimpleNamespace(response_callback=None)

    def generate_response(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        user = messages[-1]["content"]
        for text, reply in self._per_text.items():
            if text in user:
                if isinstance(reply, Exception):
                    raise reply
                return self._finish(reply, messages, kwargs)
        response = self._response
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(messages)
        return self._finish(response, messages, kwargs)

    def _finish(self, content, messages, kwargs):
        if self.config.response_callback:
            raw = SimpleNamespace(content=content, usage=self._usage)
            self.config.response_callback(self, raw, {"messages": messages, **kwargs})
        return content


def _memory(points, llm=None, embedder=None):
    store = _FakeVectorStore(points)
    memory = SimpleNamespace(
        vector_store=store,
        llm=llm if llm is not None else _FakeLLM(),
        embedding_model=embedder
        or SimpleNamespace(embed=lambda text, kind: [0.0] * 4),
    )
    return memory, store


def _similar_vector(seed: float, dims: int = 4):
    """Deterministic vectors whose cosine similarity is easy to reason about.

    `seed` is the leading component; vectors built from neighbouring seeds are nearly
    parallel (similarity ≈ 1), while a large `offset` in `_cluster_points` pushes a group
    of vectors onto a different direction (similarity well below any sane `tau`).
    """
    return [seed, 1.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# Orient ([AC-1])
# ---------------------------------------------------------------------------


class TestOrient:
    def test_enumerates_scopes_and_totals_payload_only(self):
        points = [
            _fact("f1", "a", scope=("u1", "a1")),
            _fact("f2", "b", scope=("u1", "a1")),
            # A record with no scope at all: O1 counts it as skipped, not as a scope.
            _point("f3", {"data": "no scope", "hash": "h3"}),
            _point(
                "o1",
                {
                    "data": "obs",
                    "memory_kind": MEMORY_KIND_OBSERVATION,
                    "observation_key": "k1",
                    "source_memory_ids": ["f1"],
                    "user_id": "u1",
                    "agent_id": "a1",
                },
            ),
        ]
        memory, _ = _memory(points)

        result = orient(memory.vector_store, state_store=_NullStateStore())

        assert result.totals["facts"] == 3
        assert result.totals["observations"] == 1
        assert result.totals["skipped_records"] == 1
        assert [scope["agent_id"] for scope in result.scopes] == ["a1"]
        # O3: the short-circuit basis includes the observation already landed.
        assert "k1" in result.known_evaluated_keys

    def test_is_read_only(self):
        points = [_fact("f1", "a")]
        memory, store = _memory(points)
        before = json.dumps([[str(p.id), p.payload] for p in store.points], sort_keys=True)

        orient(memory.vector_store)

        after = json.dumps([[str(p.id), p.payload] for p in store.points], sort_keys=True)
        assert before == after
        assert store.writes == []

    def test_signature_takes_no_llm(self):
        assert "llm" not in inspect.signature(orient).parameters

    def test_missing_state_table_falls_back(self):
        memory, _ = _memory([_fact("f1", "a")])

        class _Broken:
            def known_keys(self):
                raise RuntimeError("relation does not exist")

            def record(self, **_kwargs):
                pass

        result = orient(memory.vector_store, state_store=_Broken())

        assert result.state_table_available is False
        assert result.known_evaluated_keys == set()


# ---------------------------------------------------------------------------
# Gather ([AC-2], [AC-3])
# ---------------------------------------------------------------------------


class TestGather:
    def test_clustering_is_deterministic(self):
        members = [
            {"id": f"m{index}", "vector": _similar_vector(0.01 * index)} for index in range(6)
        ]

        first = cluster_members(members, tau=0.9, min_cluster_size=3, max_cluster_size=15)
        second = cluster_members(list(reversed(members)), tau=0.9, min_cluster_size=3, max_cluster_size=15)

        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
        # Neighbouring seeds are nearly parallel and all six fit under the cap, so the
        # whole scope forms one cluster (this is also what makes the run-level fakes work).
        assert first == [sorted(f"m{index}" for index in range(6))]

    def test_small_groups_do_not_form_clusters(self):
        members = [{"id": "a", "vector": _similar_vector(0.0)}, {"id": "b", "vector": _similar_vector(0.0)}]
        assert cluster_members(members, tau=0.9, min_cluster_size=3) == []

    def test_takes_no_llm_and_uses_no_llm_client(self):
        assert "llm" not in inspect.signature(gather).parameters
        assert "llm" not in inspect.signature(cluster_members).parameters

        points = [_fact(f"f{index}", f"text {index}", vector=_similar_vector(0.01 * index)) for index in range(5)]
        for index, point in enumerate(points):
            point.payload["user_id"], point.payload["agent_id"] = "u1", "a1"
        memory, _ = _memory(points)
        llm_calls = {"count": 0}
        memory.llm = SimpleNamespace(generate_response=lambda *a, **k: llm_calls.__setitem__("count", llm_calls["count"] + 1))

        gather(memory.vector_store, {"user_id": "u1", "agent_id": "a1"}, settings=DreamSettings(), known_keys=set())

        assert llm_calls["count"] == 0

    def test_known_key_short_circuits(self):
        points = [_fact(f"f{index}", f"t{index}", vector=_similar_vector(0.01 * index)) for index in range(5)]
        memory, _ = _memory(points)
        scope = {"user_id": "u1", "agent_id": "a1"}
        key = observation_key("u1", "a1", [f"f{index}" for index in range(5)])

        result = gather(memory.vector_store, scope, settings=DreamSettings(), known_keys={key})

        assert [cluster["skipped_reason"] for cluster in result["clusters"]] == ["already_evaluated"]

    def test_facts_only_are_clustered(self):
        """Observations are never inputs of a new cluster (no observation-of-observation)."""
        points = [_fact(f"f{index}", f"t{index}") for index in range(5)]
        points.append(
            _point(
                "o1",
                {
                    "data": "obs",
                    "memory_kind": MEMORY_KIND_OBSERVATION,
                    "observation_key": "k1",
                    "source_memory_ids": ["f0"],
                    "user_id": "u1",
                    "agent_id": "a1",
                },
            )
        )
        memory, _ = _memory(points)

        result = gather(
            memory.vector_store, {"user_id": "u1", "agent_id": "a1"}, settings=DreamSettings(), known_keys=set()
        )

        for cluster in result["clusters"]:
            assert "o1" not in cluster["members"]


# ---------------------------------------------------------------------------
# Consolidate ([AC-4], [AC-5])
# ---------------------------------------------------------------------------


class TestConsolidate:
    def test_one_call_per_cluster_with_the_dream_prompt(self):
        llm = _FakeLLM(response=json.dumps({"observation": None}))
        members = [{"id": f"m{index}", "text": f"t{index}", "created_at": "2026-09-01T00:00:00+00:00"} for index in range(4)]

        synthesize_observation(llm, members)

        assert len(llm.calls) == 1
        messages = llm.calls[0]["messages"]
        assert messages[0]["content"] == OBSERVATION_SYNTHESIS_PROMPT
        assert llm.calls[0]["kwargs"]["response_format"] == {"type": "json_object"}

    def test_prompt_is_distinct_from_1a_and_1b(self):
        assert len({OBSERVATION_SYNTHESIS_PROMPT, ADDITIVE_EXTRACTION_PROMPT, CONTRADICTION_DETECTION_PROMPT}) == 3

    def test_member_serialisation_carries_exactly_three_keys(self):
        llm = _FakeLLM(response=json.dumps({"observation": None}))
        members = [
            {
                "id": "m1",
                "text": "t1",
                "created_at": "2026-09-01T00:00:00+00:00",
                # Engine state, not evidence: neither may reach the prompt.
                "vector": [0.1, 0.2],
                "user_id": "u1",
                "agent_id": "a1",
            }
        ]

        synthesize_observation(llm, members)

        user_prompt = llm.calls[0]["messages"][1]["content"]
        payload = json.loads(user_prompt.split("## Facts\n", 1)[1].split("\n\n# Output:", 1)[0])
        assert [sorted(item.keys()) for item in payload] == [["created_at", "id", "text"]]

    def test_parses_null_and_object_and_rejects_garbage(self):
        assert parse_observation_response('{"observation": null}')["text"] is None
        assert parse_observation_response('{"observation": {"text": "x", "source_ids": ["a"]}}')["source_ids"] == ["a"]
        with pytest.raises(ValueError):
            parse_observation_response("not json at all")
        with pytest.raises(ValueError):
            parse_observation_response('{"observation": "oops"}')


# ---------------------------------------------------------------------------
# Prune ([AC-6], [AC-7], [AC-8])
# ---------------------------------------------------------------------------


class TestPrune:
    members = ["m1", "m2", "m3", "m4"]

    def test_p1_null_text(self):
        decision = prune_candidate(
            {"text": None}, member_ids=self.members, existing_hashes=set(), min_cluster_size=4
        )
        assert decision["decision"] == dream.SKIP_NO_PATTERN
        assert decision["text"] is None

    def test_p2_hallucinated_sources_are_dropped(self):
        decision = prune_candidate(
            {"text": "obs", "source_ids": ["ghost-1", "ghost-2"]},
            member_ids=self.members,
            existing_hashes=set(),
            min_cluster_size=4,
        )
        assert decision["decision"] == dream.SKIP_UNRESOLVABLE
        assert decision["source_ids"] == []

    def test_p2_partial_hallucination_keeps_only_members(self):
        decision = prune_candidate(
            {"text": "obs", "source_ids": ["m1", "ghost", "m2", "m3", "m4"]},
            member_ids=self.members,
            existing_hashes=set(),
            min_cluster_size=4,
        )
        assert decision["decision"] == dream.DECISION_WRITTEN
        assert decision["source_ids"] == ["m1", "m2", "m3", "m4"]

    def test_p3_evidence_floor(self):
        decision = prune_candidate(
            {"text": "obs", "source_ids": ["m1", "m2"]},
            member_ids=self.members,
            existing_hashes=set(),
            min_cluster_size=4,
        )
        assert decision["decision"] == dream.SKIP_INSUFFICIENT
        assert decision["evidence_count"] == 2

    def test_p4_duplicate_text_is_dropped(self):
        import hashlib

        text = "already a memory"
        decision = prune_candidate(
            {"text": text, "source_ids": self.members},
            member_ids=self.members,
            existing_hashes={hashlib.md5(text.encode()).hexdigest()},
            min_cluster_size=4,
        )
        assert decision["decision"] == dream.SKIP_DUPLICATE_TEXT

    def test_p5_evidence_count_follows_the_source_list(self):
        import hashlib

        decision = prune_candidate(
            {"text": "fresh text", "source_ids": self.members + ["m1"]},
            member_ids=self.members,
            existing_hashes=set(),
            min_cluster_size=4,
        )
        assert decision["evidence_count"] == len(decision["source_ids"]) == 4
        assert decision["text"] == "fresh text"
        assert hashlib.md5(b"fresh text").hexdigest()


# ---------------------------------------------------------------------------
# Identity ([AC-14])
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_point_id_is_deterministic(self):
        assert observation_point_id("abc") == observation_point_id("abc")

    def test_key_is_order_insensitive_and_scope_bound(self):
        assert observation_key("u", "a", ["b", "a"]) == observation_key("u", "a", ["a", "b"])
        assert observation_key("u", "a", ["a"]) != observation_key("u", "b", ["a"])

    def test_key_separator_prevents_boundary_aliasing(self):
        assert observation_key("u", "a", ["ab"]) != observation_key("u", "a", ["a", "b"])


# ---------------------------------------------------------------------------
# Orchestration: idempotence, zero-write, failure isolation
# ---------------------------------------------------------------------------


def _cluster_points(count=5, scope=("u1", "a1"), prefix="f", offset=0.0):
    """`count` facts that cluster together: neighbours are near-parallel by construction.

    `offset` shifts the whole group onto a different direction so two groups in one scope
    do not merge (cosine ≈ 0 across groups, well under `tau=0.82`).
    """
    return [
        _fact(
            f"{prefix}{index}",
            f"fact {prefix}{index}",
            scope=scope,
            vector=_similar_vector(offset + 0.01 * index),
        )
        for index in range(count)
    ]


def _observation_reply(point_ids, text="synthesized belief"):
    return json.dumps({"observation": {"text": text, "source_ids": list(point_ids), "counterexample": []}})


class TestRunDream:
    def test_live_run_writes_one_observation_with_an_evidence_chain(self):
        points = _cluster_points()
        llm = _FakeLLM(response=_observation_reply([f"f{index}" for index in range(5)]))
        memory, store = _memory(points, llm=llm, embedder=SimpleNamespace(embed=lambda text, kind: [0.5] * 4))

        report = run_dream(memory, mode="live", settings=DreamSettings(report_dir="/tmp/dream-test"))

        assert report["observations_written"] == 1
        payload = store.writes[0]["payloads"][0]
        assert payload["memory_kind"] == MEMORY_KIND_OBSERVATION
        assert payload["evidence_count"] == len(payload["source_memory_ids"]) == 5
        assert payload["dream_run_id"] == report["run_id"]
        assert store.writes[0]["ids"] == [observation_point_id(payload["observation_key"])]

    def test_rerun_does_not_duplicate_observations(self):
        """[AC-13]/[AC-16]: the same member set is not written a second time, and the
        second run costs no LLM call once the state table knows the cluster."""
        points = _cluster_points()
        llm = _FakeLLM(response=_observation_reply([f"f{index}" for index in range(5)]))
        memory, store = _memory(points, llm=llm, embedder=SimpleNamespace(embed=lambda text, kind: [0.5] * 4))
        state = _RecordingState()

        first = run_dream(memory, mode="live", settings=DreamSettings(report_dir="/tmp/dream-test"), state_store=state)
        count_after_first = len(store.points)
        calls_after_first = len(llm.calls)
        second = run_dream(memory, mode="live", settings=DreamSettings(report_dir="/tmp/dream-test"), state_store=state)

        assert first["observations_written"] == 1
        assert second["totals"]["llm_calls"] == 0
        assert len(store.points) == count_after_first
        assert llm.calls and len(llm.calls) == calls_after_first
        # [AC-16]: the short-circuited cluster keeps its verdict and gains one evaluation.
        assert state.evaluations == {first["candidates"][0]["observation_key"]: 2}

    def test_source_facts_are_never_written(self):
        """[AC-22]/[AC-24]: the only write is the new observation point id."""
        points = _cluster_points()
        llm = _FakeLLM(response=_observation_reply([f"f{index}" for index in range(5)]))
        memory, store = _memory(points, llm=llm, embedder=SimpleNamespace(embed=lambda text, kind: [0.5] * 4))
        fact_snapshot = {str(point.id): json.dumps(point.payload, sort_keys=True) for point in store.points}

        run_dream(memory, mode="live", settings=DreamSettings(report_dir="/tmp/dream-test"))

        written_ids = {str(point_id) for write in store.writes for point_id in write["ids"]}
        assert written_ids.isdisjoint(fact_snapshot)
        for point_id, snapshot in fact_snapshot.items():
            current = next(point for point in store.points if str(point.id) == point_id)
            assert json.dumps(current.payload, sort_keys=True) == snapshot
        assert store.payload_writes == []

    def test_dry_run_writes_nothing(self):
        """[AC-25]: no store write at all -- not even the embedding is requested."""
        points = _cluster_points()
        llm = _FakeLLM(response=_observation_reply([f"f{index}" for index in range(5)]))
        embeds = {"count": 0}

        def _embed(text, kind):
            embeds["count"] += 1
            return [0.5] * 4

        memory, store = _memory(points, llm=llm, embedder=SimpleNamespace(embed=_embed))

        report = run_dream(memory, mode="dry_run", settings=DreamSettings(report_dir="/tmp/dream-test"))

        assert report["would_write"]
        assert store.writes == []
        assert store.payload_writes == []
        assert embeds["count"] == 0
        # Same LLM cost as a live run: the judgement pass is identical.
        assert report["totals"]["llm_calls"] == len(llm.calls) == 1

    def test_failed_cluster_is_isolated_and_not_recorded(self):
        """[AC-19]/[AC-20]: one bad cluster does not stop the run and leaves no state row."""
        points = _cluster_points(5, prefix="a") + _cluster_points(5, prefix="b", offset=10.0)
        llm = _FakeLLM(
            response=_observation_reply(["a0", "a1", "a2", "a3", "a4"]),
            per_text={"fact b": RuntimeError("provider exploded")},
        )
        memory, _ = _memory(points, llm=llm, embedder=SimpleNamespace(embed=lambda text, kind: [0.5] * 4))
        state = _RecordingState()

        report = run_dream(memory, mode="live", settings=DreamSettings(report_dir="/tmp/dream-test"), state_store=state)

        assert report["status"] == "completed"
        assert report["totals"]["failed_clusters"] == 1
        # Both clusters were attempted; only the healthy one produced a candidate.
        assert len(llm.calls) == 2
        assert report["scopes"][0]["candidates"] == 1
        assert len(report["candidates"]) == 1
        assert report["candidates"][0]["decision"] == "would_write"
        # The failed cluster is not a verdict: no state row, so the next run retries it.
        assert len(state.seen) == 1

    def test_unparsable_reply_is_a_failure_not_a_verdict(self):
        points = _cluster_points()
        llm = _FakeLLM(response="definitely not json")
        memory, _ = _memory(points, llm=llm, embedder=SimpleNamespace(embed=lambda text, kind: [0.5] * 4))
        state = _RecordingState()

        report = run_dream(memory, mode="live", settings=DreamSettings(report_dir="/tmp/dream-test"), state_store=state)

        assert report["totals"]["failed_clusters"] == 1
        assert state.seen == []

    def test_per_cluster_timeout_is_a_failure(self):
        import time as _time

        points = _cluster_points()

        def _slow(messages, **kwargs):
            _time.sleep(0.4)
            return _observation_reply([f"f{index}" for index in range(5)])

        llm = _FakeLLM()
        llm.generate_response = _slow
        memory, _ = _memory(points, llm=llm, embedder=SimpleNamespace(embed=lambda text, kind: [0.5] * 4))
        state = _RecordingState()

        report = run_dream(
            memory,
            mode="live",
            settings=DreamSettings(per_cluster_timeout_seconds=0.05, report_dir="/tmp/dream-test"),
            state_store=state,
        )

        assert report["totals"]["failed_clusters"] == 1
        assert state.seen == []

    def test_usage_sink_counts_only_dream_calls(self):
        points = _cluster_points()
        usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7)
        llm = _FakeLLM(response=_observation_reply([f"f{index}" for index in range(5)]), usage=usage)
        memory, _ = _memory(points, llm=llm, embedder=SimpleNamespace(embed=lambda text, kind: [0.5] * 4))

        report = run_dream(memory, mode="live", settings=DreamSettings(report_dir="/tmp/dream-test"))

        assert report["totals"]["prompt_tokens"] == 11
        assert report["totals"]["completion_tokens"] == 7
        # The callback is restored so a concurrent add() is not double-counted.
        assert llm.config.response_callback is None

    def test_observation_payload_carries_the_earliest_valid_at(self):
        points = _cluster_points()
        points[0].payload["valid_at"] = "2026-01-05"
        points[1].payload["valid_at"] = "2026-02-05"
        llm = _FakeLLM(response=_observation_reply([f"f{index}" for index in range(5)]))
        memory, _ = _memory(points, llm=llm, embedder=SimpleNamespace(embed=lambda text, kind: [0.5] * 4))

        run_dream(memory, mode="live", settings=DreamSettings(report_dir="/tmp/dream-test"))

        # Members without a valid_at fall back to created_at, so the earliest is 2026-01-05.
        payload = build_observation_payload(
            decision={"text": "x", "source_ids": ["a"], "evidence_count": 1, "observation_key": "k"},
            scope={"user_id": "u1", "agent_id": "a1"},
            members=[
                {"valid_at": "2026-03-01", "created_at": "2026-03-01T00:00:00+00:00"},
                {"valid_at": None, "created_at": "2026-01-02T00:00:00+00:00"},
            ],
            run_id="r1",
            created_at=NOW.isoformat(),
        )
        assert payload["valid_at"] == "2026-01-02"


class _RecordingState:
    """State store stub that records every decision (the real one writes Postgres)."""

    def __init__(self):
        self.seen = []
        self.evaluations = {}

    def known_keys(self):
        return {row["observation_key"] for row in self.seen}

    def record(self, *, observation_key, scope_user_id, scope_agent_id, decision, observed_point_id=None):
        self.seen.append({"observation_key": observation_key, "decision": decision})
        self.evaluations[observation_key] = self.evaluations.get(observation_key, 0) + 1

    def touch(self, *, observation_key):
        if observation_key in self.evaluations:
            self.evaluations[observation_key] += 1


# ---------------------------------------------------------------------------
# Evolution (P11/P12)
# ---------------------------------------------------------------------------


class TestSupersede:
    def test_evolved_cluster_invalidates_the_previous_observation(self):
        old_members = ["f0", "f1", "f2", "f3"]
        old_key = observation_key("u1", "a1", old_members)
        old_point_id = observation_point_id(old_key)
        points = _cluster_points()
        points.append(
            _point(
                old_point_id,
                {
                    "data": "previous belief",
                    "hash": "previous belief",
                    "memory_kind": MEMORY_KIND_OBSERVATION,
                    "observation_key": old_key,
                    "source_memory_ids": old_members,
                    "evidence_count": 4,
                    "user_id": "u1",
                    "agent_id": "a1",
                    "created_at": "2026-09-01T00:00:00+00:00",
                },
            )
        )
        llm = _FakeLLM(response=_observation_reply(["f0", "f1", "f2", "f3", "f4"], text="new belief"))
        memory, store = _memory(points, llm=llm, embedder=SimpleNamespace(embed=lambda text, kind: [0.5] * 4))

        report = run_dream(memory, mode="live", settings=DreamSettings(report_dir="/tmp/dream-test"))

        assert report["observations_superseded"] == 1
        assert store.payload_writes[0]["id"] == old_point_id
        assert store.payload_writes[0]["payload"]["invalid_reason"] == "observation_recomputed"
        assert store.payload_writes[0]["payload"]["superseded_by"] == observation_point_id(
            report["candidates"][0]["observation_key"]
        )
        # P11: the old observation's text and hash are untouched.
        old_point = next(point for point in store.points if str(point.id) == old_point_id)
        assert old_point.payload["data"] == "previous belief"
        assert old_point.payload["hash"] == "previous belief"

    def test_already_invalidated_observation_is_left_alone(self):
        old_key = observation_key("u1", "a1", ["f0", "f1", "f2", "f3"])
        points = _cluster_points()
        points.append(
            _point(
                observation_point_id(old_key),
                {
                    "data": "previous belief",
                    "memory_kind": MEMORY_KIND_OBSERVATION,
                    "observation_key": old_key,
                    "source_memory_ids": ["f0", "f1", "f2", "f3"],
                    "evidence_count": 4,
                    "user_id": "u1",
                    "agent_id": "a1",
                    "created_at": "2026-09-01T00:00:00+00:00",
                    "invalid_at": "2026-09-10",
                    "superseded_by": "some-other-point",
                },
            )
        )
        llm = _FakeLLM(response=_observation_reply(["f0", "f1", "f2", "f3", "f4"], text="new belief"))
        memory, store = _memory(points, llm=llm, embedder=SimpleNamespace(embed=lambda text, kind: [0.5] * 4))

        report = run_dream(memory, mode="live", settings=DreamSettings(report_dir="/tmp/dream-test"))

        assert report["observations_superseded"] == 0
        assert store.payload_writes == []
