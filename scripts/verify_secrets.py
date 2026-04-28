"""secrets/secrets.enc.yaml の動作確認スクリプト（章23.3）。

- sops で復号
- pydantic でスキーマ検証
- testnet 接続テスト

使い方:
    python scripts/verify_secrets.py

依存:
    sops + age（外部コマンド）が PATH に必要。
    age 復号鍵のパスは環境変数 SOPS_AGE_KEY_FILE で指定するか、
    sops のデフォルト探索順（~/.config/sops/age/keys.txt 等）に従う。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError


class HyperLiquidSecrets(BaseModel):
    """章23.3 の HyperLiquid セクションのスキーマ。"""

    master_address: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    agent_private_key: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")
    agent_address: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    network: Literal["mainnet", "testnet"]


class SecretsSchema(BaseModel):
    """secrets.yaml 全体の親スキーマ。"""

    hyperliquid: HyperLiquidSecrets


def decrypt_secrets() -> dict[str, object]:
    """sops でファイルを復号し、YAML を dict として返す。"""
    try:
        result = subprocess.run(
            ["sops", "-d", "secrets/secrets.enc.yaml"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        print("❌ sops コマンドが見つかりません")
        print("   Install: brew install sops (macOS) / scoop install sops (Win)")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"❌ 復号失敗: {e.stderr}")
        sys.exit(1)
    parsed = yaml.safe_load(result.stdout)
    if not isinstance(parsed, dict):
        print("❌ 復号後の YAML が dict ではありません")
        sys.exit(1)
    return parsed


def validate_schema(data: dict[str, object]) -> SecretsSchema:
    """pydantic でスキーマ検証（章23.6 の方針に従う）。"""
    try:
        return SecretsSchema(**data)  # type: ignore[arg-type]
    except ValidationError as e:
        print(f"❌ スキーマ違反: {e}")
        sys.exit(1)


async def test_connection(secrets: SecretsSchema) -> None:
    """testnet 接続テスト。Master アドレスで残高 / ポジ / 注文を読む。"""
    from src.infrastructure.hyperliquid_client import HyperLiquidClient

    client = HyperLiquidClient(
        network=secrets.hyperliquid.network,
        address=secrets.hyperliquid.master_address,
    )

    try:
        balance = await client.get_account_balance_usd()
        positions = await client.get_positions()
        orders = await client.get_open_orders()
    except Exception as e:
        print(f"❌ HyperLiquid 接続失敗: {e}")
        sys.exit(1)

    print(f"✓ Network        : {secrets.hyperliquid.network}")
    print(f"✓ Master Address : {secrets.hyperliquid.master_address}")
    print(f"✓ Agent Address  : {secrets.hyperliquid.agent_address}")
    print(f"✓ Balance        : {balance} USDC")
    print(f"✓ Positions      : {len(positions)} 件")
    print(f"✓ Open Orders    : {len(orders)} 件")


def main() -> None:
    print("=" * 60)
    print("  secrets/secrets.enc.yaml 動作確認")
    print("=" * 60)
    print()

    print("[1/3] 復号 ... ", end="", flush=True)
    data = decrypt_secrets()
    print("OK")

    print("[2/3] スキーマ検証 ... ", end="", flush=True)
    secrets = validate_schema(data)
    print("OK")

    print("[3/3] testnet 接続テスト ...")
    asyncio.run(test_connection(secrets))

    print()
    print("✓ すべての確認が成功しました")
    print()
    print("次のステップ：PR6.4.1（cancel_order 実装）")


if __name__ == "__main__":
    main()
