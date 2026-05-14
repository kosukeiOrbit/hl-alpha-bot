"""mainnet balance 切り分け診断スクリプト（read-only・使い捨て）。

BOT が get_account_balance_usd() で $0 を取得する原因を特定するため、
HL Info API の各エンドポイントの raw レスポンスをそのまま表示する。

確認ポイント:
- master_address vs agent_address: どちらが BOT で参照されているか
- user_state: perp margin（marginSummary / crossMarginSummary / withdrawable）
- spot_user_state: spot 残高（USDC が spot にあれば見える）
- query_sub_accounts: サブアカウントが分けている可能性
- portfolio: 集計表示

read-only（発注しない）なので資金影響なし。BOT 稼働中でも実行可能。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.core.config_loader import load_settings  # noqa: E402
from src.infrastructure.hyperliquid_client import HyperLiquidClient  # noqa: E402
from src.infrastructure.secrets_loader import load_secrets  # noqa: E402


def _print_section(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _dump(label: str, obj: object) -> None:
    print(f"\n--- {label} ---")
    try:
        print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))
    except Exception as e:
        print(f"(JSON dump failed: {e})")
        print(f"repr: {obj!r}")


async def main() -> None:  # noqa: C901  # 使い捨て診断スクリプトのため許容
    # 環境変数設定（sops 復号用）
    if not os.environ.get("SOPS_AGE_KEY_FILE"):
        age_key = _PROJECT_ROOT / "secrets" / ".age-key"
        if age_key.exists():
            os.environ["SOPS_AGE_KEY_FILE"] = str(age_key)

    settings = load_settings(
        "config/settings.yaml", "config/profile_phase2.yaml"
    )
    secrets = load_secrets()

    _print_section("address 構成")
    print(f"network:        {settings.exchange.network}")
    print(f"master_address: {secrets.master_address}")
    print(
        f"agent_address:  "
        f"{getattr(secrets, 'agent_address', '<not set>')}"
    )
    print()
    print("BOT の `get_account_balance_usd()` は HyperLiquidClient.address を渡す。")
    print("HyperLiquidClient.address は main.py で secrets.master_address を渡している。")
    print("つまり BOT が見ているのは master_address。")

    # BOT と同じ HyperLiquidClient で実測
    client = HyperLiquidClient(
        network=settings.exchange.network,
        address=secrets.master_address,
        agent_private_key=secrets.agent_private_key,
    )
    info = client.info  # 公式 SDK Info オブジェクト

    # ─── 1. user_state(master_address) ─────
    _print_section("1. info.user_state(master_address) raw")
    try:
        master_state = await asyncio.to_thread(
            info.user_state, secrets.master_address
        )
        _dump("FULL response", master_state)
        # ハイライト
        print("\n--- ハイライト ---")
        ms = master_state.get("marginSummary", {}) if isinstance(
            master_state, dict
        ) else {}
        cms = master_state.get("crossMarginSummary", {}) if isinstance(
            master_state, dict
        ) else {}
        print(f"marginSummary.accountValue      = {ms.get('accountValue')!r}")
        print(f"marginSummary.totalMarginUsed   = {ms.get('totalMarginUsed')!r}")
        print(f"marginSummary.totalNtlPos       = {ms.get('totalNtlPos')!r}")
        print(f"marginSummary.totalRawUsd       = {ms.get('totalRawUsd')!r}")
        print(f"crossMarginSummary.accountValue = {cms.get('accountValue')!r}")
        print(f"withdrawable                    = "
              f"{master_state.get('withdrawable')!r}"
              if isinstance(master_state, dict) else "")
        positions = (
            master_state.get("assetPositions", [])
            if isinstance(master_state, dict) else []
        )
        print(f"assetPositions count            = {len(positions)}")
    except Exception as e:
        print(f"error: {e!r}")

    # ─── 2. user_state(agent_address) ─────
    agent = getattr(secrets, "agent_address", None)
    if agent:
        _print_section("2. info.user_state(agent_address) raw")
        try:
            agent_state = await asyncio.to_thread(info.user_state, agent)
            _dump("FULL response", agent_state)
        except Exception as e:
            print(f"error: {e!r}")

    # ─── 3. spot_user_state(master_address) ─────
    _print_section("3. info.spot_user_state(master_address) raw")
    try:
        spot_state = await asyncio.to_thread(
            info.spot_user_state, secrets.master_address
        )
        _dump("FULL response", spot_state)
        balances = (
            spot_state.get("balances", [])
            if isinstance(spot_state, dict) else []
        )
        print("\n--- ハイライト ---")
        print(f"balances count = {len(balances)}")
        for b in balances:
            print(
                f"  {b.get('coin')}: total={b.get('total')} "
                f"hold={b.get('hold')} entryNtl={b.get('entryNtl')}"
            )
    except Exception as e:
        print(f"error: {e!r}")

    # ─── 4. spot_user_state(agent_address) ─────
    if agent:
        _print_section("4. info.spot_user_state(agent_address) raw")
        try:
            agent_spot = await asyncio.to_thread(info.spot_user_state, agent)
            _dump("FULL response", agent_spot)
        except Exception as e:
            print(f"error: {e!r}")

    # ─── 5. query_sub_accounts(master_address) ─────
    _print_section("5. info.query_sub_accounts(master_address) raw")
    try:
        subs = await asyncio.to_thread(
            info.query_sub_accounts, secrets.master_address
        )
        _dump("FULL response", subs)
    except Exception as e:
        print(f"error: {e!r}")

    # ─── 6. portfolio(master_address) ─────
    _print_section("6. info.portfolio(master_address) raw")
    try:
        portfolio = await asyncio.to_thread(
            info.portfolio, secrets.master_address
        )
        # portfolio は通常配列なので最初の数件だけ
        if isinstance(portfolio, list) and len(portfolio) > 0:
            print(f"portfolio is a list of length {len(portfolio)}")
            _dump("first item", portfolio[0])
            if len(portfolio) > 1:
                _dump("last item", portfolio[-1])
        else:
            _dump("FULL response", portfolio)
    except Exception as e:
        print(f"error: {e!r}")

    # ─── 7. BOT の get_account_balance_usd() 再実行 ─────
    _print_section("7. BOT の get_account_balance_usd() 結果")
    try:
        bal = await client.get_account_balance_usd()
        print(f"BOT が見る balance = ${bal}")
        print(f"型: {type(bal).__name__}")
        print(f"Decimal('0') と等しい: {bal == Decimal('0')}")
    except Exception as e:
        print(f"error: {e!r}")

    print()
    print("=" * 72)
    print("  診断完了")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
