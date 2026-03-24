# 凯尔希意向状态机 — AI 交接导航

> 本文档面向接手开发的 AI 或开发者，覆盖项目架构、核心概念、数据流、模块职责与常见修改指南。

---

## 一、项目定位

本项目是一个**角色记忆与人格持久化系统**，为《明日方舟》角色"凯尔希"提供：

- **时间感知状态推演**：根据两次对话间隔，自动推演角色的内心状态变化
- **事件记忆锚点**：记录对话与生活中的关键事件，支持向量/关键词检索
- **动态人格演化**：L2 层人格和关系模式可随事件累积自动更新
- **结构化关键记录**：持久化用药方案、重要日期、关键物品等可执行信息

系统通过 **MCP (Model Context Protocol)** 与上游 LLM 前端（如 rikkahub）集成，也可通过 REST API 独立使用。

---

## 二、技术栈

| 层级 | 技术 |
|------|------|
| 后端框架 | FastAPI (Python 3.11+) |
| MCP 协议 | FastMCP（SSE + Streamable HTTP 双端点） |
| 数据库 | SQLite (aiosqlite 异步) |
| LLM 调用 | httpx → OpenAI 兼容 API |
| 向量嵌入 | 远端 Embedding API（可选）+ 本地确定性哈希回退 |
| 前端 | 纯静态 HTML/CSS/JS（无框架），暗色主题 |
| 配置 | YAML (config.yaml) + 数据库运行时设置 |

---

## 三、目录结构

```
凯尔希状态机/
├── config.yaml              # 启动配置（LLM API、数据库路径、记忆模式）
├── config.example.yaml      # 配置模板
├── requirements.txt         # Python 依赖（fastapi, uvicorn, aiosqlite, mcp, httpx, pyyaml）
├── data/
│   └── kelsey.db            # SQLite 数据库文件
├── server/                  # 后端 Python 包
│   ├── main.py              # FastAPI 入口：生命周期、路由挂载、页面路由
│   ├── config.py            # YAML 配置加载 → dataclass
│   ├── models.py            # Pydantic 数据模型（ORM 模型 + API 请求模型）
│   ├── database.py          # 数据库层：表创建、CRUD、迁移
│   ├── llm_client.py        # OpenAI 兼容 LLM 客户端（含 Token 追踪）
│   ├── prompts.py           # Prompt 模板 + PromptManager + 默认设定值
│   ├── state_machine.py     # 核心状态机：状态推演、反思、记忆检索
│   ├── environment.py       # 环境信息生成器（时间+场景→文本）
│   ├── memory_store.py      # 记忆存储抽象 + KeywordMemoryStore 实现
│   ├── vector_memory_store.py # VectorMemoryStore：向量检索、同步、压缩
│   ├── evolution.py         # 人格演化引擎：事件评分→L2更新→归档
│   ├── automation_engine.py # 自动化编排：向量同步→演化→冷压缩
│   ├── event_taxonomy.py    # 事件分类与标题生成规则
│   ├── mcp_tools.py         # MCP 工具定义（6个对外工具）
│   └── api_routes.py        # REST API 路由（CRUD + 管理端点）
├── web/                     # 前端静态文件
│   ├── index.html           # 仪表盘
│   ├── snapshots.html       # 快照历史
│   ├── events.html          # 事件历史
│   ├── key-records.html     # 关键记录管理
│   ├── settings.html        # 设定（人格/Prompt/配置/导入）
│   ├── evolution.html       # 人格演化
│   ├── vectors.html         # 向量管理
│   ├── history.html         # 历史（旧入口，保留兼容）
│   ├── guide.html           # 使用指南
│   ├── app.js               # 全部前端逻辑（单文件 ~2300 行）
│   └── style.css            # 全局样式（橄榄军绿暗色主题）
└── deploy/                  # 部署脚本和离线 wheel 包
```

---

## 四、核心概念与数据模型

### 4.1 分层人格系统

```
L1（稳定底层）——不参与自动演化
├── L1_character_background   角色背景事实
└── L1_user_background        用户背景事实

L2（动态演化层）——可被 EvolutionEngine 自动更新
├── L2_character_personality   角色人格状态
└── L2_relationship_dynamics   关系模式
```

L1/L2 的内容存储在 `system_settings` 表中，通过 `PromptManager` 读写。

### 4.2 数据库表（SQLite）

| 表名 | 作用 | 关键字段 |
|------|------|----------|
| `state_snapshots` | 凯尔希的内心状态独白 | id, created_at, type(daily/conversation_end/accumulated), content, environment(JSON), embedding_vector_id |
| `event_anchors` | 事件记忆锚点 | id, date, title, description, source(generated/manual/conversation), trigger_keywords(JSON), categories(JSON), archived, importance_score, impression_depth |
| `key_records` | 结构化关键记录 | id, type(important_date/important_item/key_collaboration/medical_advice), title, content_text, tags(JSON), status(active/archived), source |
| `system_settings` | 所有配置项（L1/L2/Prompt/向量参数/自动化开关等） | key(PK), value, category, description |
| `memory_vectors` | 向量化记忆 | entry_id(UK), source_type, vector_json, vector_dim, vector_provider(api/local), status(active/deleted), tier(warm/cold) |
| `memory_recall_stats` | 记忆被召回的统计 | entry_id(PK), recall_count, last_recalled_at |
| `automation_runs` | 自动化执行报告 | trigger, ran, report_json(完整报告+Token统计) |

### 4.3 Pydantic 模型（`models.py`）

- **ORM 模型**：`StateSnapshot`, `EventAnchor`, `KeyRecord` — 与数据库行一一对应
- **API 请求模型**：`CreateEventRequest`, `UpdateSettingRequest`, `BulkImportRequest` 等 — 用于 FastAPI 参数校验

---

## 五、核心数据流

### 5.1 对话开始 → `get_current_state`

```
MCP/REST 调用 (current_time, last_interaction_time)
  │
  ├─ 计算时间间隔 → 确定 checkpoint 数量（每 min_time_unit_hours 一个，上限30）
  │
  ├─ 循环每个 checkpoint：
  │   ├─ EnvironmentGenerator.generate() → 环境文本（LLM生成或模板）
  │   ├─ MemoryStore.search() → 历史记忆
  │   ├─ LLM.chat(system_prompt + snapshot_generation_prompt) → 新快照
  │   ├─ DB.insert_snapshot()
  │   ├─ _generate_event_anchor() → LLM 判断是否产生事件 → DB.insert_event()
  │   └─ _enforce_snapshot_limit() → 超过 max_snapshots 的旧快照向量化归档
  │
  ├─ AutomationEngine.run() → 向量同步 → 人格演化检查 → 冷记忆压缩
  │
  └─ _build_injectable_context() → 拼接 L1 + L2 + 近期热事件 + 快照 → 返回
```

**关键点**：返回值不仅是快照文本，而是完整的**可注入上下文块**（L1→L2→热事件→快照），上游前端应将其直接注入 system prompt。

### 5.2 对话结束 → `reflect_on_conversation`

```
MCP/REST 调用 (conversation_summary)
  │
  ├─ LLM.chat(reflect_snapshot_prompt) → 对话后状态独白 → DB.insert_snapshot()
  ├─ LLM.chat(reflect_event_prompt) → 对话事件锚点 → DB.insert_event()
  ├─ _enforce_snapshot_limit()
  ├─ AutomationEngine.run()
  └─ 返回新快照内容（附自动化报告）
```

### 5.3 对话中 → `recall_memories` / `recall_key_records` / `upsert_key_record`

- `recall_memories`：MemoryStore.search() → 向量相似度 + 时间衰减 + 重要性加权
- `recall_key_records`：关键词模糊匹配 key_records 表
- `upsert_key_record`：按 (type, title) 去重，存在则更新

### 5.4 人格演化流程（`EvolutionEngine`）

```
check_status() → 自上次演化后新增事件数 >= 阈值？
  │
  ├─ preview()：
  │   ├─ _score_events() → LLM 对每个事件评分（重要性0-10, 印象深度0-10）
  │   └─ _generate_updates() → LLM 基于评分结果生成新 L2 文本 + 变更摘要
  │
  └─ apply()：
      ├─ 写入新 L2_character_personality 和 L2_relationship_dynamics
      ├─ 低于 archive_importance_threshold 的事件标记 archived=1
      └─ 更新 last_evolution_time
```

### 5.5 自动化编排（`AutomationEngine`）

在 `get_current_state` 和 `reflect_on_conversation` 执行完毕后自动运行：

1. **向量同步**（`sync_eligible_vectors`）：已归档事件 + 超龄快照 → 向量化存储
2. **人格演化**（`auto_evolution`）：事件数达阈值时自动执行 preview → apply
3. **冷记忆压缩**（`compact_cold_memories`）：超过 cold_days_threshold 的旧向量按月分组摘要合并

所有开关可在 `system_settings` 中独立控制（`automation_enabled`, `automation_vector_sync` 等）。

---

## 六、记忆检索策略

### 6.1 KeywordMemoryStore（`memory_store.py`）

- 从已归档事件 + 已归档快照中做 SQL LIKE 关键词匹配
- 评分 = 关键词命中率 × 时间衰减（半衰期30天）
- **联想命中机制**：尾部候选的加权随机替换，引入多样性奖励（日期去重、分类去重）+ 冷门奖励（低召回频率条目加分）

### 6.2 VectorMemoryStore（`vector_memory_store.py`）

- Embedding 策略：优先远端 API（OpenAI 兼容），失败或未配置时回退到**本地确定性哈希嵌入**（SHA256 分桶 → 归一化）
- 检索评分 = 0.72×余弦相似度 + 0.18×时间衰减 + 0.10×重要性加权
- 向量存储在 `memory_vectors` 表的 `vector_json` 字段中（JSON 数组）
- 冷记忆压缩：按 `source_type:YYYY-MM` 分组，合并为摘要向量，原向量标记 deleted

---

## 七、MCP 工具接口

FastMCP 注册的工具（`mcp_tools.py`），上游 LLM 通过 MCP 协议调用：

| 工具名 | 触发时机 | 参数 | 返回 |
|--------|---------|------|------|
| `get_current_state` | 对话开始 | current_time, last_interaction_time (ISO) | L1+L2+热事件+快照（可注入上下文） |
| `summarize_conversation` | 对话结束前 | conversation_text | 结构化摘要 |
| `reflect_on_conversation` | 对话结束 | conversation_summary | 新状态独白 |
| `recall_memories` | 对话中 | query, top_k | 事件/快照记忆列表(JSON) |
| `upsert_key_record` | 对话中 | record_type, title, content_text, ... | 写入结果 |
| `recall_key_records` | 对话中 | query, top_k, record_type | 关键记录列表(JSON) |
| `execute_profile_evolution` | 系统提示时 | 无 | 演化结果 |

MCP 端点：
- SSE：`/mcp/sse`
- Streamable HTTP：`/mcp-http/mcp`（有中间件处理路径规范化）

---

## 八、REST API 概览

所有 REST 路由前缀 `/api`，定义在 `api_routes.py`：

**状态机测试**：`POST /api/state/current`, `/api/state/reflect`, `/api/state/summarize`
**记忆搜索**：`POST /api/memories/search`
**快照 CRUD**：`GET/POST/DELETE /api/snapshots[/{id}]`
**事件 CRUD**：`GET/POST/PUT/DELETE /api/events[/{id}]`
**关键记录**：`GET/POST/PUT/DELETE /api/key-records[/{id}]`, `POST /api/key-records/search`
**向量管理**：`/api/vectors/stats`, `/api/vectors/entries`, `/api/vectors/sync`, `/api/vectors/compact`
**设定管理**：`GET/PUT /api/settings[/{key}]`, `POST /api/settings/reset/{key}`
**演化**：`GET /api/evolution/status`, `POST /api/evolution/preview`, `POST /api/evolution/apply`
**自动化报告**：`GET /api/automation/latest`, `/api/automation/runs`, `/api/automation/token-summary`
**模型定价**：`GET/POST/DELETE /api/automation/model-pricing`
**批量导入**：`POST /api/import/bulk`
**阶段性回顾**：`POST /api/review/periodic`
**运行时 LLM**：`GET/PUT /api/runtime/llm`

---

## 九、配置系统

### 9.1 启动配置（`config.yaml`）

通过 `config.py` 加载为 `AppConfig` dataclass 树。支持以下顶层段：

```yaml
server:    # host, port
llm:       # api_base, api_key, model
database:  # path (SQLite文件路径)
environment:  # min_time_unit_hours, generator
memory_store: # type("vector"/"keyword"), max_snapshots
character:    # system_prompt / system_prompt_file
```

### 9.2 运行时配置（`system_settings` 表）

所有 Prompt 模板、L1/L2 文本、向量参数、自动化开关等均存储在数据库中，通过 `PromptManager` 读取。`prompts.py` 中的 `DEFAULT_SETTINGS` 定义了所有键的默认值、分类和描述。

**配置优先级**：数据库运行时值 > config.yaml > 代码内置默认值。

### 9.3 关键配置项分类

| 分类 | 键名示例 | 说明 |
|------|---------|------|
| foundation | `L1_character_background`, `L1_user_background` | 稳定底层，不自动演化 |
| personality | `L2_character_personality`, `L2_relationship_dynamics` | 动态层，EvolutionEngine 可更新 |
| prompt | `prompt_snapshot_generation`, `prompt_event_anchor` 等 | 所有 LLM Prompt 模板 |
| config | `min_time_unit_hours`, `evolution_event_threshold`, `archive_importance_threshold` | 状态机行为参数 |
| vector | `vector_embedding_api_base`, `vector_cold_days_threshold` 等 | 向量存储参数 |
| automation | `automation_enabled`, `automation_vector_sync` 等 | 自动化开关 |
| runtime | `llm_api_base`, `llm_api_key`, `llm_model` | 运行时 LLM（覆盖 config.yaml） |

---

## 十、Prompt 工程

所有 Prompt 模板在 `prompts.py` 中定义，支持通过 Web 设定页面在线编辑。

### 10.1 Prompt 模板列表

| 键 | 用途 | 输入变量 |
|----|------|---------|
| `prompt_snapshot_generation` | 状态快照生成 | {environment}, {previous_snapshot}, {recent_events}, {memory_context} |
| `prompt_event_anchor` | 事件锚点提取 | {current_snapshot}, {environment}, {system_layers}, {memory_context} |
| `prompt_reflect_snapshot` | 对话后快照 | {previous_snapshot}, {conversation_summary}, {memory_context} |
| `prompt_reflect_event` | 对话后事件 | {current_snapshot}, {conversation_summary}, {system_layers}, {memory_context} |
| `prompt_conversation_summary` | 对话摘要 | {previous_snapshot}, {conversation_text}, {memory_context}, {system_layers} |
| `prompt_periodic_review` | 阶段性回顾 | {time_range}, {snapshots_timeline}, {events_timeline}, {stats_summary}, {system_layers} |
| `prompt_evolution_summary` | 人格演化 | {character_personality}, {relationship_dynamics}, {scored_events} |
| `prompt_event_scoring` | 事件评分 | {events} |
| `prompt_environment_generation` | 环境信息 | {time}, {date}, {weekday}, {time_period}, {previous_env}, {latest_snapshot}, {continuity} |

### 10.2 事件锚点解析

`state_machine.py` 的 `_parse_and_save_event()` 使用正则解析 LLM 输出：
- 支持两种格式：新格式（`客观记录:` + `主观印象:`）和旧格式（`事件描述:`）
- 提取：标题、关键词、分类
- 如缺失标题/分类，由 `event_taxonomy.py` 自动生成

### 10.3 system_prompt 构建

`PromptManager.get_system_prompt()` 拼接 L1+L2 四段内容为 system prompt，传递给所有 LLM 调用。

---

## 十一、前端架构

### 11.1 页面结构

所有页面共享同一个 `app.js` 和 `style.css`。每个页面在 `<body onload>` 中调用自己的初始化函数：

| 页面 | 初始化函数 | 功能 |
|------|-----------|------|
| `index.html` | `initDashboardPage()` | 仪表盘：最新快照、自动化报告、Token 统计、模型定价 |
| `snapshots.html` | `initSnapshotsHistoryPage()` | 快照列表：查看/删除/导出 |
| `events.html` | `initEventsHistoryPage()` | 事件列表：分类筛选/编辑/归档/导出 |
| `key-records.html` | `initKeyRecordsPage()` | 关键记录：搜索/添加/编辑/删除 |
| `settings.html` | `loadSettingsPage()` | 设定：人格层/Prompt/参数/批量导入 |
| `evolution.html` | `loadEvolutionStatus()` | 人格演化：预览/应用/重算归档 |
| `vectors.html` | `initVectorsPage()` | 向量管理：统计/配置/同步/压缩 |

### 11.2 设计规范

- **配色**：橄榄军绿 × 近黑底 × 奶油文字（CSS 变量在 `:root` 中）
- **无框架**：纯 Vanilla JS，所有 API 调用通过 `apiFetch()` 封装
- **模态框**：`openModal()` / `closeModal()` 通用模态框系统
- **状态栏**：底部固定 `showStatus()` 反馈条

---

## 十二、应用启动流程（`main.py`）

```python
lifespan(app):
  1. Database.initialize()        # 创建表 + schema 迁移
  2. LLMClient(config.llm, db)   # LLM 客户端（运行时可通过DB覆盖配置）
  3. PromptManager(db)           # 初始化默认设定到 system_settings
  4. TemplateEnvironmentGenerator(prompt_manager, llm)
  5. VectorMemoryStore(db) 或 KeywordMemoryStore(db)  # 由 config 决定
  6. EvolutionEngine(db, llm, prompt_manager)
  7. AutomationEngine(db, prompt_manager, memory, evolution)
  8. StateMachine(config, db, llm, env_gen, memory, prompt_manager, automation)
  9. set_state_machine(sm)        # 注入 MCP 工具
  10. set_dependencies(...)       # 注入 REST API 路由
  11. mcp.session_manager.run()   # 启动 MCP 会话管理

路由挂载：
  /api/*         → api_routes.router
  /mcp/*         → mcp.sse_app()
  /mcp-http/*    → mcp.streamable_http_app()
  /static/*      → web/ 静态文件
  / /history /settings /events-history /snapshots-history /key-records /evolution /vectors /guide
                 → 各 HTML 页面
```

---

## 十三、LLM 客户端与 Token 追踪（`llm_client.py`）

- 使用 httpx 异步客户端调用 OpenAI 兼容 API
- `get_runtime_config()` 优先从数据库读取运行时覆盖配置
- **Token 追踪**：`begin_usage_tracking()` / `end_usage_tracking()` 基于 `ContextVar`，跨异步调用累积 prompt_tokens、completion_tokens、requests，并按模型名分桶
- Token 追踪数据随自动化报告持久化到 `automation_runs` 表

---

## 十四、事件分类系统（`event_taxonomy.py`）

预定义 6 个分类及其关键词：

- 情感交流、学术探讨、生活足迹、床榻私语、精神碰撞、工作同步

`classify_event(description, keywords)` → 关键词命中 → 返回分类列表（默认"生活足迹"）
`make_event_title(description, keywords, categories)` → 从关键词/动作提示/分类生成 ≤16 字标题

---

## 十五、常见修改场景指南

### 15.1 新增一个 MCP 工具

1. 在 `mcp_tools.py` 添加 `@mcp.tool()` 装饰的异步函数
2. 在 `state_machine.py` 添加对应的业务方法
3. 如需数据库操作，在 `database.py` 添加查询方法

### 15.2 新增一个 REST API 端点

1. 在 `models.py` 添加请求/响应 Pydantic 模型
2. 在 `api_routes.py` 添加路由函数（使用 `_db`, `_state_machine` 等模块级变量）

### 15.3 新增一个数据库表或字段

1. 在 `database.py` 的 `_CREATE_TABLES` 中添加建表语句
2. 在 `_ensure_schema_updates()` 中添加 `_ensure_column()` 调用（渐进迁移）
3. 在 `models.py` 添加对应的 Pydantic 模型

### 15.4 修改 Prompt 模板

- 优先通过 Web 设定页面在线编辑（即时生效，存在数据库中）
- 若要修改默认值，编辑 `prompts.py` 中的常量字符串和 `DEFAULT_SETTINGS` 字典
- 注意保持 `{variable}` 占位符与调用处的 `.format()` 一致

### 15.5 新增一个 Web 页面

1. 在 `web/` 下创建 HTML 文件（参考现有页面结构，引用 `style.css` 和 `app.js`）
2. 在 `app.js` 添加初始化函数和业务逻辑
3. 在 `main.py` 添加 `@app.get("/新路径")` 路由

### 15.6 修改记忆检索策略

- 关键词模式：编辑 `memory_store.py` 的 `_compute_score()` 和 `_select_with_association()`
- 向量模式：编辑 `vector_memory_store.py` 的 `search()` 中的评分公式（当前 0.72/0.18/0.10 权重）

### 15.7 新增一个系统设定项

1. 在 `prompts.py` 中定义 `KEY_xxx = "xxx"` 常量
2. 在 `DEFAULT_SETTINGS` 中添加条目（包含 value, category, description）
3. 在使用处通过 `prompt_manager.get_config_value(KEY_xxx)` 读取

---

## 十六、依赖关系图

```
main.py
  ├── config.py          (AppConfig)
  ├── database.py        (Database)
  ├── llm_client.py      (LLMClient)
  ├── prompts.py         (PromptManager, DEFAULT_SETTINGS)
  ├── environment.py     (TemplateEnvironmentGenerator)
  ├── memory_store.py    (KeywordMemoryStore)
  ├── vector_memory_store.py  (VectorMemoryStore)
  ├── evolution.py       (EvolutionEngine)
  ├── automation_engine.py    (AutomationEngine)
  ├── state_machine.py   (StateMachine) ← 核心，聚合以上所有
  ├── mcp_tools.py       (FastMCP 工具) ← 引用 StateMachine + EvolutionEngine
  └── api_routes.py      (REST 路由)   ← 引用 Database + StateMachine + 全部引擎

state_machine.py 依赖：
  Database, LLMClient, EnvironmentGenerator, MemoryStore, PromptManager, AutomationEngine
```

---

## 十七、注意事项

1. **数据库迁移是渐进式的**：`_ensure_schema_updates()` 在每次启动时检查并添加缺失列，不会破坏已有数据
2. **Token 追踪使用 ContextVar**：在异步并发场景下隔离各请求的统计
3. **向量维度必须一致**：所有向量的维度由 `vector_embedding_dim` 设定控制，切换 Embedding 模型需重建索引
4. **LLM 输出解析容错**：事件锚点解析兼容多种格式，`"无需记录"` 是特殊跳过标记
5. **前端无构建步骤**：直接编辑 JS/CSS/HTML 即生效（开发模式设 `KELSEY_DEV=1` 可启用热重载）
6. **config.yaml 中包含 API Key**：已在 `.gitignore` 中排除
7. **MCP 路径兼容**：`main.py` 中间件处理了 `/mcp-http` → `/mcp-http/mcp` 的路径规范化
8. **批量导入**：`POST /api/import/bulk` 支持一次性导入设定、快照、事件、关键记录，带 upsert 和向量同步选项
