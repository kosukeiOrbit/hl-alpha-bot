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

    def test_coerce_int_address_to_hex(self) -> None:
        # PyYAML は unquoted な 0x... を int に解釈する。validator で復元できること。
        s = HyperLiquidSecrets(
            master_address=0x910571363855665C9511F06ED7B691AB32FC1BD5,  # type: ignore[arg-type]
            agent_private_key=VALID_PRIVATE_KEY,
            agent_address=VALID_ADDRESS2,
            network="testnet",
        )
        assert s.master_address == "0x910571363855665c9511f06ed7b691ab32fc1bd5"

    def test_coerce_int_private_key_to_hex(self) -> None:
        s = HyperLiquidSecrets(
            master_address=VALID_ADDRESS,
            agent_private_key=0x1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF,  # type: ignore[arg-type]
            agent_address=VALID_ADDRESS2,
            network="testnet",
        )
        expected = "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
        assert s.agent_private_key == expected

    def test_coerce_int_pads_leading_zeros(self) -> None:
        # 先頭がゼロのアドレスでも 0x + 40桁にパディングされること。
        s = HyperLiquidSecrets(
            master_address=0x0011223344556677889900112233445566778899,  # type: ignore[arg-type]
            agent_private_key=VALID_PRIVATE_KEY,
            agent_address=VALID_ADDRESS2,
            network="testnet",
        )
        assert s.master_address == "0x0011223344556677889900112233445566778899"
        assert len(s.master_address) == 42

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
