#!/usr/bin/env python3
"""检索健康度体检 / Retrieval Health Check.

把"记忆到底有没有被读到"从体感变成数字。只读不写，跑完打印一张报告。

用法：
    python scripts/retrieval_health.py
    python scripts/retrieval_health.py --db data/kelsey.db --buckets data/ob_buckets

算三类指标（详见 docs/MEASURING_MEMORY.md）：
  1. 覆盖率 Coverage      —— 存进去的记忆，有多少被读过？（治"只写不读"）
  2. 集中度 Concentration —— 召回是不是被少数几条霸榜？（长尾有没有饿死）
  3. 高价值沉默 Silence    —— importance 高、却从没被想起的记忆有多少？（最该盯的失败指标）

依赖：pyyaml（项目已装）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

# Windows 控制台默认 GBK，中文会乱码——强制 UTF-8 输出。
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import yaml


# ──────────────────────────────────────────────────────────────────────────
# 工具
# ──────────────────────────────────────────────────────────────────────────

def _parse_frontmatter(path: Path) -> dict | None:
    """读一个 .md 桶文件的 YAML frontmatter（--- 与 --- 之间那段）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end]
    try:
        meta = yaml.safe_load(block)
    except Exception:
        return None
    return meta if isinstance(meta, dict) else None


def _days_since(ts: str | None) -> float | None:
    if not ts:
        return None
    raw = str(ts).strip().replace("Z", "")
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        try:
            dt = datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None
    return max(0.0, (datetime.utcnow() - dt).total_seconds() / 86400.0)


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


def _bar(frac: float, width: int = 24) -> str:
    fill = int(round(frac * width))
    return "█" * fill + "·" * (width - fill)


def _gini(values: list[float]) -> float:
    """基尼系数：0=完全平均（每条记忆被读次数一样），1=极度集中（一条吃掉全部）。"""
    xs = sorted(v for v in values if v >= 0)
    n = len(xs)
    if n == 0 or sum(xs) == 0:
        return 0.0
    cum = 0.0
    for i, x in enumerate(xs, 1):
        cum += i * x
    return (2 * cum) / (n * sum(xs)) - (n + 1) / n


# ──────────────────────────────────────────────────────────────────────────
# OB 桶侧（主记忆）
# ──────────────────────────────────────────────────────────────────────────

def load_buckets(buckets_dir: Path) -> list[dict]:
    out: list[dict] = []
    for sub in ("dynamic", "feel", "permanent", "archive"):
        for path in (buckets_dir / sub).rglob("*.md"):
            meta = _parse_frontmatter(path)
            if meta is None:
                continue
            meta["_dir"] = sub
            out.append(meta)
    return out


def report_ob(buckets: list[dict]) -> None:
    print("\n" + "=" * 70)
    print("  A · OB 记忆桶检索健康度（主记忆）")
    print("=" * 70)

    by_type: Counter = Counter(b.get("type", b.get("_dir", "?")) for b in buckets)
    print("\n各层数量：", dict(by_type), f"| 活跃合计={sum(v for k,v in by_type.items() if k!='archived' and k!='archive')}")

    # —— 只看 dynamic（会衰减、靠浮现被读的主力层）——
    dyn = [b for b in buckets if b.get("type") == "dynamic" and not b.get("resolved")]
    if not dyn:
        print("\n（没有活跃 dynamic 桶，跳过。）")
        return

    acts = [float(b.get("activation_count", 0) or 0) for b in dyn]
    surfs = [float(b.get("surface_count", 0) or 0) for b in dyn]

    # 1) 覆盖率：分两条通路（修了建构效度漏洞——activation_count 只计「查询命中」，
    #    「自然浮现」并不计；surface_count 是方案 C 新埋点，单独计浮现通路的'被读到'）。
    q_read = sum(1 for a in acts if a > 0)                                # 查询召回覆盖
    s_read = sum(1 for s in surfs if s > 0)                               # 自然浮现覆盖
    any_read = sum(1 for a, s in zip(acts, surfs) if a > 0 or s > 0)      # 任一通路被读 = 真覆盖
    print(f"\n[1] 覆盖率 Coverage（dynamic 被读到的比例，分通路）")
    print(f"    查询召回  {_bar(q_read/len(dyn))}  {_pct(q_read, len(dyn))}  ({q_read}/{len(dyn)})")
    print(f"    自然浮现  {_bar(s_read/len(dyn))}  {_pct(s_read, len(dyn))}  ({s_read}/{len(dyn)})")
    print(f"    任一通路  {_bar(any_read/len(dyn))}  {_pct(any_read, len(dyn))}  ({any_read}/{len(dyn)})  ← 真·覆盖率")
    print(f"    解读：'任一通路'才是真实'被读到'；只看查询召回会高估'只写不读'。")
    if s_read == 0:
        print(f"    ⚠ 自然浮现覆盖=0：surface_count 是新埋点，旧数据无此字段，需系统跑一段时间才有信号。")

    # 2) 集中度：基尼 + top10 份额
    total_act = sum(acts)
    top = sorted(dyn, key=lambda b: float(b.get("activation_count", 0) or 0), reverse=True)
    top10_share = sum(float(b.get("activation_count", 0) or 0) for b in top[:10]) / total_act if total_act else 0
    print(f"\n[2] 集中度 Concentration（召回有没有被少数霸榜）")
    print(f"    基尼系数 = {_gini(acts):.2f}   (0=雨露均沾, 1=赢家通吃)")
    print(f"    Top-10 桶吃掉了 {top10_share*100:.1f}% 的总召回")
    print(f"    霸榜前 5：")
    for b in top[:5]:
        print(f"      · act={float(b.get('activation_count',0) or 0):>4}  imp={b.get('importance','?')}  {str(b.get('name',''))[:34]}")

    # 3) 高价值沉默：importance>=7 且两条通路都没读到（查询 + 浮现皆为 0）才算真沉默
    silent = [b for b in dyn if int(b.get("importance", 0) or 0) >= 7
              and float(b.get("activation_count", 0) or 0) == 0
              and float(b.get("surface_count", 0) or 0) == 0]
    print(f"\n[3] 高价值沉默 Silence（importance>=7 且查询/浮现都没读到）★ 最该盯")
    print(f"    共 {len(silent)} 条（占高价值 dynamic 的比例见下）")
    hi = [b for b in dyn if int(b.get("importance", 0) or 0) >= 7]
    print(f"    {_bar(len(silent)/len(hi) if hi else 0)}  {_pct(len(silent), len(hi))} 的高价值记忆在沉默")
    for b in sorted(silent, key=lambda b: int(b.get("importance",0) or 0), reverse=True)[:8]:
        age = _days_since(b.get("created"))
        print(f"      · imp={b.get('importance')}  age={age:.0f}d  {str(b.get('name',''))[:38]}" if age is not None
              else f"      · imp={b.get('importance')}  {str(b.get('name',''))[:38]}")

    # 附：可归档（老且冷，还赖在活跃池）
    stale = [b for b in dyn if (_days_since(b.get("last_active") or b.get("created")) or 0) > 30
             and float(b.get("activation_count", 0) or 0) == 0]
    print(f"\n[附] 冷且老（>30天未动 + 从没被读）：{len(stale)} 条 —— 可考虑归档释放浮现池")


# ──────────────────────────────────────────────────────────────────────────
# 状态机侧（event / snapshot / key / world，走 memory_recall_stats）
# ──────────────────────────────────────────────────────────────────────────

def report_recall_stats(db_path: Path) -> None:
    print("\n" + "=" * 70)
    print("  B · 状态机条目召回（event / snapshot / key / world）")
    print("=" * 70)
    if not db_path.exists():
        print(f"\n（找不到 {db_path}，跳过。）")
        return
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    try:
        cur.execute("select entry_id, recall_count, last_recalled_at from memory_recall_stats")
        rows = cur.fetchall()
    except Exception as e:
        print(f"\n（读 memory_recall_stats 失败：{e}）")
        con.close()
        return

    by_prefix: dict[str, list[int]] = defaultdict(list)
    for entry_id, rc, _ in rows:
        prefix = str(entry_id).split("_")[0]
        by_prefix[prefix].append(int(rc or 0))

    total = sum(int(r[1] or 0) for r in rows)
    print(f"\n被记录过召回的条目：{len(rows)}  | 总召回次数：{total}")
    print(f"\n按类型：")
    for prefix, counts in sorted(by_prefix.items()):
        s = sum(counts)
        print(f"    {prefix:<10} 条目={len(counts):>4}  召回={s:>5}  人均={s/len(counts):.1f}")

    allc = [int(r[1] or 0) for r in rows]
    print(f"\n集中度：基尼={_gini([float(x) for x in allc]):.2f}")
    top = sorted(rows, key=lambda r: int(r[1] or 0), reverse=True)[:5]
    print(f"霸榜前 5：")
    for entry_id, rc, last in top:
        print(f"    · {rc:>4}次  {entry_id:<16} last={str(last)[:10]}")
    con.close()


# ──────────────────────────────────────────────────────────────────────────
# 成本护栏：注入 token 趋势（automation_runs.report_json 里找 token）
# ──────────────────────────────────────────────────────────────────────────

def _find_tokens(obj) -> int:
    """递归在 report_json 里找任何看起来像 token 总数的字段，求和。"""
    total = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and "token" in str(k).lower():
                total += int(v)
            else:
                total += _find_tokens(v)
    elif isinstance(obj, list):
        for it in obj:
            total += _find_tokens(it)
    return total


def report_cost(db_path: Path) -> None:
    print("\n" + "=" * 70)
    print("  C · 成本护栏：自动化 token 趋势（按天）")
    print("=" * 70)
    if not db_path.exists():
        print("\n（无 DB，跳过。）")
        return
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    try:
        cur.execute("select created_at, report_json from automation_runs order by created_at")
        rows = cur.fetchall()
    except Exception as e:
        print(f"\n（读 automation_runs 失败：{e}）")
        con.close()
        return
    by_day: dict[str, int] = defaultdict(int)
    found = 0
    for created, rj in rows:
        try:
            data = json.loads(rj) if rj else {}
        except Exception:
            continue
        tok = _find_tokens(data)
        if tok:
            found += 1
        by_day[str(created)[:10]] += tok
    if found == 0:
        print("\n（report_json 里没找到 token 字段——说明 token 统计可能存在别处，")
        print("  或自动化本身不直接花 token。这是个埋点缺口，见 docs/MEASURING_MEMORY.md。）")
        con.close()
        return
    print(f"\n有 token 记录的运行：{found}/{len(rows)}")
    for day in sorted(by_day)[-14:]:
        print(f"    {day}  {by_day[day]:>8} tok")
    con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="检索健康度体检（只读）")
    ap.add_argument("--db", default="data/kelsey.db")
    ap.add_argument("--buckets", default="data/ob_buckets")
    args = ap.parse_args()

    buckets_dir = Path(args.buckets)
    db_path = Path(args.db)

    print("检索健康度体检 ·", datetime.now().strftime("%Y-%m-%d %H:%M"))
    if buckets_dir.exists():
        report_ob(load_buckets(buckets_dir))
    else:
        print(f"\n（找不到桶目录 {buckets_dir}，跳过 OB 部分。）")
    report_recall_stats(db_path)
    report_cost(db_path)
    print("\n完。指标含义见 docs/MEASURING_MEMORY.md。\n")


if __name__ == "__main__":
    main()
