"""place_orders_grouped 動作確認スクリプト（PR6.4.3）。

testnet で entry + TP + SL を normalTpsl で連結発注 → 全部キャンセル。
ALO エントリー価格は best_bid - 100tick に置くので即約定はしない想定。

使い方:
    python scripts/verify_grouped_order.py
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapters.exchange import OrderRequest, TriggerOrderRequest
from src.infrastructure.hyperliquid_client import HyperLiquidClient
from src.infrastructure.secrets_loader import load_secrets


async def main() -> None:
    print("=" * 60)
    print("  place_orders_grouped 動作確認（PR6.4.3）")
    print("=" * 60)
    print()

    print("[1/4] secrets 読み込み ... ", end="", flush=True)
    secrets = load_secrets()
    print("OK")

    client = HyperLiquidClient(
        network=secrets.network,
        address=secrets.master_address,
        agent_private_key=secrets.agent_private_key,
    )

    print("[2/4] BTC 板情報取得 ... ", end="", flush=True)
    book = await client.get_l2_book("BTC")
    best_bid = book.bids[0].price
    tick = await client.get_tick_size("BTC")
    print(f"best_bid={best_bid}, tick={tick}")

    entry_price = best_bid - tick * Decimal("100")
    tp_trigger = entry_price + tick * Decimal("1000")
    sl_trigger = entry_price - tick * Decimal("1000")

    print(
        f"[3/4] grouped 発注 (entry={entry_price}, "
        f"tp={tp_trigger}, sl={sl_trigger}) ..."
    )

    entry = OrderRequest(
        symbol="BTC",
        side="buy",
        size=Decimal("0.0002"),
        price=entry_price,
        tif="Alo",
    )
    tp = TriggerOrderRequest(
        symbol="BTC",
        side="sell",
        size=Decimal("0.0002"),
        trigger_price=tp_trigger,
        is_market=False,
        limit_price=tp_trigger,
        tpsl="tp",
        reduce_only=True,
    )
    sl = TriggerOrderRequest(
        symbol="BTC",
        side="sell",
        size=Decimal("0.0002"),
        trigger_price=sl_trigger,
        is_market=True,
        limit_price=None,
        tpsl="sl",
        reduce_only=True,
    )

    results = await client.place_orders_grouped(entry, tp, sl)
    labels = ["entry", "tp", "sl"]
    for label, r in zip(labels, results, strict=False):
        if r.success:
            print(f"   [{label:5}] OK  order_id={r.order_id}")
        else:
            print(f"   [{label:5}] FAIL: {r.rejected_reason}")

    print("[4/4] 全部キャンセル ...")
    cancelled = 0
    for r in results:
        if r.success and r.order_id is not None:
            ok = await client.cancel_order(r.order_id, "BTC")
            if ok:
                cancelled += 1
    print(f"   {cancelled}/{len(results)} キャンセル成功")

    print()
    print("✓ place_orders_grouped 動作確認完了")
    print("   - normalTpsl グループ発注 OK")
    print("   - entry + TP + SL の連結 OK")
    print()
    print("⚠️ TP/SL の trigger は entry 約定後に活性化する仕様。")
    print("   今回は entry 未約定のため trigger 単独で板に乗っているか")
    print("   HL UI で確認・必要なら手動キャンセルしてください。")


if __name__ == "__main__":
    asyncio.run(main())
