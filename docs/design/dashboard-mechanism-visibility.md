# dashboard 机制可视化设计（mem0 自托管：bi-temporal / 衰减 / Dream / Graph 的可见面）

| 项 | 值 |
| --- | --- |
| 目标仓库 | `/Users/xuelingkang/Documents/Containers/mem0`（`main`） |
| 基线 commit | `737d10e0`（graph-memory 收口后），`upstream/main` = `0df3e4b8` |
| 依赖机制 | bi-temporal 事实模型、Ebbinghaus 衰减 + 访问强化、Dream 后台整合、Graphiti 旁路图检索——四项均已实现并通过独立核验 |
| 前置文档 | `docs/design/bitemporal-fact-model.md`、`docs/design/memory-decay.md`、`docs/design/memory-dream.md`、`docs/design/graph-memory.md` |
| 生效面 | `server/dashboard/`（Next.js 前端）；另含两个**只读**后端端点补充 |
| 影响面 | dashboard 新增 2 个页面、memories 页新增列与详情区、检索模式新增两个开关；`server/main.py` / `server/routers/dream.py` / `server/routers/graph.py` 各新增一个只读端点 |
| 实现形态 | 仅本地改动（相对 `upstream/main`），不提交上游 PR |
| 本次实测 | 见 `docs/design/dashboard-mechanism-visibility-evidence/`（6 组探针 + 原始输出） |

> 本文档为本地设计文档，位于仓库 `docs/design/`，与 Mintlify 文档站（`docs/**/*.mdx`）无关，不参与 `llms.txt` 收录检查。

---

# 1. 项目概述

## 1.1 背景

四项记忆机制已全部实现并通过独立核验，但它们的作用面全部在数据里，dashboard 上一处也读不到：

- 事实是否已失效、被谁取代、为什么失效（bi-temporal）；
- 一条记忆被召回多少次、上次什么时候被召回（衰减的输入）；
- 检索结果为什么这样排序、图分支这次答没答上来（融合与降级）；
- Dream 什么时候跑过、产出多少观察、观察的证据链是什么；
- 图派发了多少、成功多少、图里有几个节点几条边。

前端对 `invalid_at` / `valid_at` / `decay_weight` / `retention` / `graph_boost` / `score_details` / `memory_kind` / `observation` 的引用为 **0 处**（§2.1），memories 页浏览模式只有 Content / User / Agent / Created 四列，检索模式多一列 Score。

## 1.2 目标

1. 四项机制在 dashboard 上**可看见**：每条机制至少有一个明确的展示位置与一条可核对的读数。
2. 可查：从一条记忆能查到它的失效原因与后继、它的召回足迹、它派生的观察，并从观察查到它的源事实。
3. 可验证：页面上的每个机制字段都能与后端实际返回值逐条对上（渲染不改变语义、不做二次推断）。
4. 不破坏既有行为：既有 8 个页面的既有功能、memories 列表的游标翻页、三项特性开关的默认值全部不动。

## 1.3 范围

**包含**

- `server/dashboard/src/`：memories 页（列表列 + 详情区 + 检索模式）、两个新增页面（Dream / Graph）、与之配套的类型定义、端点常量、API 封装。
- `server/main.py` / `server/routers/dream.py` / `server/routers/graph.py`：**两个只读端点补充**（§6，卡片允许提出必要补充，逐条论证）。

**非目标（明确不做）**

- 不写记忆数据：不提供「一键实跑 Dream」（`POST /dream/run`）、不提供「按作用域删除图」（图桥 `DELETE /graph/{group_id}`）、不提供任何删除/失效/覆盖入口。本次只做可见性，不做控制面。
- 不改写检索语义：不动打分、不动时间因子、不动图加分、不改 `decay`/`graph`/`dream` 开关。
- 不改既有端点的对外契约：不新增 query 参数到 `GET /memories`、不改既有响应字段语义（§6.4）。
- 不做跨全库的离线统计视图（如「全库衰减分布」）：那需要全量扫描 payload 并离线算 `decay_weight`（`memory-decay.md` §8 定其为离线脚本），不是前端能拿到的读数。

---

# 2. 现状清点（本次实测）

## 2.1 前端零接入

`server/dashboard/src/` 全目录检索下列标识的命中数为 0：

| 标识 | 命中 | 标识 | 命中 |
| --- | --- | --- | --- |
| `invalid_at` | 0 | `valid_at` | 0 |
| `superseded_by` | 0 | `access_count` | 0 |
| `last_accessed` | 0 | `decay_weight` | 0 |
| `retention` | 0 | `graph_boost` | 0 |
| `score_details` | 0 | `graph_status` | 0 |
| `memory_kind` | 0 | `observation` | 0 |
| `evidence_count` | 0 | `dream` | 0 |

memories 页（`app/(root)/dashboard/memories/page.tsx`）的列定义为 Content / （检索模式下）Score / User / Agent / Created；行点击打开右侧 Sheet（Content / ID / User / Agent / Created / 删除按钮）；`types/api.ts` 的 `Memory` 接口只有 7 个字段。

## 2.2 后端数据面（实测，全部只读）

| 端点 | 响应形状 | 与机制相关的字段 | 实测要点 |
| --- | --- | --- | --- |
| `GET /memories`（无作用域，管理员） | `{results, next_cursor, total}` | `valid_at` `invalid_at` `superseded_by` `invalid_reason` `access_count` `last_accessed` `memory_kind` `observation_key` `source_memory_ids` `evidence_count` `dream_run_id` | 最新优先；游标 keyset；`total` 精确（实测 4043）；单页上限 1000；**含已失效条目**（第 1 页 7 条）与观察（第 1 页 79 条） |
| `GET /memories?user_id=&agent_id=&top_k=` | `{results}` | 同上（无 `run_id` / `expiration_date`） | **无 `next_cursor` / `total`**；`top_k` 上限 1000；按设计 §5.4.1 过滤已失效——实测 `user_id=xue` 前 1000 行 0 条已失效；`include_observations=true` 被服务端固定写入，观察照常返回（79 条） |
| `POST /search` | `{results}` | 上述全部 + `graph_status` + `score_details` | `score_details` 仅在 `explain=true` 时出现；`graph_status` 在**同一响应内恒定**（20/20 同值）；`include_observations` 默认 false，置 true 才纳入观察；`include_invalidated=true` 返回已失效条目 |
| `GET /graph/stats` | 扁平对象 | `enabled` `timeout_seconds` `circuit_open` `consecutive_failures` `queue_size` `graph_dispatched` `graph_synced` `graph_already_synced` `graph_failed` `graph_dropped` | **进程内累计**：新建实例后从 0 起（实测两个实例分别读到 63/51/6 与 15/15/0）；同一实例 5 秒内读数不变 |
| `GET /dream/runs`、`GET /dream/runs/{id}` | `{total, limit, offset, results}` | `mode` `status` `started_at` `finished_at` `scopes` `clusters` `llm_calls` `failed_clusters` `observations_written` `observations_superseded` `prompt_tokens` `completion_tokens` `duration_seconds` `report_path` | dry-run **不落行**（实测预览前后 `total` 都是 1） |
| `POST /dream/preview` | 完整报告 | `candidates[]`（`observation_key` `members` `text` `source_memory_ids` `evidence_count` `counterexample` `decision` `skip_reason` `would_write`）、`supersede_preview[]`、`would_write[]`、`errors[]`、`totals{...clusters_skipped, clusters_deferred}` | 零写入实测：前后 Qdrant 点数 4043→4043、`dream_runs` 1→1；产物只有 `history/dream-reports/<run_id>.json`，内容与响应体逐字段相等；本轮 4.2 秒（全部簇已判定 ⇒ `llm_calls=0`、`clusters_skipped=104`） |
| `GET /memories/{id}/observations` | `{memory_id, total, results}` | 每行只有 `id` `memory_kind` `observation_key` `source_memory_ids` `evidence_count` `dream_run_id` | **无正文（`memory`）与作用域字段**——反向追溯可定位，但不足以直接渲染观察内容 |
| `GET /memories/{id}/sources` | `{observation_id, total, missing, results}` | 每行是完整序列化记忆行（与 `GET /memories` 同口径） | 非观察 id 返回 400；`missing` 统计源事实缺失数 |
| `GET /memories/{id}` | 完整行 | 事实与观察同口径；观察另有 `memory_kind` `observation_key` `source_memory_ids` `evidence_count` `dream_run_id` | 观察亦可直接取到全量 |
| 图桥 `GET /stats?group_id=`、`GET /graphs` | `{group_id, episodes, entity_nodes, entity_edges}` / `{graphs: [...]}` | 图规模与图键清单 | mem0 侧**未代理**；图桥未映射宿主端口（compose 中无 `ports`）；`GET /stats` 对未知键**读即建键**（§2.4 / F-6） |

## 2.3 既有设计语言资产（复用面）

| 资产 | 位置 | 复用方式 |
| --- | --- | --- |
| `DataTable` | `components/shared/data-table.tsx` | 列定义（`key` / `label` / `width` / `render` / `align`）、`getRowKey`、`getRowClassName`、`onRowClick`；宽度按权重分配 |
| `Sheet` | `components/ui/sheet.tsx` | 右侧详情抽屉（memories 详情既有形态） |
| `Card` / `Badge` / `Button` / `Tooltip` / `HoverCard` / `Dialog` | `components/ui/*` | 卡片容器、状态徽章、按钮、悬浮与弹窗（`Dialog` 的既有消费者是 `delete-confirmation-modal`） |
| `Switch` | `components/ui/switch.tsx` | 组件库已有、既有页面尚未使用（`checkbox` / `slider` 同为未使用的同族组件）；本次是它的首次使用，视觉来自同一生成套件 |
| `EmptyState` / `TableSkeleton` | `components/self-hosted/empty-state.tsx`、`components/shared/table-skeleton.tsx` | 空态与加载态（既有约定） |
| `useApiQuery` | `hooks/use-api-query.ts` | 取数 + 加载态 + 错误 toast |
| `toast` / `getErrorMessage` | `components/ui/use-toast.ts`、`lib/error-message.ts` | 错误提示与 `detail` 文案提取 |
| 统计卡排版（含图表范例） | `app/(root)/dashboard/analytics/page.tsx`（recharts） | 数字卡片的排版（`grid md:grid-cols-4` + `Card`）直接沿用；本次不新增图表依赖 |
| 配色 token | `styles/globals.css` + `tailwind.config.ts` | `memBorder-primary`、`onSurface-default-{primary,secondary,tertiary}`、`surface-default-{primary,secondary,tertiary,fg-secondary}`、`memPurple-*` / `memGreen-*` / `memGold-*` / `memBlue-*` / `memRed-*`、`onSurface-{danger,positive}-primary` |
| 导航 | `app/(root)/dashboard/components/main-nav.tsx` | 分组 `ACTIVITY` / `ACCOUNT` 的条目写法（title / url / icon / active） |

## 2.4 关键实测发现（结论均来自本次探针）

| 编号 | 发现 | 对设计的影响 |
| --- | --- | --- |
| F-1 | 管理面列表（无作用域）**含**已失效条目与观察，且有 `total` 与游标；作用域列表**不含**已失效条目（时间过滤）且无游标/总数 | 失效状态与观察的列表展示落在默认浏览模式（管理面路径）；作用域列表不做失效呈现 |
| F-2 | `POST /search` 的 `graph_status` 在同一响应内恒定；`score_details` 仅在 `explain=true` 时出现；加不加 `explain` 返回的 id 序与分值口径一致（重复调用分值有 ~5e-5 的末位抖动，id 序稳定） | `graph_status` 每次检索展示**一条**汇总，不做逐行徽章；检索恒定带 `explain=true` |
| F-3 | `include_observations` 与 `include_invalidated` 默认 false，正是「为什么我在检索里看不到观察 / 看不到已失效条目」的答案 | 检索模式落两个开关，直接对应这两个既有参数 |
| F-4 | 全库 4043 点中：已失效 7、观察 79（`xue:ying` 74 / `xue:shi` 5，全部来自同一次 Dream 运行）、有召回足迹（`access_count` 非 0）923、`valid_at` 非空 476 | 规模小，页面无需虚拟滚动；观察清单可一次取回 |
| F-5 | `GET /graph/stats` 的计数归当前进程实例（两个实例读到 63/51/6 与 15/15/0）；`enabled` / `timeout_seconds` / 熔断状态同响应可读 | 计数区必须标注「自本进程启动以来」，否则「重启归零」会被读成数据丢失 |
| F-6 | 图桥 `GET /stats?group_id=<不存在的键>` **会新建一个空图键**（实测读数后 `/graphs` 从 `[default_db, mem0__]` 变为 `[default_db, mem0__, mem0_dashboard_probe_key]`）；图桥未映射宿主端口，宿主只能经 compose 网络或 `exec` 访问 | 图规模必须由 mem0 侧代理，且代理**只对清单内的键**取规模（§6.3）；探针键已在同一探针内删除并核对键集合复原 |
| F-7 | 观察清单的两条取数路径成本：逐页拉全量再前端过滤 = 6 次请求 / 4047 行 / 4.77 MB；服务端按 `memory_kind` 索引过滤 = 1 次请求 / 79 行 / 6 毫秒（`memory_kind` keyword 索引已在 Qdrant 就绪） | 观察清单走服务端过滤（§6.2） |
| F-8 | 反向追溯端点返回的观察行**无正文与作用域字段**，仅 6 个字段 | 观察清单不能由反向端点拼出；详情里的「派生观察」区只需定位与跳转 |
| F-9 | 观察在最新优先序中的位置实测分布于 44–122（前 1000 行内 79 条齐全），已失效条目位置 128–527 | 列表浏览能遇到机制条目；但位置随写入量漂移 ⇒ 不能把「列表里看得见」当作清单的保证（§6.2 的论证依据） |
| F-10 | 图键由 `user_id` 派生：`{user_id: xue, agent_id: ying}` → `mem0_xue`、`{user_id: "*"}` → `mem0__`、`{agent_id: ying}` → `mem0_ying`；现网图键为 `default_db` `mem0__` `mem0_xue`，其中 `mem0__`（前端检索模式的通配作用域）为空图 | 图规模按**图键**呈现，不造「按 agent」的伪维度；检索模式下 `graph_boost=0` 必须与图键、`graph_facts` 同屏，才能区分「图里确实没有这条」与「图没答上来」 |

---

# 3. 信息架构

## 3.1 归属判断

四项机制的作用面不同，落到不同的位置——判断依据是「该机制的读数从哪来、回答什么问题」，不是「实现代码有几个文件」。

| 机制 | 展示位置 | 取舍理由 |
| --- | --- | --- |
| bi-temporal（有效/已失效） | **并入 memories 页**（列表列 + 详情区 + 检索模式开关） | 它是**每条记录自身的属性**，不产生独立的运行或状态面。已失效条目与有效条目同在一个集合里，脱离记录列表没有可展示的对象；新开一页只会得到一张与 memories 重复的列表。 |
| 衰减 + 访问强化 | **并入 memories 页**（列表列 + 详情区 + 检索分项） | 同上：`access_count` / `last_accessed` 是记录属性。其**唯一**能解释的现象是「检索排序为什么降了」，而那必须与 `score_details` 同屏（检索模式），单独一页既无数据也无语境。 |
| Dream 后台整合 | **新增页 `/dashboard/dream`** | 它有自己的**运行面**（`dream_runs` 表、运行报告）与**产物面**（观察条目 + 证据链），两者都不是「某条记忆的属性」。且观察清单只能经服务端过滤取得（§6.2），需要一个承载分页清单的页面。 |
| Graph 旁路图检索 | **新增页 `/dashboard/graph`** | 读数来自**两个不同的存储**（mem0 进程内计数 + FalkorDB 图规模）与服务侧代理，与记录列表、与 Dream 运行都无归属关系。图规模的单位是「图键」，不是记忆，放进 memories 页无处安放。 |

## 3.2 导航结构

在既有两个分组之外新增一个分组，两个条目各占一页：

```
ACTIVITY   Requests / Memories / Entities / Analytics / Export        （既有，不动）
MECHANISMS Dream / Graph                                              （新增分组）
ACCOUNT    API Keys / Configuration / Settings                        （既有，不动）
```

- 分组名 `MECHANISMS` 与既有的大写分组标签（`ACTIVITY` / `ACCOUNT`）同形。
- 图标沿用 `lucide-react`（已装 `0.542.0`）：Dream 用 `Sparkles`，Graph 用 `Waypoints`——与既有 `Activity` / `GalleryVerticalEnd` / `Users` / `ChartLine` / `FolderInput` 的「单色线条图标」风格一致。
- 两个页面用**平级路由**而非一层 Tabs：两页的数据源、刷新节奏与读法互不相关（一个读 Postgres 的运行审计 + 一个写读的预览动作，一个读进程内计数 + 图规模代理），共享一条 URL 只会让「刷新这一页」的语义变得含糊。

## 3.3 被否方案

| 方案 | 否决理由 |
| --- | --- |
| 四项机制各开一页（4 个新页面） | bi-temporal 与衰减的展示对象与 memories 页**完全相同**（同一条记录），单开页会得到两张重复列表，且让用户在两处核对同一字段。 |
| 全部并入 memories 页 | Dream 的运行面（什么时候跑、跑多久、多少簇被跳过）不是记录属性；图规模的单位是图键。硬塞进 memories 会让该页同时承担「记录浏览」与「子系统运行审计」两种读法，且既有页已经从 375 行的浏览/检索/详情/删除四件事。 |
| 四项机制合成一页 + Tabs | Tabs 内的各页若要各自分页、各自刷新，就会在同一个路由下维护多套互不相干的取数状态；Dream 页还要承载观察清单与预览动作，塞进 tab 后「刷新」无法表达刷新的是哪一部分。 |
| 图规模由 dashboard 服务端直连图桥 | 图桥无鉴权且未对外映射端口，dashboard 直连等于把无鉴权的内部图服务变成第二个可被前端触达的面；图桥 `GET /stats` 对未知键**读即建键**（F-6），前端试探键会制造垃圾图键；UI 还会耦合图桥的私有 API 形状。改由 mem0 侧代理（§6.3）。 |
| 衰减单独做「全库衰减分布」图表 | 需要全量扫描 payload 并离线计算 `decay_weight`（`memory-decay.md` §8 已定为离线脚本），前端拿不到这份读数；做一个只有形状没有数据的图，比不做更坏。 |
| 在 `GET /memories` 上新增 `memory_kind` / `validity` 过滤参数 | 该端点有两条既有语义：无作用域是「raw 全量」，有作用域是「作用域内有效事实且默认排除观察」（`memory-dream.md` §5.5）。再加参数会让「默认排除观察」这一既有口径可被翻转，语义面变宽；观察清单另开端点更窄（§6.2）。 |

---

# 4. 页面与组件方案

## 4.1 memories 页 —— 列表

**展示什么**

| 列 | 内容 | 依据字段 | 备注 |
| --- | --- | --- | --- |
| Content | 既有 | `memory` | 不动 |
| Score | 既有（仅检索模式） | `score` | 不动 |
| **Status**（新增） | 「有效」/「已失效」 | `invalid_at` 为空 / 非空 | 已失效行另附「被取代」标记（`superseded_by` 非空时） |
| **Accesses**（新增） | 召回次数（`--` 表示从未被召回） | `access_count` | 该机制在列表上的证据；详情里给完整足迹 |
| User / Agent / Created | 既有 | `user_id` / `agent_id` / `created_at` | 不动 |
| （行内标记） | 观察条目标一个 `observation` 徽章 | `memory_kind` | 观察已经出现在该列表里（实测第 1 页 79 条），不标识会让用户把合成信念当成用户事实 |

**为什么这样取舍**

- 新增列全部是 `DataTable` 的列定义追加，不改既有列的 `key` / `label` / `render`，也不动 `getRowKey` / `onRowClick` 语义 ⇒ 行点击打开详情、删除入口、游标翻页三条既有链路完全不受影响。
- **Status 列做在默认浏览模式**，因为只有管理面列表返回已失效条目（F-1）；作用域列表本来就不返回它们，加列也不会亮。
- **不给列表加「仅显示有效」过滤**：管理面列表是游标驱动的，客户端过滤会让「1–10 of 4043」这个既有计数失效（显示的是已加载缓冲区里的一部分），而把过滤放服务端又要动 `GET /memories` 的契约（§3.3）。需要定向查看已失效条目时走检索模式的开关（§4.3）——那是一个既有的服务端语义。
- `Accesses` 放在列表而非只放详情：`memory-decay.md` 的作用面是**检索排序**，用户是在看到排序结果时才产生「为什么这条降了」的问题；列表上的一栏数字是最短的解释路径，详情给的是完整推导。

**复用的组件**：`DataTable`（列定义）、`Card`（表格容器，既有写法 `border-memBorder-primary`）、`Badge`（`observation` 徽章，既有 `variant="outline"`）、`TableSkeleton`、`EmptyState`。新增徽章样式只用既有 token（观察用 `bg-memPurple-…`，已失效用 `onSurface-default-tertiary` 弱化，被取代用 `onSurface-danger-primary`）。

## 4.2 memories 页 —— 详情（右侧 Sheet）

在既有 Sheet 内按「记录属性 / 机制属性 / 关联」三段扩展，既有字段（Content / ID / User / Agent / Created / 删除）位置与行为不动。

| 段 | 展示项 | 来源字段 | 呈现方式 |
| --- | --- | --- | --- |
| bi-temporal | 生效时刻 / 失效时刻 / 失效原因 / 被谁取代 | `valid_at` `invalid_at` `invalid_reason` `superseded_by` | 定义列表；`superseded_by` 可点击跳到被取代方（点击时按 id 取该条，沿用同一个 Sheet） |
| 衰减足迹 | 召回次数 / 上次召回 | `access_count` `last_accessed` | 定义列表；从未召回时显示 `--` 并附一句说明（「从未被检索返回过」） |
| 观察身份（仅观察） | 观察键 / 证据条数 / 所属运行 / 源事实数 | `observation_key` `evidence_count` `dream_run_id` `source_memory_ids` | 定义列表；`dream_run_id` 链接到 `/dashboard/dream` |
| 派生观察（仅非观察） | 由该记忆派生的观察 | `GET /memories/{id}/observations` | 列表；每行显示观察键 + 证据条数 + 所属运行，点击时按 id 取该观察的全量并以同一个 Sheet 打开 |
| 源事实（仅观察） | 该观察的全部源事实 | `GET /memories/{id}/sources` | 列表；`total` 与 `missing` 都显示（缺失源事实按端点口径**原样呈现为缺失数**，不做清洗） |

**为什么这样取舍**

- 双向追溯是既有端点的现成能力（`memory-dream.md` §5.9），详情是最自然的入口：用户从一条事实出发，能沿着「事实 → 派生观察 → 该观察的源事实」走完一个闭环，而不必先知道观察 id。
- 「派生观察」区的行**不含正文**（F-8），因此不做就地展开，只做定位与跳转；跳转时取一次全量（实测 `GET /memories/{id}` 20–50 ms），避免为列表逐行发请求。
- Sheet 内不做写入类操作（除既有的删除按钮）：可见性不引入新的写路径。

**复用的组件**：`Sheet`（既有）、`Label` / 定义列表排版（既有详情写法）、`Button`（可点击的关联 id）、`EmptyState` 的小型变体（无派生观察时的一行文案）、`CopyButton`（id 复制，既有）。

## 4.3 memories 页 —— 检索模式（机制透明）

检索模式是「为什么这条排在前面」的唯一场景，机制透明固定在此处落地。

**展示什么**

| 展示项 | 来源字段 | 呈现方式 |
| --- | --- | --- |
| 图分支状态（每次检索一条汇总） | `graph_status` | 结果表上方一行状态条：`ok` / `skipped` / `timeout` / `error` / `disabled`，附生效预算 `timeout_seconds`（来自 `GET /graph/stats`）；`disabled` 与 `timeout` / `error` 文案各不相同 |
| 分值构成（逐条，在详情 Sheet） | `score_details` | 分量表：语义 `semantic_score` / 关键词 `bm25_score` / 实体 `entity_boost` / 图 `graph_boost`（含 `graph_facts`）/ 合计 `raw_score` / 分母 `max_possible_score` / 阈值 `threshold` / 最终 `final_score`；**分母必须显示**，否则用户拿不到复算口径 |
| 衰减分量（逐条，在详情 Sheet） | `score_details.decay_weight` `retention` `memory_strength_days` `elapsed_days` `access_count` | 同一张分量表的下半段，标注「时间因子 = 0.90 + 0.10 × 保留率，只下调」 |
| 机制开关（两个） | 请求参数 `include_observations` / `include_invalidated` | 结果表上方的两个 `Switch`，默认关；与既有参数一一对应（F-3） |
| 状态列与召回列 | 同 §4.1 | 检索结果沿用同一套列 |

**为什么这样取舍**

- `graph_status` 做成**一次检索一条汇总**而不是逐行徽章：实测同一响应内 20/20 行同值（F-2），逐行重复渲染同一状态是噪声；而它回答的问题（这次图分支答没答上来）本来就是整次检索的问题。
- **检索恒定带 `explain=true`**：分量透明是本次必做项，做成开关会让默认视图没有分量。实测加 `explain` 前后返回的 id 序一致、分值口径一致（差异是向量检索本身的 ~5e-5 末位抖动），代价只是响应体变大。这条差异必须写进核对方式（AC-B4），避免核验时把末位抖动误判为「排序被 explain 改变」。
- 两个开关直接对应既有参数，而不是新造过滤：`include_observations=false` 与 `include_invalidated=false` 正是默认视图看不到观察与失效条目的原因（F-3）——把它们做成开关，既是可见性，也是最省的解释。
- 「`graph_boost=0`」不能解释成「图没命中」：只有当**候选池里没有任何条目被图事实引用**时加分才为 0，而它与「图键为空」在响应上同形——两者都表现为 `graph_boost=0` 且 `graph_status=ok`（对空图键发起检索在图桥侧同样是成功返回，机制见 `mem0/memory/graph_sync.py` 的 `search_graph_facts_with_status` 与 `_graph_hits`）。因此分量表必须同时给出 `graph_facts`（被图引用的事实条数）与**本次检索所用的图键**（口径：作用域派生，`user_id` 优先 ⇒ `mem0_<归一化值>`，与后端 `derive_group_id` 同规则），让「图里确实没有这条」与「图没答上来」在 UI 上可分。

**复用的组件**：`Switch`（既有）、`Badge` / `Alert`（状态条）、`Card`（分量区容器）、Sheet 详情（与 §4.2 同一个）、`Tooltip`（口径说明）。

## 4.4 Dream 页（新增 `/dashboard/dream`）

**分区一：运行列表**

| 列 | 字段 | 备注 |
| --- | --- | --- |
| 开始 / 结束 / 耗时 | `started_at` `finished_at` `duration_seconds` | 时间格式化沿用 `date-fns` 既有写法 |
| 模式 / 状态 | `mode`（`live` / `dry_run`）/ `status`（`completed` / `failed` / `timeout` / `skipped` / `running`） | 状态用徽章 |
| 作用域数 / 簇数 | `scopes` `clusters` | 数字右对齐（既有 `Entity` 表的 `align: "right"` 写法） |
| 产出 / 取代 | `observations_written` `observations_superseded` | |
| 失败簇 / LLM 调用 | `failed_clusters` `llm_calls` | 失败簇非 0 时高亮（`onSurface-danger-primary`） |
| 报告 | `report_path` | 显示为路径文本 + 复制按钮（**不做文件下载**：报告在容器内 `/app/history/`，前端无读取通道） |

分页用端点自带的 `limit` / `offset` / `total`，与既有 `Pagination` 组件配套。

**分区二：观察清单**

数据来自**新增的只读端点** `GET /observations`（§6.2）。列：观察内容（截断 + 悬浮展开，沿用 `TruncatedText`）、证据条数 `evidence_count`、源事实数 `len(source_memory_ids)`、所属运行 `dream_run_id`、生效时刻 `valid_at`、状态 `invalid_at`。行点击打开观察详情（复用 §4.2 的 Sheet，含源事实区）。游标翻页、`total` 计数与 memories 页同一套既有写法。

**分区三：dry-run 预览入口**

- 形态：一个「预览整合（dry-run）」按钮 + 确认对话框（沿用 `delete-confirmation-modal` 的 `Dialog` 形态与文案口径）；确认为必需，因为该动作会走完整四阶段并占用与实跑同一把锁。
- 结果呈现：确认后展示报告摘要（本次候选簇数、`would_write` 条数、跳过原因分布、`clusters_skipped` / `clusters_deferred`、耗时、token 数），并明示「本轮预览**未写入任何数据**」。
- 必须写明的语义（来自实测）：dry-run 不落 `dream_runs` 行（列表不会新增一行）、不写 Qdrant、不写状态表；它唯一产生的产物是一份报告文件。全量已判定时几秒返回（实测 4.2 秒、`llm_calls=0`）；存在待判定簇时会逐簇调用 LLM（上一轮实跑 104 簇 / 6 分钟 / 182k token），期间按钮保持禁用并显示进行中。
- **不做「一键实跑」**：`POST /dream/run` 会真正写入观察并可能取代既有事实，属于控制面，超出可见性目标。

**为什么这样取舍**

- 运行列表与观察清单同页：观察是运行的产物，`dream_run_id` 是它们之间的连接字段；分两页会让「这次运行产出了什么」需要两次跳转。
- dry-run 入口保留而非省略：这是**唯一**能在不写数据的前提下看到「整合会做什么」的通道（实测零写入），删掉它，用户对 Dream 的判读就只能依赖已落地的事实。

**复用的组件**：`DataTable`、`Card` / `CardHeader` / `CardTitle`、`Badge`、`Dialog`（确认；与既有 `delete-confirmation-modal` 同一形态）、`Button`、`Tooltip`、`TruncatedText`、`table-skeleton` / `EmptyState`、数字卡片排版（analytics 页的 `grid md:grid-cols-4` 卡片）。

## 4.5 Graph 页（新增 `/dashboard/graph`）

**分区一：能力与派发计数**（数据来自既有 `GET /graph/stats`）

| 展示项 | 字段 | 说明文案（必须出现） |
| --- | --- | --- |
| 能力开关 / 检索预算 | `enabled` `timeout_seconds` | 预算值用于解释「为什么这次是 timeout」 |
| 派发 / 入图成功 / 幂等跳过 / 失败 / 丢弃 | `graph_dispatched` `graph_synced` `graph_already_synced` `graph_failed` `graph_dropped` | **标注「自本进程启动以来」**（F-5：计数是进程内累计，重启归零） |
| 队列长度（瞬时） | `queue_size` | 标注「瞬时值」 |
| 熔断状态 / 连续失败 | `circuit_open` `consecutive_failures` | 熔断开启时状态条高亮 |

**分区二：图键与图规模**（数据来自**新增**只读端点 `GET /graph/keys`，§6.3）

| 列 | 字段 |
| --- | --- |
| 图键 | `group_id` |
| 记忆节点 / 实体节点 / 关系边 | `episodes` / `entity_nodes` / `entity_edges` |
| 判定 | 三者为 0 时标「空图键」（实测 `mem0__` 即此形态，来源是检索模式的通配作用域） |

页面附一句口径说明：图键由作用域派生（`user_id` 优先 ⇒ `mem0_<user_id>`），同一 `user_id` 的不同 `agent_id` **共用**一个图键——因此规模不是「按 agent」的维度（F-10）。

**分区三（只读提示）**：图桥不可用时，本页显示「图桥无响应」而不是 0 计数——两者语义不同（前者是读数失败，后者是「确实没有派发」）。

**复用的组件**：数字卡片排版（analytics 页）、`Card`、`DataTable`、`Badge`、`Alert`（图桥不可用）、`TableSkeleton` / `EmptyState`。

---

# 5. 数据来源映射表

| 展示项 | 端点 | 字段 |
| --- | --- | --- |
| 记忆列表（内容 / 用户 / 助手 / 创建时间） | `GET /memories`（无作用域，游标分页） | `results[].memory` `user_id` `agent_id` `created_at` |
| 列表总数与游标 | 同上 | `total` `next_cursor` |
| 有效 / 已失效 | 同上 | `invalid_at`（空 / 非空） |
| 失效原因 / 被谁取代 / 生效时刻 | 同上（列表列以简述呈现，详情给全值） | `invalid_reason` `superseded_by` `valid_at` |
| 召回次数 | 同上 | `access_count` |
| 上次召回 | 同上 | `last_accessed` |
| 观察标记 | 同上 | `memory_kind == "observation"` |
| 观察身份（键 / 证据数 / 所属运行） | 同上 | `observation_key` `evidence_count` `dream_run_id` |
| 检索结果与分值 | `POST /search` | `results[].score` |
| 分值构成（语义 / 关键词 / 实体 / 图 / 合计 / 分母 / 阈值 / 最终） | `POST /search`（`explain=true`） | `score_details.semantic_score` `bm25_score` `entity_boost` `graph_boost` `graph_facts` `raw_score` `max_possible_score` `threshold` `final_score` |
| 衰减分量（时间因子 / 保留率 / 强度 / 经过天数 / 召回次数） | 同上 | `score_details.decay_weight` `retention` `memory_strength_days` `elapsed_days` `access_count` |
| 图分支状态 | `POST /search`（逐条返回，同响应内恒定） | `graph_status` |
| 被图引用的事实条数 | 同上（`explain=true`） | `score_details.graph_facts` |
| 观察纳入 / 失效纳入 | `POST /search` 请求参数 | `include_observations` `include_invalidated` |
| 检索结果中的观察（含观察开关） | 同上（`include_observations=true`，配 `filters.memory_kind` 可只取观察） | `results[].memory_kind` |
| 派生观察（事实 → 观察） | `GET /memories/{id}/observations` | `results[].id` `observation_key` `evidence_count` `dream_run_id` `source_memory_ids` |
| 源事实（观察 → 事实） | `GET /memories/{id}/sources` | `results[]`（完整记忆行）、`total` `missing` |
| 单条记忆 / 单条观察全量 | `GET /memories/{id}` | 全字段（含上述机制字段） |
| Dream 运行列表 | `GET /dream/runs` | `total` `results[].started_at` `finished_at` `duration_seconds` `mode` `status` `scopes` `clusters` `failed_clusters` `llm_calls` `observations_written` `observations_superseded` `prompt_tokens` `completion_tokens` `report_path` |
| Dream 单轮统计 | `GET /dream/runs/{id}` | 同上 |
| 观察清单 | **`GET /observations`（新增，§6.2）** | `results[]`（完整记忆行：`memory` `evidence_count` `source_memory_ids` `dream_run_id` `valid_at` `invalid_at` `created_at`）、`total` `next_cursor` |
| 预览报告摘要 | `POST /dream/preview` | `totals.*` `would_write` `candidates[].decision` `candidates[].skip_reason` `supersede_preview` `errors` `duration_seconds` `report_path` |
| 图能力开关与预算 | `GET /graph/stats` | `enabled` `timeout_seconds` |
| 图派发计数 | 同上 | `graph_dispatched` `graph_synced` `graph_already_synced` `graph_failed` `graph_dropped` |
| 队列长度 / 熔断状态 | 同上 | `queue_size` `circuit_open` `consecutive_failures` |
| 图键清单与规模 | **`GET /graph/keys`（新增，§6.3）** | `keys[].group_id` `episodes` `entity_nodes` `entity_edges` |
| 检索时的图键（派生口径展示） | 前端按作用域派生（与后端 `derive_group_id` 同规则：`user_id` 优先、其余字符归一为下划线、前缀 `mem0_`） | — |

---

# 6. 后端补充

## 6.1 结论

**需要补充，共 2 项，都是只读端点**：`GET /observations`（观察清单）与 `GET /graph/keys`（图键与规模）。其余全部展示项由既有端点直接提供，不需要补字段。

## 6.2 补充一：`GET /observations`

**契约（新增，只读）**

```
GET /observations?page_size=<默认 1000，上限沿用既有 ALL_MEMORIES_LIMIT>&cursor=<不透明游标>
→ { "results": [ <与 GET /memories 同口径的完整记忆行> ], "next_cursor": <string|null>, "total": <int> }
```

- **排序口径与 `GET /memories`（无作用域）一致**（实测 2026-09-18）：`created_at` 最新优先，页内 `created_at` 单调不增；两端点对同一批观察的**相对序也一致**——79 条观察在 `GET /memories?top_k=1000` 首页里的位置为 98–176，位置随观察的返回序单调递增。`cursor` 取上一页最后一行的 `created_at`，服务端按 `created_at < cursor` 严格递减续读，因此页间永不重叠（`page_size=25` 翻完全库实测 25/25/25/4、79 条 79 唯一、0 重复，`total=79` 与 Qdrant 精确计数一致）。
- **末页判定与 `GET /memories` 不同，是本端点有意为之的口径**：本页不足 `page_size` 行时即返回 `next_cursor = null`（实测 `page_size=100` 返回 79 行 ⇒ `null`；`page_size=79` 恰好满页 ⇒ 仍返回游标，说明判据是「不足一页」而不是「少于总数」）。`GET /memories` 只要本页有行就返回游标——实测 `top_k=1000` 第 5 页 101 行仍返回游标，第 6 页 0 行才 `null`，客户端需要多请求一次空页才收敛。两者各自服务自己的读法：观察清单是会被反复打开的页面，一次说清「已到末页」省掉那次空往返；管理面列表是游标驱动的全量浏览，收敛点交给调用方。
  依据：**游标分页在「无更多数据」时返回 `null` 是标准做法**（客户端据此停止取数），实现方的口径更标准，故以实现的语义为准回写本节，而不是为了迁就本节旧稿的字面去改实现。
- **分页参数名与 `GET /memories` 不同（实测）**：本端点为 `page_size`，`GET /memories` 沿用上游既有的 `top_k`（`upstream/main:server/main.py` 即如此，属既有对外契约，本批不改）。两端点的 `cursor` 语义与页上限（`ALL_MEMORIES_LIMIT = 1000`）一致。`GET /observations` 是本批新增、无既有调用方，故直接取列表接口更通用的 `page_size`；该差异已在 `server/main.py` 的 `get_observations` docstring 中声明。
- 观察判定沿用既有口径：`memory_kind == "observation"`（`server/routers/dream.py` 已有同一常量与同一只读遍历逻辑）。
- 需要 `memory_kind` 的 payload 过滤；该字段的 keyword 索引已存在（实测 payload_schema）。

**为何既有端点不够**

1. **没有可用的清单入口**。反向追溯端点 `GET /memories/{id}/observations` 以「某条源事实」为入参，要列全量观察就得遍历 4000+ 条记忆逐个调用；且它返回的行**只有 6 个字段、没有正文**（F-8），连观察内容都渲染不出来。
2. **靠管理面列表客户端过滤会误报空集**。`GET /memories` 能返回观察，但客户端只能在自己已加载的页里找。观察在最新优先序里的位置实测是 44–122（F-9），位置由「上次 Dream 之后又写入了多少事实」决定——写入超过 1000 条新事实后，观察就整体滑出已加载页，页面会显示「无观察」而库里仍有 79 条。这正是卡片要求避免的「不误导」。
3. **成本差两个数量级**。走管理面列表逐页拉全量再过滤：6 次请求 / 4047 行 / 4.77 MB；走服务端按索引过滤：1 次请求 / 79 行 / 6 毫秒（F-7）。清单是会被反复打开的页面，前者不合适。
4. **为什么不改 `GET /memories`**：见 §3.3（怕把「默认排除观察」的既有口径做成可翻转参数）。新增端点的变更面更窄，也不影响既有两条路径的语义。

**代价**：多一个端点与其维护面；行口径沿用既有序列化，因此作用域字段（`user_id` / `agent_id`）一并返回，前端按需展示。

## 6.3 补充二：`GET /graph/keys`

**契约（新增，只读）**

```
GET /graph/keys
→ { "keys": [ { "group_id": <string>, "episodes": <int>, "entity_nodes": <int>, "entity_edges": <int> } ], "degraded": <bool> }
```

- 图键清单取自图桥 `GET /graphs`；规模取自图桥 `GET /stats?group_id=`。
- **硬约束（必须实现，否则本端点比不加更坏）**：代理端点**只对清单内已存在的键**取规模，绝不因读数而对未知键调用图桥 `GET /stats`——实测该调用会新建一个空图键（F-6）。该约束须写进端点注释并有一条可复跑的判定（[AC-E2]）。
- 图桥不可用时返回 `keys: []`、`degraded: true`（与既有降级原则一致：图侧故障不外溢，不影响 mem0 的其它端点）。
- 只读：不派发、不建键、不删键。

**为何既有端点不够**

1. 图规模与图键清单在图桥侧（`GET /stats?group_id=`、`GET /graphs`），**mem0 侧没有任何代理**：实测 `GET /graph/stats` 只返回十项计数与熔断状态，`GET /graph/graphs`、`GET /graph/bridge` 在 mem0 侧是 404。
2. 图桥**未映射宿主端口**（compose 中该服务无 `ports`），宿主上的任何消费者都进不去；由 mem0 代理是唯一不破「服务拓扑」的通道。
3. 不让 dashboard 直连图桥：图桥**无鉴权**——dashboard 直连等于把无鉴权的内部图服务暴露成一个前端可达面；且前端试探键会因「读即建键」制造垃圾图键；UI 还会耦合图桥的私有 API 形状（§3.3）。
4. 「图规模」不可能是纯前端读数：图键清单只能来自图桥，而图桥的 `/stats` 还有上述副作用，必须由服务端做「先取清单、后取规模」的顺序约束。

**代价**：mem0 侧新增一次（最多两次）对图桥的往返；图桥不可用时该段为空，页面按 `degraded` 显示「图桥无响应」。规模读数不缓存（图规模是单调上升的运维读数，缓存只会让人看到过期数字）。

## 6.4 明确不需要补充的部分

| 展示项 | 既有依据 |
| --- | --- |
| 有效/失效、失效原因、后继、生效时刻 | `GET /memories` 与 `POST /search` 已把四个字段作为一等字段返回（`bitemporal-fact-model.md` §5.4.3） |
| 召回次数与上次召回 | 同上，`access_count` / `last_accessed` 已在顶层（`memory-decay.md` §4.1） |
| 分值构成与图分支状态 | `POST /search` 的 `explain=true` + `graph_status` 已提供全部 14 个 `score_details` 键与五种状态 |
| 观察纳入 / 失效纳入 | 两个既有请求参数，无需后端改动 |
| 派生观察 / 源事实 | 两个既有端点构成双向追溯 |
| Dream 运行审计与报告摘要 | `GET /dream/runs`、`GET /dream/runs/{id}`、`POST /dream/preview` 已提供 |
| 图派发计数与熔断状态 | `GET /graph/stats` 已提供 |
| 单条记忆/观察全量 | `GET /memories/{id}` 已提供（观察与事实同口径） |

---

# 7. 设计语言与状态规范

## 7.1 组件复用清单（不新增视觉体系）

新增页面与新增区块只使用 §2.3 列出的既有组件；**不引入新的 UI 库、不引入新的图表依赖**（数字卡片与既有 `Card` 排版足够，向量规模的呈现不需要图表）。表格一律走 `DataTable`（宽度按权重），详情一律走 `Sheet`，加载/空/错误三态一律走 `TableSkeleton` / `EmptyState` / `Card` + `text-onSurface-danger-primary`（既有 analytics 页写法）。

## 7.2 配色 token（全部取自既有变量）

| 语义 | token |
| --- | --- |
| 有效 / 正常 | `onSurface-positive-primary`（仅在需要强调时；默认用 `onSurface-default-secondary` 弱化） |
| 已失效（弱化） | `onSurface-default-tertiary` |
| 失效原因 / 被取代 | `onSurface-danger-primary`（少量） |
| 观察身份 | `memPurple-*`（与既有 `memPurple-*` 用法同族） |
| 图相关（状态条、图键） | `memBlue-*` |
| 降级 / 超时 | `memGold-*` |
| 容器与分隔 | `border-memBorder-primary`、`bg-surface-default-{primary,secondary,tertiary}` |

## 7.3 空态 / 错误态 / 降级态（逐条判定）

| 场景 | 期望表现 | 判定依据 |
| --- | --- | --- |
| 全库无观察（`total=0`） | 观察清单纯空态文案，不显示「0 条」的空表 | `GET /observations.total == 0` |
| 无 Dream 运行记录 | 运行列表空态；预览按钮仍可用 | `GET /dream/runs.total == 0` |
| 机制字段缺失（存量记忆无 `valid_at`） | Status 显示「有效」（只由 `invalid_at` 判定），详情里 `valid_at` 显示 `--`，不做任何推断 | 实测 `valid_at` 非空 476 / 4043 |
| 记忆从未被召回（`access_count` 为 null） | 列显示 `--`，详情附「从未被检索返回过」 | 实测 `access_count` 非空 923 / 4043 |
| `graph_status = disabled` | 状态条显式标注「图能力已关闭（未发起图调用）」，与 `ok`、`timeout` 区分 | `graph.status.enabled=false` 时的检索响应 |
| `graph_status = skipped` | 标注「无作用域键或候选池为空，未发起调用」 | 设计 §6.4 |
| `graph_status = timeout` / `error` | 各自独立文案，超时附预算值 | `score_details` 与 `GET /graph/stats.timeout_seconds` |
| 图桥不可用 | Graph 页计数区显示读数（进程内计数仍可读）、图键区显示「图桥无响应」；**不显示 0 规模** | `GET /graph/keys.degraded == true` |
| 衰减关闭（`decay.enabled=false`） | 分量表中不出现衰减分量（`score_details` 无这些键），且不显示「0.0」占位 | 键缺失即隐藏（既有开关语义） |
| 图关闭（`graph.enabled=false`） | 分量表中不出现 `graph_boost` / `graph_facts`；Graph 页状态条为 `disabled` | 同上 |
| 取数失败（5xx / 401） | 既有 toast + 错误卡片；页面不白屏 | `getErrorMessage` 既有链路 |
| 预览被拒 | `409` 的 `detail` 原文呈现为「另一轮整合进行中」/「Dream 已关闭」两种文案（按 `status=skipped` / `disabled` 区分） | `POST /dream/preview` 的 409 语义 |

## 7.4 数值格式化

- 分值：3 位小数（沿用既有 `toFixed(3)`）；`decay_weight` / `retention` 3 位；`elapsed_days` / `memory_strength_days` 1 位并标单位「天」。
- 计数：整数、千分位（`toLocaleString`，既有 analytics 用法）。
- 时间：`date-fns` 的 `format` / `formatDistanceToNow`（既有用法）。
- 标识：等宽字体（`font-mono`，既有 id 写法）+ `CopyButton`。

---

# 8. 性能与安全约束的落实

| 约束 | 落实方式 |
| --- | --- |
| 游标翻页不被破坏 | memories 页的浏览模式：既有 `PAGE_SIZE` / `CURSOR_FETCH_LIMIT` / 缓冲区与 `next_cursor` 逻辑**一行不改**，只在列定义与 Schema 段落追加；两处新增机制数据（详情里的观察/源事实）都是**点击后**才发起，不进入列表取数路径 |
| 新增列不影响单页负载 | 列表取数仍是 `top_k=10` 的既有请求；新增展示项全部来自同一响应里已经存在的字段（响应体不因前端改动而变大） |
| 观察清单不分页爆炸 | 服务端过滤 + 游标分页（§6.2），单页上限沿用既有常量；实测全量观察 79 行 |
| 图规模读数不放大副作用 | 代理端点只对清单内的键读规模（§6.3 硬约束），读数不写库、不建键 |
| 预览动作的时长 | 按钮在请求期间禁用并显示进行中；不引入流式（响应是一次性完整报告） |
| 不暴露敏感值 | 前端不新增任何凭据读取路径：不使用 `api_key` / `ADMIN_API_KEY` / `LLM_API_KEY` / `EMBEDDER_API_KEY` / `JWT_SECRET`；`GET /configure` 的 `[redacted]` 行为不动（既有配置页语义）。判定方式见 [AC-F4]（构建产物检索 + 浏览器网络请求逐条核对） |
| 前端不做二次推断 | 页面上的每个机制字段直接来自响应；派生展示（如图键）用与后端同一规则的纯拼接，并在页面上标注口径 |

---

# 9. 边界与不变量

**不变量（不得动）**

1. `user_id=xue` 的记忆数据、`mem0_xue` 图键：只读。不写入、不失效、不删除、不重建。
2. 三项特性开关保持 `true`（`decay.enabled` / `graph.enabled` / `dream.enabled`）；不新增开关、不改默认值。
3. 既有 8 个页面（memories / analytics / export / entities / requests / configuration / settings / api-keys）的既有功能不回退：既有列、既有按钮、既有跳转、既有 toast 语义不动。
4. 既有 REST 端点的对外契约不变（新增端点与新增响应字段除外）：不改既有字段名、不改既有字段含义、不新增必填参数。
5. 游标翻页语义不变（键集、页大小上限、`next_cursor` 口径）。
6. 不引入写路径：不调用 `POST /memories`、`PUT /memories/{id}`、`DELETE /memories*`、`POST /reset`、`POST /dream/run`、图桥 `DELETE /graph/{group_id}`。

**允许的改动面（白名单）**

- `server/dashboard/src/**`（页面、组件、类型、端点常量、API 封装、导航）。
- `server/main.py` 或 `server/routers/dream.py`、`server/routers/graph.py`：仅新增 §6 的两个只读端点及其序列化辅助。
- `patches/mem0-local.patch`：若后端有改动，按仓库惯例刷新。

---

# 10. 实测证据

证据目录：`docs/design/dashboard-mechanism-visibility-evidence/`（基线 `737d10e0`，运行时为 compose 五服务，dashboard `:3000` 健康、API `:8888` 可达）。

| 文件 | 内容 | 对应发现 |
| --- | --- | --- |
| `probe_payload_inventory.py` / `.txt` | 全库 payload 清单：有效/失效/观察/召回足迹计数、最新优先序中的位置、payload 索引 | F-1、F-4、F-9 |
| `probe_rest_readings.py` / `.txt` | 全部读端点实测：管理面列表（游标、`total`、含失效与观察）与作用域列表（无游标、不含失效）、`/search` 的 `score_details` 全键与确定性、`include_observations` / `include_invalidated` 行为、`/graph/stats`、`/dream/runs`、双向追溯端点、三个开关取值 | F-1～F-5、F-8 |
| `probe_dream_dryrun.py` / `.txt` | dry-run 零写入：前后 `dream_runs` 行数、Qdrant 点数、报告与响应体逐字段相等 | §4.4 预览语义 |
| `probe_observation_cost.py` / `.txt` | 观察清单两条取数路径的成本对比（6 次 / 4.77 MB vs 1 次 / 6 ms） | F-7 |
| `probe_graph_bridge.py` / `.txt` | 图桥 `/graphs`、`/stats`、**读即建键**副作用与清理复原 | F-6 |
| `probe_graph_counters.py` / `.txt` | 计数器的实例归属（容器 1 号进程启动时刻 + 两次读数） | F-5、F-10 |

关键读数（原文见上述文件）：

- 全库 4043 点：`valid_at` 非空 476、`invalid_at` 非空 7（全部 `superseded_by_newer_fact`）、`access_count` 非 0 923、观察 79（`xue:ying` 74 / `xue:shi` 5，同一 `dream_run_id`）。
- 管理面列表：`total=4043`、`top_k=1000` 可得 1000 行、第 1/2 页重叠 0、第 1 页含 7 条已失效与 79 条观察；`top_k=10` 返回 10 行（既有前端页大小的取数路径可用）。
- 作用域列表 `user_id=xue&top_k=1000`：1000 行、已失效 0、观察 79；响应键只有 `results`。
- 检索：`score_details` 14 键齐全（含 `graph_boost=0.5` / `graph_facts=3` / `max_possible_score=1.5` 的命中样本）；同一响应内 `graph_status` 唯一；三次重复调用 id 序一致、首条分值极差 5.2e-5。
- 通配作用域（前端检索模式现状）`graph_boost` 全 0 且 `graph_status=ok`；`user_id=xue` 同一查询 `graph_boost` 出现 0.5 / 0.167。
- 图：桥 `/graphs` = `[default_db, mem0__, mem0_xue]`；`mem0_xue` 规模 63 / 108 / 130（episodes / 实体 / 边）；对未知键读 `/stats` 后新键出现，删除后键集合复原。
- `dream_runs`：1 行（live / completed / 104 簇 / 104 次 LLM / 79 观察 / 361.7 秒）；dry-run 前后行数与点数均不变。

---

# 11. 验收标准

判定基线：实现卡的提交（`git log` 上的实现 commit）与其后重建的 dashboard 镜像；页面判定以浏览器实际渲染为准；接口判定以运行中的服务读数为准。**不认工作区当下状态**。

## 11.1 memories 页：状态与足迹（bi-temporal + 衰减）

| 编号 | 判定内容 | 判定方式 |
| --- | --- | --- |
| [AC-A1] | 浏览模式存在 **Status** 列，取值由 `invalid_at` 决定（空 ⇒「有效」，非空 ⇒「已失效」），且两种取值都实际渲染过 | ① 首屏 10 行逐行核对 Status 与 `GET /memories?top_k=10` 的 `invalid_at` 一一对应；② 取一条 `invalid_at` 非空的行（共 7 条，在最新优先序的位置为 128–527）核对渲染为「已失效」——可用检索模式的「含已失效」开关直达，或连续翻页至其出现 |
| [AC-A2] | 已失效行可见其**失效原因**与**后继** | 对 [AC-A1] 中定位到的失效行，打开详情，核对 `invalid_reason` 与 `superseded_by` 与 API 原值逐字符一致（`superseded_by` 应为可点击的 id） |
| [AC-A3] | 列表存在 **Accesses** 列，值与 `access_count` 一致；`access_count` 为 null 时显示 `--` | 取 `access_count` 非 0 与为 null 的记录各 2 条，逐条核对 |
| [AC-A4] | 详情展示完整 bi-temporal 四字段（`valid_at` / `invalid_at` / `superseded_by` / `invalid_reason`），字段缺失时显示 `--` 而非推断值 | 分别打开一条有 `valid_at` 的记录与一条无 `valid_at` 的存量记录，核对 |
| [AC-A5] | 详情展示召回足迹（`access_count` / `last_accessed`），从未召回时显示 `--` 且带说明文案 | 同上抽样 |
| [AC-A6] | 观察条目在列表中带观察标记 | 列表第 1 页内的 79 条观察中抽 3 条，核对标记存在；再抽 3 条 `memory_kind` 为 null 的记录，核对无标记 |

## 11.2 memories 页：检索透明

| 编号 | 判定内容 | 判定方式 |
| --- | --- | --- |
| [AC-B1] | 一次检索在结果表上方展示**一条** `graph_status` 汇总，取值与响应逐条一致 | 同一 query 用 `POST /search`（带 `explain=true`）取响应，核对页面状态条与 `results[*].graph_status` 的唯一值一致；对同一响应断言「所有条目同值」 |
| [AC-B2] | 五种 `graph_status` 取值在 UI 上的文案两两不同（`ok` / `skipped` / `timeout` / `error` / `disabled`） | ① 运行时证据：`ok` 为常态；`error` 以「停掉 graph-bridge 后检索」稳定触发；`timeout` 若在核验窗口内自然出现则一并取证（本设计探针期间出现过）。② 映射核对：UI 的取值→文案映射含全部五种且两两不同。③ 机制依据：`disabled` 由 `graph.enabled=false` 产出、`skipped` 由无图键或空候选池产出（见 `mem0/memory/graph_sync.py` 与 `mem0/memory/main.py` 的 `_graph_hits`）。**不要求关闭任何特性开关**；`disabled` / `skipped` 两项即以 ②③ 判定 |
| [AC-B3] | 检索结果的详情展示 `score_details` 全部分量，且 `max_possible_score` 必现 | 取一次命中图加分的响应（`graph_boost > 0`）与一次未命中的响应，各抽 1 条，把页面数值与响应原文逐字段比对（`score_details` 的 14 个键逐个核对） |
| [AC-B4] | 检索模式的开启 `explain` 不改变返回条目的 id 序 | 对同一 query 连续两次 `POST /search`（`explain=true`）核对 id 序一致；页面加载后的网络请求确认为带 `explain=true` 的同一 query。**分值允许末位抖动**（实测 ~5e-5），判定以 id 序为准 |
| [AC-B5] | 检索模式提供「含观察」与「含已失效」两个开关，默认关；开启后返回集变化与端点参数语义一致 | 关—开各跑一次同一 query：`include_observations=false → true` 时结果中出现 `memory_kind=observation` 的行；`include_invalidated=false → true` 时结果中出现 `invalid_at` 非空的行（实测该开关下可稳定取到失效条目） |
| [AC-B6] | `graph_boost = 0` 与「图没答上来」在 UI 上可区分：分值区同时给出 `graph_facts` 与本次检索的图键 | 取一次 `graph_boost=0` 但 `graph_status=ok` 的检索（通配作用域下可稳定取得，其图键为 `mem0__`），核对页面同屏出现图键与 `graph_facts`，且文案未把 0 加分表述为「图没命中」；再取一次 `graph_status=timeout` 的检索，核对两种情形文案不同 |

## 11.3 Dream 页

| 编号 | 判定内容 | 判定方式 |
| --- | --- | --- |
| [AC-C1] | 运行列表字段齐全：开始/结束/耗时、模式、状态、作用域数、簇数、产出、取代、失败簇、LLM 调用数 | 与 `GET /dream/runs` 逐行逐字段比对（以既有 1 行 live 运行为准；`dry_run` 模式不落行、实跑会写数据，故不以造行方式取证，改为以 `GET /dream/runs?limit=5` 的字段集逐列核对） |
| [AC-C2] | 观察清单经 `GET /observations` 取数，含正文、证据条数、源事实数、所属运行、状态；`total` 与实际观察数一致 | 页面 `total` 与 Qdrant 侧 `memory_kind=observation` 的精确计数一致；抽样 3 条核对 `evidence_count` 与 `len(source_memory_ids)` |
| [AC-C3] | 观察行可打开详情并展示**源事实**列表；源事实可再打开为一条记忆详情 | 取一条 `evidence_count >= 4` 的观察，逐条核对源事实 id 与 `GET /memories/{id}/sources` 的 `results[].id` 一致；`total` / `missing` 都显示 |
| [AC-C4] | 一条**非观察**记忆的详情展示它派生的观察 | 取 `GET /memories/{id}/observations` 返回 `total >= 1` 的源事实，核对页面列出的观察 id 集合与端点一致 |
| [AC-C5] | dry-run 预览入口：需二次确认；执行后展示报告摘要；**不新增 `dream_runs` 行、不改变 Qdrant 点数** | 预览前后各取 `GET /dream/runs?limit=1` 的 `total` 与 Qdrant 精确点数，断言不变；页面摘要的 `would_write` 条数与响应 `would_write` 长度一致 |
| [AC-C6] | 无运行记录 / 无观察时页面正确显示空态，不崩、不误导 | 以 `total=0` 的空响应核对渲染分支（不构造假数据、不改开关）；结论记录实际观测 |
| [AC-C7] | 无「一键实跑」入口 | 页面检索不存在触发 `POST /dream/run` 的交互（网络请求核对） |

## 11.4 Graph 页

| 编号 | 判定内容 | 判定方式 |
| --- | --- | --- |
| [AC-D1] | 展示 `enabled`、`timeout_seconds`、五项 `graph_*` 计数、`queue_size`、熔断状态与连续失败数 | 与 `GET /graph/stats` 逐个字段比对 |
| [AC-D2] | 计数区标注「自本进程启动以来」 | 页面文案核对；并给出「同一实例 5 秒内读数不变、跨实例读数不同」的实测依据（`probe_graph_counters.txt`） |
| [AC-D3] | 图键清单与每个键的规模（episodes / 实体 / 边）展示正确 | 与图桥 `GET /graphs`、`GET /stats?group_id=` 逐个键比对（含 `mem0_xue`、空键 `mem0__`） |
| [AC-D4] | 空图键（三规模为 0）有显式标记，不被当作正常规模展示 | 对 `mem0__` 核对标记存在 |
| [AC-D5] | 图桥不可用时不显示 0 规模，而显示读数失败 | 停掉 graph-bridge 后刷新页面（或让代理返回 `degraded: true`），核对页面文案；恢复容器后复核 |
| [AC-D6] | 页面不展示/不触发图键删除入口 | 交互与网络请求核对 |

## 11.5 后端补充端点

| 编号 | 判定内容 | 判定方式 |
| --- | --- | --- |
| [AC-E1] | `GET /observations` 返回 `{results, next_cursor, total}`，行为与 `GET /memories` 同口径（最新优先、键集游标、页上限） | 逐页翻完全部观察：页间 id 集合无重叠、总条数等于 `total`、最后一页 `next_cursor` 为 null；与 Qdrant 精确计数一致 |
| [AC-E2] | `GET /graph/keys` **不因读数创建图键** | 读数前后各取图桥 `GET /graphs`，断言键集合不变；再对清单外的键名断言不出现 |
| [AC-E3] | `GET /graph/keys` 在图桥不可用时返回 `keys: []` 且 `degraded: true`，不影响其它端点 | 停桥后调用本端点与 `GET /graph/stats`、`GET /memories`，断言后者正常 |
| [AC-E4] | 两个新端点要求鉴权（与既有端点同一依赖），未授权返回 401 | 不带凭据调用，断言 401 |

## 11.6 不破坏与工程纪律

| 编号 | 判定内容 | 判定方式 |
| --- | --- | --- |
| [AC-F1] | 既有 8 个页面逐页打开无报错、既有功能无缺失 | 在浏览器中逐页实测，列出每页的判定结论（页面清单见 §9 不变量 3） |
| [AC-F2] | memories 列表**游标翻页**不破坏：实翻多页无重叠、无遗漏、顺序正确 | 连续翻到至少第 3 页，记录每页 id 集合，断言两两交集为空、页内 `created_at` 单调不增、（在与 API 对照时）无跳号；[AC-E1] 的翻页结论同时计入本项 |
| [AC-F3] | 既有列（Content / Score / User / Agent / Created）的标签与取值语义不变 | 逐列核对（含检索模式 Score 列） |
| [AC-F4] | 前端未暴露任何敏感值 | ① 对构建产物与服务端渲染输出检索 `ADMIN_API_KEY` / `JWT_SECRET` / `LLM_API_KEY` / `EMBEDDER_API_KEY` / `m0sk_` / `sk-` 等标识，结论为 0 命中；② 浏览器网络面板逐条核对页面自发请求，无凭据出现在 URL / 响应体；③ 确认前端源码不引用任何服务端密钥环境变量 |
| [AC-F5] | TypeScript 检查零错误、构建成功 | 在 `server/dashboard/` 执行 `pnpm typecheck` 与 `pnpm build`（或 compose 构建）成功 |
| [AC-F6] | 浏览器看到的是**重建镜像**里的新代码（非容器内手改） | `docker compose build mem0-dashboard` 后 `up -d`，核对容器镜像 ID / `CreatedAt` 晚于实现提交时间；页面出现新增页面与新增列 |
| [AC-F7] | 三项特性开关仍为 `true`，且未被本次改动改默认 | `GET /configure` 的 `decay.enabled` / `dream.enabled` / `graph.enabled` 全为 true |
| [AC-F8] | 未改动 `user_id=xue` 的记忆数据与 `mem0_xue` 图键 | 对比实现前后的 Qdrant 精确点数与 `mem0_xue` 规模（允许因正常运行写入而增加，须为单调上升而非下降）；核对无删除调用 |
| [AC-F9] | 两个新增端点在 OpenAPI 文档中可见且描述准确 | 打开 `http://localhost:8888/docs`，核对两个端点的路径、参数、响应描述 |
| [AC-F10] | 若后端有改动，`patches/mem0-local.patch` 已刷新且对 `upstream/main`（`0df3e4b8`）`git apply --check` 通过 | 按仓库惯例执行刷新与校验 |
| [AC-F11] | 仓库工作区干净（改动已提交） | `git status --short` 为空；实现 commit 在本地 `main` 上（不推送） |

## 11.7 判据的已知边界（核验时不得据此判失败）

- **分值末位抖动**：同一 query 重复调用的 `score` 有 ~5e-5 抖动（向量检索自身），判定排序一律以 **id 序**为准（[AC-B4]）。
- **机制条目在列表中的位置会漂移**：观察与失效条目的位置随写入量变化（F-9），[AC-A1]/[AC-A2] 用 API 取 id 再定位，不用固定页码。
- **作用域列表不含失效条目**：这是后端既有读语义，不是前端缺陷（F-1）。
- **图规模单调上升**：`mem0_xue` 的规模随正常运行增大（实测两次读数 63/108/130），只要不下降即合规。
- **`graph_status` 的瞬态取值**：同一 query 在不同时刻可能分别读到 `ok` 与 `timeout`（实测两种取值都出现过），单次读数不作为稳定性判据。

---

# 12. 实现卡拆分建议

四条线里 ②③④ 互不依赖、可并行；合并顺序建议 **① → ② → ③/④**。

| 卡 | 内容 | 依赖 | 可并行 |
| --- | --- | --- | --- |
| ① 后端只读端点 | `GET /observations` + `GET /graph/keys`（§6），含刷新 `patches/mem0-local.patch` | 无 | 与 ②③ 并行 |
| ② memories 页机制可见 | 列表新增 Status / Accesses 列与观察标记、详情段（bi-temporal / 足迹 / 双向追溯）、检索模式的 `explain` + 两个开关 + 图分支状态条 + 分量表（§4.1–4.3） | 无（只依赖既有端点）；其中「检索模式下含观察/含失效开关」不需要新端点 | 与 ①③ 并行 |
| ③ Dream 页 | 运行列表、观察清单、dry-run 预览入口（§4.4） | **需要 ①** 的 `GET /observations` | 与 ② 并行；其观察清单部分需等 ① 合入 |
| ④ Graph 页 | 计数区、图键与规模区、降级态（§4.5） | **需要 ①** 的 `GET /graph/keys` | 与 ③ 并行；其规模区需等 ① 合入 |

- **文件冲突面**：②③④ 都会改 `types/api.ts` 与 `utils/api-endpoints.ts`（追加类型与常量），合并时按追加合并；③④ 都会改 `components/main-nav.tsx`（各加一个条目），冲突小但需人工核对分组标签只加一次。
- **建议合并顺序**：先合 ①（它解掉 ③④ 的外部依赖，也是唯一会动后端的卡）→ 再合 ②（无外部依赖，改动面与 ③④ 的重叠最小）→ 最后合 ③ 与 ④。
- **不建议再拆**：② 内部（列表列 / 详情 / 检索）共用同一份类型与同一个 Sheet，拆开会产生三次同文件改动；③④ 各含一个页面加一个取数封装，拆开后协调成本大于收益。

---

# 附录 A：术语

| 术语 | 含义 |
| --- | --- |
| 管理面列表 | `GET /memories` 不带 `user_id` / `agent_id` / `run_id` 时的列表：raw 全量、含已失效与观察、有 `total` 与游标（要求 admin 权限） |
| 作用域列表 | 同上带作用域参数时的列表：不含已失效条目、无游标无总数 |
| 观察 | Dream 产出的合成信念条目（`memory_kind = "observation"`），带 `observation_key` / `source_memory_ids` / `evidence_count` / `dream_run_id` |
| 图键 | Graphiti/FalkorDB 的图名，由作用域派生（`user_id` 优先 ⇒ `mem0_<value>`） |
| 分量 | `score_details` 中的一个可解释输入（语义 / 关键词 / 实体 / 图 / 时间因子） |
| 图分支状态 | `graph_status`：`ok` / `skipped` / `timeout` / `error` / `disabled` |

# 附录 B：证据复跑

```bash
cd /Users/xuelingkang/Documents/Containers/mem0/server
EV=../docs/design/dashboard-mechanism-visibility-evidence

# 管理员 token（30 分钟有效）
docker compose exec -T mem0 python -c "
import sys; sys.path.insert(0,'/app')
from auth import create_access_token
print(create_access_token('<users.id>','admin'))" | tr -d '\r' > /tmp/mem0_token.txt

# A. payload 清单
docker compose cp $EV/probe_payload_inventory.py mem0:/tmp/
docker compose exec -T mem0 python /tmp/probe_payload_inventory.py

# B. REST 读面
python3 $EV/probe_rest_readings.py

# C. dry-run 零写入（会在容器内写一份报告并自行删除）
docker compose cp $EV/probe_dream_dryrun.py mem0:/tmp/
docker compose exec -T mem0 python /tmp/probe_dream_dryrun.py

# D. 观察清单取数成本
docker compose cp $EV/probe_observation_cost.py mem0:/tmp/
docker compose exec -T mem0 python /tmp/probe_observation_cost.py

# E. 图桥与「读即建键」（读多写一：删除的是探针键，末尾核对键集合复原）
docker compose cp $EV/probe_graph_bridge.py graph-bridge:/tmp/
docker compose exec -T graph-bridge python /tmp/probe_graph_bridge.py

# F. 计数器归属实例
docker compose cp $EV/probe_graph_counters.py mem0:/tmp/
docker compose exec -T mem0 python /tmp/probe_graph_counters.py
```
