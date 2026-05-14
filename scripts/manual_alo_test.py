"""mainnet 実発注 ALO テスト（使い捨て診断スクリプト）。

mainnet ETH SHORT 4 層通過 5 件 → trades 0 件の根本原因を確定させるため、
ALO post-only 発注の仕組みを実 API で検証する。

設計:
- 段階 A: markPx × 1.15 で発注（板のはるか上）→ ALO 機構そのものが動作することを確認
- 段階 B: markPx 近傍で発注（BOT と同じ価格ロジック）→ 拒否されることを確認
- 各段階で place_order（単発）と place_orders_grouped の両経路をテスト
- HL 拒否時の raise / silent return の挙動差を実機で観察

安全装置:
- 段階 A は板のはるか上 → 約定しない
- 段階 B は ALO 拒否されることが期待値 → 約定しない（万一刺さったら即キャンセル）
- 段階ごとに input() で人手確認
- 各 sub-test 後に該当 order_id を即キャンセル
- スクリプト終端で open orders を表示して残りを目視確認

実行前の必須事項:
- BOT が停止していること
- mainnet（network=mainnet）が profile_phase2.yaml で設定されていること
- SOPS_AGE_KEY_FILE が設定されているか、または secrets/.age-key が読める状態
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

# プロジェクトルートを sys.path に追加（直接実行用）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.adapters.exchange import (  # noqa: E402
    OrderRejectedError,
    OrderRequest,
    TriggerOrderRequest,
)
from src.core.config_loader import load_settings  # noqa: E402
from src.infrastructure.hyperliquid_client import HyperLiquidClient  # noqa: E402
from src.infrastructure.secrets_loader import load_secrets  # noqa: E402

SYMBOL = "ETH"
TARGET_NOTIONAL_USD = Decimal("15")  # HL min $10 + マージン
STAGE_A_MULTIPLIER = Decimal("1.15")  # markPx の +15% で sell ALO（絶対約定しない）


def _print_banner() -> None:
    print("=" * 72)
    print("  mainnet 実発注 ALO テスト（手動診断）")
    print("=" * 72)
    print()
    print("  [WARN] これは mainnet 実資金口座でのテストです。")
    print("  [WARN] 段階 A は約定しない価格で発注しますが、念のため即キャンセルします。")
    print("  [WARN] 段階 B は ALO 拒否される想定の価格です（万一約定したら即キャンセル）。")
    print()


def _confirm(prompt: str) -> bool:
    ans = input(f"{prompt} [y/N]: ").strip().lower()
    return ans in ("y", "yes")


def _round_size(raw: Decimal, sz_decimals: int) -> Decimal:
    q = Decimal("1") / (Decimal("10") ** sz_decimals)
    return raw.quantize(q, rounding=ROUND_DOWN)


def _round_price(raw: Decimal, tick_size: Decimal) -> Decimal:
    # tick_size の倍数に丸める（最も近い tick へ ROUND_DOWN）
    if tick_size <= 0:
        return raw
    return (raw / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick_size


def _print_response(label: str, result: object) -> None:
    print(f"\n[{label}] レスポンス:")
    print(f"  type: {type(result).__name__}")
    print(f"  repr: {result!r}")


def _print_exception(label: str, exc: BaseException) -> None:
    print(f"\n[{label}] 例外発生:")
    print(f"  class: {type(exc).__name__}")
    if isinstance(exc, OrderRejectedError):
        print(f"  code:  {getattr(exc, 'code', None)}")
    print(f"  message: {exc}")
    print("  traceback:")
    for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
        print(f"    {line.rstrip()}")


async def _cleanup_order(
    client: HyperLiquidClient, symbol: str, order_id: int | None, label: str
) -> None:
    """order_id があれば即キャンセル。失敗しても続行。"""
    if order_id is None:
        return
    print(f"\n[{label}] order_id={order_id} を即キャンセル...")
    try:
        ok = await client.cancel_order(order_id=order_id, symbol=symbol)
        print(f"  cancel result: {ok}")
    except Exception as e:
        print(f"  cancel failed: {e}")


async def _list_open_orders(client: HyperLiquidClient, symbol: str, label: str) -> None:
    print(f"\n[{label}] {symbol} の open orders 確認:")
    try:
        orders = await client.get_open_orders()
    except Exception as e:
        print(f"  取得失敗: {e}")
        return
    relevant = [o for o in orders if o.symbol == symbol]
    if not relevant:
        print(f"  {symbol} の open order なし [OK]")
        return
    for o in relevant:
        print(
            f"  order_id={o.order_id} side={o.side} size={o.size} "
            f"price={o.price} tif={o.tif}"
        )


async def _cancel_all_for_symbol(
    client: HyperLiquidClient, symbol: str, label: str
) -> None:
    """grouped 経由で TP/SL が残るリスクに備えて symbol の全 open を掃除。"""
    print(f"\n[{label}] {symbol} の残り open orders を一括キャンセル...")
    try:
        orders = await client.get_open_orders()
    except Exception as e:
        print(f"  open_orders 取得失敗: {e}")
        return
    for o in orders:
        if o.symbol != symbol:
            continue
        try:
            ok = await client.cancel_order(order_id=o.order_id, symbol=symbol)
            print(f"  cancel order_id={o.order_id}: {ok}")
        except Exception as e:
            print(f"  cancel order_id={o.order_id} failed: {e}")


# ─── テスト本体 ─────────────────────────────


async def _test_single(
    client: HyperLiquidClient,
    symbol: str,
    size: Decimal,
    price: Decimal,
    label: str,
) -> None:
    """place_order（単発 ALO sell）。失敗は OrderRejectedError として raise される想定。"""
    print(f"\n{'─' * 60}")
    print(f"[{label}] place_order(single) sell ALO @ {price} size={size}")
    print(f"{'─' * 60}")
    req = OrderRequest(
        symbol=symbol,
        side="sell",
        size=size,
        price=price,
        tif="Alo",
        reduce_only=False,
    )
    try:
        result = await client.place_order(req)
        _print_response(label, result)
        await _cleanup_order(client, symbol, result.order_id, label)
    except Exception as e:
        _print_exception(label, e)


async def _test_grouped(
    client: HyperLiquidClient,
    symbol: str,
    size: Decimal,
    entry_price: Decimal,
    tp_price: Decimal,
    sl_price: Decimal,
    label: str,
) -> None:
    """place_orders_grouped（ALO sell + TP/SL）。silent rejection 経路の検証。"""
    print(f"\n{'─' * 60}")
    print(
        f"[{label}] place_orders_grouped sell ALO @ {entry_price} "
        f"size={size} tp={tp_price} sl={sl_price}"
    )
    print(f"{'─' * 60}")
    entry = OrderRequest(
        symbol=symbol,
        side="sell",
        size=size,
        price=entry_price,
        tif="Alo",
        reduce_only=False,
    )
    tp = TriggerOrderRequest(
        symbol=symbol,
        side="buy",  # SHORT の決済は buy
        size=size,
        trigger_price=tp_price,
        is_market=False,
        limit_price=tp_price,
        tpsl="tp",
        reduce_only=True,
    )
    sl = TriggerOrderRequest(
        symbol=symbol,
        side="buy",
        size=size,
        trigger_price=sl_price,
        is_market=True,
        limit_price=None,
        tpsl="sl",
        reduce_only=True,
    )
    try:
        results = await client.place_orders_grouped(entry, tp, sl)
        _print_response(label, results)
        # tuple なので個別表示
        for i, r in enumerate(results):
            print(
                f"  [{i}] success={r.success} order_id={r.order_id} "
                f"rejected_reason={r.rejected_reason}"
            )
        # entry の order_id があれば cancel（TP/SL は entry に紐付くので追随する想定）
        if results and results[0].order_id:
            await _cleanup_order(
                client, symbol, results[0].order_id, f"{label} entry"
            )
        # 念のため symbol の全 open を掃除（grouped TP/SL が残ってないか確認）
        await _cancel_all_for_symbol(client, symbol, label)
    except Exception as e:
        _print_exception(label, e)


# ─── メイン ───────────────────────────────


async def main() -> None:
    _print_banner()

    # 環境変数（age 鍵）の自動設定
    if not os.environ.get("SOPS_AGE_KEY_FILE"):
        age_key = _PROJECT_ROOT / "secrets" / ".age-key"
        if age_key.exists():
            os.environ["SOPS_AGE_KEY_FILE"] = str(age_key)
            print(f"SOPS_AGE_KEY_FILE = {age_key}")

    if not _confirm("BOT は停止していますね？テストを実行しますか？"):
        print("中止しました。")
        return

    # 設定・secrets ロード
    settings = load_settings(
        "config/settings.yaml", "config/profile_phase2.yaml"
    )
    if settings.exchange.network != "mainnet":
        print(
            f"[WARN] network={settings.exchange.network} です。"
            "mainnet ではありません。中止。"
        )
        return
    secrets = load_secrets()

    # HyperLiquidClient（BOT と同じ初期化経路）
    client = HyperLiquidClient(
        network=settings.exchange.network,
        address=secrets.master_address,
        agent_private_key=secrets.agent_private_key,
    )

    # 銘柄情報
    snap = await client.get_market_snapshot(SYMBOL)
    mark_px = Decimal(str(snap.current_price))
    sz_decimals = await client.get_sz_decimals(SYMBOL)
    tick_size = await client.get_tick_size(SYMBOL)
    balance = await client.get_account_balance_usd()

    raw_size = TARGET_NOTIONAL_USD / mark_px
    size = _round_size(raw_size, sz_decimals)

    stage_a_price_raw = mark_px * STAGE_A_MULTIPLIER
    stage_a_price = _round_price(stage_a_price_raw, tick_size)
    stage_b_price = _round_price(mark_px, tick_size)

    print()
    print(f"  symbol:        {SYMBOL}")
    print(f"  balance:       ${balance}")
    print(f"  markPx:        ${mark_px}")
    print(f"  sz_decimals:   {sz_decimals}")
    print(f"  tick_size:     {tick_size}")
    print(f"  size:          {size} {SYMBOL} (~${size * mark_px:.2f})")
    print(f"  Stage A price: ${stage_a_price}  (markPx × {STAGE_A_MULTIPLIER})")
    print(f"  Stage B price: ${stage_b_price}  (markPx 近傍)")
    print()

    # ベースライン: 既存 open orders
    await _list_open_orders(client, SYMBOL, "baseline")

    if not _confirm("\n段階 A に進みますか？（板のはるか上で発注・約定しない想定）"):
        print("中止しました。")
        return

    # 段階 A: markPx × 1.15
    stage_a_tp = _round_price(stage_a_price * Decimal("0.96"), tick_size)
    stage_a_sl = _round_price(stage_a_price * Decimal("1.04"), tick_size)
    await _test_single(client, SYMBOL, size, stage_a_price, "Stage A / single")
    await _test_grouped(
        client, SYMBOL, size, stage_a_price, stage_a_tp, stage_a_sl,
        "Stage A / grouped",
    )

    if not _confirm("\n段階 B に進みますか？（markPx 近傍・ALO 拒否される想定）"):
        print("中止しました。")
        await _list_open_orders(client, SYMBOL, "final")
        return

    # 段階 B: markPx 近傍
    stage_b_tp = _round_price(stage_b_price * Decimal("0.96"), tick_size)
    stage_b_sl = _round_price(stage_b_price * Decimal("1.04"), tick_size)
    await _test_single(client, SYMBOL, size, stage_b_price, "Stage B / single")
    await _test_grouped(
        client, SYMBOL, size, stage_b_price, stage_b_tp, stage_b_sl,
        "Stage B / grouped",
    )

    # 最終確認
    await _list_open_orders(client, SYMBOL, "final")
    print("\n[OK] テスト完了。レスポンス内容を観察してください。")


if __name__ == "__main__":
    asyncio.run(main())
