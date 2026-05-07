# 凯尔希状态机 AI Handoff

> 面向下一位接手这个仓库的人。  
> 目标不是写变更流水账，而是用“产品定位 + 运行结构 + 关键链路 + 当前边界 + 接手建议”的方式，帮助你快速建立全局理解。

---

## 0. 一句话判断这个项目是什么
这是一个以 **“凯尔希作为持续存在的角色”** 为核心的后端系统。它不是普通聊天接口，而是一个同时维护：

- 角色当前状态
- 角色独立生活推进
- 用户与角色之间的关系连续性
- 结构化长期记忆
- 可回溯的生活流与事件史

的“角色状态机 + 分层记忆系统 + 轻量自主生活系统”。

它的目标不是“保存聊天记录”，而是让上游对话系统每次开聊时都能拿到：

- 凯尔希此刻处在什么状态
- 最近两天生活是怎样流过来的
- 哪些变化值得记住
- 哪些长期事实要继续生效
- 她与 user 目前的短期关系态势如何
- 她今天原本的安排与现实偏差是什么

---

## 1. 当前产品边界
按当前代码与本窗口最后状态，系统已经明确分成以下几层：

### 1.1 状态层
- `snapshot`
  - 表示“她现在怎样”
  - 是即时切片，不负责承载完整连续叙事

### 1.2 短期关系层
- `relationship_state`
  - 表示短期关系态势与联系时间感
  - 负责：多久没联系、当前更想靠近还是留空间、适合主动提什么话题、对日程的节律倾向影响
  - 不并入 L2，不并入 snapshot 正文

### 1.3 生活流层
- `life_flow_trace`
  - 表示某一段生活是怎样流过去的
  - 不是 event，也不是 key record
  - 负责承接未升格的连续生活痕迹

- `slowline`
  - 表示中期、有方向、但不要求每天显性推进的生活线
  - 例如：求职、论文、恢复、项目推进、学习线、角色自身工作线
  - 不承担精确事实存储

### 1.4 事件层
- `event`
  - 表示可单独命名、可回溯、可检索的离散锚点
  - 当前已经刻意收紧，不再因为“有 delta”就成立

### 1.5 结构化事实层
- `key_record`
  - 表示长期可调用、结构化、可执行的事实
  - 是权威事实库
  - 可驱动 `slowline`，但不与 slowline 混同

### 1.6 长期人格与稳定设定层
- `L2`
  - 长期动态人格、关系模式、生活倾向
- `L1`
  - 稳定背景与基础设定

---

## 2. 当前系统最重要的产品判断
### 2.1 日程不再等于事件来源
这是本轮非常关键的修正。

当前产品边界是：
- 日程系统负责角色自己的生活推进
- 日程执行结果不再自动写入 event
- 日程最多只更新 plan item outcome，并在必要时补 `life_flow_trace`
- 真实事件由：
  - 前台对话
  - 明确的后台事件判定链
  - 手动记录
  三者共同把关

### 2.2 日程继续存在，但已“去用户化”
当前方向不是让角色围着 user 排日程，而是：
- 角色有自己的独立生活安排
- user 会通过对话影响她的状态、节律、注意力和后续计划倾向
- 但后台不会再替双方安排“见面、共读、共同活动”之类的伪互动剧情

### 2.3 对话与自主生活等权
系统当前设计明确要求：
- user 对话产生的内容不能把角色自己的生活推进全部挤掉
- 角色自主生活产生的内容也不能覆盖真实对话线
- 注入时必须按来源平衡编排，而不是谁最近谁全占

### 2.4 现实生活细节不必强行升格为主线或事件
目前架构已经接受一个前提：
- 现实生活高不确定、高突发、细节膨胀
- 它们不一定适合强行压进某条主线
- 也不一定值得升格成 event
- 因此需要：
  - `slowline` 承接远景与中期线
  - `life_flow_trace` 承接连续流
  - `近景碎片池` 承接高张力、未必成主线的近景碎片

---

## 3. 当前总体架构
```text
上游聊天系统 / MCP / Web / Android
              |
           FastAPI
              |
  ---------------------------------
  |               |               |
StateMachine   PlanEngine      API Routes
  |               |               |
  |               |          CRUD / 管理 / 调试
  |
  |---- EnvironmentGenerator
  |---- PromptManager
  |---- MemoryStore
  |       |---- KeywordMemoryStore
  |       └---- VectorMemoryStore
  |---- EvolutionEngine
  |---- AutomationEngine
  |---- LLM Clients
  └---- Database
```

真实的系统中心仍然是：
- `StateMachine`

第二核心是：
- `PlanEngine`

当前项目不是“单 API 服务 + 一堆工具函数”，而是：
- 一个持续运行的状态推进内核
- 外加独立生活调度
- 外加分层记忆注入系统

---

## 4. 启动与后台循环
入口在：
- [server/main.py](D:/Eloise/coding/凯尔希状态机/server/main.py)

启动时主要完成：
1. 读取 `config.yaml`
2. 初始化 SQLite
3. 执行 schema 补列 / 迁移
4. 初始化主 LLM、环境专用 LLM、快照/评分专用 LLM
5. 初始化 `PromptManager`
6. 将 `prompts.py` 中默认设置灌入 `system_settings`
7. 初始化 `MemoryStore`
8. 初始化 `EvolutionEngine`
9. 初始化 `AutomationEngine`
10. 初始化 `StateMachine`
11. 初始化 `PlanEngine`
12. 挂载 MCP / REST / 静态页面
13. 启动后台 life scheduler loop

当前真正持续运行的是：
- `life scheduler loop`

它负责：
- 生成当日计划
- 执行当前小时 plan item
- 维护 snapshot 推进
- 维护 relationship_state 漂移
- 必要时触发 replan
- 承担部分自动压缩 / 自动化调度职责

---

## 5. 数据模型总览
主要定义在：
- [server/models.py](D:/Eloise/coding/凯尔希状态机/server/models.py)

### 5.1 核心表 / 模型
- `StateSnapshot`
- `EventAnchor`
- `KeyRecord`
- `WorldBook`
- `DailyPlan`
- `PlanItem`
- `LifeFlowTrace`
- `ConversationTimeClaim`
- `RelationshipState`
- `SlowLine`
- `NPCEntity`
- `CharacterNotification`

### 5.2 当前新增但很关键的两层
#### `RelationshipState`
短期关系感知层，字段包括：
- `last_meaningful_contact_at`
- `days_since_meaningful_contact`
- `contact_recency_bucket`
- `connection_need`
- `pride_or_distance`
- `valence`
- `arousal`
- `life_immersion`
- `relationship_feeling_summary`
- `space_need_level`
- `concern_level`
- `proactive_topics`
- `plan_bias_hint`

#### `SlowLine`
中期生活议题组织层，字段包括：
- `theme`
- `source_family`
- `stage_summary`
- `current_tension`
- `recent_movement_summary`
- `open_questions`
- `salience`
- `last_touched_at`
- `linked_key_record_ids`
- `linked_event_ids`

### 5.3 当前数据库层一个重要事实
系统大量行为不只由代码决定，也受 `system_settings` 和 prompt 配置影响。  
如果你发现某个行为“看起来不符合代码直觉”，优先检查：
- `system_settings`
- Web 设置页是否改过 prompt
- 运行时 LLM 覆盖设置

---

## 6. 当前最重要的调用链

## 6.1 `get_current_state`
入口：
- MCP `get_current_state`
- REST `POST /api/state/current`

它是真正的主链路。

当前理解应是：
1. 读取最新 snapshot、relationship_state、计划上下文、trace、events、key records 等
2. 如有必要，做有限 catch-up 推进
3. 维护对话占时 / conversation claim
4. 生成最终注入块
5. 把必要的主动通知附在结果里

### 当前注入结构（最新理解）
当前注入不应再是“很多平铺的大段摘要”，而应是按层组织。最新方向是：
1. `L1`
2. `L2`
3. `近程记忆桥`
4. `近期生活主线`
5. `近景碎片池`
6. `短期关系感知`
7. `当前日程与偏差`
8. `当前状态快照`

解释：
- 越稳定、越长期的东西越靠前
- 越即时、越切片的东西越靠后
- 这样上游模型先建立“她是谁”和“她最近处在什么阶段”，再进入当下状态

### 注入层当前的三个关键判断
1. `conversation_summary` 四段式内容不应原样泄漏进注入
2. `近期生活主线` 可以保留关键细节，但不能与“重要转折细节”重复
3. `重要转折细节` 已降级，不应再作为第二份 event 列表存在

## 6.2 `summarize_conversation`
入口：
- MCP `summarize_conversation`
- REST `POST /api/state/summarize`

当前仍会产出结构化 conversation summary，但它的正确定位是：
- 内部分析产物
- 用于反思、关系评估、trace digest 提取
- 不应再整段原样注入下次对话 prompt

## 6.3 `reflect_on_conversation`
入口：
- MCP `reflect_on_conversation`
- REST `POST /api/state/reflect`

当前理解：
1. 基于对话 summary 和相关记忆生成 `conversation_end` snapshot
2. 更新 / 关闭活跃对话 claim
3. 生成 conversation 对应的 `life_flow_trace` 或 digest
4. 触发 relationship_state 轻量更新
5. 必要时触发自动化和 replan

重要边界：
- 当前 `reflect_on_conversation` 不应默认自动写 event
- 如果对话里有真正值得保留的事件，应由显式事件链或人工记录处理

---

## 7. 事件系统的当前正确理解
### 7.1 event 不再等于“有变化”
当前最重要的收紧判断：
> `delta` 只适合做 trace / fragment 候选材料，不再天然支持 event 成立。

### 7.2 现在只有这些东西应升格为 event
- 明确决策 / 原则 / 承诺
- 医疗阶段性转折
- 高价值情感节点，并且改写彼此认知
- 主动标记
- 身体现象学信号被明确赋义
- 外部变化明确改写后续生活路径

### 7.3 不应自动升格为 event 的东西
- 普通时间推进带来的 delta
- 高突发但短促的现实碎片
- 普通主线推进中的细部波动
- 计划项正常执行结果
- 仅仅“与上一时段不同”的内容

### 7.4 当前事件页已经是清洗工作台
事件历史页当前支持：
- 来源筛选：`generated / conversation / manual`
- 分类筛选
- 分页
- 评分筛选：
  - `importance_score`
  - `impression_depth`
  - 只看已评分事件
- 批量删除当前评分筛选结果

当前新增 API：
- `GET /api/events` 支持分数与来源筛选
- `POST /api/events/delete-by-score`

这套功能的定位是：
- 帮你低成本清洗历史低密度 generated 事件

---

## 8. 日程系统的当前正确理解
主要在：
- [server/plan_engine.py](D:/Eloise/coding/凯尔希状态机/server/plan_engine.py)

### 8.1 当前日程系统的产品定位
- 角色自己的独立生活骨架
- 不是围绕 user 预设共同剧情的安排器
- 不再是自动制造事件的后台来源

### 8.2 当前真实行为
#### 已经实现
- 计划项执行不再自动创建 event
- 计划项只更新：
  - `status`
  - `outcome`
  - `executed_at`
- 必要时只补 trace

#### 已经明确的产品边界
- 不再规划与 user 的见面、共读、共同活动
- 关系状态可以影响节律和倾向
- 但不能直接生成 user 相关计划项

### 8.3 baseline 机制
新增：
- `POST /api/plans/{plan_id}/use-as-baseline`

用途：
- 允许你手动调整一版日程
- 再把它设为新的计划生成基线
- 避免系统继续沿旧日程逻辑错误外推

---

## 9. 生活流层的当前理解
### 9.1 `life_flow_trace`
职责：
- 承接“这一段时间是怎样流过去的”
- 不是 event
- 不是 key record
- 不是 snapshot

### 9.2 `slowline`
职责：
- 承接中期、有方向的生活线
- 不必每天显性推进
- 让“远景议题”不必硬塞进每天的细节事件里

### 9.3 `近景碎片池`
职责：
- 承接 today / yesterday 高突发、高现实张力、难自然并入主线的碎片
- 不要求它们都变成 event
- 不要求它们都变成主线

### 9.4 `近程记忆桥`
职责：
- 把更早但仍与当前相关的内容压缩成可注入背景
- 不再平铺旧事件细节
- 用摘要连接 today / yesterday 与更远背景

### 9.5 自主生活与对话线必须等权
这是当前架构的硬边界：
- 近期主线编排时，自主生活内容和 conversation 内容都应保留可见位置
- 不能谁信息更密就整体吞没另一侧

---

## 10. 短期关系感知层的当前理解
`relationship_state` 的目标不是“替代关系剧情”，而是提供一种短期、可漂移、可影响生活节律的关系态势层。

### 当前应影响的东西
- 对话开场时的关系氛围
- 可主动开启的话题
- 更想靠近 / 观察 / 留空间 / 确认近况的倾向
- 当天计划密度和节律偏好

### 当前不应做的事
- 不应直接变成 user 共同行动日程
- 不应直接制造 user 相关后台事件
- 不应把短期波动写回 L2

换句话说：
- `L2` 继续负责长期关系模式
- `relationship_state` 负责短中期波动
- `snapshot` 只体现即时影响

---

## 11. 记忆存储与检索
抽象接口在：
- [server/memory_store.py](D:/Eloise/coding/凯尔希状态机/server/memory_store.py)

实现有两套：
- `KeywordMemoryStore`
- `VectorMemoryStore`

### 11.1 VectorMemoryStore 仍是主路
当前统一索引：
- event
- snapshot
- key record
- world book
- cold summary / 压缩摘要

### 11.2 冷记忆压缩仍然存在
`VectorMemoryStore.compact_cold_memories()` 仍负责：
- 把更久远的大量碎片压缩成摘要型记忆项
- 减少向量噪声
- 为更长期的连续性提供低成本背景

这和新的 `近程记忆桥` 是互补关系：
- 冷压缩更偏底层存储优化
- 记忆桥更偏注入层展示与恢复连续性

---

## 12. Prompt / 配置系统的当前理解
### 12.1 `config.yaml`
仍负责启动级配置：
- 服务地址
- 主 LLM 基础配置
- 数据库路径
- memory store 类型

### 12.2 `system_settings`
仍负责运行时配置：
- L1 / L2 内容
- prompts
- 自动化开关
- scheduler 参数
- vector 参数
- plan 参数
- 运行时 LLM 覆盖

### 12.3 当前一个非常实际的判断
如果某个行为和代码看起来不一致，请优先检查：
1. prompt 是否被前端设置页覆盖
2. `system_settings` 是否已有旧值
3. 运行时是否还在用旧 prompt

这是为什么过去会出现：
- 注入结构已经改了，但展示仍像旧版本
- 环境生成结构已改，但文本仍沿旧模板跑

---

## 13. API / 前端 / Android 的当前定位
### 13.1 MCP
仍是给上游对话系统用的主入口。
关键工具依然包括：
- `get_current_state`
- `summarize_conversation`
- `reflect_on_conversation`
- `recall_memories`
- `upsert_event`
- `upsert_key_record`
- `recall_key_records`
- `execute_profile_evolution`

### 13.2 REST API
现在已经不仅是 MCP 的附属，而是完整的管理端 API。  
尤其这轮后：
- 事件历史清洗接口
- baseline 接口
- 计划、事件、key record、vector、debug 等管理链路
都已经是正式产品能力的一部分。

### 13.3 Web 前端
当前前端仍然是：
- 多页面静态 HTML
- 大量逻辑集中在 [web/app.js](D:/Eloise/coding/凯尔希状态机/web/app.js)

优点：
- 改动快
- 适合快速迭代产品逻辑

缺点：
- 有历史覆盖式实现
- 后定义覆盖前定义较多
- 后续重构成本会上升

### 13.4 Android
仍是轻量客户端，不是完整控制台。  
不要把它当成与 Web 等价的管理入口。

---

## 14. 当前最值得注意的技术现实
### 14.1 `state_machine.py` 与 `app.js` 存在“后定义覆盖前定义”
这是当前代码最容易让接手者误判的地方。

尤其在：
- [server/state_machine.py](D:/Eloise/coding/凯尔希状态机/server/state_machine.py)
- [web/app.js](D:/Eloise/coding/凯尔希状态机/web/app.js)

当前存在：
- 同名方法多次定义
- 后补 override 行为
- JS 后定义函数覆盖前定义函数

这不是理想状态，但它解释了：
- 为什么某些函数“明明前面写了旧逻辑，实际运行却是后面的版本”

接手时请始终看：
- Python 中类体里最后一个同名方法
- JS 文件里最后一个同名函数定义

### 14.2 AI_HANDOFF 曾经存在编码和内容过时问题
这一版已经整体重写为最新架构说明。  
不要再参考旧的乱码段落或老版本 README 来判断当前系统行为。

### 14.3 数据库内容可能带有旧结构遗留文本
例如旧 summary 中的标题文本、旧事件标题风格、旧 slowline 摘要结构等，都可能作为存量数据继续存在。  
所以：
- 代码修正后，页面仍可能因为旧数据而表现得像旧逻辑
- 这时要区分：是“链路没改对”，还是“旧数据需要清洗”

---

## 15. 接手时建议先验证什么
不要先看代码猜，请先跑真实行为检查。

### 第一组：事件与清洗
1. 日程执行后是否不再自动新增 event
2. 事件历史页的来源筛选是否真实生效
3. 分页是否能翻过 50 条
4. 评分筛选是否只命中已评分事件
5. “删除当前评分筛选结果”是否真的删库并清向量

### 第二组：注入层
1. `get_current_state` 是否按“长期/稳定在前，即时/切片在后”组织注入
2. 是否还会看到四段式 conversation summary 标题原样泄漏
3. `近期生活主线` 和 `近景碎片池` 是否仍有大面积重复
4. 自主生活线和对话线是否都有可见位置

### 第三组：关系感知与计划
1. relationship_state 是否会随时间漂移
2. conversation_end 后是否会更新关系感受与主动话题倾向
3. 日程是否仍会偷偷规划与 user 的共同行动
4. baseline 是否能成功改变后续计划生成起点

---

## 16. 如果继续迭代，优先级建议
### 高优先级
1. 清理 `state_machine.py` / `app.js` 中的历史覆盖式实现
2. 继续收束“主线 / 碎片 / 记忆桥 / slowline”的职责边界
3. 进一步提高事件标题质量
4. 做更稳的历史数据清洗工具，而不是只靠人工逐条删

### 中优先级
1. 为 `relationship_state` 增加更清楚的管理/诊断可视化
2. 为 `slowline` 增加明确的后台更新与调试入口
3. 把事件页和计划页的“清洗/重基线”能力做得更可解释

### 低优先级
1. Android 扩展更多管理能力
2. WeChat 桥接真正落地
3. 更深的 NPC 生态与互动系统

---

## 17. 最后一句话
当前这套系统最准确的理解是：

> 一个围绕凯尔希这个角色构建的、带有独立生活推进、短期关系感知、分层记忆注入、长期事实沉淀与历史清洗能力的运行时后端。

如果你接手后把它只当成“事件 CRUD + 快照 CRUD 服务”，基本一定会误判。  
真正要守住的是：
- 状态切片
- 生活连续性
- 关系短期波动
- 结构化长期事实
- 独立生活与对话交互的平衡
