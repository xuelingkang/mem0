# bi-temporal 事实模型设计（mem0 自托管记忆）

| 项 | 值 |
| --- | --- |
| 目标仓库 | `/Users/xuelingkang/Documents/Containers/mem0` |
| 实现提交 | `beaf09d0`（bi-temporal 事实模型）、`cfa8ecaf`（patch 刷新） |
| 运行时 | Docker Compose 四服务（`server/docker-compose.yaml`：mem0 / postgres / qdrant / dashboard），源码经 volume 挂载 |
| 存储 | Qdrant 1.19.0，集合 `memories_2048`（2048 维，含 `bm25` sparse 槽位） |
| 影响面 | 提取提示词、SDK 写入管道、SDK 检索、REST 序列化与导出、Qdrant payload 索引 |
| 实现形态 | 仅本地改动（相对 `upstream/main`），不提交上游 PR |

> 本文档为本地设计文档，位于仓库 `docs/design/`，与 Mintlify 文档站（`docs/**/*.mdx`）无关，不参与 `llms.txt` 收录检查。

---

# 1. 项目概述

## 1.1 背景

自托管 mem0 承载用户长期记忆，事实随时间演进（居住城市、在职公司、偏好取向都会变化）。当前记录形态是扁平文本条目：每次写入新增一条自包含的事实陈述，事实之间不携带任何时间字段，系统无法回答「这条事实从何时起为真」「哪条事实已被取代」。

事实模型为每条事实携带生效时间与失效时间，使「哪一条是当前真相」成为一条可机器判定的谓词。

## 1.2 目标

1. 事实条目携带真实世界的生效时间 `valid_at`（不是入库时间）。
2. 新事实与旧事实互相矛盾时，旧事实获得失效边界，且旧事实的文本、记录与其入库时间完整保留。
3. 失效判定采用业界标准的三段式结构：LLM 只做语义判断，处置由确定性代码完成。
4. 检索默认返回当前有效事实；给定时刻 T 可返回当时有效的事实。
5. 以上全部落在 Qdrant payload 上，不引入第二份存储。

## 1.3 范围

包含：

- `mem0/configs/prompts.py`：提取提示词产出 `valid_at`；新增独立矛盾检测提示词。
- `mem0/memory/main.py`：写入管道插入「独立矛盾检测」与「确定性时序处置」；检索默认加有效事实过滤；point-in-time 查询入口。
- `server/main.py`：REST 层字段序列化、导出列、检索参数透传。
- `mem0/vector_stores/qdrant.py`：新增 payload 索引字段声明。
- `tests/memory/`：新增单测与端到端验证。

---

# 2. 需求分析

## 2.1 功能需求

| 编号 | 需求 |
| --- | --- |
| F1 | 提取事实时同时产出该事实的生效时间 `valid_at`（`YYYY-MM-DD`）；无法判定时输出 `null` |
| F2 | 提取阶段只做 ADD，不判断、不改写、不删除任何已有记忆 |
| F3 | 独立的一次 LLM 调用，只回答「哪些新事实与哪些已有事实互相矛盾」，输出矛盾对 |
| F4 | 确定性代码按生效时间与时序规则完成失效处置，写入 `invalid_at` / `superseded_by` / `invalid_reason` |
| F5 | 失效是元数据变更：文本不改写、记录不删除、入库时间不变、向量与 BM25 稀疏向量不动 |
| F6 | 检索默认只返回未失效事实；`include_invalidated=true` 返回全量 |
| F7 | 给定时刻 T 可查询当时有效的事实（point-in-time） |
| F8 | 导出与序列化把 bi-temporal 字段作为一等字段输出，不落进 `metadata` |

## 2.2 非功能需求

| 项 | 要求 |
| --- | --- |
| 增量成本 | 每次 `add()` 增加 1 次 LLM 调用；新事实为空或候选池为空时跳过该次调用 |
| 读路径开销 | 默认读新增 1 个 Qdrant 过滤条件，命中 payload 索引 |
| 幂等性 | 同一批矛盾对重复处置不改变结果 |
| 可复现 | 处置规则为纯函数，同输入同输出，不依赖 LLM 临场判断 |

---

# 3. 总体架构设计

## 3.1 责任边界与数据接口

三段式的核心是**严格职责分离**：每一段只有一个职责，段与段之间以明确的数据结构交接。

```mermaid
flowchart LR
    A["1a 时间抽取<br/>LLM 调用 #1"] -->|新事实[{id,text,valid_at}]| B["1b 独立矛盾检测<br/>LLM 调用 #2"]
    A -->|新事实文本| E["嵌入 + hash 去重"]
    B -->|矛盾对[{new_id,old_id}]| C["1c 确定性时序处置<br/>纯代码"]
    E -->|新记录 payload| C
    D["候选池（现有有效事实）"] --> B
    D --> C
    C -->|invalid_at/superseded_by/invalid_reason| F["Qdrant payload 更新"]
    E -->|新记录| F
```

| 段 | 职责 | 输入 | 输出 | 边界 |
| --- | --- | --- | --- | --- |
| 1a 时间抽取 | 从对话提取事实，并给出该事实的生效时间 | 新消息、观察日期、已有记忆（去重参考） | `[{id, text, attributed_to, linked_memory_ids, valid_at}]` | 只产 ADD；不判断矛盾、不做处置 |
| 1b 独立矛盾检测 | 只回答「哪些新事实与哪些已有事实互相矛盾」 | 新事实列表（含 id 与 valid_at）；候选池（现有有效事实，含 id / text / valid_at / created_at） | `{"contradictions": [{"new_id", "old_id"}]}` | 只说配对，不含保留/失效结论，不产生任何写操作 |
| 1c 确定性时序处置 | 按时间规则决定对哪些旧记录写入失效字段 | 矛盾对集合、新旧记录的时间字段、当前时刻 | `[{memory_id, invalid_at, superseded_by, invalid_reason}]` | 纯代码纯函数：不调用 LLM、不访问网络、同输入同输出 |

**接口交割顺序**：1a 与 1b 是两次独立的 LLM 调用（两次调用各自的 system prompt 互不共享）。1c 在两次 LLM 调用之后执行，其输入全部来自前两段的产出与 payload 读取。

## 3.2 写入管道编排

`_add_to_vector_store`（sync：`mem0/memory/main.py` L879；async：L2534）的分阶段编排：

| 阶段 | 动作 | 本方案改动 |
| --- | --- | --- |
| Phase 0 | 会话上下文：`get_last_messages` + `parse_messages` | 不变 |
| Phase 1 | 现有记忆检索（top_k=10） | 检索条件加「只取有效事实」；`top_k` 提升为 `BITEMP_CANDIDATE_K`（默认 20），该结果同时作为 1a 的去重参考与 1b 的候选池 |
| Phase 2 | 1a 提取（LLM 调用 #1） | 提示词新增 `valid_at` 产出 |
| Phase 3 | 批量嵌入 | 不变 |
| Phase 4 | hash 去重 | 不变（依赖 Phase 1 已排除失效记录，见 3.3） |
| **Phase 5** | **1b 矛盾检测（LLM 调用 #2）** | 新增。新事实为空、或候选池为空时跳过 |
| Phase 6 | 批量落库 + 写 history | 新记录 payload 增加 `valid_at`（缺省为 `null`） |
| **Phase 7** | **1c 确定性时序处置** | 新增。按处置结果对旧记录做 payload-only 更新 |

## 3.3 检索过滤的位置

有效事实过滤必须加在 **SDK 层 Phase 1 检索**，而不能只加在 REST 读接口上。原因是 Phase 4 的 hash 精确去重以 Phase 1 的检索结果为参照：若 Phase 1 仍取回已失效记录，一条事实在失效后重新出现时，其 `hash` 会与那条已失效记录命中而被跳过落库，系统中不会留下任何有效副本。

因此两处（Phase 1 检索、读接口）使用同一个过滤谓词。

---

# 4. 数据模型设计

## 4.1 payload 字段

新增 4 个字段，全部落在 Qdrant payload 的顶层（与 `data` / `hash` / `created_at` 同级），作为一等字段。

| 字段 | 类型 | 语义 | 写入时机 |
| --- | --- | --- | --- |
| `valid_at` | `YYYY-MM-DD` 字符串 或 `null` | 该事实在真实世界开始为真的日期 | Phase 6 落库时写入（由 1a 产出） |
| `invalid_at` | `YYYY-MM-DD` 字符串 或 `null` | 该事实不再为真的日期；记录在此之后退出默认读 | Phase 7 payload 更新 |
| `superseded_by` | uuid 字符串 或 `null` | 取代该事实的新记录 id | Phase 7，与 `invalid_at` 同源写入 |
| `invalid_reason` | 字符串 或 `null` | 失效原因枚举，当前取值 `superseded_by_newer_fact` | Phase 7 |

字段形态约定：未失效的记录**不写** `invalid_at` 键（键缺失即「未失效」）。`valid_at` 为 `null` 时以入库时间 `created_at` 作为生效起点兜底（见 5.3）。

## 4.2 时序比较键

每条记录定义比较键 `(生效时间, 入库时间)`：

- 生效时间 = `valid_at` 存在则取其值，否则取 `created_at`；
- 入库时间 = `created_at`。

键为全序，用于 1c 的一切时序判定。

---

# 5. 功能模块设计

## 5.1 模块 1a：时间抽取（`mem0/configs/prompts.py`）

### 5.1.1 提示词改动点

（a）「# 输出格式 → ## 字段」节新增一项：

```text
- **valid_at**（字符串或 null）：这条事实在真实世界中**开始为真**的日期，格式 `YYYY-MM-DD`。它不是入库时间。
  - 事实带有明确时间（"2024 年 3 月 5 日签约"）→ 写该日期。
  - 事实带有相对时间（"昨天"、"上周"、"下个月"）→ 按「观察日期」解析为绝对日期后写入。
  - 事实描述的是当前状态（"我现在住在柏林"、"已经从杏仁奶换成燕麦奶"）→ 写观察日期。
  - 事实指向未来（"下个月搬到柏林"、"预计 2026 年 3 月迎来第一个宝宝"）→ 写该未来日期。
  - 事实是持久属性、偏好或无法判定何时开始（"我喜欢喝黑咖啡"、"用户有条叫 Poppy 的狗"）→ 写 null。
  - 不要用「当前日期」填空，不要推测，不要为了填满字段而编造日期。
```

（b）「# 输出格式 → ## 结构」的示例对象同步增加该字段：

```json
{
  "memory": [
    {"id": "0", "text": "第一条提取的记忆", "attributed_to": "user", "linked_memory_ids": ["相关已有记忆的uuid"], "valid_at": "2024-03-05"},
    {"id": "1", "text": "第二条提取的记忆", "attributed_to": "assistant", "valid_at": null}
  ]
}
```

（c）「### 时间锚定」节补一段：

```text
每条记忆都必须带 valid_at。带时间的事实写绝对日期；描述当前状态的事实写观察日期；
无法判定生效起点的持久事实写 null。valid_at 表述的是事实本身何时为真，与这条记忆何时进入系统无关。
```

（d）「# 示例」中示例 1（多主题提取）与示例 11（长多主题对话）的输出补 `valid_at`，作为形态示范：

- 示例 1 的 `"用户名叫 Marcus，在 2025 年 8 月 12 日前后晋升为 Shopify 高级工程师"` → `"valid_at": "2025-08-12"`；`"用户喜欢看网飞上有强叙事性的体育纪录片"` → `"valid_at": null`。
- 示例 11 的 `"用户于 2025 年 3 月 1-2 日前后领养了一只名叫 Max 的比格混血小狗"` → `"valid_at": "2025-03-01"`；`"用户开始上每周二的陶艺课"` → `"valid_at": "2025-03-10"`（当前状态陈述，写观察日期）。

### 5.1.2 缺省语义汇总

| 事实形态 | `valid_at` | 1c 实际使用的生效时间 |
| --- | --- | --- |
| 明确绝对时间 | 该日期 | `valid_at` |
| 相对时间（有新消息锚点） | 按观察日期解析后的日期 | `valid_at` |
| 当前状态陈述 | 观察日期 | `valid_at` |
| 未来事件（"下个月搬到柏林"） | 该未来日期 | `valid_at`（未来时间在 point-in-time 与默认读中均按谓词自然生效） |
| 持久属性 / 偏好 / 无法判定 | `null` | `created_at` |

## 5.2 模块 1b：独立矛盾检测

### 5.2.1 新增常量全文

在 `mem0/configs/prompts.py` 中新增（与 `ADDITIVE_EXTRACTION_PROMPT` 同级）：

```python
CONTRADICTION_DETECTION_PROMPT = """# 角色

你是一名矛盾检测器。你的唯一职责是找出「哪些新事实与哪些已有事实互相矛盾」，并输出这些配对。

你不提取事实，不改写事实，不判断保留哪一条、失效哪一条——处置由下游的确定性代码按时间规则完成，不属于你的职责。

# 什么构成矛盾

一条新事实与一条已有事实构成矛盾，必须同时满足三个条件：

1. **同主体**：两条事实关于同一个主体（同一个人、同一个 AI 助手、同一个具体实体）。
2. **同属性**：两条事实描述该主体的同一个属性或同一个待判定问题（例如"居住城市"这一个属性）。
3. **互斥值**：两条事实给出的取值不能同时为真。取值随时间先后变化，正是矛盾的典型形态，而不是排除矛盾的理由。

三者缺一即不是矛盾。以下情况不是矛盾：

- 不同主体（"用户住在巴黎"与"用户的妹妹住在里昂"）
- 同一主体的不同属性（"用户住在巴黎"与"用户养了一条狗"）
- 同一属性但取值可以并存（"用户喜欢喝咖啡"与"用户喜欢喝茶"）
- 同一属性的不同事件、不同侧面或不同时间点的不同取值之外的信息（"用户周二跑了 5 公里"与"用户周三跑了 3 公里"）
- 一条事实是另一条的细化、补充或蕴含，取值不冲突（"用户有只狗"与"用户的狗叫 Poppy"）
- 语义等价或近等价的同一事实的两种说法——等价属于去重范畴，不是矛盾

# 输入

## 新事实

本次从对话中新提取的事实。格式：

[{"id": "新事实的 uuid", "text": "事实文本", "valid_at": "生效日期或 null"}]

## 已有事实

与本次对话相关的现有事实。格式：

[{"id": "已有事实的 uuid", "text": "事实文本", "valid_at": "生效日期或 null", "created_at": "入库时间"}]

# 输出

只返回可由 json.loads() 解析的有效 JSON。不要任何文本、推理、解释或包装。

{
  "contradictions": [
    {"new_id": "新事实的 uuid", "old_id": "已有事实的 uuid"}
  ]
}

# 规则

- `new_id` 只能取自「新事实」列表中真实存在的 id；`old_id` 只能取自「已有事实」列表中真实存在的 id。绝不虚构或猜测 id。
- 同一个 (new_id, old_id) 配对只输出一次。
- 一条新事实可以与多条已有事实矛盾；一条已有事实也可以被多条新事实矛盾——照实全部输出。
- 没有矛盾时返回：{"contradictions": []}
- 只输出配对，不给出保留或失效的建议。

# 判定纪律

- **拿不准就不输出**。多输出一个错误配对会让一条本来有效的事实被错误失效；漏输出只意味着这条矛盾留待下一次对话处理。
- 时间先后不是排除矛盾的理由，也不是判定矛盾的理由——你只判断取值是否互斥。
"""
```

```python
BITEMP_EPOCH = "1970-01-01T00:00:00Z"
INVALID_REASON_SUPERSEDED = "superseded_by_newer_fact"
BITEMP_CANDIDATE_K = 20
```

### 5.2.2 新增 user prompt 构造函数

在 `mem0/configs/prompts.py` 中新增，风格与既有 `generate_additive_extraction_prompt` 一致（统一 `## 节` 拼接、末尾 `# Output:`）：

```python
def generate_contradiction_detection_prompt(
    new_facts=None,
    existing_facts=None,
):
    """构建矛盾检测的 user prompt。与 CONTRADICTION_DETECTION_PROMPT 成对使用。"""
    sections = []
    sections.append(f"## New Facts\n{_serialize_memories(new_facts)}")
    sections.append(f"## Existing Facts\n{_serialize_memories(existing_facts)}")
    sections.append("# Output:")
    return "\n\n".join(sections)
```

### 5.2.3 调用契约

| 项 | 值 |
| --- | --- |
| 触发条件 | 1a 提取结果非空 **且** 候选池非空 |
| system prompt | `CONTRADICTION_DETECTION_PROMPT`（与 1a 的 `ADDITIVE_EXTRACTION_PROMPT` 互不共享） |
| user prompt | `generate_contradiction_detection_prompt(new_facts=..., existing_facts=...)` |
| `response_format` | `{"type": "json_object"}` |
| 解析失败处理 | 与 1a 一致：`remove_code_blocks` → `json.loads(strict=False)` → `extract_json` 兜底；仍失败则本次不处置任何记录，log error，写入流程继续 |
| LLM 调用异常 | 抛 `LLMError`（与现有 1a 行为一致，交由调用方处理重试） |

新事实传入 1b 前先完成 hash 去重，去重掉的事实不参与矛盾检测。候选池取自 Phase 1 检索结果。

## 5.3 模块 1c：确定性时序处置（`mem0/memory/main.py`）

### 5.3.1 函数签名

模块级纯函数，不依赖实例状态、不访问网络、不调用 LLM：

```python
def _apply_contradictions(
    contradictions: List[Dict[str, Any]],   # [{"new_id": str, "old_id": str}]
    new_records: Dict[str, Dict[str, Any]], # {new_id: {"valid_at": str|None, "created_at": str}}
    old_records: Dict[str, Dict[str, Any]], # {old_id: 该记录的现有 payload}
) -> List[Dict[str, Any]]:                  # [{"memory_id", "invalid_at", "superseded_by", "invalid_reason"}]
```

### 5.3.2 处置规则

| 规则 | 精确语义 |
| --- | --- |
| R1 主体合法性 | `old_id` 必须存在于 `old_records`。不存在 → 丢弃该配对（1b 幻觉 id 的防线），log warning |
| R2 自反 | `new_id == old_id` → 丢弃 |
| R3 时序守卫 | 生效键 `k(new) = (生效时间(new), created_at(new))`，`k(old)` 同理。**仅当 `k(new) > k(old)`**（字典序严格大于）才处置这条旧记录；相等或更小 → 不处置 |
| R4 失效写入 | `invalid_at = 生效时间(new)`；`superseded_by = new_id`；`invalid_reason = "superseded_by_newer_fact"` |
| R5 重复失效 | 旧记录已有 `invalid_at` 时取 **min(已有值, 新写入值)**；当新写入值更早时，`superseded_by` / `invalid_reason` 与该 `invalid_at` 同源覆盖，保证三字段指向同一次取代 |
| R6 多条候选同时矛盾同一旧记录 | 先按 R3 过滤，再按 `k(new)` 取最大者作为唯一处置依据；`k(new)` 完全相等时取 `new_id` 字典序最小者。一条旧记录在同一批次内只处置一次 |
| R7 链式取代 | 只处置直接矛盾对。A←B、B←C 两条独立写入：`A.invalid_at = B.生效时间`、`B.invalid_at = C.生效时间`，`A.superseded_by` 保持为 `B.id`，不递归传播到 C |
| R8 悬空 superseded_by | `superseded_by` 指向的记录日后被删除时，该字段原样保留、不清洗、不报错；读侧把它当标记输出，不做跳转解析 |
| R9 写入方式 | 只对旧记录做 payload-only 更新（Qdrant `set_payload`，即 `vector_store.update(vector_id=..., vector=None, payload={三字段})`）：文本、`hash`、`text_lemmatized`、`created_at`、`updated_at` 全部不变，向量与 BM25 稀疏向量不重算 |
| R10 审计痕迹 | 失效是元数据变更，不写 history 行；payload 三字段本身构成可查询的审计痕迹 |
| R11 空输入 | 矛盾对为空 → 返回空列表，不产生任何写操作 |

### 5.3.3 时间格式与时区

- `valid_at` 一律写 `YYYY-MM-DD`（日期精度）。
- `invalid_at` 一律写 `YYYY-MM-DD`（日期精度）。
- URL / REST 传入的 `as_of` 接受 `YYYY-MM-DD` 或 ISO8601 带时区两种形态；进入过滤谓词前统一规范化为 UTC ISO8601。

## 5.4 模块 1d：检索过滤（`mem0/memory/main.py`）

### 5.4.1 过滤谓词

两处（Phase 1 检索、读接口）共用同一构造函数，产出 mem0 既有 filter DSL：

**有效事实（默认读，T = 当前时刻）**

```python
{"NOT": [{"invalid_at": {"lte": T}}]}
```

**给定时刻 T 的有效事实（point-in-time）**

```python
{"OR": [{"valid_at": {"lte": T}},
        {"AND": [{"created_at": {"lte": T}},
                 {"NOT": [{"valid_at": {"gte": BITEMP_EPOCH}}]}]}],
 "NOT": [{"invalid_at": {"lte": T}}]}
```

语义：生效时间 `<= T`（`valid_at` 缺失时以 `created_at` 兜底），且（未失效 或 失效时间 `> T`）。

两条谓词都已在本机容器内以真实链路验证（`_process_metadata_filters` → `_create_filter` → Qdrant 执行），见第 9 节证据。

### 5.4.2 读接口参数

| 层 | 参数 | 默认 | 语义 |
| --- | --- | --- | --- |
| SDK `Memory.search` / `AsyncMemory.search` | `as_of: Optional[str]` | `None` | 给定时刻 T 的有效事实；`None` 表示当前时刻 |
| 同上 | `include_invalidated: bool` | `False` | `True` 时不加时间过滤，返回全量（含已失效），用于查看完整历史 |
| SDK `Memory.get_all` / `AsyncMemory.get_all` | 同上两个参数 | 同上 | 同上 |
| REST `POST /search` | `as_of` / `include_invalidated` | 同上 | 透传至 SDK |
| REST `GET /memories`、`GET /memories/export` | 无新增参数 | — | 管理面按全量返回（失效状态由四个 payload 字段体现），导出不丢数据 |

`show_expired` 参数与 `invalid_at` 相互独立：前者管 `expiration_date`，后者管事实失效，两者可同时生效。

### 5.4.3 结果字段

检索结果与单条读取结果中，四个 bi-temporal 字段作为**一等字段**出现在顶层（与 `id` / `memory` / `hash` / `created_at` / `updated_at` 同级），**不进入** `metadata`。

## 5.5 模块 1e：REST 序列化与导出（`server/main.py`）

| 位置 | 改动 |
| --- | --- |
| `_RESERVED_PAYLOAD_KEYS`（L404） | 加入 `valid_at` / `invalid_at` / `superseded_by` / `invalid_reason`，使四者不落入 `metadata` |
| `_serialize_memory`（L407） | 显式输出四个字段（与 `hash` / `expiration_date` 同级写法） |
| `SearchRequest`（L216） | 新增 `as_of: Optional[str]`、`include_invalidated: Optional[bool]` |
| `search_memories`（L564） | 透传 `as_of` / `include_invalidated` |
| `EXPORT_CSV_COLUMNS`（L491） | 追加 `valid_at` / `invalid_at` / `superseded_by` / `invalid_reason` 四列 |

## 5.6 模块 1f：一等字段清单收敛（`mem0/memory/main.py`）

`promoted_payload_keys` 目前有 **6 处完全重复的字面块**（L1225 / L1343 / L1691 / L2883 / L3001 / L3355，sync 与 async 各 3 处）。本方案把该字面块抽为模块级常量 `BI_TEMPORAL_PAYLOAD_KEYS` 并加入 4 个新字段，6 处引用同一常量。

理由有二：一是新字段只需在一处声明，避免漏改导致某条读路径把 bi-temporal 字段挤进 `metadata`；二是上游同步时冲突点从 6 处收敛到 1 处。

---

# 6. Qdrant payload 索引方案

| 字段 | schema | 说明 |
| --- | --- | --- |
| `invalid_at` | `datetime` | 默认读与 point-in-time 的 `Range` 过滤依赖 |
| `valid_at` | `datetime` | point-in-time 的 `Range` 过滤依赖 |
| `created_at` | `datetime` | 已存在于 `memories_2048`；补充为本函数声明字段，使新建集合自带（游标翻页 `order_by` 亦依赖） |
| `user_id` / `agent_id` / `run_id` / `actor_id` | `keyword` | 维持现状不变 |

实现位置：`mem0/vector_stores/qdrant.py` 的 `_create_filter_indexes()`（L166）。该函数当前对 `common_fields` 统一使用 `field_schema="keyword"`，需改为按字段声明 schema（datetime 字段与 keyword 字段分组创建）。`is_local` 分支保持原样跳过。

**存量集合的索引生效路径**：`create_col()`（L128）在集合已存在的分支中会调用 `_create_filter_indexes()`（L151），因此本改动只需 `docker compose restart mem0` 即在新进程启动时对既有 `memories_2048` 补齐索引，无需手工执行 Qdrant 管理命令。

---

# 7. 存量记录兼容性结论

**结论：存量记录不需要回填。**

存量口径：本方案成稿时实测 `memories_2048` 共 **3510 条**（2026-09-17 12:26 CST，HEAD `74191115`）。任务下达时的快照为 3474 条，两者之差是这期间正常写入所致；计数会随日常使用继续增长，本结论与具体条数无关。

依据（三项均为本机实测）：

1. **默认读的谓词对缺失字段天然放行**。默认读为 `NOT[{"invalid_at": {"lte": T}}]`；Qdrant 的 `Range` 条件不匹配字段缺失的记录，取反后缺失字段的记录被保留。实测：对 `memories_2048` 执行该谓词，count = 3510，与集合总数相等；`must_not is_empty invalid_at` 的 count = 0，即现存记录全部表现为「未失效」。
2. **point-in-time 的生效起点有兜底**。谓词中 `valid_at` 缺失时改判 `created_at <= T`，而现存 3510 条全部具备 `created_at`（实测 `must_not is_empty created_at` = 3510）。因此存量在「其入库时刻之后」的任何 T 上都判定为有效。
3. **`created_at` 是存量记录唯一真实可信的生效时间证据**。为存量写 `valid_at` 只能凭推测，与提取阶段的「不虚构」原则相冲突；写入推测值会让 point-in-time 查询返回伪造的时间结论。

后续行为：新写入的事实一律携带 `valid_at`（可能为 `null`）；存量的 `valid_at` 保持缺失，由 `created_at` 兜底。两条路径共用同一谓词，无需区分。

（可选、非必需）若日后需要为存量补齐 `valid_at`，可在确认口径后单独出脚本，把 `valid_at` 置为 `created_at` 的日期部分；届时谓词的兜底分支自然退化为恒不命中。

---

# 8. 与上游 mem0 的冲突面（rebase 代价）

现状：本地 `main` 相对 `upstream/main` 领先 9 个提交，累计 23 个文件（`git diff --stat upstream/main..HEAD`）。本地改动以提交区间承载，`patches/mem0-local.patch` 由 `bash patches/generate-patch.sh` 从 `upstream/main..HEAD` 导出。

本方案触及的文件与冲突面：

| 文件 | 本地既有改动（相对 upstream） | 本方案新增改动 | 冲突风险 |
| --- | --- | --- | --- |
| `mem0/configs/prompts.py` | +306 / -331（中文化 + ADD-only 改造），是本地差异最大的文件 | 改 `ADDITIVE_EXTRACTION_PROMPT`；新增 `CONTRADICTION_DETECTION_PROMPT` 与构造函数 | **高**。上游对提取提示词与 `generate_additive_extraction_prompt` 的任何改动都会与本地中文版冲突 |
| `mem0/memory/main.py` | +6 / -5 | `_add_to_vector_store`（sync L879 / async L2534）插入 Phase 5/7、Phase 1 过滤；检索与 `get_all` 增参；6 处字面块收敛为常量 | **高**。该函数是上游 V3 分阶段管道的核心，上游改动频繁 |
| `mem0/vector_stores/qdrant.py` | +28 / -2（游标分页） | `_create_filter_indexes` 按字段声明 schema | 中。函数本身未被本地改过 |
| `server/main.py` | +128 / -16（分页 / 导出 / 独立 provider 配置） | `_RESERVED_PAYLOAD_KEYS`、`_serialize_memory`、`SearchRequest`、`EXPORT_CSV_COLUMNS` | 中。四处均为小改，但都在本地已改过的文件内 |
| `tests/memory/` | 无 | 新增 `test_bitemporal.py`；既有断言随参数签名同步 | 低 |

每次同步上游预计需人工解冲突的文件数 ≥ 4。本方案通过 5.6 的常量收敛把 `main.py` 的重复冲突点从 6 处降到 1 处，是本次顺带降低 rebase 成本的一项改动。

实现完成后需重跑 `bash patches/generate-patch.sh` 刷新本地补丁。

---


# 10. 验收标准

判定一律以**实现后的实际执行输出**为准，与具体 commit 对照，不认「工作区当下状态」。

## 10.1 三段式职责分离

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-1] | 1a 产出 `valid_at`：输入含明确日期的事实（如「2024-03-05 我签了租房合同」）时，落库记录的 `valid_at` 日期部分等于 `2024-03-05` | 隔离 user_id `POST /memories` → 读该记录 payload |
| [AC-2] | 无明确时间的事实 `valid_at` 为 `null`：输入「我喜欢喝黑咖啡」（无任何时间指代）时，`valid_at` 缺失或为 `None` | 同上 |
| [AC-3] | 1a 与 1b 是两次独立 LLM 调用且提示词互不共享 | 单测：mock LLM，断言一次 add 触发 `generate_response` 调用 2 次，第 2 次 `messages[0]["content"] == CONTRADICTION_DETECTION_PROMPT`，且第 2 次调用内容 != 第 1 次 |
| [AC-4] | 1c 是确定性代码：同输入 → 同输出，且不接触 LLM | 单测：`_apply_contradictions` 同一入参调用两次，返回结果 `json.dumps(sort_keys=True)` 相等；函数签名不含任何 llm/client 参数 |
| [AC-5] | 1b 的 LLM 不可用或返回不可解析内容时，写入流程不中断、不产生任何失效写入 | 单测：mock LLM 第 2 次调用抛异常/返回垃圾串，断言 `add()` 正常返回、目标记录 `invalid_at` 仍为空 |

## 10.2 演化场景（端到端，真实 HTTP）

统一场景：隔离 user_id `test_bt_<时间戳>`，依次 `POST /memories` 两条：

1. `"2020-01-01 起我住在巴黎"`
2. `"2021-01-01 我搬到柏林了"`

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-6] | 旧记忆获得 `invalid_at` 与 `superseded_by`：旧记录 `invalid_at` 非空且等于新记录生效时间；`superseded_by` 等于新记录 id；`invalid_reason` 等于 `superseded_by_newer_fact` | `GET /memories/{旧id}` |
| [AC-7] | **文本未被改写、记录仍存在**：旧记录 `data` 与该记录写入后立即读取的原文逐字节相等；旧 id 仍可被 `GET /memories/{id}` 取到 | 对比两次读取的 `data` 与 `hash` |
| [AC-8] | 新记忆独立存在且未失效：新 id 可取到，其 `invalid_at` 缺失 | `GET /memories/{新id}` |
| [AC-9] | `POST /search` 默认不返回已失效事实：搜索「我住在哪里」的结果 id 集合不含旧 id、含新 id | `POST /search`（默认参数） |

## 10.3 point-in-time 与全量读

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-10] | `as_of` 落在两个生效时间之间时，返回旧记录、不返回新记录 | `POST /search` 带 `as_of="2020-06-01"` |
| [AC-11] | `as_of` 晚于新记录生效时间时，返回新记录、不返回旧记录 | `POST /search` 带 `as_of="2022-01-01"` |
| [AC-12] | `include_invalidated=true` 返回全量（新旧都在） | `POST /search` 带 `include_invalidated=true` |

## 10.4 处置规则边界（单测 `_apply_contradictions`）

| 编号 | 验收内容 |
| --- | --- |
| [AC-13] | 多条候选同时矛盾同一旧记录：恰好产生 1 条更新，`invalid_at` 取生效键最大的那条新事实的生效时间，`superseded_by` 与 `invalid_at` 同源；以同一输入重复调用不产生第二次更新 |
| [AC-14] | 链式取代：A←B、B←C 两条独立处置，`A.invalid_at = B.生效时间`、`B.invalid_at = C.生效时间`、`A.superseded_by` 保持为 B，不被改写为 C |
| [AC-15] | 重复失效取最早：旧记录已有 `invalid_at = 2022-06-01`，新对给出 2021-01-01 → 结果 `invalid_at = 2021-01-01` 且 `superseded_by` 同步为新事实 id；反向（已有 2021-01-01，新给 2022-06-01）→ 三字段保持不变 |
| [AC-16] | 时序守卫：新事实生效键不大于旧记录生效键时不处置（含两者完全相等的用例） |
| [AC-17] | 悬空 `superseded_by`：删除被指向的记录后，旧记录的 `superseded_by` 原样保留，读取不报错 |
| [AC-18] | 幻觉 id 防线：`old_id` 不在候选集合内时丢弃该配对并记 warning，不抛异常、不产生写操作 |

## 10.5 序列化、索引与存量兼容

| 编号 | 验收内容 | 判定方式 |
| --- | --- | --- |
| [AC-19] | 四个 bi-temporal 字段是一等字段：`GET /memories/{id}` 与 `POST /search` 结果的顶层含四字段，且 `metadata` 不含其中任何一个 | 读取响应 JSON，检查键集合 |
| [AC-20] | 导出不污染 metadata：`GET /memories/export?format=json` 中每条记录的 `metadata` 不含四字段；`format=csv` 的表头含四列 | 导出文件检查 |
| [AC-21] | Qdrant payload 索引已建立：`GET /collections/memories_2048` 的 `payload_schema` 含 `invalid_at: datetime` 与 `valid_at: datetime` | 直接查 Qdrant REST |
| [AC-22] | 存量记录不受影响：改动前后对同一组固定 query（≥3 条，`top_k=20`）执行 `POST /search`，返回的 id 列表与顺序完全一致；`GET /memories?limit=1` 的 `total` 与改动前基线相同 | 改动前后各跑一次并 diff（基线见第 9 节证据 7） |
| [AC-23] | 缺失 / 显式 null / 已赋值三种形态语义一致：构造三条记录分别「未失效」「`invalid_at` 为 null」「`invalid_at` 已赋值」，默认读返回前两条、不返回第三条 | 隔离 user_id 端到端验证 |
| [AC-24] | 索引建立后查询不退化：对 `memories_2048` 连续 5 次执行「默认读谓词」的 `count`，每次 `time` < 0.05s，且 5 次 `count` 结果相同（等于集合当前总数） | Qdrant `POST /collections/memories_2048/points/count` 返回的 `time` 字段 |

## 10.6 验证纪律

| 编号 | 验收内容 |
| --- | --- |
| [AC-25] | 全部端到端验证使用隔离 user_id（`test_bt_*` 前缀）；验证完毕后 `DELETE /memories?user_id=test_bt_*` 清理，`GET /memories?limit=1` 的 `total` 回到基线值；主记忆集 `xue` 的 `POST /search` 结果与第 9 节证据 7 的基线一致 |
| [AC-26] | 存量 3510 条真实记忆全程无删除、无批量改写操作 |
| [AC-27] | `make lint`（ruff，line length 120）对 `mem0/`、`server/` 改动零告警 |
| [AC-28] | 实现完成后 `bash patches/generate-patch.sh` 成功刷新 `patches/mem0-local.patch`，且该 patch 覆盖本次全部改动文件 |

---


# 12. 附录

## 附录 A：术语表

| 术语 | 含义 |
| --- | --- |
| bi-temporal | 事实模型同时记录「事实在真实世界何时为真」（`valid_at` / `invalid_at`）与「记录何时进入系统」（`created_at`） |
| 1a / 1b / 1c | 本方案的三段式：时间抽取 / 独立矛盾检测 / 确定性时序处置 |
| 生效键 | `(生效时间, 入库时间)` 组成的全序比较键 |
| point-in-time | 给定时刻 T，返回当时为真的那组事实 |
| 有效事实 | `invalid_at` 缺失，或 `invalid_at > T` 的事实 |

## 附录 B：参考文献

| 来源 | 用途 |
| --- | --- |
| Graphiti / Zep：`graphiti_core/prompts/dedupe_edges.py` 的 `EdgeDuplicate`（只返回 `contradicted_facts`） | 1b「只报矛盾对」的形态参照 |
| Graphiti：`resolve_edge_contradictions` / `resolve_duplicate_invalidations`（候选边更早生效则不退役；每个事件只允许一次失效，取最早失效时间） | 1c 的 R3 / R5 规则参照 |
| graphiti issue #1728：纯时间规则无法判断「两条事实是否同一命题」 | 1b 提示词显式约束「同主体 + 同属性 + 互斥值」三条件的依据 |

