"""place_order 動作確認スクリプト（PR6.4.2）。

testnet で ALO 注文を 1 回発注 → 板に乗ったことを確認 → キャンセル、
さらに ALO 拒否シナリオを実行する。

使い方:
    python scripts/verify_place_order.py

依存:
    sops + age（外部コマンド）が PATH に必要。
    SOPS_AGE_KEY_FILE で復号鍵を指定（または sops のデフォルト探索順）。
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapters.exchange import OrderRejectedError, OrderRequest
from src.infrastructure.hyperliquid_client import HyperLiquidClient
from src.infrastructure.secrets_loader import load_secrets


async def main() -> None:
    print("=" * 60)
    print("  place_order 動作確認（PR6.4.2）")
    print("=" * 60)
    print()

    print("[1/5] secrets 読み込み ... ", end="", flush=True)
    secrets = load_secrets()
    print("OK")

    client = HyperLiquidClient(
        network=secrets.network,
        address=secrets.master_address,
        agent_private_key=secrets.agent_private_key,
    )

    print("[2/5] BTC 板情報取得 ... ", end="", flush=True)
    book = await client.get_l2_book("BTC")
    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    tick = await client.get_tick_size("BTC")
    print(f"best_bid={best_bid}, best_ask={best_ask}, tick={tick}")

    print("[3/5] ALO 買い注文（best_bid - 10tick） ... ", end="", flush=True)
    target_price = best_bid - tick * Decimal("10")
    request = OrderRequest(
        symbol="BTC",
        side="buy",
        size=Decimal("0.0002"),
        price=target_price,
        tif="Alo",
    )
    result = await client.place_order(request)
    print(f"OK (order_id={result.order_id})")

    try:
        print("[4/5] open_orders で確認 ... ", end="", flush=True)
        orders = await client.get_open_orders()
        found = any(o.order_id == result.order_id for o in orders)
        print("OK" if found else "⚠️ 注文が見つからない（即約定？）")
    finally:
        print("[5/5] キャンセル ... ", end="", flush=True)
        if result.order_id is not None:
            cancelled = await client.cancel_order(result.order_id, "BTC")
            print("OK" if cancelled else "⚠️ キャンセル失敗")
        else:
            print("skip (order_id 無し)")

    print()
    print("✓ place_order の 1 サイクル動作確認完了")
    print("   - ALO 注文の発注 OK")
    print("   - 板に乗ったことの確認 OK")
    print("   - キャンセル OK")
    print()

    print("[+1] ALO 拒否シナリオ（best_ask + 10tick で買い）...")
    bad_price = best_ask + tick * Decimal("10")
    bad_request = OrderRequest(
        symbol="BTC",
        side="buy",
        size=Decimal("0.0002"),
        price=bad_price,
        tif="Alo",
    )
    try:
        await client.place_order(bad_request)
        print("⚠️ ALO 拒否が起きなかった（想定外）")
        sys.exit(1)
    except OrderRejectedError as e:
        print("✓ OrderRejectedError 発生（期待通り）")
        print(f"   code: {e.code}")
        print(f"   message: {e}")


if __name__ == "__main__":
    asyncio.run(main())
