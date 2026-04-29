"""secrets_loader のテスト（章23.3）。

- 単体: pydantic スキーマの境界条件
- mock: subprocess(sops) を mock した load_secrets フローのテスト
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from src.infrastructure.secrets_loader import (
    HyperLiquidSecrets,
    HyperLiquidSecretsModel,
    SecretsLoadError,
    load_secrets,
)

VALID_ADDRESS = "0x" + "a" * 40
VALID_ADDRESS2 = "0x" + "c" * 40
VALID_PRIVATE_KEY = "0x" + "b" * 64


class TestHyperLiquidSecretsModel:
    def test_valid_testnet(self) -> None:
        m = HyperLiquidSecretsModel(
            master_address=VALID_ADDRESS,
            agent_private_key=VALID_PRIVATE_KEY,
            agent_address=VALID_ADDRESS2,
            network="testnet",
        )
        assert m.network == "testnet"

    def test_valid_mainnet(self) -> None:
        m = HyperLiquidSecretsModel(
            master_address=VALID_ADDRESS,
            agent_private_key=VALID_PRIVATE_KEY,
            agent_address=VALID_ADDRESS2,
            network="mainnet",
        )
        assert m.network == "mainnet"

    def test_coerce_int_to_address(self) -> None:
        # PyYAML が unquoted 0x... を int に解釈した時の復元（sops の罠対策）。
        m = HyperLiquidSecretsModel(
            master_address=0x910571363855665C9511F06ED7B691AB32FC1BD5,  # type: ignore[arg-type]
            agent_private_key=VALID_PRIVATE_KEY,
            agent_address=VALID_ADDRESS2,
            network="testnet",
        )
        assert m.master_address == "0x910571363855665c9511f06ed7b691ab32fc1bd5"

    def test_coerce_int_to_private_key(self) -> None:
        # 小さい int も 64 桁ゼロパディングで復元される。
        m = HyperLiquidSecretsModel(
            master_address=VALID_ADDRESS,
            agent_private_key=0xBB,  # type: ignore[arg-type]
            agent_address=VALID_ADDRESS2,
            network="testnet",
        )
        assert m.agent_private_key == "0x" + "0" * 62 + "bb"

    def test_coerce_int_pads_leading_zeros(self) -> None:
        m = HyperLiquidSecretsModel(
            master_address=0x0011223344556677889900112233445566778899,  # type: ignore[arg-type]
            agent_private_key=VALID_PRIVATE_KEY,
            agent_address=VALID_ADDRESS2,
            network="testnet",
        )
        assert m.master_address == "0x0011223344556677889900112233445566778899"

    def test_invalid_address_too_short(self) -> None:
        with pytest.raises(ValidationError):
            HyperLiquidSecretsModel(
                master_address="0x" + "a" * 30,
                agent_private_key=VALID_PRIVATE_KEY,
                agent_address=VALID_ADDRESS2,
                network="testnet",
            )

    def test_invalid_private_key_format(self) -> None:
        with pytest.raises(ValidationError):
            HyperLiquidSecretsModel(
                master_address=VALID_ADDRESS,
                agent_private_key="0x" + "z" * 64,
                agent_address=VALID_ADDRESS2,
                network="testnet",
            )

    def test_invalid_network(self) -> None:
        with pytest.raises(ValidationError):
            HyperLiquidSecretsModel(
                master_address=VALID_ADDRESS,
                agent_private_key=VALID_PRIVATE_KEY,
                agent_address=VALID_ADDRESS2,
                network="devnet",  # type: ignore[arg-type]
            )


class TestLoadSecrets:
    _MOCK_YAML = (
        "hyperliquid:\n"
        '  master_address: "0x910571363855665c9511f06ed7b691ab32fc1bd5"\n'
        f'  agent_private_key: "0x{"b" * 64}"\n'
        '  agent_address: "0xb4a8b7c48114308b03d40de1c958704338a5cd1b"\n'
        '  network: "testnet"\n'
    )

    def test_file_not_found(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "missing.enc.yaml"
        with pytest.raises(SecretsLoadError, match="not found"):
            load_secrets(nonexistent)

    def test_successful_load_returns_frozen_dataclass(self, tmp_path: Path) -> None:
        fake_file = tmp_path / "secrets.enc.yaml"
        fake_file.write_text("encrypted-content")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=self._MOCK_YAML, returncode=0)
            secrets = load_secrets(fake_file)

        assert isinstance(secrets, HyperLiquidSecrets)
        assert secrets.network == "testnet"
        assert secrets.master_address == "0x910571363855665c9511f06ed7b691ab32fc1bd5"
        # frozen=True なので代入で例外
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            secrets.network = "mainnet"  # type: ignore[misc]

    def test_load_uses_default_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # デフォルト引数 "secrets/secrets.enc.yaml" の経路を踏ませる。
        monkeypatch.chdir(tmp_path)
        (tmp_path / "secrets").mkdir()
        (tmp_path / "secrets" / "secrets.enc.yaml").write_text("x")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=self._MOCK_YAML, returncode=0)
            secrets = load_secrets()
        assert secrets.network == "testnet"

    def test_sops_not_found(self, tmp_path: Path) -> None:
        fake_file = tmp_path / "secrets.enc.yaml"
        fake_file.write_text("content")

        with (
            patch("subprocess.run", side_effect=FileNotFoundError),
            pytest.raises(SecretsLoadError, match="sops command not found"),
        ):
            load_secrets(fake_file)

    def test_sops_decrypt_error(self, tmp_path: Path) -> None:
        fake_file = tmp_path / "secrets.enc.yaml"
        fake_file.write_text("content")

        err = subprocess.CalledProcessError(1, ["sops"], stderr="decryption failed")
        with (
            patch("subprocess.run", side_effect=err),
            pytest.raises(SecretsLoadError, match="Failed to decrypt"),
        ):
            load_secrets(fake_file)

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        fake_file = tmp_path / "secrets.enc.yaml"
        fake_file.write_text("content")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="invalid: yaml: [", returncode=0)
            with pytest.raises(SecretsLoadError, match="Invalid YAML"):
                load_secrets(fake_file)

    def test_yaml_not_a_mapping(self, tmp_path: Path) -> None:
        # ルートが list だった場合は SecretsLoadError。
        fake_file = tmp_path / "secrets.enc.yaml"
        fake_file.write_text("content")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="- a\n- b\n", returncode=0)
            with pytest.raises(SecretsLoadError, match="not a mapping"):
                load_secrets(fake_file)

    def test_schema_validation_error(self, tmp_path: Path) -> None:
        fake_file = tmp_path / "secrets.enc.yaml"
        fake_file.write_text("content")

        bad_yaml = (
            "hyperliquid:\n"
            '  master_address: "not-an-address"\n'
            '  agent_private_key: "0xbb"\n'
            '  agent_address: "0xcc"\n'
            '  network: "testnet"\n'
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=bad_yaml, returncode=0)
            with pytest.raises(SecretsLoadError, match="Schema validation failed"):
                load_secrets(fake_file)


class TestDiscordSection:
    """PR7.5d で追加した discord セクション対応。"""

    _BASE_YAML = (
        "hyperliquid:\n"
        '  master_address: "0x910571363855665c9511f06ed7b691ab32fc1bd5"\n'
        f'  agent_private_key: "0x{"b" * 64}"\n'
        '  agent_address: "0xb4a8b7c48114308b03d40de1c958704338a5cd1b"\n'
        '  network: "testnet"\n'
    )

    @staticmethod
    def _run_with_yaml(tmp_path: Path, yaml_text: str) -> object:
        from src.infrastructure.secrets_loader import load_secrets

        fake_file = tmp_path / "secrets.enc.yaml"
        fake_file.write_text("ciphertext")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=yaml_text, returncode=0
            )
            return load_secrets(fake_file)

    def test_no_discord_section_returns_none(self, tmp_path: Path) -> None:
        secrets = self._run_with_yaml(tmp_path, self._BASE_YAML)
        assert secrets.discord is None  # type: ignore[attr-defined]

    def test_full_discord_section_is_configured(
        self, tmp_path: Path
    ) -> None:
        yaml_text = self._BASE_YAML + (
            "discord:\n"
            '  webhook_signal: "https://discord.com/api/webhooks/sig"\n'
            '  webhook_alert: "https://discord.com/api/webhooks/alt"\n'
            '  webhook_summary: "https://discord.com/api/webhooks/sum"\n'
            '  webhook_error: "https://discord.com/api/webhooks/err"\n'
        )
        secrets = self._run_with_yaml(tmp_path, yaml_text)
        assert secrets.discord is not None  # type: ignore[attr-defined]
        assert secrets.discord.is_configured is True  # type: ignore[attr-defined]
        assert (  # type: ignore[attr-defined]
            secrets.discord.webhook_signal
            == "https://discord.com/api/webhooks/sig"
        )

    def test_partial_discord_section_not_configured(
        self, tmp_path: Path
    ) -> None:
        # 3 つだけ設定 → is_configured は False
        yaml_text = self._BASE_YAML + (
            "discord:\n"
            '  webhook_signal: "https://discord.com/api/webhooks/sig"\n'
            '  webhook_alert: "https://discord.com/api/webhooks/alt"\n'
            '  webhook_summary: "https://discord.com/api/webhooks/sum"\n'
        )
        secrets = self._run_with_yaml(tmp_path, yaml_text)
        assert secrets.discord is not None  # type: ignore[attr-defined]
        assert secrets.discord.is_configured is False  # type: ignore[attr-defined]
        assert secrets.discord.webhook_error is None  # type: ignore[attr-defined]

    def test_discord_secrets_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        from src.infrastructure.secrets_loader import DiscordSecrets

        d = DiscordSecrets(
            webhook_signal="a",
            webhook_alert="b",
            webhook_summary="c",
            webhook_error="d",
        )
        assert d.is_configured is True
        with pytest.raises(FrozenInstanceError):
            d.webhook_signal = "x"  # type: ignore[misc]
