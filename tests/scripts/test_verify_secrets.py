"""verify_secrets.py のスキーマ検証テスト（章23.6）。

復号や testnet 接続は副作用が大きいので別途 e2e で扱い、
ここでは pydantic スキーマの境界条件のみを純粋に検証する。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scripts.verify_secrets import HyperLiquidSecrets, SecretsSchema

VALID_ADDRESS = "0x" + "a" * 40
VALID_ADDRESS2 = "0x" + "b" * 40
VALID_PRIVATE_KEY = "0x" + "c" * 64


class TestHyperLiquidSecrets:
    def test_valid_testnet(self) -> None:
        secrets = HyperLiquidSecrets(
            master_address=VALID_ADDRESS,
            agent_private_key=VALID_PRIVATE_KEY,
            agent_address=VALID_ADDRESS2,
            network="testnet",
        )
        assert secrets.network == "testnet"
        assert secrets.master_address == VALID_ADDRESS
        assert secrets.agent_address == VALID_ADDRESS2
        assert secrets.agent_private_key == VALID_PRIVATE_KEY

    def test_valid_mainnet(self) -> None:
        secrets = HyperLiquidSecrets(
            master_address=VALID_ADDRESS,
            agent_private_key=VALID_PRIVATE_KEY,
            agent_address=VALID_ADDRESS2,
            network="mainnet",
        )
        assert secrets.network == "mainnet"

    def test_invalid_network(self) -> None:
        with pytest.raises(ValidationError):
            HyperLiquidSecrets(
                master_address=VALID_ADDRESS,
                agent_private_key=VALID_PRIVATE_KEY,
                agent_address=VALID_ADDRESS2,
                network="devnet",  # type: ignore[arg-type]
            )

    def test_invalid_address_format(self) -> None:
        # 0x プレフィックス無し
        with pytest.raises(ValidationError):
            HyperLiquidSecrets(
                master_address="a" * 40,
                agent_private_key=VALID_PRIVATE_KEY,
                agent_address=VALID_ADDRESS2,
                network="testnet",
            )
        # 文字数不足
        with pytest.raises(ValidationError):
            HyperLiquidSecrets(
                master_address="0x" + "a" * 39,
                agent_private_key=VALID_PRIVATE_KEY,
                agent_address=VALID_ADDRESS2,
                network="testnet",
            )
        # 16進以外の文字
        with pytest.raises(ValidationError):
            HyperLiquidSecrets(
                master_address="0x" + "z" * 40,
                agent_private_key=VALID_PRIVATE_KEY,
                agent_address=VALID_ADDRESS2,
                network="testnet",
            )

    def test_invalid_private_key_length(self) -> None:
        # 短すぎ
        with pytest.raises(ValidationError):
            HyperLiquidSecrets(
                master_address=VALID_ADDRESS,
                agent_private_key="0x" + "c" * 63,
                agent_address=VALID_ADDRESS2,
                network="testnet",
            )
        # 長すぎ
        with pytest.raises(ValidationError):
            HyperLiquidSecrets(
                master_address=VALID_ADDRESS,
                agent_private_key="0x" + "c" * 65,
                agent_address=VALID_ADDRESS2,
                network="testnet",
            )


class TestSecretsSchema:
    def test_full_schema(self) -> None:
        schema = SecretsSchema(
            hyperliquid=HyperLiquidSecrets(
                master_address=VALID_ADDRESS,
                agent_private_key=VALID_PRIVATE_KEY,
                agent_address=VALID_ADDRESS2,
                network="testnet",
            )
        )
        assert schema.hyperliquid.network == "testnet"

    def test_missing_hyperliquid(self) -> None:
        with pytest.raises(ValidationError):
            SecretsSchema()  # type: ignore[call-arg]
