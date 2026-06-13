# OB 记忆：统一整合原语 + 稳定层模型（规格 v2）

> Status: **Phase 1 + 2 已落地**。本文件是 Phase 1–4 的实现依据；§10 标注各阶段进度。
> 不含可执行代码；定义字段语义、不变量、交互协议与每层参数。
>
> 已完成前置（PR #3，已合并）：
> - OB 写入去重改为**模型 gated 两段式**（`hold` pending → `merge_into`/`force_new` + `merge_buckets`）。
> - **feel aging**：feel 不再恒 50，温和衰减并可被后台归档。
> - 删除冗余的 `grow` MCP 工具（人工 web 面板 `/api/ob/grow` 保留）。

---

## 1. 背景与问题（简述）

OB 的结构病：**写入无界、浮现有界、整合靠模型自觉（失灵）、recall 很少用** → 记忆沦为只写不读的沉积。
- 衰减其实温柔（dynamic 半衰期 ~2 周）；"断崖"是类型台阶（permanent 999 / feel 50 / dynamic 个位数）。
- `crystallize_feel` 只喂模型 5 条样本却把整簇标 `crystallized` 隐藏 → 抹掉不可还原的 moment。
- `permanent` 层把多条正交的轴压成一团：`pinned`/`protected`/`type=permanent`/`domain` 语义重叠，无退役机制，注入纯按新近会挤掉基础原则。

Layer 1（写入去重 + feel aging）已堵住下层积压。本规格解决**上层**：整合（feel → 稳定层）与稳定层自身的取舍，以及 `permanent` 的重新定位。

---

## 2. 二维模型（type × role）

稳定层的混乱来自用 `pinned`/`protected`/`domain` 去近似多条独立属性。正解是把它们拆成两条正交轴。

### 2.1 轴一 `type`：会遗忘 vs 豁免遗忘

- `dynamic` / `feel`：**会遗忘**。相关性随时间衰减 → 最终归档（`archive` 改 `type=archived`、移入 archive 目录、退出 `list_buckets(include_archive=False)` 的全部活跃浮现/recall 路径）。文件与 embedding 不删，可 `restore`——是"冷存储/可被重新唤起的淡出"，不是删除。
- `permanent`：**豁免遗忘**。相关性与时间无关 → 永不衰减、永不归档、永在活跃检索池。

> **permanent 的本质不是"更珍贵的 dynamic"，而是"被判定为不随时间失效、因而豁免遗忘"。**

### 2.2 轴二 `role`：主动注入 vs 仅检索

`permanent` 内部**只用 `role` 这一个轴**再分类（不再用 pinned/protected/domain 表达行为）：

| role | 内容 | 形态 | 注入策略 | 退役 |
|---|---|---|---|---|
| `evolving_principle` | 关系整体感受 / 当前相处模式 | feel 聚合 | get_current_state，**新近 N=5** | 被新结晶顶出窗 → **自动转 `anchor`**（保留可 recall） |
| `standing_invariant` | 边界、稳定偏好、重大共识 | 准则/约定 | get_current_state，**始终注入、与年龄无关**（量少，全量） | 仅显式废止 |
| `anchor` | 原汁原味的珍贵关键事件 | 原始 episodic | **不自动注入**；recall + 偶发 echo | 不退役、永不占注入 |

要点：
- `evolving_principle` 按新近合理——关系在演化，最新结晶最能代表"现在的关系"。
- `standing_invariant` 与年龄无关——两年前定的边界/偏好也必须每次注入。这是与 evolving 的关键区别。
- `anchor` = 被"豁免遗忘"的原始事件：从 dynamic 的衰减轨道里拎出、钉进永不淡出的池，但不重要到要每次注入。

### 2.3 字段定义

- 在 permanent bucket 的 metadata 增加 `role ∈ {evolving_principle, standing_invariant, anchor}`。
- `role` 是 permanent 内部行为的**唯一**决定项：驱动注入与浮现。

### 2.4 Legacy 兼容（pinned/protected 保留）

`pinned`/`protected`/`domain` **保留**，不删除。未显式标注 `role` 的现存 permanent 走**回退推断**，保证迁移期不破坏行为：

| 现存条件（无 `role`） | 回退视作 | 理由 |
|---|---|---|
| `pinned` 或 `protected` 为真 | `evolving_principle` | 接近当前"注入最近 5 条 pinned 原则"的行为 |
| 既非 pinned 也非 protected 的 permanent | `anchor`（仅 recall） | 修复"非 pinned 高分却冒上来"的意外浮现 |

- 一旦该 bucket 被**手动**赋了 `role`，回退推断不再生效，以显式 `role` 为准。
- 迁移由用户在 **web 前端手动逐条标注**（不做自动迁移脚本）。Phase 1 需在 web 面板提供设置 `role` 的控件。

---

## 3. 不变量（所有接入层共享）

- **I1 加法自动、有损 gated**：生成聚合是加法、可自动提示产出；任何隐藏/合并/退役/降权必须模型**显式点名**。
- **I2 永不销毁、永远可 recall**：被聚合的源永远保留；最多降权或退出注入，绝不 `delete`。
- **I3 模型判语义、后台做账**：keep/merge/retire 由模型基于**全文**判断；后台只供干净信号 + 机械应用。
- **I4 分类信号年龄无关**：用 `arousal` / `importance` / `uniqueness`（到簇心距离），**不用**被时间污染的复合衰减分。
- **I5 约束注入、不约束存储**：存储便宜、注入稀缺。无硬性条数上限；用 role + 注入窗控制进上下文的量。
- **I6 自动度随层升高而降低**：feel 层可自动提示聚类；稳定层（principle）**manual-first**，原语是评审助手不是 autopilot。

---

## 4. 统一整合原语（Consolidation Primitive）

### 4.1 概念

一个层无关的"**聚类 → 迭代评审-综合 → keep/demote → 保留锚点**"操作。差异只在**内容**与**注入位置**。参数化用于两处应用（§5）。

### 4.2 交互协议：迭代游标式（核心，所有应用共享）

```
review(job_id, cursor="")
  → { items:[本批全文条目 + signals], size, cursor, next_cursor, has_more }

commit(job_id, cursor, synthesis, keep_ids=[], demote_ids=[])
  → 幂等 upsert 聚合体（用最新 synthesis 覆盖）；返回下一批；随时可停、部分结果即有效
```

- 每批限 **6 条全文**（保证综合质量靠分批全文阅读，不靠截断摘要）。
- 批内排序：**uniqueness 降序**（先看最独特的离群项，模式骨架早定）。
- **`demote_ids` 默认空 = 不隐藏任何东西**（纯加法）。
- 模型在自己上下文里携带并逐轮精修 `synthesis`/`keep`/`demote`；后台无状态（状态在聚合体 + cursor）。
- 预算友好：读两三批即可停，未读条目仍作为锚点保留、可 recall。

### 4.3 数据形态

**review packet item**
```json
{
  "id": "f_ab12",
  "full_text": "……该 feel 的完整正文……",
  "arousal": 0.8, "importance": 7, "uniqueness": 0.91,
  "source_dynamic": "d_77", "created": "..."
}
```

**聚合体（crystal）bucket metadata（principle）**
```json
{
  "type": "permanent",
  "role": "evolving_principle",
  "crystal_id": "c_55",
  "principle_pattern": "模型写的那段综合",
  "anchor_refs": [ {"id":"f_ab12","source_dynamic":"d_77","snippet":"一行短句"}, ... ],   // 全簇，含未读条目
  "source_ids": ["f_ab12", ...],
  "importance": 8
}
```
- `anchor_refs` 覆盖**整簇**（不只是模型读过的批），作为保留指针 → I2。源 feel 本体不删。

### 4.4 信号（年龄无关，后台预算）

- `arousal` / `importance`：写入时的静态属性。
- `uniqueness`：到簇心的距离（embedding 开时用向量；关时退化为文本新颖度，需标注）。
- **不**把复合衰减分喂给分类——它被年龄污染（旧的鲜活 moment 分数天然低）。

### 4.5 keep / demote 语义 + 晋升去向

- **keep_ids**：受保护、不被降权的项。其中：
  - 不可还原的 **moment** → 模型可将其**提升为 `anchor`**（占永不淡出池、仅 recall）。
  - 形成持续约束的**共识** → 提升为 `standing_invariant`（始终注入）。
- **demote_ids**（默认空）：冗余项退出自动浮现（仍可 recall），**绝不删除**。

---

## 5. 每应用参数表

### 5.1 `feel → principle`
| 维度 | 取值 |
|---|---|
| 触发 | dream 末尾轻提示（"有 N 簇成熟可结晶"），模型主动 |
| 源 | feel 相似簇 |
| 综合产物 | 一条 `evolving_principle` |
| keep 去向 | moment→`anchor`、共识→`standing_invariant` |
| demote 去向 | 冗余 feel 退出浮现（可 recall） |
| 自动度 | 加法可自动提示；有损 gated |

### 5.2 `principle-review`（稳定层评审）
| 维度 | 取值 |
|---|---|
| 触发 | **dream 里检测到 principle 重叠时轻提示**（非硬上限触发） |
| 源 | principle 重叠/相似簇 |
| 综合产物 | 合并后的 principle |
| keep 去向 | 保留更基础的原则 |
| demote 去向 | 过时/被取代的 principle → `type=archived`（可 recall） |
| 自动度 | **全程 gated、manual-first** |

### 5.3 明确排除
- `key_record`：保持**手动 + 新近注入**（与阶段性生活计划逻辑一致），**不接入**本原语；其 `update_if_exists` 自动去重**暂不改**。
- `dynamic`：现有 dedup/decay/resolve/archive 流程**不重写**，仅概念映射（`resolve` ≈ demote；旧 `grow` ≈ 本原语退化的单条即时版）。

---

## 6. 注入策略（参数，非原语核心）

| 来源 | 选择规则 |
|---|---|
| `evolving_principle` | 新近 N=5（保持现状） |
| `standing_invariant` | 始终全量注入（量少） |
| `anchor` | 不自动注入；recall + 偶发 echo |
| `key_record` | 新近（不变） |

原则：**约束注入窗，不约束底层存储条数**（I5）。permanent 长到几十上百也无妨，只要注入受 role + 窗控制。

---

## 7. 迁移（手动、web 驱动）

- 现存 ~34 条 permanent 由用户在 **web 前端逐条手动标注 `role`**。
- 未标注者走 §2.4 回退推断，行为不破。
- Phase 1 交付物之一：web 面板增加 `role` 设置控件。

---

## 8. 已定决策（基线，实现以此为准）

| # | 决策 |
|---|---|
| 1 | 二维模型（type=遗忘/豁免 × role=注入/检索）；permanent 内部仅用 `role` 分类 |
| 2 | `pinned`/`protected` **保留兼容**，未标注 permanent 走回退推断 |
| 3 | `standing_invariant` 全量始终注入 |
| 4 | `evolving_principle` 新近 N=5 |
| 5 | `evolving_principle` 顶出窗 → 自动转 `anchor` |
| 6 | keep 去向：moment→anchor、共识→standing_invariant |
| 7 | `principle-review` 触发 = dream 重叠轻提示 |
| 8 | 批大小 6、批内 **salience 降序**（uniqueness⊕arousal⊕importance）、`demote` 默认空 |
| 9 | migration 手动（web 操作） |
| 10 | **Gate 1**：单簇超 30 条时在更紧相似度上递归二次切分（防 review job 无界、保结晶聚焦） |
| 11 | **Gate 2**：review 天花板 = 3 批×batch；超出尾部只给统计摘要(count+时间跨度)，commit 仍整簇保留 anchor_refs |
| 12 | **Q3 深痕**：review 排序融合 arousal/importance，使情绪深痕前置可晋升；`demote` 对 arousal>0.7 软否决，需 `force_demote=True` 覆盖 |
| 13 | **第四档 settled**：commit 给整簇留存 feel 盖 `consolidated_into` 回指 → 已覆盖的簇在 hint/菜单**静默**；同主题再攒够 `min_cluster_size` 条新 feel 才重新浮现，并经回指**并回同一条结晶**（无显式 crystal_id 时自动复用）。源 feel 仍是 feel、可 recall、可重聚。`feel_crystals(include_settled=True)` 可手动复查 |

---

## 9. 推迟到 Phase 4+（不在本规格实现范围）

- `resolve` 软化（回归衰减而非硬删）+ dream 护栏。
- 读取侧：activation^0.3 反馈阻尼、**"旧回声"槽**、高阈值环境式 recall。
  - **回声槽定案（讨论结论）**：breath_bundle 2 个 free 槽分工——槽一保持纯随机（自由联想 + 抗反刍安全阀）；槽二做"回声"，**只从 anchor 池**按 **arousal ×（久未翻看）加权随机**抽一条，模拟非自主记忆。锁定 anchor 而非"任意古老高-arousal bucket"：后者基本已衰减归档，只有 anchor 作为 permanent 持久存在，所以 anchor 既是材料所在、又更干净；且配 `last_revisited` 实现"很久没翻到的那页突然翻开"。加权随机同时保住随机性与抗反刍：最珍贵那条不会每次必中。理由：最近的强烈情绪 relational 槽已覆盖，真正缺的是珍贵旧记忆不期然浮现。

### 9.1 breath_bundle 槽位改造计划（待办，Phase 4 读取侧）

> 核心判断：**深度要"稀有"不要"占槽"**。给 anchor/回声固定槽会同时廉价化珍贵记忆 + 挤掉日常连续性；做成概率性即化解两难——85% 的 bundle 永远是连续性主体，深度只在偶发命中时占半格。

| 层 | 频率 | 槽 | 负责 | 改动 |
|---|---|---|---|---|
| 前台 | 每轮 | personal 3 + relational 8 | 近期事件/感受/我的生活（连续性主体 ~85%） | feel 排序：纯新近 → **新近 ⊕ arousal**（改 `_feel_breath`，让有分量的近期感受多赖几轮；不加槽） |
| 背景 | 每轮 | free 槽 A | 漫游/自由联想 + 抗反刍安全阀 | 保持现状（随机 top-N 取 1） |
| 深处 | ~每三轮 | free 槽 B | 珍贵旧记忆不期然浮现（**仅 anchor**） | **概率回声 p≈0.35**：命中→**只从 anchor 池**按 arousal × (久未翻看) 加权随机；未命中→退化为漫游。理由：除 anchor 外的古老高-arousal bucket 基本已衰减归档，只有 anchor 作为 permanent 持久存在；锁定 anchor 更干净、正是材料所在，且配 last_revisited 实现"很久没翻到的那页突然翻开"。回声命中也 bump last_revisited。 |

- **anchor 不占固定 breath 槽**：其常态露出靠 get_current_state 的"相册目录"关键词（Phase 3 块3）；breath 里的回声只是偶发惊喜。
- **两类情绪记忆各归其位**：近期重感受 → relational feel 排序（新近⊕arousal，前台稳定）；古老深记忆 → 槽 B 回声（age×arousal，偶发）。
- **旋钮**：回声概率 p、褪色池年龄阈值、feel recency⊕arousal 权重。
- **前置依赖**：anchor 相册（Phase 3 块3）+ "褪色池"定义；先观察 arousal 加权衰减上线后老高-arousal 池实况再调参。
- 横切一致性：归档项 embedding 仍在向量库 → 将来纯语义 recall 要显式排除 archived；`key_record` 的 `update_if_exists` 与"模型 gated"哲学对齐。

---

## 10. 实现阶段（落地顺序）

1. **Phase 1 — permanent 重定义**（✅ 已落地）：引入 `role` 字段 + 回退推断 + 按 role 的注入/浮现分流 + web 面板 role 控件。修复"非 pinned 高分浮现""基础原则被挤出"等线上问题。
2. **Phase 2 — 原语 on `feel → principle`**（✅ 已落地）：迭代游标工具 + 锚点存储 + keep 晋升（anchor/standing）+ dream 轻提示。先验证原语。
   - 工具：`review_feel_cluster(cluster_id, cursor)` 分批 6 条全文（**salience 降序**）；`commit_feel_crystal(synthesis, anchor_ids, standing_ids, demote_ids, crystal_id, force_demote)` 幂等 upsert。
   - 源 feel **不再标 crystallized**：纯加法结晶 + anchor_refs 覆盖整簇，源靠 feel aging 自然淡出；`demote_ids` 退出浮现（score×0.1）仍可 recall；keep → moment 升 `anchor` / 共识升 `standing_invariant`。
   - 旧 MCP `crystallize_feel` 工具已删除（web `/ob/crystallize-feel` 手动端点保留）；dream 末尾 `_dream_crystal_hint` 改为「有 N 簇成熟可结晶」并指向新流程。
   - **防卡死/真实性（Q1/Q3）**：Gate 1 单簇封顶+递归切分；Gate 2 review 天花板 18 条 + 尾部统计摘要；salience 排序使深痕前置；`demote` 对 arousal>0.7 软否决。详见决策表 #8/#10/#11/#12。
   - **第四档 settled（反复读问题）**：源 feel 既不晋升也不 demote 时，过去毫无标记 → 同一簇每晚复读。现 commit 给整簇盖 `consolidated_into` 回指，已覆盖簇静默；攒够新材料才重新浮现并并回同一结晶。详见决策表 #13。
   - **遗留待办（✅ 已落地）**：feel 衰减改 arousal 加权（`effective_λ = 0.04·(1-0.6·arousal)`，深痕半衰期 17d→38d）+ dashboard 半衰期诊断。
3. **Phase 3 — 稳定层代谢**（✅ 已落地）：standing 无界增长饿死 evolving、anchor 堆积稀释召回。分三块：
   - **块1（✅ 已落地）— evolving 注入保底 + 软预算**：`EVOLVING_INJECT_FLOOR=3` 始终渲染；standing 全量不截断；溢出 = 触发 standing-review 的信号，而非丢 evolving。
   - **块3（✅ 已落地）— anchor 相册**：anchor 是**特权记忆**，三条通道并存而非互斥——①普通关键词 recall **保留**且**加权**（`ANCHOR_RECALL_BOOST=1.3`，相关时更易浮现，而非被排除）；②get_current_state 注入"相册目录"（主题关键词，按 salience=arousal×最近翻看 封顶 `ANCHOR_INDEX_CAP=50`，超出退索引仍可检索）作为直通车；③独立 `recall_anchors(query)` 路径按相关度×情感排序，翻看 bump `last_revisited`。anchor 不 merge（不可还原），偶发重复靠 promote 时去重兜底。「堆积稀释」由相册目录名额封顶（管 top-of-mind）解决，不靠砍召回。
   - **块2（✅ 已落地）— standing-review**：standing 条数 > `STANDING_REVIEW_THRESHOLD=8` → get_current_state 注入提醒 → `review_standing()` **通读全部 standing**（不采样、无天花板）→ 模型读完写合并正文 → `commit_standing_merge(merged_content, retired_ids, user_confirmed=True)` 建新 standing、原条目转 `type=dynamic`（打 `retired_from=standing`、importance=4）自然衰减、仍可 recall。`user_confirmed` 强制用户确认门（最重 gated）。一次一组，多组多次调用。
4. **Phase 4 — resolve + 读取侧**（见 §9、§9.1 breath_bundle 槽位改造）。
