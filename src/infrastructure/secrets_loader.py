"""secrets/secrets.enc.yaml の読み込みモジュール（章23.3）。

sops で復号 → pydantic でスキーマ検証 → 型安全な dataclass を返す。

PyYAML は unquoted な 0x... を hex 整数として int に解釈してしまうため、
mode='before' の validator で int を 0x プレフィックス付きのゼロパディング
hex 文字列へ復元する（章23.3 の運用上の罠への対策）。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError, field_validator

_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_PRIVATE_KEY_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")


class HyperLiquidSecretsModel(BaseModel):
    """章23.3 の HyperLiquid セクションの pydantic スキーマ。"""

    master_address: str
    agent_private_key: str
    agent_address: str
    network: Literal["mainnet", "testnet"]

    @field_validator("master_address", "agent_address", mode="before")
    @classmethod
    def coerce_address(cls, v: object) -> object:
        if isinstance(v, int) and not isinstance(v, bool):
            return "0x" + format(v, "040x")
        return v

    @field_validator("agent_private_key", mode="before")
    @classmethod
    def coerce_private_key(cls, v: object) -> object:
        if isinstance(v, int) and not isinstance(v, bool):
            return "0x" + format(v, "064x")
        return v

    @field_validator("master_address", "agent_address")
    @classmethod
    def validate_address_format(cls, v: str) -> str:
        if not _ADDRESS_RE.match(v):
            raise ValueError(f"Invalid address format: {v}")
        return v

    @field_validator("agent_private_key")
    @classmethod
    def validate_private_key_format(cls, v: str) -> str:
        if not _PRIVATE_KEY_RE.match(v):
            raise ValueError(f"Invalid private key format: {v}")
        return v


class DiscordSecretsModel(BaseModel):
    """Discord Webhook URLs（PR7.5d で追加・全フィールド optional）。

    secrets.yaml に discord セクションが無くても従来通り動作するよう
    完全 optional にしてある。
    """

    webhook_signal: str | None = None
    webhook_alert: str | None = None
    webhook_summary: str | None = None
    webhook_error: str | None = None


class SecretsModel(BaseModel):
    """secrets.yaml 全体の親スキーマ。"""

    hyperliquid: HyperLiquidSecretsModel
    discord: DiscordSecretsModel | None = None


@dataclass(frozen=True)
class DiscordSecrets:
    """Discord Webhook URLs（None なら DiscordNotifier を使わない）。"""

    webhook_signal: str | None
    webhook_alert: str | None
    webhook_summary: str | None
    webhook_error: str | None

    @property
    def is_configured(self) -> bool:
        """4 つ全部が設定されているか。部分設定は使わない判定。"""
        return all(
            [
                self.webhook_signal,
                self.webhook_alert,
                self.webhook_summary,
                self.webhook_error,
            ]
        )


@dataclass(frozen=True)
class HyperLiquidSecrets:
    """型安全な不変データクラス（運用コードはこれを受け取る）。"""

    master_address: str
    agent_private_key: str
    agent_address: str
    network: Literal["mainnet", "testnet"]
    discord: DiscordSecrets | None = None


class SecretsLoadError(Exception):
    """secrets 読み込み失敗時に raise される例外。"""


def load_secrets(
    encrypted_path: str | Path = "secrets/secrets.enc.yaml",
) -> HyperLiquidSecrets:
    """sops で復号 → pydantic 検証 → HyperLiquidSecrets を返す。

    Args:
        encrypted_path: 暗号化された secrets ファイルのパス。

    Returns:
        HyperLiquidSecrets（frozen dataclass）。

    Raises:
        SecretsLoadError: 復号・検証に失敗した場合。
    """
    path = Path(encrypted_path)
    if not path.exists():
        raise SecretsLoadError(f"Secrets file not found: {path}")

    try:
        result = subprocess.run(
            ["sops", "-d", str(path)],
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",  # Windows cp932 対策（章26）
        )
    except FileNotFoundError as e:
        raise SecretsLoadError(
            "sops command not found. Install: brew install sops (macOS) "
            "/ scoop install sops (Win)"
        ) from e
    except subprocess.CalledProcessError as e:
        raise SecretsLoadError(f"Failed to decrypt: {e.stderr}") from e

    try:
        data = yaml.safe_load(result.stdout)
    except yaml.YAMLError as e:
        raise SecretsLoadError(f"Invalid YAML: {e}") from e

    if not isinstance(data, dict):
        raise SecretsLoadError("Decrypted YAML is not a mapping")

    try:
        validated = SecretsModel(**data)
    except ValidationError as e:
        raise SecretsLoadError(f"Schema validation failed: {e}") from e

    discord_secrets: DiscordSecrets | None = None
    if validated.discord is not None:
        discord_secrets = DiscordSecrets(
            webhook_signal=validated.discord.webhook_signal,
            webhook_alert=validated.discord.webhook_alert,
            webhook_summary=validated.discord.webhook_summary,
            webhook_error=validated.discord.webhook_error,
        )

    return HyperLiquidSecrets(
        master_address=validated.hyperliquid.master_address,
        agent_private_key=validated.hyperliquid.agent_private_key,
        agent_address=validated.hyperliquid.agent_address,
        network=validated.hyperliquid.network,
        discord=discord_secrets,
    )
