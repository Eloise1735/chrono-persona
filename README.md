# 凯尔希意向状态机 (Kal'tsit State Machine)

为 rikkahub 或其他前端提供凯尔希角色的持久化记忆与时间感知系统。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

编辑 `config.yaml`，填入你的 LLM API 信息：

```yaml
llm:
  api_base: "https://your-api-provider.com/v1"
  api_key: "your-api-key"
  model: "your-model-name"
```

### 3. 启动服务

```bash
python -m server.main
```

服务启动后：
- Web 管理面板: http://localhost:8000
- 历史记录: http://localhost:8000/history
- 关键记录: http://localhost:8000/key-records
- MCP SSE 端点: http://localhost:8000/mcp/sse
- REST API: http://localhost:8000/api/
- 向量管理: http://localhost:8000/vectors

### 云服务器访问（公网部署）

当服务运行在云服务器上时，请把 `localhost` 替换成你的公网 IP（或域名）：

- Web 管理面板：`http://47.115.35.155:8000`
- MCP SSE：`http://47.115.35.155:8000/mcp/sse`
- MCP Streamable HTTP（兼容入口）：`http://47.115.35.155:8000/mcp-http`

已提供一键入口脚本：

- 双击 `deploy/open_web.bat`：自动打开 `deploy/web_url.txt` 中配置的地址
- 你可以直接编辑 `deploy/web_url.txt` 的第一行来替换为新的公网地址
- 也可命令行调用：
  - `deploy/open_web.bat http://47.115.35.155:8000`
  - `deploy/open_web.bat 47.115.35.155 8000`

## MCP 工具

| 工具 | 触发时机 | 说明 |
|------|---------|------|
| `get_current_state` | 对话开始 | 根据时间间隔生成状态快照序列，返回最新状态独白 |
| `reflect_on_conversation` | 对话结束 | 基于对话摘要生成新快照和事件锚点 |
| `recall_memories` | 对话中 | 搜索过往记忆（事件锚点+历史快照） |
| `periodic_review`（REST） | 手动触发 | 基于自定义时间段生成阶段性生活与关系发展回顾 |
| `upsert_key_record` | 对话中 | 写入/更新关键记录（关键日期/物品/协作/医疗建议） |
| `recall_key_records` | 对话中 | 检索结构化关键记录 |

### 对话模板建议（可复制到上游系统提示词）

当出现以下场景时，请优先使用关键记录工具：

- 用户询问或提及既有的**用药方案/剂量/频次/注意事项**
- 用户提及**共同计划、生活安排、医疗嘱咐、待办清单**
- 用户提及**纪念日、关键物品（礼物/信物）**且需要具体细节

推荐调用顺序：

1. 先 `recall_key_records` 获取可执行细节（优先 `active`）
2. 若本轮对话产生了新的可复用结构化信息（表格/清单/方案），调用 `upsert_key_record` 持久化
3. 需要叙事背景时再补充 `recall_memories`（事件锚点）

建议写入规范：

- `record_type` 明确使用：`important_date` / `important_item` / `key_collaboration` / `medical_advice`
- `title` 用稳定短标题（便于后续更新命中）
- `content_text` 保留完整可执行内容（可包含表格/步骤）
- 有时效信息时填写 `start_date` / `end_date`

## REST API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/snapshots` | 快照列表 |
| GET | `/api/snapshots/latest` | 最新快照 |
| POST | `/api/snapshots` | 手动创建快照 |
| GET | `/api/events` | 事件列表 |
| POST | `/api/events` | 手动添加事件 |
| PUT | `/api/events/{id}` | 编辑事件（描述/关键词） |
| DELETE | `/api/events/{id}` | 删除事件 |
| GET | `/api/key-records` | 关键记录列表（支持类型筛选） |
| POST | `/api/key-records` | 添加关键记录 |
| PUT | `/api/key-records/{id}` | 编辑关键记录 |
| DELETE | `/api/key-records/{id}` | 删除关键记录 |
| POST | `/api/key-records/search` | 搜索关键记录 |
| GET | `/api/search?q=关键词` | 关键词搜索 |
| GET | `/api/vectors/stats` | 向量库统计 |
| GET | `/api/vectors/entries` | 向量条目列表 |
| GET | `/api/vectors/settings` | 向量参数读取 |
| PUT | `/api/vectors/settings` | 向量参数更新 |
| POST | `/api/vectors/sync` | 向量同步/重建 |
| POST | `/api/vectors/compact` | 冷记忆压缩（旧向量摘要合并） |
| DELETE | `/api/vectors/entries/{entry_id}` | 删除向量条目 |
| GET | `/api/runtime/llm` | 运行时 LLM 配置读取 |
| PUT | `/api/runtime/llm` | 运行时 LLM 配置更新 |
| GET | `/api/automation/latest` | 最近一次自动化执行报告 |
| GET | `/api/automation/runs` | 自动化执行历史列表 |
| GET | `/api/automation/token-summary` | 自动化 Token 汇总（今日/本周/累计） |
| POST | `/api/state/current` | 测试 get_current_state |
| POST | `/api/state/reflect` | 测试 reflect_on_conversation |
| POST | `/api/memories/search` | 测试 recall_memories |
| POST | `/api/review/periodic` | 生成阶段性回顾（可自定义起止日期） |
| POST | `/api/import/bulk` | 一键批量导入（settings/snapshots/events/key_records） |

## 配置项说明

```yaml
server:
  host: "0.0.0.0"        # 监听地址
  port: 8000              # 端口

llm:
  api_base: ""            # OpenAI 兼容 API 地址
  api_key: ""             # API Key
  model: ""               # 模型名称

database:
  path: "./data/kelsey.db"  # SQLite 数据库路径

environment:
  min_time_unit_hours: 24   # 最小时间单位（小时）
  generator: "template"     # 环境生成器类型

memory_store:
  type: "vector"            # 记忆检索类型（vector 或 keyword）
  max_snapshots: 7          # 最大保留快照数
```

向量记忆策略：
- 已归档事件会进入向量化队列
- 超过 `vector_snapshot_days_threshold`（默认14天）的快照会进入向量化队列
- 若配置了 Embedding API，则优先使用远端 embedding；否则自动回退到本地 deterministic embedding
- 检索评分融合：语义相似度 + 时间衰减 + 事件重要性/印象深度
- 冷记忆压缩：对超过 `vector_cold_days_threshold` 的旧向量按分组摘要合并，降低噪声与堆积

自动化编排（默认开启）：
- 在 `get_current_state` / `reflect_on_conversation` 后自动执行：向量同步 → 阈值触发的人格演化 → 冷记忆压缩（按最小间隔）
- 保留全部手动入口：`/vectors` 的同步、压缩、重建仍可随时手动触发
- 自动执行后会在状态机返回文本尾部附加“自动记忆整理报告”
- 仪表盘内置“自动化记忆整理（子面板）”，可查看最近一次执行结果与最近20条历史记录
- 自动化报告包含本次完整流程的 LLM Token 统计（输入/输出/总计/请求次数）
- 仪表盘新增可折叠“Token 汇总”卡片，展示今日/本周/累计 Token 与请求数
- Token 汇总卡片支持按模型单价进行成本估算，并展示模型成本拆分（按 USD / 1M tokens 计）
