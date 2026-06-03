# 修复计划：快照调度循环烧钱事故

> 交给 Codex 执行。本文档自包含，不依赖任何对话上下文。
>
> **背景**：后台 snapshot scheduler 21 小时未生成新快照，但 LLM API 被以 ~2–3 分钟的固定节奏、**完全相同的 13161 token prompt** 重复调用，把账户余额烧光。详细排查见本文末"附录：根因分析"。
>
> **执行顺序**：A → B → C。A 段不依赖其他改动，可独立合入并立刻生效。B 段是根治。C 段是加固。每个子步骤后都列出验收标准与必须新增/修改的测试。

---

## 阶段 A：紧急止血（A1 已跳过，直接做 A2 + A3）

### A2. LLM 预算硬上限（hard cost cap）

**目标**：任何 bug 都不可能再把余额烧光——超额直接拒绝调用并停掉 scheduler。

#### 改动点

1. **`config.example.yaml`** 新增段：
   ```yaml
   llm_budget:
     enabled: true
     hourly_token_limit: 30000      # 单小时累计 prompt+completion token 上限
     daily_token_limit: 200000      # 单日累计上限
     # 触发上限后的动作：reject = 抛异常；其余调用者捕获后停 scheduler
     on_exceed: reject
   ```
   同步更新 `server/config.py` 的解析（参考既有 config 字段写法），暴露为 `Config.llm_budget`。

2. **`server/llm_client.py`**：
   - 新增异常类 `class BudgetExceeded(RuntimeError): ...`，与既有 `LLMError` 同层。
   - 在 `LLMClient` 上挂一个轻量预算追踪器（**进程内即可**，不必持久化——重启清零是可接受的；如果想跨重启持久，写到 sqlite `llm_budget_usage(date, hour_bucket, tokens)` 一张表里，二选一）：
     ```python
     class _BudgetTracker:
         def __init__(self, cfg): self.cfg = cfg; self._hour = deque(); self._day = deque()
         def check_and_reserve(self, est_tokens: int):
             # 用 prompt token 估算（messages 总字符数 / 3 取整再 +500 余量）做预扣
             # 滑动窗口清理过期项
             # 若 hourly_sum + est > limit 或 daily_sum + est > limit -> raise BudgetExceeded
         def record_actual(self, actual_tokens: int):
             # 调用成功后用真实 usage 校准
     ```
   - 在 [llm_client.py:113](../server/llm_client.py) `chat()` 入口先 `check_and_reserve`，调用成功后 `record_actual`；失败时也要把预扣回滚（或直接以预扣为准，更保守）。
   - `BudgetExceeded` 必须**绕过** [llm_client.py:113-160](../server/llm_client.py) 既有的 `TRANSIENT_STATUS_MAX_RETRIES` 重试逻辑——它不是 transient。

3. **`server/main.py`** scheduler loop（见 [main.py:95-160](../server/main.py)）：
   - 捕获 `BudgetExceeded` 后**立即把 scheduler 标记为 `paused=True`**，写一行事件到日志和（如果方便）数据库 events 表，然后跳出 `while True`（或进入"每 10 分钟检查一次预算是否已重置"的休眠态）。
   - 这一处与 C1 熔断器复用同一个 `paused` 标志。

#### 验收

- 单元测试：mock 一个会让 tracker 立刻超额的配置，调用 `LLMClient.chat` 必须抛 `BudgetExceeded` 且**不发出任何 HTTP 请求**（用 mock 验证 transport 调用次数为 0）。
- 单元测试：scheduler loop 在收到 `BudgetExceeded` 后必须在下一次 tick 之前停摆。

---

### A3. Prompt hash 短路去重

**目标**：哪怕 scheduler 重复进入同一个 tick，**完全相同的 prompt 在冷却窗口内只允许发出一次**。这一步单独就能切断本次事故。

#### 改动点

1. **`server/llm_client.py`**：
   - 在 `LLMClient` 上挂一个 `_recent_prompts: dict[str, float]`（key = sha256，value = 发出时间戳），用普通 dict + 周期性清理或 `collections.OrderedDict` LRU（容量 256）。
   - 新增异常 `class DuplicatePromptError(RuntimeError): ...`。
   - 在 `chat()` 入口（A2 的 `check_and_reserve` **之前**）算 `digest = hashlib.sha256(json.dumps({"model":..., "messages":..., "tools":..., "temperature":...}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()`。
   - 若 `digest` 在过去 **60 秒** 内出现过 → 抛 `DuplicatePromptError(digest, last_sent_at)`。冷却窗口写在配置里：
     ```yaml
     llm_dedup:
       enabled: true
       window_seconds: 60
     ```
   - 调用成功后才把 `digest -> now` 写入 map（失败的 prompt 不进 map，方便用户改 prompt 后立刻重试）。

2. **调用方处理**：
   - [state_machine.py:2500-2623](../server/state_machine.py) `run_snapshot_scheduler_tick` 和 [state_machine.py:2625-2819](../server/state_machine.py) `reflect_on_conversation` 调到 LLM 的位置（`snapshot_llm.chat(...)`）必须捕获 `DuplicatePromptError`，按"本 tick 跳过、保持 baseline 不变、不更新 latest_snapshot 时间戳、记一条 warning 日志"处理，**而非** re-raise 让 scheduler loop 失败计数 +1（否则会触发 C1 熔断器误判）。

#### 验收

- 单元测试：连续两次用相同 messages 调 `chat()`，第二次必须抛 `DuplicatePromptError` 且不发 HTTP。
- 单元测试：等待 > window_seconds 后第三次调用必须正常通过。
- 单元测试：scheduler tick 在 `DuplicatePromptError` 下不应该使 `consecutive_failures` 计数器递增。

---

## 阶段 B：根治（B1–B4）

### B1. 修复 snapshot 排序根因

**问题**：snapshots 表 `created_at` 列同时存在 `...Z`（UTC）和 `+08:00`（本地带偏移）两种字面量，SQL `ORDER BY created_at DESC` 按字符串排，导致 `+08:00` 行被排到 `Z` 行之后；`get_latest_snapshot` 返回的"最新"不一定是真正最新。这是 21 小时无新快照 + reflect_on_conversation 落库错乱的源头之一。

> 工作区里已经有未提交的 [server/database.py](../server/database.py) 改动和 [tests/test_database_snapshot_ordering.py](../tests/test_database_snapshot_ordering.py)。先看这些 diff 是否已经实现了下面的方案，若已实现只做收尾。

#### 改动点

1. **统一时间字面量**：所有写 `created_at` 的路径（[database.py:622-639](../server/database.py)、相关 importer、迁移脚本）必须只产出 `YYYY-MM-DDTHH:MM:SS.ffffffZ` 形式（`format_utc_instant_z`）。
2. **改 `get_latest_snapshot`** 的 ORDER BY 用 `ORDER BY julianday(created_at) DESC, id DESC`（julianday 把带 `Z` / `+08:00` 都解析成同一时间轴）。同样改动应用到其他按 `created_at` 排序的查询：grep `ORDER BY created_at` 全仓审一遍。
3. **一次性 backfill migration**：新增 `migrate/normalize_snapshot_created_at.py`，把所有非 `Z` 结尾的 `created_at` 解析成 datetime 再写回 `Z` 格式。脚本必须幂等（重跑无副作用）。在 README / deploy 指南里加一句"升级时执行此脚本"。
4. **测试**：[tests/test_database_snapshot_ordering.py](../tests/test_database_snapshot_ordering.py) 已存在——确保通过；补一个 case：混合插入 `Z` 和 `+08:00` 行，`get_latest_snapshot` 返回的必须是按真实 UTC 时刻最新的那行。

#### 验收

- backfill 脚本在样本库上跑过，所有行 `created_at` 形如 `...Z`。
- 所有 snapshot 排序相关单测通过。

---

### B2. 引入 in-flight marker / 幂等屏障

**目标**：LLM 已经烧过的 prompt **不能因为副作用失败而再烧一次**。

#### 改动点

1. **数据库**：snapshots 表新增列（migration）：
   ```sql
   ALTER TABLE snapshots ADD COLUMN status TEXT NOT NULL DEFAULT 'done';
   ALTER TABLE snapshots ADD COLUMN prompt_hash TEXT;
   ALTER TABLE snapshots ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
   CREATE INDEX idx_snapshots_status ON snapshots(status);
   CREATE INDEX idx_snapshots_prompt_hash ON snapshots(prompt_hash);
   ```
   - `status` ∈ `{'in_flight','done','failed'}`。已有历史行 `'done'`。
   - migration 必须幂等，能多次执行。

2. **`server/state_machine.py` `_advance_until_locked`**（[state_machine.py:5978-6471](../server/state_machine.py)）改造为：
   ```
   for each checkpoint:
       prompt_hash = sha256(prompt_payload)
       # 已完成？跳过
       if db.find_snapshot_by_prompt_hash(prompt_hash, status='done'): continue
       # 进行中？看 started_at；< 10min 直接跳过本 tick，> 10min 视为悬挂、复位为 failed
       row = db.find_snapshot_by_prompt_hash(prompt_hash, status='in_flight')
       if row and now - row.started_at < 10min: skip_this_tick; continue
       if row: db.mark_failed(row.id)
       # 失败次数 >= 3 -> 死信，记 warning，跳过
       if db.failed_attempts(checkpoint_cst, prompt_hash) >= 3:
           log_warning_with_dead_letter(); continue
       # 占坑（事务提交）
       row_id = db.insert_snapshot_placeholder(status='in_flight', prompt_hash=..., started_at=now, attempt_count=prev+1)
       try:
           result = await snapshot_llm.chat(...)   # 真正烧 token 在此
           db.finalize_snapshot(row_id, result, status='done')  # 把内容写回，状态置 done
       except Exception:
           db.mark_failed(row_id)
           raise   # 让上层 tick 失败计数 +1（除 DuplicatePromptError/BudgetExceeded 之外，见 A3）
   ```
   - **关键不变量**：LLM 调用之前，DB 中必须已经存在 `in_flight` 占位行（事务已 commit），这样即使进程崩溃也能被下一次 tick 识别为"悬挂"，而不是重新发起。

3. **`reflect_on_conversation`**（[state_machine.py:2625-2819](../server/state_machine.py)）用同一套模式包裹：先 `insert_snapshot_placeholder(prompt_hash, status='in_flight')`，再 LLM，再 `finalize`。

#### 验收

- 单元测试：在 LLM 调用成功后、`finalize_snapshot` 调用前手动抛异常 → 下一次 tick 用相同 prompt **不能再发起 LLM 调用**（mock transport 计数仍为 1），且 attempt_count 递增。
- 单元测试：连续 3 次 LLM 失败后，第 4 次 tick 必须跳过该 checkpoint 并打 dead-letter warning。
- 单元测试：`in_flight` 行超过 10 分钟未完成，下次 tick 必须把它复位为 `failed` 而非永远跳过。

---

### B3. 主事务 / 副作用拆分

**问题**：LLM 出结果后还要写 OB hold、relationship_thought、disturbance_pulse、life-flow-trace、slowlines、plan replan 等 6+ 处副作用（[state_machine.py:6333-6469](../server/state_machine.py)、[state_machine.py:2710-2810](../server/state_machine.py)）。任何一个副作用抛错就让整个 tick 失败回重试。

#### 改动点

- **主事务**：仅写 `snapshots` 主表（B2 中的 `finalize_snapshot`）。提交即视为成功。
- **副作用**：在主事务 commit 之后，每一个副作用单独 `try/except Exception as e: logger.warning("post-snapshot side-effect %s failed: %s", name, e)`，**不向上抛**。
- 把所有副作用列成一个 `POST_SNAPSHOT_EFFECTS: list[Callable]`，顺序执行，逐项保护。
- 副作用是否成功通过新增列 `snapshots.side_effects_status JSON`（`{"ob_hold":"ok","relationship_thought":"failed:Foo",...}`）记录，便于事后人工补刀。

#### 验收

- 单元测试：让任一副作用抛 RuntimeError，主 snapshot 仍然 `status='done'`，tick 整体返回 success；`side_effects_status` 字段如实记录失败项。

---

### B4. `reflect_on_conversation` 加 idempotency_key

**问题**：前端如果对失败请求自动重试，每次都会跑一遍完整 LLM 流程。

#### 改动点

1. MCP 工具签名（[mcp_tools.py:606-627](../server/mcp_tools.py)）的 `reflect_on_conversation` 增加可选参数 `idempotency_key: str | None`。建议前端传 `sha256(conversation_summary 内容)`。
2. 服务端入口先查 `db.find_reflect_result_by_idem_key(key)`：5 分钟内已有成功结果 → 直接返回缓存结果，**不调 LLM**。
3. DB 新建 `reflect_idem_cache(key TEXT PRIMARY KEY, result_json TEXT, created_at TEXT)`，附 TTL 清理（每 1 小时清一次 > 24h 的）。
4. [api_routes.py:649-655](../server/api_routes.py) 把 `idempotency_key` 透传给底层。
5. 前端 `web/` 里调用方（grep `reflect_on_conversation`）改为在请求前生成 key。

#### 验收

- 单元测试：同一 idempotency_key 连续两次调用 → LLM 只被打一次，两次返回相同结果。
- 单元测试：不传 key 时行为保持原样（不破坏向后兼容）。

---

## 阶段 C：加固预防（C1–C4）

### C1. Scheduler 熔断器

**目标**：任何持续失败的 scheduler 在烧光预算之前先停下。

#### 改动点（[server/main.py:95-160](../server/main.py)）

```python
async def _snapshot_scheduler_loop(state_machine, interval_sec, ...):
    consecutive_failures = 0
    paused_until = 0.0
    backoff_steps = [30, 120, 300, 900, 1800]   # 30s, 2m, 5m, 15m, 30m
    while True:
        if scheduler_paused_flag.is_set():
            await asyncio.sleep(60); continue
        if time.monotonic() < paused_until:
            await asyncio.sleep(paused_until - time.monotonic()); continue
        try:
            await state_machine.run_snapshot_scheduler_tick()
            consecutive_failures = 0
        except BudgetExceeded:
            scheduler_paused_flag.set()         # 直接停摆等人介入
            emit_admin_event("scheduler_paused_budget")
            continue
        except DuplicatePromptError:
            # 不计入失败
            pass
        except Exception:
            logger.exception("snapshot tick failed")
            consecutive_failures += 1
            if consecutive_failures >= 10:
                scheduler_paused_flag.set()
                emit_admin_event("scheduler_paused_failures")
                continue
            step = backoff_steps[min(consecutive_failures-1, len(backoff_steps)-1)]
            paused_until = time.monotonic() + step
            continue
        await asyncio.sleep(interval_sec)
```

- `scheduler_paused_flag` 是个全局 `asyncio.Event`（或简单的 bool + lock），暴露在 `state_machine` 上；管理 API 可以读 / 重置（参见 C3）。
- 同样的逻辑套用到 `_life_scheduler_loop`。

#### 验收

- 单元测试：连续 10 次 mock tick 抛异常 → flag 置位，之后 loop 不再调用 tick。
- 单元测试：抛 `BudgetExceeded` → flag 立即置位（不必等 10 次）。
- 单元测试：抛 `DuplicatePromptError` → consecutive_failures 不递增。

---

### C2. 预算告警事件

**目标**：在到 hard cap 之前先告警，避免突然停摆。

#### 改动点

- A2 的 `_BudgetTracker` 在以下阈值跨越时各发一次事件（同窗口期内只发一次）：
  - hourly 80% / 95%
  - daily 80% / 95%
- 事件写入：既有的事件流（同 C1 的 `emit_admin_event`）+ 日志 WARNING。
- 前端 dashboard（参见 C3）在每个窗口期顶部显示 token 累计进度条。

#### 验收

- 单元测试：连续多次 `record_actual` 跨越 80% → 仅触发一次 80% 告警；跨越 95% 时再触发一次。

---

### C3. `/admin/health` 可观测页

**目标**：这次能熬 21 小时无人发现，根因是没有可观测性。

#### 改动点

1. **后端**：[server/api_routes.py](../server/api_routes.py) 加 `GET /api/admin/health` 返回 JSON：
   ```json
   {
     "scheduler": {
       "snapshot": {"paused": false, "paused_reason": null, "consecutive_failures": 0, "last_tick_at": "...", "last_success_at": "..."},
       "life": { ... }
     },
     "llm_budget": {"hourly_used": 12345, "hourly_limit": 30000, "daily_used": 80000, "daily_limit": 200000},
     "recent_ticks": [ {"at":"...", "result":"ok|skipped|failed", "tokens": 13161, "duration_ms": 26000} ... last 50 ],
     "in_flight_snapshots": [ {"id":..., "checkpoint_cst":"...", "started_at":"...", "age_s": 42} ... ],
     "last_snapshot_at": "...",
     "last_reflect_at": "..."
   }
   ```
2. **管理动作端点**（POST，要求一个简单的 admin token，从 config 读）：
   - `POST /api/admin/scheduler/resume` — 清 paused_flag，重置 consecutive_failures。
   - `POST /api/admin/budget/reset` — 清空当前窗口预算计数（仅调试 / 应急）。
3. **前端**：新页面 `web/admin-health.html` + `web/admin-health.js`，每 10s 拉一次。
   - 顶部三个卡片：scheduler 状态（绿/橙/红）、当前预算条、最近一次 tick 时间。
   - 下方两个表格：最近 50 次 tick、in_flight snapshots。
   - 两个按钮：恢复 scheduler、重置预算（带二次确认）。
4. **现存 dashboard**：[web/ob-dashboard.html](../web/ob-dashboard.html) 顶部加一个小角标，显示"scheduler: ok / paused"和"distance to last snapshot: 12m"（直接消费 `/api/admin/health` 的部分字段）。这样不打开 admin 页也能看到异常。

#### 验收

- 手测：在 dev 环境制造 5 次连续 tick 失败 → admin 页 scheduler 卡片变橙；10 次 → 变红 + paused。
- 手测：预算用到 80% → 卡片告警条变橙。
- 手测：点恢复按钮 scheduler 恢复，consecutive_failures 清零。

---

### C4. 回归测试集

集中放在 [tests/](../tests/) 下，命名 `test_snapshot_loop_safety_*.py`：

1. `test_snapshot_loop_safety_dedup.py`：模拟 LLM 在副作用阶段抛错 → 下一次 tick 不再发起相同 prompt 的 LLM 调用。
2. `test_snapshot_loop_safety_budget.py`：mock 配置 budget 小到一次调用就超限 → tick 立刻被 `BudgetExceeded` 切断，且 loop 进入 paused。
3. `test_snapshot_loop_safety_circuit_breaker.py`：连续抛 10 次普通异常 → loop 进入 paused 且不再调用 tick。
4. `test_snapshot_loop_safety_reflect_idem.py`：同一 idempotency_key 调用 reflect_on_conversation 两次 → LLM 只被打一次。
5. `test_snapshot_loop_safety_ordering.py`：snapshots 表插入 `Z` 和 `+08:00` 混合行 → `get_latest_snapshot` 按真实 UTC 时刻返回最新行。
6. `test_snapshot_loop_safety_in_flight_recovery.py`：DB 中存在一条 11 分钟前的 `in_flight` 行 → 下次 tick 把它复位为 `failed` 而非永远跳过。

CI 必须跑全套（如果当前没有 CI，则在 `tests/README.md` 里说明 `pytest tests/test_snapshot_loop_safety_*.py` 必须本地全绿才能合并）。

---

## 合并顺序与回滚预案

| 顺序 | 步骤 | 可独立合并 | 回滚方式 |
|----|----|----|----|
| 1 | A2 (budget cap) | ✅ | 改 config `llm_budget.enabled: false` |
| 2 | A3 (prompt dedup) | ✅ | 改 config `llm_dedup.enabled: false` |
| 3 | B1 (ordering fix + backfill) | ✅ | backfill 幂等，代码改动可 revert，但 schema 不需 |
| 4 | B2 (in_flight marker) | 依赖 B1 | revert + migration down（保留新列不删，旧代码不读即可） |
| 5 | B3 (主/副事务拆分) | 依赖 B2 | 直接 revert |
| 6 | B4 (reflect idem) | ✅ | revert |
| 7 | C1 (熔断器) | 依赖 A2 | revert |
| 8 | C2 (告警) | 依赖 A2 | revert |
| 9 | C3 (admin 页) | 依赖 A2/C1 | 仅新增端点和文件，删除即可 |
| 10 | C4 (测试集) | 跟随对应改动 | — |

每一步 PR 必须：
1. 包含本步骤"验收"列出的全部测试；
2. PR 描述里贴出该步骤实测的 `pytest -k <pattern>` 输出片段；
3. 不动其它无关代码。

---

## 附录：根因分析（保留供后续审阅）

### 出血点 1：LLM 调用在前，落库在后，失败无去重
- [state_machine.py:2500-2623](../server/state_machine.py) `run_snapshot_scheduler_tick` / [state_machine.py:5978-6471](../server/state_machine.py) `_advance_until_locked`：先读 `latest_snapshot` → 拼 prompt → **调 LLM** → 然后才 `db.insert_snapshot` 并写 6+ 个副作用表。任何后续步骤抛错都让本次 LLM 调用作废。
- 没有 in-flight marker / 没有 prompt hash 去重 → 下一 tick 完全相同的 baseline → 完全相同的 13161 token prompt → 再烧一次。
- 603ce3f 的 `_limit_checkpoints(mode="latest")` 只把单次烧量限制到 3 个 checkpoint，没切断循环。

### 出血点 2：调度 loop 没有失败熔断
- [main.py:95-160](../server/main.py) `_snapshot_scheduler_loop` / `_life_scheduler_loop` 用 `try/except Exception: logger.exception` 吞掉所有错误后照原节奏继续。

### 出血点 3：DB 排序错乱触发背景重放
- snapshots 表 `created_at` 列里 `...Z` 和 `+08:00` 两种格式混存，字符串排序错位（`+08:00` 字符串排在 `Z` 之后）；`get_latest_snapshot` 拿到的"最新"行可能是错的。
- 工作区里已有未提交的 [database.py](../server/database.py) diff 和 [tests/test_database_snapshot_ordering.py](../tests/test_database_snapshot_ordering.py) 印证此问题。

### 出血点 4：`reflect_on_conversation` 同样"先烧后写"
- [state_machine.py:2625-2819](../server/state_machine.py) — LLM 在 2689 行，`insert_snapshot` 在 2710 行，之后还有 6 个副作用写入。前端一重试就再烧。

### 出血点 5：`llm_client` 自带 retry 无全局熔断
- [llm_client.py:113-160](../server/llm_client.py) 对 5xx/429 有 `TRANSIENT_STATUS_MAX_RETRIES` 重试，没有全局熔断 / 日预算上限。

### 嫌疑提交
- **603ce3f** "Limit snapshot scheduler catch-up to latest three"：一次 58k 行的大改伪装成小修，引入或暴露了上述链路上的多处问题。
