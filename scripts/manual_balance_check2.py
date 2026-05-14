"""HL balance 完全切り分け（cross-margin / unified margin 仮説の検証）。

問題: spot=$295, perp=$0 なのに ALO 発注（Stage A）が受理された矛盾。
仮説: HL は spot USDC を perp 担保として使える（unified / cross-margin）。

このスクリプトは:
1. 現在時刻のタイムスタンプ付きで全関連エンドポイントを再取得
2. spot/perp 以外の「取引可能額」フィールドを徹底列挙
3. 入出金履歴（ledger updates）で過去の資金移動を可視化
4. SDK にある全ての balance/state 系メソッドの戻り値を網羅

read-only。発注しない。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.core.config_loader import load_settings  # noqa: E402
from src.infrastructure.hyperliquid_client import HyperLiquidClient  # noqa: E402
from src.infrastructure.secrets_loader import load_secrets  # noqa: E402


def _section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _dump(label: str, obj: object) -> None:
    print(f"\n--- {label} ---")
    try:
        print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))
    except Exception as e:
        print(f"(JSON dump failed: {e})  repr={obj!r}")


def _walk_keys(obj: object, path: str = "") -> list[tuple[str, object]]:
    """dict/list を再帰的に歩いて全ての (path, value) を列挙。"""
    out: list[tuple[str, object]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{path}.{k}" if path else k
            out.append((sub, v))
            out.extend(_walk_keys(v, sub))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            sub = f"{path}[{i}]"
            out.extend(_walk_keys(v, sub))
    return out


async def main() -> None:  # noqa: C901
    if not os.environ.get("SOPS_AGE_KEY_FILE"):
        age_key = _PROJECT_ROOT / "secrets" / ".age-key"
        if age_key.exists():
            os.environ["SOPS_AGE_KEY_FILE"] = str(age_key)

    settings = load_settings(
        "config/settings.yaml", "config/profile_phase2.yaml"
    )
    secrets = load_secrets()

    _section("0. 実行コンテキスト")
    now_ms = int(time.time() * 1000)
    print(f"now (UTC ms):   {now_ms}")
    print(f"now (ISO):      {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"network:        {settings.exchange.network}")
    print(f"master_address: {secrets.master_address}")
    print(f"agent_address:  {getattr(secrets, 'agent_address', None)}")

    client = HyperLiquidClient(
        network=settings.exchange.network,
        address=secrets.master_address,
        agent_private_key=secrets.agent_private_key,
    )
    info = client.info

    # ─── 1. user_state（perp）──────────────
    _section("1. info.user_state(master) — perp 側の全フィールド")
    master_state = await asyncio.to_thread(
        info.user_state, secrets.master_address
    )
    _dump("FULL", master_state)
    # 全フィールド walk
    print("\n--- すべての (path, value) 列挙 ---")
    for p, v in _walk_keys(master_state):
        if isinstance(v, str | int | float | bool) or v is None:
            print(f"  {p} = {v!r}")

    # ─── 2. spot_user_state ──────────────
    _section("2. info.spot_user_state(master) — spot 側の全フィールド")
    spot_state = await asyncio.to_thread(
        info.spot_user_state, secrets.master_address
    )
    _dump("FULL", spot_state)
    print("\n--- すべての (path, value) 列挙 ---")
    for p, v in _walk_keys(spot_state):
        if isinstance(v, str | int | float | bool) or v is None:
            print(f"  {p} = {v!r}")

    # ─── 3. user_role: アカウント種別を確認 ──
    _section("3. info.user_role(master) — アカウント種別/権限")
    try:
        role = await asyncio.to_thread(info.user_role, secrets.master_address)
        _dump("FULL", role)
    except Exception as e:
        print(f"error: {e!r}")

    # ─── 4. user_fees: cross-margin の手がかり ──
    _section("4. info.user_fees(master) — fee tier")
    try:
        fees = await asyncio.to_thread(info.user_fees, secrets.master_address)
        _dump("FULL", fees)
    except Exception as e:
        print(f"error: {e!r}")

    # ─── 5. perp_dexs: マルチ DEX 構成 ──
    _section("5. info.perp_dexs() — perp DEX 一覧（unified margin 手がかり）")
    try:
        dexs = await asyncio.to_thread(info.perp_dexs)
        _dump("FULL", dexs)
    except Exception as e:
        print(f"error: {e!r}")

    # ─── 6. query_user_abstraction_state ──
    _section("6. info.query_user_abstraction_state(master)")
    try:
        ua = await asyncio.to_thread(
            info.query_user_abstraction_state, secrets.master_address
        )
        _dump("FULL", ua)
    except Exception as e:
        print(f"error: {e!r}")

    # ─── 7. query_user_dex_abstraction_state ──
    _section("7. info.query_user_dex_abstraction_state(master)")
    try:
        uda = await asyncio.to_thread(
            info.query_user_dex_abstraction_state, secrets.master_address
        )
        _dump("FULL", uda)
    except Exception as e:
        print(f"error: {e!r}")

    # ─── 8. extra_agents ──
    _section("8. info.extra_agents(master) — agent wallet 一覧")
    try:
        agents = await asyncio.to_thread(
            info.extra_agents, secrets.master_address
        )
        _dump("FULL", agents)
    except Exception as e:
        print(f"error: {e!r}")

    # ─── 9. user_non_funding_ledger_updates: 入出金履歴 ──
    _section("9. info.user_non_funding_ledger_updates(master, since=7d) — 資金移動履歴")
    since_ms = now_ms - 7 * 24 * 60 * 60 * 1000
    try:
        ledger = await asyncio.to_thread(
            info.user_non_funding_ledger_updates,
            secrets.master_address,
            since_ms,
        )
        if isinstance(ledger, list):
            print(f"events: {len(ledger)}")
            for ev in ledger:
                _dump("event", ev)
        else:
            _dump("FULL", ledger)
    except Exception as e:
        print(f"error: {e!r}")

    # ─── 10. user_fills: 約定履歴（過去 fills が見えるか） ──
    _section("10. info.user_fills(master) — 約定履歴")
    try:
        fills = await asyncio.to_thread(info.user_fills, secrets.master_address)
        if isinstance(fills, list):
            print(f"fills count: {len(fills)}")
            for f in fills[:20]:
                _dump("fill", f)
        else:
            _dump("FULL", fills)
    except Exception as e:
        print(f"error: {e!r}")

    # ─── 11. 仮説検証: BOT の get_account_balance_usd ──
    _section("11. BOT の get_account_balance_usd() 実測")
    bal = await client.get_account_balance_usd()
    print(f"BOT が見る balance = ${bal}")
    print("→ marginSummary.accountValue から計算")
    print("→ spot USDC が cross-margin で perp 担保として使えるかは別問題")

    print()
    print("=" * 78)
    print("  診断完了")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
