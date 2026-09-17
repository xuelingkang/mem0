"""Tests for the bi-temporal fact model (design: docs/design/2026-09-17-bitemporal-fact-model-design.md).

Three concerns are covered here:
  * 1b is a second, independent LLM call whose prompt shares nothing with 1a ([AC-3]).
  * 1c ``_apply_contradictions`` is deterministic pure code ([AC-4], rules R1-R11).
  * A failing 1b never breaks the write ([AC-5]) and invalidation stays payload-only (R9).
"""

import inspect
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from mem0.configs.prompts import (
    ADDITIVE_EXTRACTION_PROMPT,
    CONTRADICTION_DETECTION_PROMPT,
    INVALID_REASON_SUPERSEDED,
)
from mem0.memory.main import (
    Memory,
    _apply_contradictions,
    _bitemporal_filter,
    _coerce_bitemporal_date,
    _normalize_as_of,
    _with_bitemporal_filter,
)


class _PayloadMemory:
    """Minimal stand-in for a vector-store row (id + payload + score)."""

    def __init__(self, memory_id, payload, score=0.9):
        self.id = memory_id
        self.payload = payload
        self.score = score


def _record(valid_at=None, created_at="2024-01-01T00:00:00+00:00"):
    return {"valid_at": valid_at, "created_at": created_at}


class TestApplyContradictionsDeterminism:
    """[AC-4] 同输入 → 同输出，且不接触 LLM / 网络 / 当前时刻。"""

    def test_signature_takes_no_llm_client_or_now(self):
        params = set(inspect.signature(_apply_contradictions).parameters)
        assert params == {"contradictions", "new_records", "old_records"}
        assert not params & {"llm", "client", "now", "self"}

    def test_same_input_yields_identical_output(self):
        contradictions = [
            {"new_id": "new-b", "old_id": "old-1"},
            {"new_id": "new-a", "old_id": "old-1"},
            {"new_id": "new-a", "old_id": "old-2"},
        ]
        new_records = {"new-a": _record("2021-01-01"), "new-b": _record("2022-01-01")}
        old_records = {"old-1": _record("2020-01-01"), "old-2": _record("2019-01-01")}

        first = _apply_contradictions(contradictions, new_records, old_records)
        second = _apply_contradictions(contradictions, new_records, old_records)
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_result_is_independent_of_input_order(self):
        new_records = {"new-a": _record("2021-01-01"), "new-b": _record("2022-01-01")}
        old_records = {"old-1": _record("2020-01-01")}
        forward = [{"new_id": "new-a", "old_id": "old-1"}, {"new_id": "new-b", "old_id": "old-1"}]
        assert _apply_contradictions(forward, new_records, old_records) == _apply_contradictions(
            list(reversed(forward)), new_records, old_records
        )


class TestApplyContradictionsRules:
    """方案 §5.3.2 的处置规则 R1-R11（[AC-13]-[AC-18]）。"""

    def test_r11_empty_pairs_produce_no_write(self):
        assert _apply_contradictions([], {"a": _record("2021-01-01")}, {"b": _record("2020-01-01")}) == []

    def test_r1_unknown_old_id_is_dropped_with_warning(self, caplog):
        result = _apply_contradictions(
            [{"new_id": "new-a", "old_id": "hallucinated"}],
            {"new-a": _record("2021-01-01")},
            {"old-1": _record("2020-01-01")},
        )
        assert result == []
        assert any("unknown old_id" in record.message for record in caplog.records)

    def test_r1_unknown_new_id_is_dropped(self, caplog):
        result = _apply_contradictions(
            [{"new_id": "hallucinated", "old_id": "old-1"}],
            {"new-a": _record("2021-01-01")},
            {"old-1": _record("2020-01-01")},
        )
        assert result == []
        assert any("unknown new_id" in record.message for record in caplog.records)

    def test_r2_self_referential_pair_is_dropped(self):
        assert (
            _apply_contradictions(
                [{"new_id": "same", "old_id": "same"}],
                {"same": _record("2021-01-01")},
                {"same": _record("2020-01-01")},
            )
            == []
        )

    def test_r3_older_or_equal_new_fact_does_not_invalidate(self):
        old = {"old-1": _record("2021-01-01")}
        # 更新的事实生效更早 → 不处置
        assert _apply_contradictions([{"new_id": "new-a", "old_id": "old-1"}], {"new-a": _record("2020-01-01")}, old) == []
        # 生效键完全相等 → 不处置
        assert (
            _apply_contradictions(
                [{"new_id": "new-a", "old_id": "old-1"}],
                {"new-a": _record("2021-01-01")},
                old,
            )
            == []
        )

    def test_r3_valid_at_missing_falls_back_to_created_at(self):
        """存量记录没有 valid_at，生效时间由 created_at 兜底。"""
        old = {"old-1": {"created_at": "2020-01-01T00:00:00+00:00"}}
        new = {"new-a": {"valid_at": "2021-01-01", "created_at": "2026-01-01T00:00:00+00:00"}}
        updates = _apply_contradictions([{"new_id": "new-a", "old_id": "old-1"}], new, old)
        assert updates == [
            {
                "memory_id": "old-1",
                "invalid_at": "2021-01-01",
                "superseded_by": "new-a",
                "invalid_reason": INVALID_REASON_SUPERSEDED,
            }
        ]

    def test_r4_invalid_at_uses_new_fact_valid_at(self):
        updates = _apply_contradictions(
            [{"new_id": "new-a", "old_id": "old-1"}],
            {"new-a": _record("2021-06-15", "2026-09-17T10:00:00+00:00")},
            {"old-1": _record("2020-01-01")},
        )
        assert updates[0]["invalid_at"] == "2021-06-15"
        assert updates[0]["invalid_reason"] == INVALID_REASON_SUPERSEDED

    def test_r5_repeated_invalidation_keeps_the_earliest_boundary(self):
        # 已有 2022-06-01，新对给出更早的 2021-01-01 → 覆盖，三字段同源
        updates = _apply_contradictions(
            [{"new_id": "new-a", "old_id": "old-1"}],
            {"new-a": _record("2021-01-01")},
            {"old-1": {**_record("2019-01-01"), "invalid_at": "2022-06-01", "superseded_by": "old-newer"}},
        )
        assert updates == [
            {
                "memory_id": "old-1",
                "invalid_at": "2021-01-01",
                "superseded_by": "new-a",
                "invalid_reason": INVALID_REASON_SUPERSEDED,
            }
        ]

    def test_r5_reverse_case_leaves_existing_fields_untouched(self):
        updates = _apply_contradictions(
            [{"new_id": "new-a", "old_id": "old-1"}],
            {"new-a": _record("2022-06-01")},
            {"old-1": {**_record("2019-01-01"), "invalid_at": "2021-01-01", "superseded_by": "old-newer"}},
        )
        assert updates == []

    def test_r6_one_update_per_old_record_taking_the_latest_new_fact(self):
        updates = _apply_contradictions(
            [
                {"new_id": "new-a", "old_id": "old-1"},
                {"new_id": "new-b", "old_id": "old-1"},
                {"new_id": "new-c", "old_id": "old-1"},
            ],
            {"new-a": _record("2021-01-01"), "new-b": _record("2022-01-01"), "new-c": _record("2023-01-01")},
            {"old-1": _record("2020-01-01")},
        )
        assert len(updates) == 1
        # 生效键最大的新事实胜出，且 superseded_by 与该 invalid_at 同源
        assert updates[0]["invalid_at"] == "2023-01-01"
        assert updates[0]["superseded_by"] == "new-c"

    def test_r6_equal_effective_keys_break_ties_by_smallest_new_id(self):
        updates = _apply_contradictions(
            [{"new_id": "new-b", "old_id": "old-1"}, {"new_id": "new-a", "old_id": "old-1"}],
            {"new-a": _record("2022-01-01"), "new-b": _record("2022-01-01")},
            {"old-1": _record("2020-01-01")},
        )
        assert updates[0]["superseded_by"] == "new-a"

    def test_r7_chain_is_not_propagated_recursively(self):
        """A←B 与 B←C 是两次独立处置：A 的 superseded_by 保持为 B。"""
        first = _apply_contradictions(
            [{"new_id": "B", "old_id": "A"}], {"B": _record("2021-01-01")}, {"A": _record("2020-01-01")}
        )
        assert first[0]["superseded_by"] == "B"
        # C 到来时 B 自身尚未失效（B 是上一次处置的新记录），因此这条链路正常推进
        second = _apply_contradictions(
            [{"new_id": "C", "old_id": "B"}], {"C": _record("2022-01-01")}, {"B": _record("2021-01-01")}
        )
        assert second[0]["memory_id"] == "B"
        assert second[0]["superseded_by"] == "C"
        assert second[0]["invalid_at"] == "2022-01-01"
        # A 的结论未被改写成 C，保持为 B
        assert first[0] == {
            "memory_id": "A",
            "invalid_at": "2021-01-01",
            "superseded_by": "B",
            "invalid_reason": INVALID_REASON_SUPERSEDED,
        }


class TestBitemporalFilter:
    """[AC-5] point-in-time 与默认读的谓词构造。"""

    def test_default_read_excludes_invalidated_facts(self):
        before = datetime.now(timezone.utc)
        predicate = _bitemporal_filter()
        after = datetime.now(timezone.utc)
        assert list(predicate) == ["NOT"]
        boundary = datetime.fromisoformat(predicate["NOT"][0]["invalid_at"]["lte"])
        assert before <= boundary <= after

    def test_as_of_uses_created_at_fallback_branch(self):
        predicate = _bitemporal_filter(as_of="2020-06-01")
        moment = "2020-06-01T00:00:00+00:00"
        assert predicate["OR"][0] == {"valid_at": {"lte": moment}}
        assert predicate["OR"][1]["AND"][0] == {"created_at": {"lte": moment}}
        assert predicate["OR"][1]["AND"][1] == {"NOT": [{"valid_at": {"gte": "1970-01-01T00:00:00Z"}}]}
        assert predicate["NOT"] == [{"invalid_at": {"lte": moment}}]

    def test_include_invalidated_drops_the_predicate_entirely(self):
        assert _bitemporal_filter(include_invalidated=True) is None
        assert _with_bitemporal_filter({"user_id": "u"}, include_invalidated=True) == {"user_id": "u"}

    def test_scope_filters_are_combined_by_and_not_overwritten(self):
        merged = _with_bitemporal_filter({"user_id": "u"})
        assert merged["AND"][0] == {"user_id": "u"}
        assert "NOT" in merged["AND"][1]

    def test_normalize_as_of_rejects_garbage(self):
        with pytest.raises(ValueError):
            _normalize_as_of("not-a-date")

    def test_coerce_bitemporal_date_keeps_only_real_dates(self):
        assert _coerce_bitemporal_date("2024-03-05") == "2024-03-05"
        assert _coerce_bitemporal_date("2024-03-05T08:00:00Z") == "2024-03-05"
        assert _coerce_bitemporal_date("最近") is None
        assert _coerce_bitemporal_date(None) is None


def _setup_mocks(mocker, existing_results=None, contradiction_response='{"contradictions": []}'):
    """Build a Memory whose embedder / vector store / LLM are all mocked."""
    embedder = mocker.MagicMock()
    embedder.embed.return_value = [0.1, 0.2, 0.3]
    embedder.embed_batch.return_value = [[0.4, 0.5, 0.6]]
    mocker.patch("mem0.utils.factory.EmbedderFactory.create", return_value=embedder)

    vector_store = mocker.MagicMock()
    vector_store.search.return_value = list(existing_results or [])
    mocker.patch(
        "mem0.utils.factory.VectorStoreFactory.create", side_effect=[vector_store, mocker.MagicMock()]
    )

    llm = mocker.MagicMock()
    llm.generate_response.side_effect = [
        json.dumps({"memory": [{"text": "用户住在柏林", "attributed_to": "user", "valid_at": "2021-01-01"}]}),
        contradiction_response,
    ]
    mocker.patch("mem0.utils.factory.LlmFactory.create", return_value=llm)

    mocker.patch("mem0.memory.storage.SQLiteManager", mocker.MagicMock())
    mocker.patch("mem0.memory.main.capture_event")

    memory = Memory()
    memory.db.get_last_messages = MagicMock(return_value=[])
    memory.db.save_messages = MagicMock()
    memory.db.batch_add_history = MagicMock()
    return memory, vector_store, llm


def _existing_paris():
    return _PayloadMemory(
        "old-paris",
        {
            "data": "用户住在巴黎",
            "hash": "hash-paris",
            "valid_at": "2020-01-01",
            "created_at": "2026-09-01T00:00:00+00:00",
            "user_id": "test_bt_unit",
        },
    )


class TestContradictionDetectionCall:
    """[AC-3] 1a 与 1b 是两次独立 LLM 调用，提示词互不共享。"""

    def test_add_makes_two_independent_llm_calls(self, mocker):
        memory, _, llm = _setup_mocks(mocker, existing_results=[_existing_paris()])

        memory._add_to_vector_store(
            messages=[{"role": "user", "content": "我已经搬到柏林了"}],
            metadata={"user_id": "test_bt_unit"},
            filters={"user_id": "test_bt_unit"},
            infer=True,
        )

        assert llm.generate_response.call_count == 2
        first_messages = llm.generate_response.call_args_list[0].kwargs["messages"]
        second_messages = llm.generate_response.call_args_list[1].kwargs["messages"]
        # 两次调用各自的 system prompt 互不共享，user prompt 也完全不同
        assert first_messages[0]["content"] == ADDITIVE_EXTRACTION_PROMPT
        assert second_messages[0]["content"] == CONTRADICTION_DETECTION_PROMPT
        assert second_messages[0]["content"] != first_messages[0]["content"]
        assert second_messages[1]["content"] != first_messages[1]["content"]
        # 1b 的输入两侧都带 id：新事实用本批次落库的 uuid，已有事实用 payload 里的真实 id
        second_user_prompt = second_messages[1]["content"]
        assert "old-paris" in second_user_prompt
        assert "用户住在柏林" in second_user_prompt
        assert '"created_at"' in second_user_prompt

    def test_second_call_is_skipped_without_a_candidate_pool(self, mocker):
        memory, _, llm = _setup_mocks(mocker, existing_results=[])

        memory._add_to_vector_store(
            messages=[{"role": "user", "content": "我喜欢喝黑咖啡"}],
            metadata={"user_id": "test_bt_unit"},
            filters={"user_id": "test_bt_unit"},
            infer=True,
        )

        assert llm.generate_response.call_count == 1


class TestAddPipelineInvalidation:
    """写入管道的 1c 落地：只更新 payload，文本与记录都不动（R9/R10）。"""

    def test_matching_pair_invalidates_the_old_record(self, mocker):
        memory, vector_store, llm = _setup_mocks(mocker, existing_results=[_existing_paris()])

        def _respond(messages, **kwargs):
            if messages[0]["content"] == CONTRADICTION_DETECTION_PROMPT:
                new_facts = json.loads(messages[1]["content"].split("## New Facts\n")[1].split("\n\n")[0])
                return json.dumps({"contradictions": [{"new_id": new_facts[0]["id"], "old_id": "old-paris"}]})
            return json.dumps({"memory": [{"text": "用户住在柏林", "attributed_to": "user", "valid_at": "2021-01-01"}]})

        llm.generate_response.side_effect = _respond

        memory._add_to_vector_store(
            messages=[{"role": "user", "content": "我已经搬到柏林了"}],
            metadata={"user_id": "test_bt_unit"},
            filters={"user_id": "test_bt_unit"},
            infer=True,
        )

        new_id = vector_store.insert.call_args.kwargs["ids"][0]
        assert vector_store.update.call_count == 1
        assert vector_store.update.call_args.kwargs == {
            "vector_id": "old-paris",
            "vector": None,
            "payload": {
                "invalid_at": "2021-01-01",
                "superseded_by": new_id,
                "invalid_reason": INVALID_REASON_SUPERSEDED,
            },
        }
        # 新记录的 payload 带上了 1a 抽出的有效日期
        assert vector_store.insert.call_args.kwargs["payloads"][0]["valid_at"] == "2021-01-01"

    @pytest.mark.parametrize(
        "second_call",
        [ValueError("429 rate limit"), "not-json-at-all", ""],
        ids=["llm-error", "garbage", "empty"],
    )
    def test_failing_1b_does_not_break_the_write(self, mocker, second_call):
        """[AC-5] 1b 不可用或输出不可解析时，写入照常完成且不产生任何失效。"""
        memory, vector_store, llm = _setup_mocks(mocker, existing_results=[_existing_paris()])
        llm.generate_response.side_effect = [
            json.dumps({"memory": [{"text": "用户住在柏林", "valid_at": "2021-01-01"}]}),
            second_call,
        ]

        result = memory._add_to_vector_store(
            messages=[{"role": "user", "content": "我已经搬到柏林了"}],
            metadata={"user_id": "test_bt_unit"},
            filters={"user_id": "test_bt_unit"},
            infer=True,
        )

        assert result and result[0]["event"] == "ADD"
        vector_store.insert.assert_called_once()
        vector_store.update.assert_not_called()

    def test_invalid_valid_at_from_the_llm_is_stored_as_null(self, mocker):
        memory, vector_store, llm = _setup_mocks(mocker, existing_results=[])
        llm.generate_response.side_effect = [
            json.dumps({"memory": [{"text": "用户喜欢喝黑咖啡", "valid_at": "很久以前"}]})
        ]

        memory._add_to_vector_store(
            messages=[{"role": "user", "content": "我喜欢喝黑咖啡"}],
            metadata={"user_id": "test_bt_unit"},
            filters={"user_id": "test_bt_unit"},
            infer=True,
        )

        assert vector_store.insert.call_args.kwargs["payloads"][0]["valid_at"] is None
