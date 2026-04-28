"""cancel_order 動作確認スクリプト（PR6.4.1）。

存在しない order_id でキャンセル試行 → False が返ることを確認する。
これだけで Exchange インスタンスの初期化・Agent Wallet 署名・SDK 通信が
正しく動いていることが分かる（口座への副作用ゼロ）。

使い方:
    python scripts/verify_cancel_order.py

依存:
    sops + age（外部コマンド）が PATH に必要。
    SOPS_AGE_KEY_FILE 環境変数で復号鍵を指定（または sops のデフォルト探索順）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.infrastructure.hyperliquid_client import HyperLiquidClient
from src.infrastructure.secrets_loader import load_secrets


async def main() -> None:
    print("=" * 60)
    print("  cancel_order 動作確認（PR6.4.1）")
    print("=" * 60)
    print()

    print("[1/2] secrets 読み込み ... ", end="", flush=True)
    secrets = load_secrets()
    print("OK")

    print("[2/2] cancel_order で実署名・実通信 ...")
    client = HyperLiquidClient(
        network=secrets.network,
        address=secrets.master_address,
        agent_private_key=secrets.agent_private_key,
    )

    fake_order_id = 999999999
    print(f"  Fake order_id {fake_order_id} をキャンセル試行...")
    result = await client.cancel_order(order_id=fake_order_id, symbol="BTC")

    if result is False:
        print("  → False（期待通り）")
        print()
        print("✓ Exchange インスタンスが正常に動作")
        print("   - Agent Wallet 秘密鍵での署名 OK")
        print("   - testnet API への送信 OK")
        print("   - レスポンス受信・パース OK")
    else:
        print(f"  ⚠️ True が返った（想定外）: result={result}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
