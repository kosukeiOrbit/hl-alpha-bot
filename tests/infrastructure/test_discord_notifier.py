"""DiscordNotifier のテスト。

実 Webhook には接続せず、aiohttp.ClientSession.post をモックする。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from src.adapters.notifier import Notifier
from src.infrastructure.discord_notifier import (
    DiscordNotifier,
    DiscordNotifierConfig,
)


def make_config(**overrides: Any) -> DiscordNotifierConfig:
    base: dict[str, Any] = {
        "webhook_signal": "https://discord.com/api/webhooks/signal",
        "webhook_alert": "https://discord.com/api/webhooks/alert",
        "webhook_summary": "https://discord.com/api/webhooks/summary",
        "webhook_error": "https://discord.com/api/webhooks/error",
        "dedup_window_seconds": 300,
        "request_timeout_seconds": 10.0,
        "max_message_length": 1900,
    }
    base.update(overrides)
    return DiscordNotifierConfig(**base)


class _FakeResponse:
    """aiohttp.ClientSession.post の async with 戻り値モック。"""

    def __init__(
        self, status: int = 204, body: str = "", request_info: Any = None
    ) -> None:
        self.status = status
        self._body = body
        self.request_info = request_info or MagicMock()
        self.history: tuple[Any, ...] = ()

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


def _make_session(
    response: _FakeResponse | None = None,
    post_side_effect: BaseException | None = None,
) -> Any:
    """aiohttp.ClientSession を装うモック。post は context manager を返す。"""
    session = MagicMock(spec=aiohttp.ClientSession)
    if post_side_effect is not None:
        session.post = MagicMock(side_effect=post_side_effect)
    else:
        session.post = MagicMock(return_value=response or _FakeResponse())
    session.close = AsyncMock()
    return session


# ─── チャンネル別送信 ────────────────────


class TestSendChannels:
    @pytest.mark.asyncio
    async def test_signal_posts_to_signal_webhook(self) -> None:
        session = _make_session()
        notifier = DiscordNotifier(make_config(), session=session)
        await notifier.send_signal("Hello")
        url = session.post.call_args[0][0]
        body = session.post.call_args[1]["json"]
        assert url == "https://discord.com/api/webhooks/signal"
        assert "[SIGNAL]" in body["content"]
        assert "Hello" in body["content"]

    @pytest.mark.asyncio
    async def test_alert_posts_to_alert_webhook(self) -> None:
        session = _make_session()
        notifier = DiscordNotifier(make_config(), session=session)
        await notifier.send_alert("warn me")
        assert (
            session.post.call_args[0][0]
            == "https://discord.com/api/webhooks/alert"
        )
        assert "[ALERT]" in session.post.call_args[1]["json"]["content"]

    @pytest.mark.asyncio
    async def test_summary_posts_to_summary_webhook(self) -> None:
        session = _make_session()
        notifier = DiscordNotifier(make_config(), session=session)
        await notifier.send_summary("daily")
        assert (
            session.post.call_args[0][0]
            == "https://discord.com/api/webhooks/summary"
        )
        assert "[SUMMARY]" in session.post.call_args[1]["json"]["content"]

    @pytest.mark.asyncio
    async def test_error_posts_to_error_webhook(self) -> None:
        session = _make_session()
        notifier = DiscordNotifier(make_config(), session=session)
        await notifier.send_error("fatal")
        assert (
            session.post.call_args[0][0]
            == "https://discord.com/api/webhooks/error"
        )
        assert "[ERROR]" in session.post.call_args[1]["json"]["content"]


# ─── dedup_key ───────────────────────────


class TestDedup:
    @pytest.mark.asyncio
    async def test_same_key_within_window_suppressed(self) -> None:
        session = _make_session()
        notifier = DiscordNotifier(make_config(), session=session)
        await notifier.send_signal("first", dedup_key="k")
        await notifier.send_signal("second", dedup_key="k")
        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_different_keys_both_sent(self) -> None:
        session = _make_session()
        notifier = DiscordNotifier(make_config(), session=session)
        await notifier.send_signal("a", dedup_key="ka")
        await notifier.send_signal("b", dedup_key="kb")
        assert session.post.call_count == 2

    @pytest.mark.asyncio
    async def test_no_dedup_key_always_sends(self) -> None:
        session = _make_session()
        notifier = DiscordNotifier(make_config(), session=session)
        await notifier.send_signal("a")
        await notifier.send_signal("b")
        assert session.post.call_count == 2

    @pytest.mark.asyncio
    async def test_dedup_window_expires(self) -> None:
        session = _make_session()
        notifier = DiscordNotifier(
            make_config(dedup_window_seconds=1), session=session
        )
        await notifier.send_signal("a", dedup_key="k")
        await asyncio.sleep(1.1)
        await notifier.send_signal("b", dedup_key="k")
        assert session.post.call_count == 2


# ─── exception kwarg ─────────────────────


class TestExceptionKwarg:
    @pytest.mark.asyncio
    async def test_exception_appended_to_error(self) -> None:
        session = _make_session()
        notifier = DiscordNotifier(make_config(), session=session)
        try:
            raise ValueError("boom")
        except ValueError as e:
            await notifier.send_error("oops", exception=e)
        content = session.post.call_args[1]["json"]["content"]
        assert "oops" in content
        assert "ValueError" in content
        assert "boom" in content
        assert "```" in content


# ─── 失敗ハンドリング ────────────────────


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_http_500_does_not_raise(self) -> None:
        session = _make_session(
            response=_FakeResponse(status=500, body="server error")
        )
        notifier = DiscordNotifier(make_config(), session=session)
        await notifier.send_signal("test")  # 例外伝播しない

    @pytest.mark.asyncio
    async def test_timeout_does_not_raise(self) -> None:
        session = _make_session(post_side_effect=TimeoutError())
        notifier = DiscordNotifier(make_config(), session=session)
        await notifier.send_signal("test")

    @pytest.mark.asyncio
    async def test_arbitrary_exception_does_not_raise(self) -> None:
        session = _make_session(post_side_effect=RuntimeError("network"))
        notifier = DiscordNotifier(make_config(), session=session)
        await notifier.send_alert("test")


# ─── 長さ制限 ────────────────────────────


class TestMessageLength:
    @pytest.mark.asyncio
    async def test_short_message_unchanged(self) -> None:
        session = _make_session()
        notifier = DiscordNotifier(
            make_config(max_message_length=100), session=session
        )
        await notifier.send_signal("short")
        content = session.post.call_args[1]["json"]["content"]
        assert "truncated" not in content

    @pytest.mark.asyncio
    async def test_long_message_truncated(self) -> None:
        session = _make_session()
        notifier = DiscordNotifier(
            make_config(max_message_length=100), session=session
        )
        await notifier.send_signal("A" * 500)
        content = session.post.call_args[1]["json"]["content"]
        assert len(content) <= 100
        assert "truncated" in content


# ─── セッション管理 ──────────────────────


class TestSession:
    @pytest.mark.asyncio
    async def test_close_owned_session(self) -> None:
        notifier = DiscordNotifier(make_config())
        # 内部 session を 1 度作って差し替える
        fake = MagicMock(spec=aiohttp.ClientSession)
        fake.close = AsyncMock()
        notifier._session = fake
        await notifier.close()
        fake.close.assert_awaited_once()
        assert notifier._session is None

    @pytest.mark.asyncio
    async def test_close_external_session_not_touched(self) -> None:
        external = _make_session()
        notifier = DiscordNotifier(make_config(), session=external)
        await notifier.close()
        external.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_close_when_no_session_is_noop(self) -> None:
        notifier = DiscordNotifier(make_config())
        # session 未生成のまま close を呼んでも例外にならない
        await notifier.close()

    @pytest.mark.asyncio
    async def test_ensure_session_creates_when_missing(self) -> None:
        # 内部 session 未指定でも初回 send で作られる（カバレッジ用）
        notifier = DiscordNotifier(make_config())
        # post を成功させるため ClientSession を差し替えるのではなく、
        # 実 ClientSession を作って即 close する経路だけ検証
        await notifier._ensure_session()
        assert notifier._session is not None
        await notifier.close()


# ─── Protocol 準拠 ───────────────────────


class TestProtocolConformance:
    def test_satisfies_notifier_protocol(self) -> None:
        notifier: Notifier = DiscordNotifier(make_config())
        assert notifier is not None
