"""ConsoleNotifier のテスト。"""

from __future__ import annotations

import io
import logging

import pytest

from src.adapters.notifier import Notifier
from src.infrastructure.console_notifier import ConsoleNotifier


class TestStreamMode:
    """use_logging=False — 直接 stream に書く経路。"""

    @pytest.mark.asyncio
    async def test_signal_prefix(self) -> None:
        buf = io.StringIO()
        notifier = ConsoleNotifier(stream=buf, use_logging=False)
        await notifier.send_signal("Hello")
        assert "[SIGNAL] Hello" in buf.getvalue()

    @pytest.mark.asyncio
    async def test_alert_prefix(self) -> None:
        buf = io.StringIO()
        notifier = ConsoleNotifier(stream=buf, use_logging=False)
        await notifier.send_alert("warn me")
        assert "[ALERT] warn me" in buf.getvalue()

    @pytest.mark.asyncio
    async def test_summary_prefix(self) -> None:
        buf = io.StringIO()
        notifier = ConsoleNotifier(stream=buf, use_logging=False)
        await notifier.send_summary("daily")
        assert "[SUMMARY] daily" in buf.getvalue()

    @pytest.mark.asyncio
    async def test_error_prefix(self) -> None:
        buf = io.StringIO()
        notifier = ConsoleNotifier(stream=buf, use_logging=False)
        await notifier.send_error("fatal")
        assert "[ERROR] fatal" in buf.getvalue()

    @pytest.mark.asyncio
    async def test_dedup_key_accepted_and_ignored(self) -> None:
        # dedup_key を渡しても落ちずに通常出力
        buf = io.StringIO()
        notifier = ConsoleNotifier(stream=buf, use_logging=False)
        await notifier.send_signal("with dedup", dedup_key="abc")
        await notifier.send_alert("with dedup", dedup_key="def")
        assert "[SIGNAL] with dedup" in buf.getvalue()
        assert "[ALERT] with dedup" in buf.getvalue()

    @pytest.mark.asyncio
    async def test_error_with_exception_includes_traceback(self) -> None:
        buf = io.StringIO()
        notifier = ConsoleNotifier(stream=buf, use_logging=False)
        try:
            raise RuntimeError("boom")
        except RuntimeError as e:
            await notifier.send_error("oops", exception=e)
        out = buf.getvalue()
        assert "[ERROR] oops" in out
        assert "RuntimeError" in out
        assert "boom" in out

    @pytest.mark.asyncio
    async def test_default_stream_is_stdout(self) -> None:
        # stream を渡さないと sys.stdout が使われる
        notifier = ConsoleNotifier(use_logging=False)
        import sys

        assert notifier.stream is sys.stdout

    @pytest.mark.asyncio
    async def test_write_failure_does_not_raise(self) -> None:
        buf = io.StringIO()
        buf.close()
        notifier = ConsoleNotifier(stream=buf, use_logging=False)
        # 例外を投げない
        await notifier.send_signal("test")
        await notifier.send_alert("test")
        await notifier.send_summary("test")
        await notifier.send_error("test")


class TestLoggingMode:
    """use_logging=True — logger 経由（デフォルト）。"""

    @pytest.mark.asyncio
    async def test_signal_logged_as_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        notifier = ConsoleNotifier()
        with caplog.at_level(logging.INFO, logger="src.infrastructure.console_notifier"):
            await notifier.send_signal("via logger")
        assert any(
            r.levelno == logging.INFO and "[SIGNAL] via logger" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_alert_logged_as_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        notifier = ConsoleNotifier()
        with caplog.at_level(logging.WARNING, logger="src.infrastructure.console_notifier"):
            await notifier.send_alert("warn me")
        assert any(
            r.levelno == logging.WARNING and "[ALERT] warn me" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_summary_logged_as_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        notifier = ConsoleNotifier()
        with caplog.at_level(logging.INFO, logger="src.infrastructure.console_notifier"):
            await notifier.send_summary("daily")
        assert any(
            r.levelno == logging.INFO and "[SUMMARY] daily" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_error_logged_as_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        notifier = ConsoleNotifier()
        with caplog.at_level(logging.ERROR, logger="src.infrastructure.console_notifier"):
            await notifier.send_error("fatal")
        assert any(
            r.levelno == logging.ERROR and "[ERROR] fatal" in r.message
            for r in caplog.records
        )


class TestProtocolConformance:
    def test_satisfies_notifier_protocol(self) -> None:
        # 構造的サブタイピングのチェック
        notifier: Notifier = ConsoleNotifier()
        assert notifier is not None
