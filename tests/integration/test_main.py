"""main.py / config_loader / config schema の統合テスト。

実 testnet には触れない。設定読込・依存組み立て・signal ハンドラ登録の
スモークまで。
"""

from __future__ import annotations

import logging
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from config.schema import AppSettings
from src.core.config_loader import deep_merge, load_settings
from src.main import (
    build_scheduler,
    install_signal_handlers,
    setup_logging,
)

# ─── deep_merge ─────────────────────────


class TestDeepMerge:
    def test_simple(self) -> None:
        assert deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_override_value(self) -> None:
        assert deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested(self) -> None:
        result = deep_merge(
            {"trading": {"is_dry_run": False, "leverage": 5}},
            {"trading": {"is_dry_run": True}},
        )
        assert result == {
            "trading": {"is_dry_run": True, "leverage": 5}
        }

    def test_non_dict_replaces(self) -> None:
        # list は dict ではないので完全上書き
        result = deep_merge(
            {"watchlist": {"fixed": ["BTC", "ETH"]}},
            {"watchlist": {"fixed": ["SOL"]}},
        )
        assert result == {"watchlist": {"fixed": ["SOL"]}}

    def test_dict_replaces_non_dict(self) -> None:
        # base が dict じゃないキーに override が dict を入れた場合
        # → 上書き（再帰しない）
        result = deep_merge({"x": 1}, {"x": {"a": 1}})
        assert result == {"x": {"a": 1}}


# ─── load_settings ──────────────────────


class TestLoadSettings:
    def test_load_real_settings_yaml(self) -> None:
        # 実プロジェクトの settings.yaml が読める
        settings = load_settings("config/settings.yaml")
        assert settings.phase == "phase_0"
        assert settings.trading.is_dry_run is True
        assert settings.exchange.network == "testnet"

    def test_load_with_profile_phase0(self) -> None:
        settings = load_settings(
            "config/settings.yaml",
            "config/profile_phase0.yaml",
        )
        assert settings.phase == "phase_0"
        assert settings.trading.is_dry_run is True

    def test_base_only(self, tmp_path: Path) -> None:
        base = tmp_path / "settings.yaml"
        base.write_text(
            "phase: phase_0\ntrading:\n  is_dry_run: true\n",
            encoding="utf-8",
        )
        s = load_settings(base)
        assert s.phase == "phase_0"
        assert s.trading.is_dry_run is True

    def test_profile_overrides(self, tmp_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text(
            "phase: phase_0\ntrading:\n  is_dry_run: false\n  leverage: 3\n",
            encoding="utf-8",
        )
        prof = tmp_path / "prof.yaml"
        prof.write_text(
            "trading:\n  is_dry_run: true\n",
            encoding="utf-8",
        )
        s = load_settings(base, prof)
        assert s.trading.is_dry_run is True
        assert s.trading.leverage == 3

    def test_missing_base_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            load_settings("nonexistent.yaml")

    def test_missing_profile_raises(self, tmp_path: Path) -> None:
        base = tmp_path / "settings.yaml"
        base.write_text("phase: phase_0\n", encoding="utf-8")
        with pytest.raises(FileNotFoundError, match="Profile not found"):
            load_settings(base, "missing_profile.yaml")

    def test_invalid_phase_raises(self, tmp_path: Path) -> None:
        base = tmp_path / "settings.yaml"
        base.write_text("phase: invalid_phase\n", encoding="utf-8")
        with pytest.raises(ValidationError):
            load_settings(base)

    def test_extra_field_rejected(self, tmp_path: Path) -> None:
        base = tmp_path / "settings.yaml"
        base.write_text(
            "phase: phase_0\nunknown_field: 1\n", encoding="utf-8"
        )
        with pytest.raises(ValidationError):
            load_settings(base)

    def test_empty_yaml_uses_defaults(self, tmp_path: Path) -> None:
        # 空ファイル → 全デフォルト
        base = tmp_path / "settings.yaml"
        base.write_text("", encoding="utf-8")
        s = load_settings(base)
        assert s.phase == "phase_0"

    def test_empty_profile_uses_base(self, tmp_path: Path) -> None:
        base = tmp_path / "settings.yaml"
        base.write_text("phase: phase_0\n", encoding="utf-8")
        prof = tmp_path / "prof.yaml"
        prof.write_text("", encoding="utf-8")
        s = load_settings(base, prof)
        assert s.phase == "phase_0"


# ─── build_scheduler ────────────────────


def _make_secrets() -> SimpleNamespace:
    return SimpleNamespace(
        master_address="0x" + "a" * 40,
        agent_private_key="0x" + "1" * 64,
    )


class TestBuildScheduler:
    def test_creates_all_components(self) -> None:
        settings = AppSettings()  # 全デフォルト
        scheduler, repo = build_scheduler(settings, _make_secrets())
        assert scheduler is not None
        assert repo is not None
        assert scheduler.entry_flow is not None
        assert scheduler.position_monitor is not None
        assert scheduler.reconciler is not None

    def test_propagates_dry_run_flag(self) -> None:
        settings = AppSettings(trading={"is_dry_run": True})  # type: ignore[arg-type]
        scheduler, _ = build_scheduler(settings, _make_secrets())
        assert scheduler.entry_flow.config.is_dry_run is True

    def test_propagates_watchlist(self) -> None:
        settings = AppSettings(
            watchlist={  # type: ignore[arg-type]
                "fixed": ["SOL", "AVAX"],
                "directions": ["LONG", "SHORT"],
            }
        )
        scheduler, _ = build_scheduler(settings, _make_secrets())
        assert scheduler.config.watchlist == ("SOL", "AVAX")
        assert scheduler.config.directions == ("LONG", "SHORT")


# ─── setup_logging ──────────────────────


class TestSetupLogging:
    def _restore_logger(self) -> None:
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)

    def test_creates_log_directory(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        settings = AppSettings(
            logging={  # type: ignore[arg-type]
                "log_file": str(log_dir / "bot.log"),
                "rotation_when": "midnight",
                "rotation_backup_count": 5,
                "level": "INFO",
            }
        )
        try:
            setup_logging(settings)
            assert log_dir.exists()
            root = logging.getLogger()
            assert root.level == logging.INFO
            # ハンドラが 2 つ（ファイル + stdout）
            assert len(root.handlers) == 2
        finally:
            self._restore_logger()

    def test_replaces_existing_handlers(self, tmp_path: Path) -> None:
        # 事前にダミーハンドラを差しておく
        root = logging.getLogger()
        dummy = logging.NullHandler()
        root.addHandler(dummy)

        settings = AppSettings(
            logging={  # type: ignore[arg-type]
                "log_file": str(tmp_path / "logs/bot.log"),
                "rotation_when": "midnight",
                "rotation_backup_count": 5,
                "level": "DEBUG",
            }
        )
        try:
            setup_logging(settings)
            # ダミーは消えている
            assert dummy not in root.handlers
            assert root.level == logging.DEBUG
        finally:
            self._restore_logger()

    def test_called_twice_does_not_accumulate_handlers(
        self, tmp_path: Path
    ) -> None:
        settings = AppSettings(
            logging={  # type: ignore[arg-type]
                "log_file": str(tmp_path / "logs/bot.log"),
                "rotation_when": "midnight",
                "rotation_backup_count": 5,
                "level": "INFO",
            }
        )
        try:
            setup_logging(settings)
            setup_logging(settings)
            root = logging.getLogger()
            # 2 回呼んでも 2 つだけ（既存除去 → 新規追加）
            assert len(root.handlers) == 2
        finally:
            self._restore_logger()


# ─── install_signal_handlers ────────────


class TestInstallSignalHandlers:
    def test_registers_sigint(self) -> None:
        scheduler = MagicMock()
        with patch("src.main.signal.signal") as mock_signal:
            install_signal_handlers(scheduler)
        # 少なくとも SIGINT は登録されている
        registered = [c.args[0] for c in mock_signal.call_args_list]
        assert signal.SIGINT in registered

    def test_handler_calls_request_shutdown(self) -> None:
        scheduler = MagicMock()
        captured: dict[int, object] = {}
        with patch("src.main.signal.signal") as mock_signal:
            mock_signal.side_effect = (
                lambda sig, hdlr: captured.update({sig: hdlr})
            )
            install_signal_handlers(scheduler)

        # ハンドラを直接呼んで shutdown が要求されることを確認
        handler = captured[signal.SIGINT]
        handler(signal.SIGINT, None)  # type: ignore[operator]
        scheduler.request_shutdown.assert_called_once()

    def test_sigterm_registered_when_available(self) -> None:
        if not hasattr(signal, "SIGTERM"):
            pytest.skip("SIGTERM not available on this platform")
        scheduler = MagicMock()
        with patch("src.main.signal.signal") as mock_signal:
            install_signal_handlers(scheduler)
        registered = [c.args[0] for c in mock_signal.call_args_list]
        assert signal.SIGTERM in registered

    def test_no_sigterm_branch(self) -> None:
        # SIGTERM 属性が無い環境のシミュレーション
        scheduler = MagicMock()
        with (
            patch("src.main.signal.signal") as mock_signal,
            patch("src.main.signal", autospec=False) as mock_module,
        ):
            # signal.SIGINT は本物を残しつつ SIGTERM 属性を削除
            mock_module.SIGINT = signal.SIGINT
            mock_module.signal = mock_signal
            mock_module.Signals = signal.Signals
            # hasattr(signal, "SIGTERM") が False になるよう構成
            del mock_module.SIGTERM
            install_signal_handlers(scheduler)
        # SIGINT のみ
        assert mock_signal.call_count == 1
