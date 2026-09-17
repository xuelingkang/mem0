"""Tests for memory decay (design: docs/design/memory-decay.md).

Three concerns are covered here:
  * The time factor is a pure, UTC-based function of the record's own footprint,
    with the parameters of design §3 ([AC-1]-[AC-5]).
  * The ranking layer multiplies it into the hybrid score without ever filtering a
    candidate out, and publishes the five components under `explain` (design §5.1-5.3).
  * The reinforcement write follows the four write-amplification controls: only the
    returned hits, one batched request, a per-memory cooldown, and an asynchronous
    dispatch whose failure cannot change the response ([AC-15]-[AC-20]).
"""

import asyncio
import inspect
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mem0.configs.base import DecayConfig, MemoryConfig
from mem0.memory import main as memory_main
from mem0.memory.main import (
    AsyncMemory,
    Memory,
    _coerce_access_count,
    _decay_factors_for_candidates,
    _decay_retention,
    _decay_weight,
    _parse_decay_timestamp,
    _reinforce_search_hits,
)
from mem0.utils.scoring import score_and_rank

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


DEFAULT_DECAY = DecayConfig()


def _candidate(memory_id: str, payload: dict) -> dict:
    return {"id": memory_id, "score": 0.5, "payload": payload}


class TestRetentionFunction:
    """[AC-1] R = exp(-Δt/S)，S = S₀ + ΔS·min(n, N_cap)。"""

    def test_matches_exp_of_default_halflife(self):
        for elapsed in (0.0, 1.0, 7.0, 16.91, 100.0):
            expected = math.exp(-elapsed / 12)
            assert _decay_retention(
                elapsed, 0, halflife_days=12, strength_step_days=3, access_cap=20
            ) == pytest.approx(expected, abs=1e-9)

    def test_one_access_uses_strength_15(self):
        for elapsed in (0.0, 3.5, 30.0):
            expected = math.exp(-elapsed / 15)
            assert _decay_retention(
                elapsed, 1, halflife_days=12, strength_step_days=3, access_cap=20
            ) == pytest.approx(expected, abs=1e-9)

    def test_access_count_above_cap_behaves_like_cap(self):
        for elapsed in (0.0, 5.0, 90.0):
            at_cap = _decay_retention(
                elapsed, 20, halflife_days=12, strength_step_days=3, access_cap=20
            )
            for over in (21, 25, 1000):
                assert _decay_retention(
                    elapsed, over, halflife_days=12, strength_step_days=3, access_cap=20
                ) == pytest.approx(at_cap, abs=1e-12)

    def test_negative_elapsed_is_neutral(self):
        """时钟回拨不得让 R 超过 1（否则时间因子会变成放大）。"""
        assert _decay_retention(-10.0, 0, halflife_days=12, strength_step_days=3, access_cap=20) == 1.0

    def test_strength_growth_halves_slower(self):
        """半衰期随召回次数从 8.3 天延展（设计 §3.3 依据 C）。"""
        decayed_n0 = _decay_retention(8.3178, 0, halflife_days=12, strength_step_days=3, access_cap=20)
        decayed_n20 = _decay_retention(49.906, 20, halflife_days=12, strength_step_days=3, access_cap=20)
        assert decayed_n0 == pytest.approx(0.5, abs=1e-4)
        assert decayed_n20 == pytest.approx(0.5, abs=1e-4)


class TestWeightBounds:
    """[AC-1]/[AC-2] 时间因子落在 [floor, 1]，且只下调。"""

    def test_zero_elapsed_is_one(self):
        assert _decay_weight(1.0, 0.90) == 1.0

    def test_infinite_elapsed_hits_the_floor(self):
        assert _decay_weight(0.0, 0.90) == pytest.approx(0.90)
        assert _decay_weight(_decay_retention(10000.0, 0, halflife_days=12, strength_step_days=3, access_cap=20), 0.90) >= 0.90

    def test_weight_never_amplifies(self):
        for retention in (0.0, 0.25, 0.5, 0.999, 1.0):
            assert _decay_weight(retention, 0.90) <= 1.0

    def test_weight_floor_follows_configured_floor(self):
        assert _decay_weight(0.0, 0.75) == pytest.approx(0.75)


class TestFactorComputation:
    """[AC-1]-[AC-5] 批量因子计算：起点、UTC 基准、参数覆盖。"""

    def test_footprint_wins_over_created_at(self):
        payload = {"created_at": _iso(100), "last_accessed": _iso(1), "access_count": 0}
        factors = _decay_factors_for_candidates([_candidate("a", payload)], DEFAULT_DECAY, NOW)
        assert factors["a"]["elapsed_days"] == pytest.approx(1.0, abs=1e-9)
        assert factors["a"]["retention"] == pytest.approx(math.exp(-1 / 12), abs=1e-9)

    def test_missing_footprint_falls_back_to_created_at(self):
        factors = _decay_factors_for_candidates(
            [_candidate("a", {"created_at": _iso(16.91)})], DEFAULT_DECAY, NOW
        )
        assert factors["a"]["elapsed_days"] == pytest.approx(16.91, abs=1e-9)
        # 设计 §3.3 依据 B：中位龄处的时间因子不得低于 0.92。
        assert factors["a"]["decay_weight"] == pytest.approx(0.9245, abs=1e-4)
        assert factors["a"]["access_count"] == 0
        assert factors["a"]["memory_strength_days"] == 12.0

    def test_no_time_information_is_neutral(self):
        factors = _decay_factors_for_candidates([_candidate("a", {"data": "x"})], DEFAULT_DECAY, NOW)
        assert factors["a"]["elapsed_days"] == 0.0
        assert factors["a"]["decay_weight"] == 1.0

    def test_access_count_grows_strength_and_weight(self):
        fresh_low = _candidate("low", {"created_at": _iso(30), "access_count": 0})
        fresh_high = _candidate("high", {"created_at": _iso(30), "access_count": 20})
        factors = _decay_factors_for_candidates([fresh_low, fresh_high], DEFAULT_DECAY, NOW)
        assert factors["high"]["memory_strength_days"] == 72.0
        assert factors["low"]["memory_strength_days"] == 12.0
        assert factors["high"]["decay_weight"] > factors["low"]["decay_weight"]
        # ρ = 1/floor：两条同龄记录的因子比不得超过它。
        assert factors["high"]["decay_weight"] / factors["low"]["decay_weight"] < 1 / DEFAULT_DECAY.floor

    @pytest.mark.parametrize(
        "left,right",
        [
            ("2026-09-16T20:00:00+08:00", "2026-09-16T12:00:00Z"),
            ("2026-09-16T12:00:00+00:00", "2026-09-16T12:00:00Z"),
        ],
    )
    def test_equivalent_instants_across_timezones(self, left, right):
        """[AC-4] 同一瞬间的不同时区写法得出相同 elapsed_days。"""
        factors = _decay_factors_for_candidates(
            [_candidate("l", {"last_accessed": left}), _candidate("r", {"last_accessed": right})],
            DEFAULT_DECAY,
            NOW,
        )
        assert factors["l"]["elapsed_days"] == pytest.approx(factors["r"]["elapsed_days"], abs=1e-6)

    def test_naive_timestamp_is_read_as_utc(self):
        factors = _decay_factors_for_candidates(
            [_candidate("a", {"last_accessed": "2026-09-16T12:00:00"})], DEFAULT_DECAY, NOW
        )
        assert factors["a"]["elapsed_days"] == pytest.approx(1.0, abs=1e-6)

    def test_unparsable_footprint_degrades_to_created_at(self):
        payload = {"last_accessed": "not-a-date", "created_at": _iso(2)}
        factors = _decay_factors_for_candidates([_candidate("a", payload)], DEFAULT_DECAY, NOW)
        assert factors["a"]["elapsed_days"] == pytest.approx(2.0, abs=1e-9)

    def test_custom_parameters_change_the_weight(self):
        """[AC-5] 配置覆盖生效：非默认参数改变 factor，floor 改变下界。"""
        customized = DecayConfig(halflife_days=2, strength_step_days=1, access_cap=5, floor=0.75)
        payload = {"created_at": _iso(4), "access_count": 5}
        default_factors = _decay_factors_for_candidates([_candidate("a", payload)], DEFAULT_DECAY, NOW)
        custom_factors = _decay_factors_for_candidates([_candidate("a", payload)], customized, NOW)
        assert custom_factors["a"]["memory_strength_days"] == 7.0  # 2 + 1*min(5,5)
        assert custom_factors["a"]["retention"] == pytest.approx(math.exp(-4 / 7), abs=1e-9)
        assert custom_factors["a"]["decay_weight"] == pytest.approx(0.75 + 0.25 * math.exp(-4 / 7), abs=1e-12)
        assert custom_factors["a"]["decay_weight"] < default_factors["a"]["decay_weight"]
        assert custom_factors["a"]["decay_weight"] >= 0.75

    def test_cap_bounds_the_strength_component(self):
        customized = DecayConfig(access_cap=3)
        factors = _decay_factors_for_candidates(
            [_candidate("a", {"created_at": _iso(1), "access_count": 9})], customized, NOW
        )
        assert factors["a"]["memory_strength_days"] == 12.0 + 3 * 3


class TestPureFunctionContract:
    """[AC-3] 纯函数：无 LLM / 网络 / 写操作，同入参同输出。"""

    @pytest.mark.parametrize("func", [_decay_retention, _decay_weight, _decay_factors_for_candidates])
    def test_signature_has_no_io_dependency(self, func):
        params = set(inspect.signature(func).parameters)
        assert not params & {"llm", "client", "vector_store", "db", "session", "memory", "self"}

    def test_repeated_calls_are_identical(self):
        candidates = [_candidate("a", {"created_at": _iso(3), "access_count": 2})]
        first = _decay_factors_for_candidates(candidates, DEFAULT_DECAY, NOW)
        second = _decay_factors_for_candidates(candidates, DEFAULT_DECAY, NOW)
        assert first == second

    def test_computation_performs_no_write(self, monkeypatch):
        def _fail(*args, **kwargs):  # pragma: no cover - only hit on regression
            raise AssertionError("decay computation must not write")

        monkeypatch.setattr(memory_main, "_dispatch_decay_reinforcement", _fail)
        _decay_factors_for_candidates([_candidate("a", {"created_at": _iso(3)})], DEFAULT_DECAY, NOW)


class TestScoringFusion:
    """[AC-6]/[AC-10]/[AC-11] 融合：乘进混合分，只重排不淘汰。"""

    def _results(self):
        return [
            {"id": "old", "score": 0.8, "payload": {"data": "old"}},
            {"id": "new", "score": 0.79, "payload": {"data": "new"}},
        ]

    def test_absent_factors_keep_the_original_score(self):
        scored = score_and_rank(self._results(), {}, {}, threshold=0.1, top_k=10, explain=True)
        assert [r["id"] for r in scored] == ["old", "new"]
        assert scored[0]["score"] == pytest.approx(0.8)
        assert "decay_weight" not in scored[0]["score_details"]

    def test_factor_multiplies_the_hybrid_score(self):
        factors = {"old": {"decay_weight": 0.9}, "new": {"decay_weight": 1.0}}
        scored = score_and_rank(self._results(), {}, {}, threshold=0.1, top_k=10, decay_factors=factors)
        assert [r["id"] for r in scored] == ["new", "old"]
        assert scored[0]["score"] == pytest.approx(0.79)
        assert scored[1]["score"] == pytest.approx(0.72)

    def test_ratio_at_rho_cannot_be_flipped(self):
        """相似度比 ≥ 1/floor 的候选对，时间因子无法翻转其次序（设计 §5.2 硬不变量）。"""
        results = [
            {"id": "high", "score": 0.90, "payload": {}},
            {"id": "low", "score": 0.81, "payload": {}},
        ]
        factors = {"high": {"decay_weight": DEFAULT_DECAY.floor}, "low": {"decay_weight": 1.0}}
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=10, decay_factors=factors)
        assert [r["id"] for r in scored] == ["high", "low"]

    def test_below_rho_can_be_flipped(self):
        results = [
            {"id": "high", "score": 0.90, "payload": {}},
            {"id": "low", "score": 0.89, "payload": {}},
        ]
        factors = {"high": {"decay_weight": DEFAULT_DECAY.floor}, "low": {"decay_weight": 1.0}}
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=10, decay_factors=factors)
        assert [r["id"] for r in scored] == ["low", "high"]

    def test_factor_never_raises_a_score(self):
        results = [{"id": f"m{i}", "score": 0.5 + i / 100, "payload": {}} for i in range(5)]
        plain = score_and_rank(results, {}, {}, threshold=0.1, top_k=10)
        factors = {
            r["id"]: {"decay_weight": _decay_weight(math.exp(-i), 0.9)} for i, r in enumerate(results)
        }
        decayed = score_and_rank(results, {}, {}, threshold=0.1, top_k=10, decay_factors=factors)
        plain_by_id = {r["id"]: r["score"] for r in plain}
        for result in decayed:
            original = plain_by_id[result["id"]]
            assert result["score"] <= original + 1e-12
            assert result["score"] / original >= 0.9 - 1e-12

    def test_threshold_still_gates_the_semantic_score(self):
        results = [
            {"id": "below", "score": 0.05, "payload": {}},
            {"id": "above", "score": 0.5, "payload": {}},
        ]
        factors = {"below": {"decay_weight": 1.0}, "above": {"decay_weight": 0.9}}
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=10, decay_factors=factors)
        assert [r["id"] for r in scored] == ["above"]

    def test_explain_publishes_the_five_components(self):
        """[AC-12] explain=true 时 score_details 含五项时间因子分量。"""
        factors = {
            "old": {
                "decay_weight": 0.91,
                "retention": 0.1,
                "memory_strength_days": 12.0,
                "elapsed_days": 27.6,
                "access_count": 0,
            },
            "new": {"decay_weight": 1.0, "retention": 1.0, "memory_strength_days": 12.0, "elapsed_days": 0.0, "access_count": 0},
        }
        scored = score_and_rank(
            self._results(), {}, {}, threshold=0.1, top_k=10, explain=True, decay_factors=factors
        )
        by_id = {r["id"]: r["score_details"] for r in scored}
        assert set(memory_main.DECAY_DETAIL_KEYS) <= set(by_id["new"])
        assert by_id["old"]["decay_weight"] == 0.91
        assert by_id["old"]["final_score"] == pytest.approx(0.8 * 0.91)


def _memory_with_decay(enabled=True, **decay_kwargs):
    """Minimal Memory stand-in carrying only what the reinforcement path touches."""
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(decay=DecayConfig(enabled=enabled, **decay_kwargs))
    memory.vector_store = MagicMock()
    memory._decay_write_at = {}
    memory._decay_write_lock = memory_main.threading.Lock()
    return memory


def _hits(*ids):
    return [{"id": memory_id, "memory": memory_id, "access_count": 0} for memory_id in ids]


class TestReinforcementWrite:
    """[AC-14]-[AC-18] 足迹写入与写放大控制。"""

    def test_disabled_writes_nothing(self):
        """[AC-26] 关闭时不产生任何写入，也不登记冷却表。"""
        memory = _memory_with_decay(enabled=False)
        _reinforce_search_hits(memory, _hits("a", "b"))
        assert memory.vector_store.update_payload_batch.call_count == 0
        assert memory._decay_write_at == {}

    def test_enabled_writes_one_batch_for_all_hits(self):
        """[AC-18] 一次检索的 N 条入选记忆只触发一次批量写请求。"""
        memory = _memory_with_decay()
        _reinforce_search_hits(memory, _hits("a", "b", "c"))
        assert memory.vector_store.update_payload_batch.call_count == 1
        updates = memory.vector_store.update_payload_batch.call_args.args[0]
        assert set(updates) == {"a", "b", "c"}
        assert all(patch["access_count"] == 1 for patch in updates.values())
        assert all("last_accessed" in patch for patch in updates.values())

    def test_write_is_payload_only(self):
        """[AC-17] 只写两个足迹键，不携带向量或其它字段。"""
        memory = _memory_with_decay()
        _reinforce_search_hits(memory, _hits("a"))
        payload = memory.vector_store.update_payload_batch.call_args.args[0]["a"]
        assert set(payload) == {"last_accessed", "access_count"}

    def test_access_count_accumulates_from_the_current_value(self):
        memory = _memory_with_decay()
        _reinforce_search_hits(memory, [{"id": "a", "access_count": 7}])
        assert memory.vector_store.update_payload_batch.call_args.args[0]["a"]["access_count"] == 8

    def test_cooldown_skips_repeat_hits(self):
        """[AC-15] 冷却窗口内同一记忆只写一次。"""
        memory = _memory_with_decay()
        _reinforce_search_hits(memory, _hits("a"))
        _reinforce_search_hits(memory, _hits("a"))
        assert memory.vector_store.update_payload_batch.call_count == 1

    def test_zero_cooldown_writes_every_time(self):
        memory = _memory_with_decay(cooldown_seconds=0)
        _reinforce_search_hits(memory, _hits("a"))
        _reinforce_search_hits(memory, _hits("a"))
        assert memory.vector_store.update_payload_batch.call_count == 2

    def test_cooldown_table_does_not_grow_without_bound(self):
        memory = _memory_with_decay(cooldown_seconds=0)
        for i in range(5):
            _reinforce_search_hits(memory, [{"id": f"m{i}"}])
        assert memory._decay_write_at == {}

    def test_instance_without_decay_config_is_a_no_op(self):
        """实例未携带 decay 配置段时不得产生副作用（本机制对检索路径严格增量）。"""
        memory = Memory.__new__(Memory)  # 无 config 属性
        _reinforce_search_hits(memory, _hits("a"))

        memory_with_partial_config = Memory.__new__(Memory)
        memory_with_partial_config.config = SimpleNamespace(llm=SimpleNamespace(config={}))
        _reinforce_search_hits(memory_with_partial_config, _hits("a"))

    def test_mock_config_is_not_read_as_enabled(self):
        """替身式配置（MagicMock）不得被当作「已开启」，属性值也不得流进算式。"""
        memory = Memory.__new__(Memory)
        memory.config = MagicMock()
        _reinforce_search_hits(memory, _hits("a"))  # 不得抛错、不得触碰 vector_store
        assert memory._decay_factors([_candidate("a", {"created_at": _iso(1)})]) is None

    def test_entries_without_id_are_ignored(self):
        memory = _memory_with_decay()
        _reinforce_search_hits(memory, [{"memory": "no id"}, {"id": "a"}])
        assert set(memory.vector_store.update_payload_batch.call_args.args[0]) == {"a"}

    def test_nothing_returned_means_nothing_written(self):
        memory = _memory_with_decay()
        _reinforce_search_hits(memory, [])
        assert memory.vector_store.update_payload_batch.call_count == 0

    def test_access_count_coercion(self):
        assert _coerce_access_count(None) == 0
        assert _coerce_access_count("3") == 3
        assert _coerce_access_count(-4) == 0
        assert _coerce_access_count("abc") == 0
        assert _coerce_access_count(True) == 0


class TestSearchIntegration:
    """检索路径：因子注入打分器、只写入选条目、写入失败不影响结果。"""

    def _search_memory(self, enabled=True):
        memory = Memory.__new__(Memory)
        memory.config = SimpleNamespace(decay=DecayConfig(enabled=enabled))
        memory.vector_store = MagicMock()
        memory.reranker = None
        memory._decay_write_at = {}
        memory._decay_write_lock = memory_main.threading.Lock()
        memory._has_advanced_operators = MagicMock(return_value=False)
        memory._compute_entity_boosts = MagicMock(return_value={})
        memory.embedding_model = MagicMock()
        memory.embedding_model.embed.return_value = [0.1, 0.2]
        memory.vector_store.keyword_search.return_value = None
        return memory

    def _payload_memory(self, memory_id, created_at, access_count=None):
        """Fake vector-store row; the stale one carries the marginal semantic edge."""
        payload = {"data": f"memory {memory_id}", "created_at": created_at, "user_id": "u1"}
        if access_count is not None:
            payload["access_count"] = access_count
        row = MagicMock()
        row.id = memory_id
        row.payload = payload
        row.score = 0.79 if memory_id == "fresh" else 0.80
        return row

    def test_decay_reorders_and_only_returned_entries_are_written(self, monkeypatch):
        monkeypatch.setattr(memory_main, "capture_event", MagicMock())
        monkeypatch.setattr(memory_main, "extract_entities", lambda q: [])
        memory = self._search_memory()
        rows = [
            self._payload_memory("stale", _iso(40)),
            self._payload_memory("fresh", _iso(0)),
        ]
        memory.vector_store.search.return_value = rows

        results = memory._search_vector_store("q", {"user_id": "u1"}, 1)

        assert [r["id"] for r in results] == ["fresh"]
        _reinforce_search_hits(memory, results)
        assert memory.vector_store.update_payload_batch.call_count == 1
        assert set(memory.vector_store.update_payload_batch.call_args.args[0]) == {"fresh"}

    def test_disabled_search_keeps_the_plain_order(self, monkeypatch):
        monkeypatch.setattr(memory_main, "capture_event", MagicMock())
        monkeypatch.setattr(memory_main, "extract_entities", lambda q: [])
        memory = self._search_memory(enabled=False)
        memory.vector_store.search.return_value = [
            self._payload_memory("stale", _iso(40)),
            self._payload_memory("fresh", _iso(0)),
        ]

        results = memory._search_vector_store("q", {"user_id": "u1"}, 2)

        assert [r["id"] for r in results] == ["stale", "fresh"]
        assert all("score_details" not in r for r in results)

    def test_search_explains_the_time_factor(self, monkeypatch):
        monkeypatch.setattr(memory_main, "capture_event", MagicMock())
        monkeypatch.setattr(memory_main, "extract_entities", lambda q: [])
        memory = self._search_memory()
        memory.vector_store.search.return_value = [self._payload_memory("fresh", _iso(0))]

        results = memory._search_vector_store("q", {"user_id": "u1"}, 2, explain=True)

        details = results[0]["score_details"]
        assert set(memory_main.DECAY_DETAIL_KEYS) <= set(details)
        assert details["decay_weight"] == 1.0

    def test_write_failure_does_not_change_the_response(self, monkeypatch):
        """[AC-20] 故障注入：强化写入抛错时检索结果照常返回。"""
        monkeypatch.setattr(memory_main, "capture_event", MagicMock())
        monkeypatch.setattr(memory_main, "extract_entities", lambda q: [])
        memory = self._search_memory()
        memory.vector_store.search.return_value = [self._payload_memory("fresh", _iso(0))]
        memory.vector_store.update_payload_batch.side_effect = RuntimeError("qdrant down")

        class _ImmediateThread:
            """Inline the background write so the failure surfaces inside the test."""

            def __init__(self, target, **kwargs):
                self._target = target

            def start(self):
                self._target()

        monkeypatch.setattr(memory_main.threading, "Thread", lambda target, **kwargs: _ImmediateThread(target))
        results = memory._search_vector_store("q", {"user_id": "u1"}, 2)
        _reinforce_search_hits(memory, results)  # 不得抛错

        assert [r["id"] for r in results] == ["fresh"]
        assert memory.vector_store.update_payload_batch.call_count == 1

    def test_search_dispatch_runs_in_a_background_thread(self, monkeypatch):
        """[AC-18]/设计 §4.3：写入不进入响应时延路径。"""
        monkeypatch.setattr(memory_main, "capture_event", MagicMock())
        monkeypatch.setattr(memory_main, "extract_entities", lambda q: [])
        memory = self._search_memory()
        memory.vector_store.search.return_value = [self._payload_memory("fresh", _iso(0))]
        dispatch = MagicMock()
        monkeypatch.setattr(memory_main, "_dispatch_decay_reinforcement", dispatch)

        results = memory._search_vector_store("q", {"user_id": "u1"}, 2)

        # `_search_vector_store` 自身不写入；写入由 `search()` 在拿到最终列表后派发，
        # 而派发本身把写入交给后台线程（`_dispatch_decay_reinforcement`）。
        assert dispatch.call_count == 0
        assert len(results) == 1

    def test_dispatch_uses_a_daemon_thread(self, monkeypatch):
        started = {}

        class _RecordingThread:
            def __init__(self, target, name=None, daemon=None):
                started["target"] = target
                started["name"] = name
                started["daemon"] = daemon

            def start(self):
                started["started"] = True

        monkeypatch.setattr(memory_main.threading, "Thread", _RecordingThread)
        vector_store = MagicMock()
        memory_main._dispatch_decay_reinforcement(vector_store, {"a": {"access_count": 1}})

        assert started["daemon"] is True and started["started"] is True
        started["target"]()
        vector_store.update_payload_batch.assert_called_once()


@pytest.mark.asyncio
async def test_async_search_reinforces_hits(monkeypatch):
    """异步面：`AsyncMemory.search` 同样在返回前派发强化，且只写入选条目。"""
    monkeypatch.setattr(memory_main, "capture_event", MagicMock())
    monkeypatch.setattr(memory_main, "extract_entities", lambda q: [])
    memory = AsyncMemory.__new__(AsyncMemory)
    memory.config = SimpleNamespace(decay=DecayConfig(enabled=True), llm=SimpleNamespace(config={}))
    memory.api_version = "v1.1"
    memory.reranker = None
    memory.vector_store = MagicMock()
    memory.vector_store.update_payload_batch = MagicMock()
    memory._decay_write_at = {}
    memory._decay_write_lock = memory_main.threading.Lock()
    memory._has_advanced_operators = MagicMock(return_value=False)
    memory._compute_entity_boosts_async = AsyncMock(return_value={})
    memory.embedding_model = MagicMock()
    memory.embedding_model.embed.return_value = [0.1, 0.2]
    memory.vector_store.keyword_search = MagicMock(return_value=None)
    row = MagicMock()
    row.id = "hit"
    row.payload = {"data": "memory hit", "created_at": _iso(0), "user_id": "u1"}
    row.score = 0.8
    memory.vector_store.search = MagicMock(return_value=[row])

    result = await memory.search("q", filters={"user_id": "u1"}, top_k=2)

    assert [r["id"] for r in result["results"]] == ["hit"]
    for _ in range(50):
        if memory.vector_store.update_payload_batch.call_count:
            break
        await asyncio.sleep(0.01)
    assert memory.vector_store.update_payload_batch.call_count == 1
    assert set(memory.vector_store.update_payload_batch.call_args.args[0]) == {"hit"}


class TestConfigWiring:
    def test_memory_config_declares_the_decay_section(self):
        """[AC-5]/E7：配置段必须显式声明，否则被静默忽略。"""
        config = MemoryConfig(**{"decay": {"enabled": True, "floor": 0.8, "cooldown_seconds": 0}})
        assert config.decay.enabled is True
        assert config.decay.floor == 0.8
        assert "decay" in MemoryConfig().model_dump()

    def test_defaults_match_the_design(self):
        decay = MemoryConfig().decay
        assert (decay.enabled, decay.halflife_days, decay.strength_step_days) == (False, 12.0, 3.0)
        assert (decay.access_cap, decay.floor, decay.cooldown_seconds) == (20, 0.90, 300.0)

    def test_decay_config_rejects_out_of_range_values(self):
        for bad in ({"floor": 0}, {"floor": 1.5}, {"halflife_days": 0}, {"cooldown_seconds": -1}):
            with pytest.raises(Exception):
                DecayConfig(**bad)

    def test_footprint_keys_are_first_class_read_fields(self):
        """[AC-29] 两个足迹字段是可提升的一等字段，不得落进 metadata。"""
        assert set(memory_main.DECAY_PAYLOAD_KEYS) <= set(memory_main.BI_TEMPORAL_PAYLOAD_KEYS)

    def test_parse_timestamp_accepts_z_suffix_and_rejects_garbage(self):
        assert _parse_decay_timestamp("2026-09-17T00:00:00Z") == datetime(2026, 9, 17, tzinfo=timezone.utc)
        assert _parse_decay_timestamp("") is None
        assert _parse_decay_timestamp(None) is None
        assert _parse_decay_timestamp("yesterday") is None
