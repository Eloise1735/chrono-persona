#!/usr/bin/env python3
"""召回评测：修复前 vs 修复后的 recall@k / MRR / 平均名次。

把"#39→#4 的单次轶事"升级成"recall@5: X% → Y%"的可复现百分比。

两种模式：
  - 默认离线（字面通道）：保守下界，不需要 API。
  - 接向量（生产口径）：设置环境变量后自动启用，给查询做语义向量化，与生产一致。
        Windows PowerShell:  $env:KELSEY_VECTOR_EMBEDDING_API_KEY="你的key"; python scripts/recall_eval.py
        bash:                KELSEY_VECTOR_EMBEDDING_API_KEY=你的key python scripts/recall_eval.py
    （api_base / model 从 kelsey.db 的 system_settings 读，无需改动。）

只对比"打分公式"这一个变量——候选池、anchor 加权、resolved、向量通道两边都一样：
  - 旧打分：topic×4 + emotion×2 + time(last_active)×1.5 + importance，/8.5×100，阈值 18；feel 排除；vsim>0.5 时 max。
  - 新打分：relevance×100 + time(created)×5 + importance×3，门槛 topic≥0.10 或 vsim≥0.50；feel 纳入；vsim 达标 max。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from datetime import datetime
from server.ob_client import OBClient, OBBucket  # noqa: E402
from server.ob_embedding import OBEmbeddingStore  # noqa: E402


class _SettingsShim:
    """最小 DB 垫片：OBEmbeddingStore 只需要 get_setting() 读 embedding 配置。"""

    def __init__(self, db_path: str):
        self._path = db_path

    async def get_setting(self, key: str):
        con = sqlite3.connect(self._path)
        try:
            row = con.execute("select value from system_settings where key=?", (key,)).fetchone()
            return {"value": row[0]} if row else None
        finally:
            con.close()


def _matches(bucket: OBBucket, token: str) -> bool:
    t = str(token).lower()
    meta = bucket.metadata or {}
    return (
        t == str(bucket.id).lower()
        or t in str(meta.get("name", "")).lower()
        or t in str(bucket.content or "").lower()
    )


def _eligible_base(b: OBBucket) -> bool:
    meta = b.metadata or {}
    return not meta.get("pinned") and not meta.get("protected")


def _time_score(days: float) -> float:
    return math.exp(-0.02 * min(max(days, 0.0), 365))


def _score_old(ob: OBClient, b: OBBucket, query: str, now: datetime, vsim: float):
    """旧公式（含当年的向量通道）。返回 (score, eligible)。feel 排除；阈值 18。"""
    meta = b.metadata or {}
    if ob._bucket_type(meta) == "feel":
        return (0.0, False)
    topic = ob._topic_score(query, b)
    emotion = ob._emotion_score(None, None, meta)   # 旧版无情绪时默认 0.5
    days = ob._days_since(meta.get("last_active") or meta.get("created"), now)
    importance = max(1, min(10, int(meta.get("importance", 5) or 5))) / 10.0
    total = topic * 4.0 + emotion * 2.0 + _time_score(days) * 1.5 + importance
    score = total / 8.5 * 100
    if vsim > 0.5:                                   # 旧版向量通道
        score = max(score, vsim * 100.0)
    if ob.effective_role(meta) == ob.ROLE_ANCHOR:
        score *= ob.ANCHOR_RECALL_BOOST
    if meta.get("resolved"):
        score *= 0.3
    return (score, score >= 18)


def _score_new(ob: OBClient, b: OBBucket, query: str, now: datetime, vsim: float):
    """新公式（与当前 _query_breath 一致）。返回 (score, eligible)。"""
    meta = b.metadata or {}
    topic = ob._topic_score(query, b)
    # 门槛式：字面或向量任一达标才有资格
    if topic < float(ob.recall_topic_min) and vsim < float(ob.recall_vector_min):
        return (0.0, False)
    days = ob._days_since(meta.get("created") or meta.get("last_active"), now)
    importance = max(1, min(10, int(meta.get("importance", 5) or 5))) / 10.0
    score = topic * 100.0 + _time_score(days) * float(ob.recall_time_nudge) + importance * float(ob.recall_importance_nudge)
    if vsim >= float(ob.recall_vector_min):
        # 混合融合：向量为主 + 双通道一致性加成（与 _query_breath 一致）
        score = max(score, vsim * 100.0)
        if topic >= float(ob.recall_topic_min):
            score += topic * float(ob.recall_agreement_bonus)
    if ob.effective_role(meta) == ob.ROLE_ANCHOR:
        score *= ob.ANCHOR_RECALL_BOOST
    if meta.get("resolved"):
        score *= 0.3
    return (score, score > 0)


def _rank_of(ob, buckets, query, expects, scorer, now, vmap):
    scored = []
    for b in buckets:
        if not _eligible_base(b):
            continue
        s, ok = scorer(ob, b, query, now, float(vmap.get(b.id, 0.0)))
        if ok:
            scored.append((s, b))
    scored.sort(key=lambda x: x[0], reverse=True)
    for i, (_, b) in enumerate(scored, 1):
        if any(_matches(b, tok) for tok in expects):
            return i
    return None


async def main():
    ap = argparse.ArgumentParser(description="召回评测：修复前后 recall@k / MRR / 平均名次")
    ap.add_argument("--gold", default="scripts/recall_gold.json")
    ap.add_argument("--buckets", default="data/ob_buckets")
    ap.add_argument("--db", default="data/kelsey.db")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--no-vector", action="store_true", help="强制离线（不接向量）")
    args = ap.parse_args()

    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    cases = gold.get("cases", gold) if isinstance(gold, dict) else gold

    store = None
    vector_on = False
    if not args.no_vector:
        store = OBEmbeddingStore(_SettingsShim(args.db), args.buckets)
        try:
            vector_on = await store.is_enabled()
        except Exception:
            vector_on = False

    ob = OBClient(args.buckets, embedding_store=store if vector_on else None)
    buckets = await ob.list_buckets(include_archive=False)
    now = datetime.utcnow()
    K = args.k

    # 预取每条查询的向量相似度（一次性，避免重复 embed）
    vmaps = []
    for c in cases:
        vmap = {}
        if vector_on:
            try:
                hits = await store.search_similar(c["query"], top_k=300)
                vmap = {str(bid): float(sim) for bid, sim in hits}
            except Exception as e:
                print(f"[warn] 向量检索失败（{c['query'][:14]}）：{e}")
        vmaps.append(vmap)

    # Q2 正文覆盖率是"新"侧的改动之一：旧基线用 pre-Q2 的 _topic_score（bonus=0），
    # 否则共享方法会把旧基线一起抬高，污染 AB。
    saved_cov = ob.recall_content_coverage_bonus
    rows = []
    for c, vmap in zip(cases, vmaps):
        ob.recall_content_coverage_bonus = 0.0
        r_old = _rank_of(ob, buckets, c["query"], c["expect"], _score_old, now, vmap)
        ob.recall_content_coverage_bonus = saved_cov
        r_new = _rank_of(ob, buckets, c["query"], c["expect"], _score_new, now, vmap)
        rows.append((c, r_old, r_new))

    def fmt(r):
        return f"#{r}" if r else "未召回"

    mode = "接向量（生产口径）" if vector_on else "离线字面通道（保守下界）"
    print(f"\n候选记忆数 {len(buckets)}  |  gold 用例 {len(rows)}  |  k={K}  |  模式：{mode}\n")
    print(f'{"旧":>6} {"新":>6}   query → 期待')
    print("-" * 64)
    for c, r_old, r_new in rows:
        flag = "  ✅" if (r_new and r_new <= K) and not (r_old and r_old <= K) else ""
        print(f'{fmt(r_old):>6} {fmt(r_new):>6}   {c["query"][:26]} → {c.get("note","")[:16]}{flag}')

    def recall_at_k(ranks):
        hit = sum(1 for r in ranks if r and r <= K)
        return hit, len(ranks), (100.0 * hit / len(ranks) if ranks else 0)

    def mrr(ranks):
        return sum((1.0 / r) for r in ranks if r) / len(ranks) if ranks else 0

    def mean_rank_hits(ranks):
        hits = [r for r in ranks if r]
        return sum(hits) / len(hits) if hits else None

    old_ranks = [r for _, r, _ in rows]
    new_ranks = [r for _, _, r in rows]
    ho, no_, po = recall_at_k(old_ranks)
    hn, nn, pn = recall_at_k(new_ranks)
    mr_o, mr_n = mean_rank_hits(old_ranks), mean_rank_hits(new_ranks)

    print("\n" + "=" * 64)
    print(f"聚合指标（修复前 → 修复后）  |  {mode}")
    print(f"  Recall@{K}（命中率）   {po:5.1f}%  →  {pn:5.1f}%   ({ho}/{no_} → {hn}/{nn})")
    print(f"  MRR（平均倒数名次）   {mrr(old_ranks):.3f}  →  {mrr(new_ranks):.3f}")
    print(f"  命中项平均名次       {('%.1f'%mr_o) if mr_o else 'n/a':>6}  →  {('%.1f'%mr_n) if mr_n else 'n/a':>6}")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
