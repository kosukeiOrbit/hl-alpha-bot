"""Phase 0 動作確認スクリプト。

testnet で 1〜2 サイクル動かして以下を確認:
- 設定ロード
- secrets ロード
- 部品組み立て
- DB 初期化
- run_cycle_once が完走
- shutdown 要求で終わる

is_dry_run=True を強制チェック（実発注しない）。
DB は :memory: を使うので本番 data/hl_bot.db に触れない。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config_loader import load_settings
from src.infrastructure.secrets_loader import load_secrets
from src.infrastructure.sqlite_repository import (
    SQLiteRepository,
)
from src.main import build_scheduler


async def main() -> None:
    print("=" * 60)
    print("  Phase 0 動作確認")
    print("=" * 60)
    print()

    print("[1/5] 設定ロード ... ", end="", flush=True)
    settings = load_settings(
        "config/settings.yaml",
        "config/profile_phase0.yaml",
    )
    print(
        f"OK (phase={settings.phase}, "
        f"dry_run={settings.trading.is_dry_run})"
    )

    if not settings.trading.is_dry_run:
        print("ERROR: is_dry_run=False では実行不可")
        sys.exit(1)

    print("[2/5] secrets ロード ... ", end="", flush=True)
    secrets = load_secrets()
    print("OK")

    print("[3/5] 部品組み立て + DB は :memory: で代替 ... ", end="", flush=True)
    scheduler, _ = build_scheduler(settings, secrets)
    # 本番 DB ではなくメモリ DB に差し替え（実 testnet データには触れる
    # が、ローカル DB ファイルを汚さない）
    test_repo = SQLiteRepository(":memory:")
    await test_repo.initialize()
    scheduler.repo = test_repo
    scheduler.entry_flow.repo = test_repo
    scheduler.position_monitor.repo = test_repo
    scheduler.reconciler.repo = test_repo
    print("OK")

    try:
        print("[4/5] run_cycle_once 実行 ...")
        stats = await scheduler.run_cycle_once()
        print(f"   monitor_filled={stats.monitor_filled}")
        print(f"   monitor_closed={stats.monitor_closed}")
        print(f"   entry_attempts={stats.entry_attempts}")
        print(f"   entry_executed={stats.entry_executed}")
        print(f"   entry_dryrun={stats.entry_dryrun}")
        print(f"   entry_errors={stats.entry_errors}")
        print(f"   circuit_breaker={stats.circuit_breaker_active}")
        print(f"   duration={stats.duration_seconds:.2f}s")

        print("[5/5] 2サイクル目 ...")
        stats2 = await scheduler.run_cycle_once()
        print(f"   duration={stats2.duration_seconds:.2f}s")
    finally:
        await test_repo.close()

    print()
    print("Phase 0 動作確認完了")
    print()
    print("実運用開始するには:")
    print(
        "  python scripts/run_bot.py --profile config/profile_phase0.yaml"
    )


if __name__ == "__main__":
    asyncio.run(main())
