#!/usr/bin/env python3
"""单次召回诊断探针 / Recall Probe.

当你"主动让模型回想某条特定事件，却召回不到"时，用它定位原因。
离线、只读、不用起服务、不用 API（只算字面通道，不含向量）。

用法：
    python scripts/recall_probe.py "你召回时用的查询词"
    python scripts/recall_probe.py "六爻 十天干" --expect "十天干"
    python scripts/recall_probe.py "那次深夜谈话" --expect a0704482213b --top 15

参数：
    query        你召回时实际用的查询字符串
    --expect     你**期待**被召回的那条：桶 id，或它 name/正文里的一个子串
    --top        打印前 N 条（默认 12）
    --buckets    桶目录（默认 data/ob_buckets）

它会告诉你：
  · 期待的那条排第几、各分项多少
  · 排在它前面的"赢家"是靠 topic(相关) 赢的，还是靠 time/importance(底噪) 赢的
  · 一句诊断：关键词问题 / 结构性底噪污染 / 新近度滚雪球
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 让脚本能 import server 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from server.ob_client import OBClient  # noqa: E402


def _match_expected(row: dict, expect: str) -> bool:
    if not expect:
        return False
    e = expect.lower()
    return e == str(row["id"]).lower() or e in str(row["name"]).lower()


async def run(query: str, expect: str, top: int, buckets_dir: str) -> None:
    ob = OBClient(buckets_dir, embedding_store=None)  # 字面通道，不连向量
    dbg = await ob.breath_debug(query=query)
    rows = dbg["items"]
    gate = dbg.get("gate", {})
    scoring = dbg.get("scoring", "")

    print(f'\n查询: "{query}"')
    print(f'打分: {scoring}')
    print(f'门槛: topic_min={gate.get("topic_min")}  vector_min={gate.get("vector_min")}（字面或向量任一达标即合格）')
    print("（注意：这是字面/关键词通道，不含向量语义。topic=相关性，其余=底噪）\n")

    header = f'{"#":>3}  {"score":>6} {"topic":>6} {"emo":>5} {"time":>5} {"imp":>5}  {"过":>2}  name'
    print(header)
    print("-" * len(header))
    for i, r in enumerate(rows[:top], 1):
        star = "★" if _match_expected(r, expect) else " "
        passed = "✓" if r["passed_threshold"] else "✗"
        print(f'{i:>3}{star} {r["search_score"]:>6} {r["topic"]:>6} {r["emotion"]:>5} '
              f'{r["time"]:>5} {r["importance"]:>5}  {passed:>2}  {str(r["name"])[:40]}')

    if not expect:
        print("\n（没传 --expect，无法定位特定事件。补上 --expect <id或name子串> 看诊断。）")
        return

    # 找期待项
    idx = next((i for i, r in enumerate(rows) if _match_expected(r, expect)), None)
    if idx is None:
        print(f'\n⚠ 没找到匹配 "{expect}" 的桶（可能 name/id 不对，或被 domain 过滤）。')
        return
    tgt = rows[idx]
    print(f'\n{"="*60}\n诊断：期待项 "{str(tgt["name"])[:40]}"')
    print(f'  排名 第 {idx+1} / {len(rows)}   search_score={tgt["search_score"]}   过阈值={tgt["passed_threshold"]}')
    print(f'  分项: topic={tgt["topic"]}  emotion={tgt["emotion"]}  time={tgt["time"]}  importance={tgt["importance"]}')

    # 看排在它前面的赢家，靠什么赢
    winners = rows[:idx]
    if not winners:
        print("  → 它已经排第一，召回不到可能是 limit 太小或上游没真的调用查询。")
        return
    beat_by_topic = sum(1 for r in winners if r["topic"] > tgt["topic"])
    beat_by_floor = sum(1 for r in winners if r["topic"] <= tgt["topic"])
    print(f'\n  排在它前面的 {len(winners)} 条里：')
    print(f'    · {beat_by_topic} 条 topic 更高（靠"相关性"赢——合理）')
    print(f'    · {beat_by_floor} 条 topic 并不更高（靠 time/importance 底噪赢——污染）')

    print("\n  一句话诊断：")
    if tgt["topic"] < 0.2:
        print("    ▶ 关键词/字面匹配太弱（topic<0.2）。要么这条的 name/tags 没覆盖你的查询词，")
        print("      要么得靠向量语义通道（本探针看不到，需查 sim 是否>0.5）。先修 tags/name。")
    elif beat_by_floor >= beat_by_topic and beat_by_floor > 0:
        print("    ▶ 结构性污染：把它挤下去的主要是 time/importance 底噪，不是更相关的条目。")
        print("      这是公式问题（相关性只占47%），不是你关键词的错。需调权重/降底噪。")
    elif not tgt["passed_threshold"]:
        print("    ▶ 它没过阈值(18)。topic 中等但被稀释。提升 tags 精度或降低非相关权重。")
    else:
        print("    ▶ 它其实分数不低、也过了阈值，但被同样相关的条目挤出 limit。")
        print("      考虑增大召回 limit，或这本就是合理的近似召回。")


def main() -> None:
    ap = argparse.ArgumentParser(description="单次召回诊断探针（离线只读）")
    ap.add_argument("query")
    ap.add_argument("--expect", default="")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--buckets", default="data/ob_buckets")
    args = ap.parse_args()
    asyncio.run(run(args.query, args.expect, args.top, args.buckets))


if __name__ == "__main__":
    main()
