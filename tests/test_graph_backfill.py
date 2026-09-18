"""Tests for the graph-bridge backfill endpoint (`server/graph-bridge/app.py`).

设计见 `docs/design/graph-memory.md` §5.5。这里钉的是**回填自身的语义**，不重复验证
graphiti 的抽取行为（那是实测面的职责）：

  * 图键 → 作用域后缀的形态校验，以及与 SDK 侧 `derive_group_id` 的**逐条同构对照**——
    图键派生规则在两端漂移，会让回填把事实写进别的图键，这是本卡最贵的错误；
  * 事实主存访问面（`QdrantFactSource`）**发到线上的请求形态**：作用域 OR 嵌在 `must` 内、
    `created_at` 的 keyset 游标、`created_at` 降序、排除观察条目——用 `httpx.MockTransport`
    拦住真实的请求体断言，而不是断言自己构造的对象；
  * 批次语义：`limit` 只计新入图条数、已同步的跳过不计数、游标严格前进且批次不重叠；
  * 限速：`rate` 产生真实等待，`rate=0` 不等待；
  * HTTP 面：作用域形态非法 → 400，已有回填在跑 → 409，进度端点可读到最新一次的计数。

图桥镜像不含 SDK 源码，本文件按文件路径导入 `server/graph-bridge/app.py`（目录名带连字符
无法用包导入）；只替换它的外部依赖（事实主存访问面、Graphiti 实例、单条入图），
不重写被测逻辑。
"""

import asyncio
import importlib.util
import inspect
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("httpx", reason="httpx not installed")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
_APP_PATH = _REPO_ROOT / "server" / "graph-bridge" / "app.py"

# 仓库根在导入面上，`mem0.memory.graph_sync`（SDK 侧派生规则的对照源）才可导入。
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_spec = importlib.util.spec_from_file_location("graph_bridge_app", _APP_PATH)
app = importlib.util.module_from_spec(_spec)
sys.modules["graph_bridge_app"] = app
_spec.loader.exec_module(app)

GROUP_ID = "mem0_probe_backfill"
SCOPE = "probe_backfill"


def _record(uuid, created_at, user_id=SCOPE, **extra):
    payload = {"user_id": user_id, "data": f"事实 {uuid}", "created_at": created_at, **extra}
    return {"id": uuid, "payload": payload}


def _cursor_of(filter_body):
    """取出候选过滤器里的 `created_at` keyset 游标（无游标时为 `None`）。"""
    for condition in filter_body["must"]:
        if condition.get("key") == "created_at":
            return condition["range"]["lt"]
    return None


class _FakeFactSource:
    """事实主存访问面的替身：按 `created_at` 降序 + 严格小于游标地翻页，并回报总数。

    过滤器**不在替身里求值**——作用域 OR 与排除观察条目的语义由 `TestQdrantFactSource`
    在线上形态层面断言，这里只保留每次收到的游标供批次衔接的对账。
    """

    def __init__(self, records):
        self.records = sorted(records, key=lambda row: row["payload"]["created_at"], reverse=True)
        self.cursors = []
        self.closed = False

    async def scroll(self, group_id, cursor, limit):
        self.cursors.append(cursor)
        rows = [row for row in self.records if cursor is None or row["payload"]["created_at"] < cursor]
        return [dict(row) for row in rows[:limit]]

    async def count(self, group_id):
        return len(self.records)

    async def aclose(self):
        self.closed = True


class _FakeGraphiti:
    """只提供 `_count` 需要的 `driver.execute_query`（图内 episode 读数）。"""

    def __init__(self, episodes=0):
        self.episodes = episodes
        self.driver = type("_Driver", (), {"execute_query": self._execute})()

    async def _execute(self, cypher):
        return [{"count": self.episodes}], None, None


@pytest.fixture(autouse=True)
def clean_state():
    """回填读数与单飞标志是模块级状态，逐例复位，避免用例之间互相干扰。"""
    app._backfill_runs.clear()
    app._backfill_task = None
    yield
    app._backfill_runs.clear()
    app._backfill_task = None


@pytest.fixture
def harness(monkeypatch):
    """把回填的外部依赖换成替身：事实主存访问面、Graphiti 实例、单条入图。

    `_sync_one` 被替换掉的正是「入图」这一步；`already_synced` 的判定由返回的 status
    驱动，其真实实现与 `/episodes` 共用（见文件末的共用实现断言）。
    """
    state = {"synced": set(), "calls": [], "source": None, "graphiti": None}

    def _install(records, episodes=0):
        state["source"] = _FakeFactSource(records)
        state["graphiti"] = _FakeGraphiti(episodes=episodes)
        monkeypatch.setattr(app, "_build_fact_source", lambda: state["source"])

        async def _get(group_id):
            return state["graphiti"]

        monkeypatch.setattr(app.registry, "get", _get)

        async def _already_synced(graphiti_arg, uuid):
            """`/episodes` 幂等判定的替身（真实实现要读 FalkorDB）。"""
            return uuid in state["synced"]

        monkeypatch.setattr(app, "_episode_already_synced", _already_synced)

        async def _sync_one(graphiti_arg, group_id, uuid, text, reference_time=None, source_description=None):
            state["calls"].append(uuid)
            if uuid in state["synced"]:
                return "already_synced", 0, 0
            state["synced"].add(uuid)
            # 真实入图让图内 episode 数 +1：调用结束时的 `graph_episodes` 读数因此**含**本次
            # 新入图的部分（这正是 `remaining` 不能再减一次 `processed` 的原因）。
            state["graphiti"].episodes += 1
            return "synced", 2, 1

        monkeypatch.setattr(app, "_sync_one", _sync_one)
        return state["source"]

    state["install"] = _install
    return state


def _run(harness, records=None, **request):
    """跑一次回填主体，返回 `(response, 事实主存替身)`（替身用于断言游标与关闭）。

    `records=None` 表示沿用已安装的替身（调用方在测失败路径时要自己换掉 `_sync_one`）。
    """
    if records is not None:
        harness["install"](records)
    payload = {"group_id": GROUP_ID, "limit": 2, "scan_limit": 50, **request}
    run = app.BackfillRun(
        group_id=GROUP_ID,
        limit=payload["limit"],
        rate=payload.get("rate", 0.0),
        scan_limit=payload["scan_limit"],
        cursor=payload.get("cursor"),
        started_at="2026-01-01T00:00:00+00:00",
        started_monotonic=app.time.monotonic(),
    )
    response = asyncio.run(app._run_backfill(app.BackfillRequest(**payload), run))
    return response, harness["source"]


# --------------------------------------------------------------------------- 图键派生


class TestGraphKeyDerivation:
    def test_matches_the_sdk_derivation_for_every_scoping_shape(self):
        """[AC-3] 的判定面：两端派生规则必须逐条一致，否则回填会写进别的图键。"""
        from mem0.memory.graph_sync import derive_group_id as sdk_derive_group_id

        payloads = [
            {"user_id": "xue"},
            {"user_id": "xue", "agent_id": "ying", "run_id": "r1"},
            {"agent_id": "ying"},
            {"run_id": "r1"},
            {"user_id": "", "agent_id": "ying"},
            {"user_id": "a:b", "agent_id": "c#d"},
            {"user_id": "  padded  "},
            {"user_id": "长" * 200},
            {},
            {"agent_id": None},
        ]
        for payload in payloads:
            assert app._derive_group_id(payload) == sdk_derive_group_id(payload), payload

    def test_priority_is_user_then_agent_then_run(self):
        assert app._derive_group_id({"user_id": "u", "agent_id": "a", "run_id": "r"}) == "mem0_u"
        assert app._derive_group_id({"agent_id": "a", "run_id": "r"}) == "mem0_a"
        assert app._derive_group_id({"run_id": "r"}) == "mem0_r"

    def test_scope_suffix_rejects_keys_that_are_not_sdk_derived(self):
        assert app._scope_suffix("mem0_xue") == "xue"
        with pytest.raises(Exception) as excinfo:
            app._scope_suffix("xue")
        assert getattr(excinfo.value, "status_code", None) == 400


class TestCandidateFilter:
    def test_scope_or_is_nested_inside_must_and_observations_are_excluded(self):
        """作用域 OR 必须嵌在 `must` 内（顶层 must+should 的语义有过歧义，实测见 §5.5 落地记录）。"""
        built = app._candidate_filter(GROUP_ID, None)

        assert len(built["must"]) == 1
        scope = built["must"][0]
        assert [condition["key"] for condition in scope["should"]] == list(app.SCOPE_KEYS)
        assert {condition["match"]["value"] for condition in scope["should"]} == {SCOPE}
        assert built["must_not"] == [{"key": "memory_kind", "match": {"value": app.MEMORY_KIND_OBSERVATION}}]

    def test_cursor_is_a_strict_lower_bound_on_created_at(self):
        built = app._candidate_filter(GROUP_ID, "2026-09-18T05:39:40.269663+00:00")
        assert len(built["must"]) == 2
        assert _cursor_of(built) == "2026-09-18T05:39:40.269663+00:00"
        # 严格小于：并列 created_at 会被跨批次跳过，故范围里不得出现 `lte`/`gte`。
        assert set(built["must"][1]["range"]) == {"lt"}


class TestQdrantFactSource:
    """事实主存访问面的线上形态（用 `httpx.MockTransport` 拦真实请求体断言）。"""

    @staticmethod
    def _source(handler):
        source = app.QdrantFactSource(host="qdrant", port=6333, collection="memories_2048")
        source._client = httpx.AsyncClient(base_url="http://qdrant:6333", transport=httpx.MockTransport(handler))
        return source

    def test_scroll_requests_the_documented_reading_shape(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"result": {"points": [{"id": "p1", "payload": {"created_at": "t1"}}]}})

        source = self._source(handler)
        points = asyncio.run(source.scroll(GROUP_ID, "2026-09-18T05:39:40.269663+00:00", 64))

        assert seen["path"] == "/collections/memories_2048/points/scroll"
        assert seen["body"]["limit"] == 64
        assert seen["body"]["with_payload"] is True
        assert seen["body"]["with_vector"] is False
        assert seen["body"]["order_by"] == {"key": "created_at", "direction": "desc"}
        assert _cursor_of(seen["body"]["filter"]) == "2026-09-18T05:39:40.269663+00:00"
        # 过滤器的两个约束都在线上：作用域 OR + 排除观察条目。
        assert seen["body"]["filter"]["must"][0]["should"][0]["match"]["value"] == SCOPE
        assert seen["body"]["filter"]["must_not"][0]["key"] == "memory_kind"
        assert points == [{"id": "p1", "payload": {"created_at": "t1"}}]

    def test_count_uses_exact_and_the_scope_filter(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"result": {"count": 4114}})

        source = self._source(handler)
        assert asyncio.run(source.count(GROUP_ID)) == 4114
        assert seen["path"] == "/collections/memories_2048/points/count"
        assert seen["body"]["exact"] is True
        assert "filter" in seen["body"]

    def test_non_2xx_raises_so_a_broken_store_is_not_read_as_empty(self):
        source = self._source(lambda request: httpx.Response(503, json={"status": "error"}))
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(source.scroll(GROUP_ID, None, 10))

    def test_a_missing_result_body_reads_as_empty(self):
        source = self._source(lambda request: httpx.Response(200, json={"status": "ok"}))
        assert asyncio.run(source.scroll(GROUP_ID, None, 10)) == []
        assert asyncio.run(source.count(GROUP_ID)) == 0


# --------------------------------------------------------------------------- 批次语义


class TestBatchSemantics:
    def _facts(self, count=6):
        # created_at 降序：f1 最新，与 Qdrant 的读面口径一致（扫过顺序即 f1, f2, ...）。
        return [
            _record(f"f{index}", f"2026-09-18T05:{20 - index:02d}:00.00000{index}+00:00")
            for index in range(1, count + 1)
        ]

    def test_limit_counts_only_newly_ingested_facts(self, harness):
        response, client = _run(harness, self._facts(), limit=2)
        assert response.scanned == 2
        assert harness["calls"] == ["f1", "f2"]
        assert app._backfill_runs[GROUP_ID].processed == 2
        assert app._backfill_runs[GROUP_ID].exhausted is False

    def test_already_synced_facts_are_skipped_and_do_not_consume_the_limit(self, harness):
        harness["synced"].update({"f1", "f2", "f3"})
        response, _client = _run(harness, self._facts(), limit=2)
        run = app._backfill_runs[GROUP_ID]
        assert run.already_synced == 3
        assert run.processed == 2
        assert response.scanned == 5
        # 已同步的三条**根本没进**入图路径（不重复抽取），新入图的是 f4 / f5。
        assert harness["calls"] == ["f4", "f5"]

    def test_cursor_advances_strictly_so_batches_never_overlap(self, harness):
        records = self._facts()
        first, first_client = _run(harness, records, limit=2)
        app._backfill_runs.clear()
        second, second_client = _run(harness, records, limit=2, cursor=first.next_cursor)

        assert first.next_cursor == records[1]["payload"]["created_at"]
        # 第二批的过滤器拿到的正是第一批的续跑锚点：两批不重叠。
        assert first_client.cursors == [None]
        assert second_client.cursors == [records[1]["payload"]["created_at"]]
        assert harness["calls"] == ["f1", "f2", "f3", "f4"]
        assert second.processed == 2
        assert second.next_cursor == records[3]["payload"]["created_at"]

    def test_rescanning_from_the_top_is_safe_but_counts_as_already_synced(self, harness):
        records = self._facts()
        _run(harness, records, limit=3)
        app._backfill_runs.clear()
        again, _client = _run(harness, records, limit=3)
        assert again.already_synced == 3
        assert again.processed == 3

    def test_scan_limit_bounds_the_work_when_everything_is_already_synced(self, harness):
        harness["synced"].update({f"f{index}" for index in range(1, 7)})
        _run(harness, self._facts(), limit=5, scan_limit=2)
        run = app._backfill_runs[GROUP_ID]
        assert run.scanned == 2
        assert run.processed == 0
        assert run.already_synced == 2
        assert run.exhausted is False

    def test_exhausted_is_reported_when_the_scope_is_fully_scanned(self, harness):
        _run(harness, self._facts(count=2), limit=50, scan_limit=50)
        run = app._backfill_runs[GROUP_ID]
        assert run.processed == 2
        assert run.exhausted is True

    def test_records_belonging_to_another_graph_key_are_never_ingested(self, harness):
        """候选过滤按作用域值粗筛是上界：`user_id=other` 的记录属于 `mem0_other`。"""
        records = [_record("f1", "2026-09-18T05:01:00.000001+00:00")] + [
            _record("other1", "2026-09-18T05:02:00.000002+00:00", user_id="other"),
        ]
        _run(harness, records, limit=2)
        run = app._backfill_runs[GROUP_ID]
        assert run.out_of_scope == 1
        assert run.processed == 1
        assert "other1" not in harness["calls"]

    def test_failed_facts_are_counted_and_do_not_stop_the_batch(self, harness):
        records = self._facts()
        harness["install"](records)

        async def _flaky(graphiti, group_id, uuid, text, reference_time=None, source_description=None):
            if uuid == "f1":
                raise RuntimeError("episodes 500")
            return "synced", 1, 1

        original = app._sync_one
        app._sync_one = _flaky
        try:
            response, _client = _run(harness, None, limit=2)
        finally:
            app._sync_one = original
        assert response.failed == 1
        assert response.processed == 2
        assert response.scanned == 3  # 失败的那条不消耗 limit，批次继续直到新入图 2 条
        assert response.errors and "episodes 500" in response.errors[0]

    def test_remaining_is_recomputed_from_the_graph_scale(self, harness):
        """`remaining` 取的是**终点**图规模读数，不是起点读数加计数推断：本批新入图 2 条后
        `graph_episodes` 为 3（起点 1 + 本批 2），故 `remaining = 4 − 3 = 1`；若实现用的是起点
        读数（1），这里会读到 3。"""
        harness["install"](self._facts(count=4), episodes=1)
        run = app.BackfillRun(
            group_id=GROUP_ID,
            limit=2,
            rate=0.0,
            scan_limit=50,
            cursor=None,
            started_at="2026-01-01T00:00:00+00:00",
            started_monotonic=app.time.monotonic(),
        )
        response = asyncio.run(app._run_backfill(app.BackfillRequest(group_id=GROUP_ID, limit=2, scan_limit=50), run))
        assert response.scope_total == 4
        assert response.graph_episodes == 3
        assert response.remaining == 1


# --------------------------------------------------------------------------- 进度读数口径


# 响应与 `/backfill/status` 的同名字段集合：同名即同义，必须同值（唯一取值处见 app.py 的
# `_progress_fields`）。核验实测（`bfchk_a` 16 vs 12、`bfchk_b` 16 vs 12、`bfchk_tie` 3 vs 2）
# 的形态就是两侧只有 `remaining` 一个字段不一致，而差值恒等于 `processed`。
_SHARED_PROGRESS_FIELDS = (
    "group_id",
    "processed",
    "already_synced",
    "failed",
    "out_of_scope",
    "scanned",
    "exhausted",
    "next_cursor",
    "elapsed_seconds",
    "paced_wait_seconds",
    "facts_per_minute",
    "scope_total",
    "graph_episodes",
    "remaining",
    "errors",
)


class TestProgressReadingConsistency:
    """[AC-1][AC-2][AC-3][AC-4] 进度读数：`POST /backfill` 的响应与 `GET /backfill/status`
    必须同口径、同值。

    `remaining` 的口径是 `scope_total − graph_episodes`，其中 `graph_episodes` 是**调用结束时**
    的图规模读数，已含本次新入图的部分；再减一次 `processed` 就是重复扣减（读数系统性偏小，
    差值恒等于 `processed`）。这里把「同名即同值」钉成不变式，而不是只钉 `remaining` 一个字段。
    """

    def _facts(self, count=4):
        # created_at 降序：f1 最新，与 Qdrant 的读面口径一致（扫过顺序即 f1, f2, ...）。
        return [
            _record(f"f{index}", f"2026-09-18T05:{20 - index:02d}:00.00000{index}+00:00")
            for index in range(1, count + 1)
        ]

    def _post(self, harness, client, **body):
        payload = {"group_id": GROUP_ID, "limit": 2, "scan_limit": 50, **body}
        response = client.post("/backfill", json=payload)
        assert response.status_code == 200
        return response.json()

    def _status(self, client):
        body = client.get("/backfill/status", params={"group_id": GROUP_ID}).json()
        assert body["running"] is False
        return body["runs"][0]

    def test_status_and_response_agree_on_every_shared_field(self, harness):
        """限速批次（`paced_wait_seconds` 非整）也要求逐字段相等：浮点读数的精度同样只在
        一处定，否则同一含义的字段会在两个面上给出不同的数。"""
        harness["install"](self._facts())
        client = TestClient(app.app)
        response = self._post(harness, client, rate=600)

        status = self._status(client)
        assert {field: status[field] for field in _SHARED_PROGRESS_FIELDS} == {
            field: response[field] for field in _SHARED_PROGRESS_FIELDS
        }

    def test_remaining_is_scope_total_minus_graph_episodes(self, harness):
        """数值验算：`remaining == scope_total − graph_episodes`，`processed` 只经由终点
        `graph_episodes` 读数参与一次（修复前 status 侧读到的是 `max(0, 4 − 2 − 2) = 0`）。"""
        harness["install"](self._facts())
        client = TestClient(app.app)
        response = self._post(harness, client)

        assert app._backfill_runs[GROUP_ID].processed == 2
        # 终点读数含本次新入图：起点 0 + 本批 2 = 2（`_run_backfill` 的终点复算）。
        assert response["graph_episodes"] == 2
        assert response["scope_total"] == 4
        assert response["remaining"] == response["scope_total"] - response["graph_episodes"]
        assert response["remaining"] == 2
        assert self._status(client)["remaining"] == 2

    def test_a_resumed_batch_keeps_both_readings_on_the_same_scale(self, harness):
        """[AC-4] 续跑：第二批接着第一批的 `next_cursor` 推进，两处的 `processed` / `remaining`
        同步反映真实剩余（同作用域、同一批数据）。"""
        harness["install"](self._facts(count=4))
        client = TestClient(app.app)
        first = self._post(harness, client)
        second = self._post(harness, client, cursor=first["next_cursor"])

        assert (first["processed"], first["remaining"]) == (2, 2)
        assert (second["processed"], second["remaining"]) == (2, 0)
        status = self._status(client)
        assert status["state"] == "completed"
        assert {field: status[field] for field in _SHARED_PROGRESS_FIELDS} == {
            field: second[field] for field in _SHARED_PROGRESS_FIELDS
        }

    def test_the_scan_breakdown_covers_every_scanned_record(self, harness):
        """[AC-3] `scanned` 是分母：每条扫过的记录恰好落进 `processed` / `already_synced` /
        `failed` / `out_of_scope` 之一，故四者之和恒等于 `scanned`（各字段关系的可复核形态）。"""
        records = [
            _record("f1", "2026-09-18T05:04:00.000001+00:00"),
            _record("f2", "2026-09-18T05:03:00.000002+00:00"),
            _record("f3", "2026-09-18T05:02:00.000003+00:00"),
            _record("other1", "2026-09-18T05:01:00.000004+00:00", user_id="other"),
            _record("empty1", "2026-09-18T05:00:00.000005+00:00", data="   "),
        ]
        harness["synced"].update({"f1"})
        harness["install"](records)
        client = TestClient(app.app)
        response = self._post(harness, client, limit=5, scan_limit=5)

        assert (response["already_synced"], response["processed"]) == (1, 2)
        assert (response["out_of_scope"], response["failed"], response["scanned"]) == (1, 1, 5)
        assert response["scanned"] == (
            response["processed"] + response["already_synced"] + response["failed"] + response["out_of_scope"]
        )


# --------------------------------------------------------------------------- 限速


class TestRateLimit:
    def test_rate_produces_real_waiting(self, harness):
        records = [_record(f"f{index}", f"2026-09-18T05:{20 - index:02d}:00.00000{index}+00:00") for index in range(1, 4)]
        # 600 条/分钟 = 0.1s 间隔：3 条事实至少等待 2 个间隔。
        _response, client = _run(harness, records, limit=3, rate=600)
        run = app._backfill_runs[GROUP_ID]
        assert run.processed == 3
        assert run.paced_wait_seconds >= 0.15
        assert client.closed is True

    def test_rate_zero_does_not_wait(self, harness):
        records = [_record(f"f{index}", f"2026-09-18T05:{20 - index:02d}:00.00000{index}+00:00") for index in range(1, 4)]
        _run(harness, records, limit=3, rate=0)
        run = app._backfill_runs[GROUP_ID]
        assert run.processed == 3
        assert run.paced_wait_seconds == 0.0

    def test_skipped_facts_do_not_consume_the_rate_budget(self, harness):
        """重扫已入图区域只花图内查询：不应被 `rate` 压到 60 秒一条。"""
        harness["synced"].update({f"f{index}" for index in range(1, 6)})
        records = [_record(f"f{index}", f"2026-09-18T05:{20 - index:02d}:00.00000{index}+00:00") for index in range(1, 7)]
        _run(harness, records, limit=1, rate=600, scan_limit=6)  # 0.1s 间隔
        run = app._backfill_runs[GROUP_ID]
        assert run.already_synced == 5
        assert run.processed == 1
        # 五条跳过不产生等待，只有第 6 条（本批首次入图）之前的一次等待（首条不等待 → 0）。
        assert run.paced_wait_seconds == 0.0

    def test_the_first_fact_is_not_delayed_by_the_rate_limit(self, harness):
        records = [_record("f1", "2026-09-18T05:01:00.000001+00:00")]
        _run(harness, records, limit=1, rate=1)  # 1 条/分钟：若首条也等待，这里会等满 60s
        assert app._backfill_runs[GROUP_ID].processed == 1
        assert app._backfill_runs[GROUP_ID].paced_wait_seconds == 0.0


# --------------------------------------------------------------------------- HTTP 面


class TestHttpSurface:
    def _client(self, harness, records=None):
        if records is not None:
            harness["install"](records)
        return TestClient(app.app)

    def test_rejects_a_group_id_that_is_not_sdk_derived(self, harness):
        client = self._client(harness, [])
        response = client.post("/backfill", json={"group_id": "xue", "limit": 1})
        assert response.status_code == 400
        assert "mem0_" in response.json()["detail"]

    def test_conflicts_while_another_run_is_in_progress(self, harness):
        self._client(harness, [])
        app._backfill_runs[GROUP_ID] = app.BackfillRun(
            group_id=GROUP_ID,
            limit=1,
            rate=0.0,
            scan_limit=1,
            cursor=None,
            started_at="2026-01-01T00:00:00+00:00",
            started_monotonic=app.time.monotonic(),
        )
        app._backfill_task = type("_Task", (), {"done": lambda self: False})()

        response = self._client(harness).post("/backfill", json={"group_id": GROUP_ID, "limit": 1})
        assert response.status_code == 409
        assert GROUP_ID in response.json()["detail"]

    def test_status_reads_the_latest_run_and_the_running_flag(self, harness):
        client = self._client(harness, [_record("f1", "2026-09-18T05:01:00.000001+00:00")])
        assert client.post("/backfill", json={"group_id": GROUP_ID, "limit": 1, "scan_limit": 5}).status_code == 200

        body = client.get("/backfill/status", params={"group_id": GROUP_ID}).json()
        assert body["running"] is False
        assert len(body["runs"]) == 1
        run = body["runs"][0]
        assert run["group_id"] == GROUP_ID
        assert run["state"] == "completed"
        assert run["processed"] == 1
        assert run["scanned"] == 1
        assert "remaining" in run

    def test_status_freezes_the_elapsed_of_a_finished_run(self, harness):
        """[AC-4] 已结束的调用在 `/backfill/status` 里必须给出它自己的耗时，而不是「从开始到
        现在」的时长。未冻结时该读数会随查询时间无限增长、`facts_per_minute` 随之衰减
        （实测：一次 62.7s 的调用在约 6 分钟后被读成 379.5s / 0.316 条/分钟）。
        """
        client = self._client(harness, [_record("f1", "2026-09-18T05:01:00.000001+00:00")])
        assert client.post("/backfill", json={"group_id": GROUP_ID, "limit": 1, "scan_limit": 5}).status_code == 200

        run = app._backfill_runs[GROUP_ID]
        assert run.finished_monotonic is not None
        # 把这次调用搬到「62.7s 跑完、之后又过了 6 分钟才来读状态」的时间关系上。
        run.started_monotonic = app.time.monotonic() - 62.7 - 360.0
        run.finished_monotonic = run.started_monotonic + 62.7

        body = client.get("/backfill/status", params={"group_id": GROUP_ID}).json()
        assert body["runs"][0]["elapsed_seconds"] == pytest.approx(62.7, abs=1e-3)
        assert body["runs"][0]["facts_per_minute"] == pytest.approx(60.0 / 62.7, rel=1e-3)

    def test_an_interrupted_run_stays_readable_with_partial_progress(self, harness):
        """[AC-4] 调用被异常/取消打断时，读数必须留在 `/backfill/status` 里并标成 `interrupted`，
        且耗时同样冻结（部分进度可读是续跑判据的观测面）。"""
        self._client(harness, [])
        source = harness["source"]

        async def _boom(group_id, cursor, limit):
            raise RuntimeError("qdrant exploded")

        source.scroll = _boom
        client = TestClient(app.app, raise_server_exceptions=False)
        assert client.post("/backfill", json={"group_id": GROUP_ID, "limit": 2, "scan_limit": 5}).status_code == 500

        run = app._backfill_runs[GROUP_ID]
        assert run.state == "interrupted"
        assert run.finished_monotonic is not None
        assert app._backfill_task is None
        body = client.get("/backfill/status", params={"group_id": GROUP_ID}).json()
        assert body["runs"][0]["state"] == "interrupted"

    def test_status_without_a_group_id_returns_every_recorded_run(self, harness):
        self._client(harness, [])
        app._backfill_runs["mem0_other"] = app.BackfillRun(
            group_id="mem0_other",
            limit=1,
            rate=0.0,
            scan_limit=1,
            cursor=None,
            started_at="2026-01-01T00:00:00+00:00",
            started_monotonic=app.time.monotonic(),
        )
        body = self._client(harness).get("/backfill/status").json()
        assert [run["group_id"] for run in body["runs"]] == ["mem0_other"]

    def test_request_validation_bounds_the_batch(self, harness):
        client = self._client(harness, [])
        assert client.post("/backfill", json={"group_id": GROUP_ID, "limit": 0}).status_code == 422
        assert client.post("/backfill", json={"group_id": GROUP_ID, "rate": -1}).status_code == 422

    def test_openapi_exposes_the_backfill_contract(self, harness):
        schema = self._client(harness, []).get("/openapi.json").json()
        backfill = schema["paths"]["/backfill"]["post"]
        assert backfill["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("BackfillRequest")
        fields = schema["components"]["schemas"]["BackfillRequest"]["properties"]
        assert {"group_id", "limit", "rate", "cursor", "scan_limit"} <= set(fields)
        response_fields = schema["components"]["schemas"]["BackfillResponse"]["properties"]
        assert {"processed", "already_synced", "failed", "remaining", "exhausted", "next_cursor"} <= set(response_fields)


def test_episodes_and_backfill_share_one_ingest_implementation():
    """`/episodes` 与回填必须走同一入图实现（回填因此复用 `already_synced` 而去重不另造）。"""
    episodes_source = inspect.getsource(app.ingest_episode)
    assert "_sync_one(" in episodes_source
    assert "add_episode" not in episodes_source

    single = inspect.getsource(app._sync_one)
    assert "add_episode" in single
    assert "_episode_already_synced" in single
    assert _APP_PATH.name == "app.py"
