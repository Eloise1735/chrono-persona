"""One-off：把 DB 里的几个关键 prompt 重置回源码里的当前版本。

用法（在云服务器项目根目录下）：

    # 默认 DB 路径 ./data/kelsey.db
    python scripts/reset_prompts_to_source.py

    # 自定义 DB 路径
    python scripts/reset_prompts_to_source.py --db D:/some/path/kelsey.db
    python scripts/reset_prompts_to_source.py --db /opt/kelsey/data/kelsey.db

会重置以下 4 个 prompt：
  - prompt_environment_generation     （env 生成主 prompt — V1→V2 升级）
  - prompt_environment_event_rollup   （生活碎片聚合 prompt — 时序/因果改造）
  - prompt_daily_plan_generation      （日程生成 prompt — 上下文重组）
  - prompt_plan_replan                （日程重排 prompt — 用户禁令统一）

注意：这会**覆盖**你 DB 里如果手动调过的内容。脚本会在重置前先打印每个 key 当前内容的前 200
字作为备份提示——看一眼如果有重要的手改内容，先 Ctrl+C 取消，手动备份后再跑。

设计目标：本脚本**不依赖** pyyaml / 完整 server 配置链。只直接读 DB + 调 PromptManager。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 让脚本能从项目根目录直接运行
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 直接 import 这两个模块——它们不依赖 yaml。
# 注意：千万不要在这里 from server.config import load_config，那样会触发 yaml 依赖。
from server.database import Database
from server.prompts import PromptManager


KEYS_TO_RESET = [
    "prompt_environment_generation",
    "prompt_environment_event_rollup",
    "prompt_daily_plan_generation",
    "prompt_plan_replan",
]

DEFAULT_DB_PATH = "./data/kelsey.db"


async def main(db_path: str) -> int:
    resolved = str(Path(db_path).expanduser().resolve())
    print(f"[info] Using DB: {resolved}")
    if not Path(resolved).exists():
        print(f"[error] DB 文件不存在：{resolved}")
        print("        请用 --db <path> 指定正确路径。")
        return 1

    db = Database(resolved)
    await db.initialize()
    pm = PromptManager(db)

    try:
        for key in KEYS_TO_RESET:
            current = await pm.get_prompt(key)
            preview = (current or "").strip().replace("\n", " ")[:200]
            print(f"\n[backup-preview] {key} 当前内容前 200 字：")
            print(f"  {preview!r}")

        print("\n以上为重置前的当前内容预览（已截短到 200 字）。")
        print("如有手动调过的重要内容，请立即 Ctrl+C 取消并手动备份完整内容。")
        print("否则按回车继续，将这 4 个 key 重置为源码里的最新版本……")
        try:
            input()
        except EOFError:
            print("[info] 非交互执行环境，跳过确认，直接继续。")

        for key in KEYS_TO_RESET:
            ok = await pm.reset_setting(key)
            status = "OK" if ok else "FAIL (key 未在 DEFAULT_SETTINGS 中)"
            print(f"[reset] {key}: {status}")

        print("\n[done] 全部完成。服务不需要重启——PromptManager 实时从 DB 拉取，")
        print("       下一次 env 生成或下一次 plan 生成自然就用新版了。")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset 4 key prompts in DB to source defaults.")
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"DB file path (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.db)))
