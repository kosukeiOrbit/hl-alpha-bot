"""設定ローダー（章23.7）。

settings.yaml + profile_*.yaml を deep-merge して AppSettings を返す。
純関数（I/O は YAML 読み込みのみ・副作用なし）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from config.schema import AppSettings


def deep_merge(
    base: dict[str, Any], override: dict[str, Any]
) -> dict[str, Any]:
    """dict を再帰的にマージ（override が優先）。

    両方が dict のキーは再帰的にマージ。
    片方が dict でなければ override で完全に上書き。
    """
    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_settings(
    base_path: Path | str = "config/settings.yaml",
    profile_path: Path | str | None = None,
) -> AppSettings:
    """settings.yaml + profile を読み込んで AppSettings を返す。

    Args:
        base_path: 基本設定ファイル
        profile_path: プロファイル（None なら base のみ）

    Raises:
        FileNotFoundError: ファイル無し
        ValidationError: pydantic スキーマ違反
    """
    base = Path(base_path)
    if not base.exists():
        raise FileNotFoundError(f"Settings not found: {base}")
    with base.open("r", encoding="utf-8") as f:
        base_data = yaml.safe_load(f) or {}

    if profile_path is not None:
        prof = Path(profile_path)
        if not prof.exists():
            raise FileNotFoundError(f"Profile not found: {prof}")
        with prof.open("r", encoding="utf-8") as f:
            profile_data = yaml.safe_load(f) or {}
        merged = deep_merge(base_data, profile_data)
    else:
        merged = base_data

    return AppSettings(**merged)
