# 凯尔希自主生活系统测试指南

本文用于测试以下能力是否按预期工作：

- 每日计划生成
- 计划项执行
- 主动消息投递
- NPC 管理与互动
- 网络搜索（Tavily）
- 对话后重规划

## 1. 启动前准备

### 1.1 启动服务

```bash
python -m server.main
```

确认可访问：

- `http://localhost:8000/`
- `http://localhost:8000/schedule`
- `http://localhost:8000/npcs`

### 1.2 可选：配置 Tavily 搜索

网络搜索配置走运行时设置，不走 `config.yaml`。

推荐通过设定页或 REST API 设置：

```bash
curl -X PUT "http://localhost:8000/api/settings/plan_web_search_enabled" ^
  -H "Content-Type: application/json" ^
  -d "{\"value\":\"true\"}"

curl -X PUT "http://localhost:8000/api/settings/plan_web_search_api_base" ^
  -H "Content-Type: application/json" ^
  -d "{\"value\":\"https://api.tavily.com/search\"}"

curl -X PUT "http://localhost:8000/api/settings/plan_web_search_api_key" ^
  -H "Content-Type: application/json" ^
  -d "{\"value\":\"你的_tavily_key\"}"
```

如果暂时不测搜索，可跳过。

## 2. 日程表功能测试

### 2.1 生成今日计划

操作：

1. 打开 `http://localhost:8000/schedule`
2. 点击“生成今日计划”

预期：

- “今日计划”卡片显示最新 `raw_plan`
- “时间轴”显示多个计划项
- “计划历史”出现一条新记录
- 数据库 `daily_plans`、`plan_items` 有新增数据

可选验证：

```bash
curl "http://localhost:8000/api/plans/today"
```

### 2.2 查看历史计划

操作：

1. 在“计划历史”点击“查看详情”

预期：

- 当前展示切换到该历史计划
- 时间轴更新为该计划的条目

### 2.3 编辑计划项

操作：

1. 在“时间轴”点击某条“查看 / 编辑”
2. 修改 `activity`、`action_type`、`action_payload`
3. 点击“保存计划项”

推荐测试 payload：

```json
{"intent":"关心近况","tone":"warm"}
```

或：

```json
{"query":"罗德岛 医疗部 最新研究","intent":"收集医疗信息"}
```

预期：

- 保存成功提示出现
- 重新加载后修改仍在
- `PUT /api/plans/items/{id}` 可正常工作

## 3. 主动消息测试

### 3.1 手动把某个计划项改成 `message_user`

操作：

1. 编辑一个计划项
2. `action_type` 设为 `message_user`
3. `action_payload` 设为：

```json
{"intent":"询问今天的身体状态","tone":"warm"}
```

4. 将该计划项的时间改到当前小时
5. 等待 scheduler 一个轮询周期，或重启服务后等待执行

预期：

- `character_notifications` 表新增记录
- `/api/notifications` 返回未读消息
- `schedule` 页面“主动消息”区域显示该消息
- 点击“标记已读”后状态变为 `read`

可选验证：

```bash
curl "http://localhost:8000/api/notifications"
curl "http://localhost:8000/api/notifications/history"
```

### 3.2 MCP 注入测试

操作：

1. 保证存在一条 `pending` 通知
2. 调用 `get_current_state`

预期：

- 返回文本包含 `【角色主动消息】`
- 相应通知状态变为 `delivered`

## 4. NPC 管理与互动测试

### 4.1 创建 NPC

操作：

1. 打开 `http://localhost:8000/npcs`
2. 点击“新增 NPC”
3. 填入名称、身份、背景、关系

预期：

- NPC 列表出现新条目
- 右侧详情编辑区域可读取该 NPC
- 数据库 `npc_entities` 新增记录

### 4.2 编辑 NPC

操作：

1. 点击某 NPC 的“查看 / 编辑”
2. 修改背景、关系、个性特征、备注
3. 点击“保存 NPC”

预期：

- 修改保存成功
- 刷新后内容仍在

### 4.3 NPC 互动执行

操作：

1. 把一个当前小时计划项改成 `npc_interaction`
2. `action_payload` 设为：

```json
{"npc_id": 1}
```

或自动生成：

```json
{"npc_id": "auto"}
```

预期：

- 计划项执行后 `outcome` 更新
- NPC 的 `interaction_count` 增加
- `last_interaction_at` 更新
- 如 LLM 判断应记事件，`event_anchors` 中新增事件

## 5. 网络搜索测试

### 5.1 Tavily 接入测试

前提：

- 已配置 `plan_web_search_enabled=true`
- 已配置 Tavily base 和 key

操作：

1. 编辑一个当前小时计划项
2. `action_type` 设为 `web_search`
3. `action_payload` 填：

```json
{"query":"小红书 罗德岛 医疗 灵感设定", "intent":"寻找与医疗部门相关的公开信息灵感"}
```

预期：

- 计划项执行后 `outcome` 更新
- 若搜索成功，会在事件表里新增一条 `source=generated` 的检索事件
- 搜索结果会被 LLM 消化成第一人称阅读笔记

失败排查：

- 若 `outcome` 提示“网络搜索功能未启用”，检查设置键
- 若无结果，检查 Tavily key / api base
- 若上游返回 4xx/5xx，查看服务日志

## 6. 对话后重规划测试

### 6.1 通过 REST 触发

操作：

```bash
curl -X POST "http://localhost:8000/api/state/reflect" ^
  -H "Content-Type: application/json" ^
  -d "{\"conversation_summary\":\"【事实性信息】今天临时改变了原本的安排，晚些时候还需要继续跟进新的事项。\\n【关系动态变化】与用户的联系欲望增强。\\n【情感关键时刻】对对方状态产生额外关注。\\n【未完成线索】晚间仍可能继续联系。\"}"
```

预期：

- 写入新的 `conversation_end` 快照
- 若剩余计划项存在，`maybe_replan()` 可能生成新的 `daily_plans`
- 原计划可能被标记为 `replanned`

### 6.2 手动重规划

操作：

1. 在 `schedule` 页面点击“手动重规划”

预期：

- 若模型判断需要重排，则生成新计划
- 若不需要，则返回 `No replan needed`

## 7. 回归测试建议

每次改动后至少回归以下项目：

1. `get_current_state` 仍能正常返回，不因计划系统报错
2. `reflect_on_conversation` 能正常写入 `conversation_end`
3. 未配置 Tavily 时，除 `web_search` 计划项外，其余功能正常
4. `schedule` 和 `npcs` 页面可打开，不出现前端脚本错误
5. MCP 返回的主动消息不会破坏原有演化提示逻辑

## 8. 建议的最小测试顺序

如果你只想快速确认主链路，按下面顺序走：

1. 启动服务
2. 在 `schedule` 页面生成今日计划
3. 在 `npcs` 页面创建 1 个 NPC
4. 把一个当前小时计划项改成 `npc_interaction`
5. 等待执行，看是否产生 `outcome` 和事件
6. 把另一个当前小时计划项改成 `message_user`
7. 看通知是否出现
8. 最后调用一次 `/api/state/reflect`，确认是否触发 replan
