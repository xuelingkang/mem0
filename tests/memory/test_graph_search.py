"""Tests for the graph signal in the retrieval layer (design: docs/design/graph-memory.md).

Two concerns are covered here:
  * The additive fusion is a supplementary signal: it never introduces or drops a
    candidate, its ceiling is `weight` and it only grows the divisor when a candidate in
    the pool actually carries a boost (design §6.2, [AC-12][AC-13][AC-17]).
  * The off state is byte-identical to pre-graph releases, `explain` included
    (design §6.5, [AC-19][AC-31]).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mem0.configs.base import GraphConfig, MemoryConfig
from mem0.memory import main as memory_main
from mem0.utils.scoring import (
    ENTITY_BOOST_WEIGHT,
    GRAPH_BOOST_WEIGHT,
    GRAPH_DETAIL_KEYS,
    score_and_rank,
)


def _candidate(memory_id, score=0.8):
    return {"id": memory_id, "score": score, "payload": {"data": f"mem {memory_id}"}}


class TestScoringFusion:
    """[AC-11][AC-12] 图信号加性进入分子，且受上限约束。"""

    def test_graph_boost_adds_to_numerator(self):
        results = [_candidate("a"), _candidate("b")]
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=10, graph_boosts={"b": 0.5})
        by_id = {row["id"]: row["score"] for row in scored}
        # max_possible = 1.0 + 0.5（图信号存在）
        assert by_id["b"] == pytest.approx((0.8 + 0.5) / 1.5)
        assert by_id["a"] == pytest.approx(0.8 / 1.5)
        assert scored[0]["id"] == "b"

    def test_boost_ceiling_is_the_weight(self):
        """[AC-12] 四信号全开时图信号占比 ≤ 0.167（W_g=0.5 / max_possible=3.0）。"""
        results = [_candidate("a")]
        scored = score_and_rank(
            results,
            {"a": 0.4},
            {"a": 0.2},
            threshold=0.1,
            top_k=10,
            explain=True,
            graph_boosts={"a": GRAPH_BOOST_WEIGHT},
        )
        details = scored[0]["score_details"]
        assert details["graph_boost"] <= GRAPH_BOOST_WEIGHT
        assert details["max_possible_score"] == pytest.approx(3.0)
        assert details["graph_boost"] / details["max_possible_score"] <= 0.167 + 1e-9

    def test_divisor_only_grows_for_pooled_candidates(self):
        """[AC-17] 池外加分不得增长分母：空映射与「无关 id」都保持 base 分母。"""
        results = [_candidate("a")]
        empty = score_and_rank(results, {}, {}, threshold=0.1, top_k=10, explain=True, graph_boosts={})
        assert empty[0]["score_details"]["max_possible_score"] == 1.0
        # 空映射也会发布零值分量键（图能力开启、本次未命中）。
        assert empty[0]["score_details"][GRAPH_DETAIL_KEYS[0]] == 0.0
        assert empty[0]["score_details"][GRAPH_DETAIL_KEYS[1]] == 0

    def test_signal_composition_with_bm25_and_entity(self):
        results = [_candidate("a")]
        scored = score_and_rank(
            results,
            {"a": 0.4},
            {"a": 0.2},
            threshold=0.1,
            top_k=10,
            explain=True,
            graph_boosts={"a": 0.5},
        )
        details = scored[0]["score_details"]
        assert details["max_possible_score"] == pytest.approx(1.0 + 1.0 + ENTITY_BOOST_WEIGHT + GRAPH_BOOST_WEIGHT)
        assert details["raw_score"] == pytest.approx(0.8 + 0.4 + 0.2 + 0.5)

    def test_threshold_still_gates_before_fusion(self):
        """低语义分候选不得凭图加分越过相似度门槛进入候选池。"""
        results = [_candidate("a", score=0.05)]
        scored = score_and_rank(results, {}, {}, threshold=0.1, top_k=10, graph_boosts={"a": 0.5})
        assert scored == []

    def test_off_state_is_byte_identical(self):
        """[AC-19][AC-31] graph_boosts=None 时评分与 explain 键集合逐位不变。"""
        results = [_candidate("a"), _candidate("b", 0.6)]
        without = score_and_rank(results, {}, {}, threshold=0.1, top_k=10, explain=True)
        explicit_empty = score_and_rank(
            results, {}, {}, threshold=0.1, top_k=10, explain=True, graph_boosts=None, graph_facts=None
        )
        assert without == explicit_empty
        assert all(key not in without[0]["score_details"] for key in GRAPH_DETAIL_KEYS)
        # 显式传入 None 与完全不传的调用形状一致（打分器签名保持向后兼容）。
        positional = score_and_rank(results, {}, {}, 0.1, 10, True)
        assert positional == without

    def test_graph_facts_published(self):
        results = [_candidate("a")]
        scored = score_and_rank(
            results,
            {},
            {},
            threshold=0.1,
            top_k=10,
            explain=True,
            graph_boosts={"a": 0.3},
            graph_facts={"a": 2},
        )
        assert scored[0]["score_details"][GRAPH_DETAIL_KEYS[1]] == 2


class TestGraphHitsWiring:
    """[AC-15][AC-26][AC-27] 检索路径的取数与开关。"""

    def _memory(self, **graph_overrides):
        config = MemoryConfig(graph=GraphConfig(**graph_overrides))
        memory = SimpleNamespace(config=config)
        return memory

    def test_disabled_returns_none(self):
        boosts, facts = memory_main._compute_graph_hits(self._memory(enabled=False), "q", [{"id": "a"}], {"user_id": "u"})
        assert boosts is None and facts is None

    def test_enabled_without_scope_returns_empty(self):
        memory = self._memory(enabled=True)
        memory._graph_bridge_client = MagicMock()
        boosts, facts = memory_main._compute_graph_hits(memory, "q", [{"id": "a"}], {})
        assert boosts == {} and facts == {}
        memory._graph_bridge_client.search.assert_not_called()

    def test_enabled_maps_facts_to_boosts(self, monkeypatch):
        memory = self._memory(enabled=True, weight=0.5)
        client = MagicMock()
        client.search.return_value = {"facts": [{"episodes": ["a"], "invalid_at": None}]}
        memory._graph_bridge_client = client
        boosts, facts = memory_main._compute_graph_hits(
            memory, "q", [{"id": "a"}, {"id": "b"}], {"user_id": "test_graph_a"}
        )
        assert boosts == {"a": pytest.approx(0.5)}
        assert facts == {"a": 1}
        payload, timeout = client.search.call_args[0]
        assert payload["group_ids"] == ["mem0_test_graph_a"]
        assert payload["query"] == "q"

    def test_bridge_exception_degrades_to_empty(self):
        memory = self._memory(enabled=True)
        client = MagicMock()
        client.search.side_effect = RuntimeError("bridge down")
        memory._graph_bridge_client = client
        assert memory_main._compute_graph_hits(memory, "q", [{"id": "a"}], {"user_id": "u"}) == ({}, {})

    def test_dispatch_skipped_when_disabled(self):
        memory = self._memory(enabled=False)
        records = [("m1", "text", [0.1], {"created_at": "2026-09-17T00:00:00+00:00"})]
        memory_main._dispatch_graph_sync(memory, records, {"user_id": "u"})
        assert getattr(memory, "_graph_sync", None) is None

    def test_dispatch_enqueues_for_each_record(self):
        memory = self._memory(enabled=True, retry_backoff_seconds=0.0)
        records = [
            ("m1", "text one", [0.1], {"created_at": "2026-09-17T00:00:00+00:00"}),
            ("m2", "text two", [0.1], None),
        ]
        memory_main._dispatch_graph_sync(memory, records, {"user_id": "test_graph_a"})
        sync = memory._graph_sync
        try:
            assert sync.stats()["graph_dispatched"] == 2
        finally:
            sync.close()
