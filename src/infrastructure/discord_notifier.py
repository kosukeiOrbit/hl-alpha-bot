"""DiscordNotifier: Discord Webhook 経由の通知（章25）。

4 チャンネル（signal / alert / summary / error）に Webhook 経由で投稿する。
Notifier Protocol を ConsoleNotifier と同じシグネチャで満たすので、
main.py 側では URL 設定の有無で 1 行差し替えできる。

特徴:
- dedup_key で重複抑制（章 25.3）
- exception kwarg を渡すと traceback をコードブロックで添付
- 送信失敗で例外を伝播させない（logger fallback のみ）
- aiohttp で非同期 POST

実装上の注意:
- Discord 公式のレート制限は 30msg/60sec/channel。本実装は dedup_key
  ベースの抑制（同じ key を window 秒以内は無視）でアプリ側カバー。
- max_message_length は 1900（Discord 上限 2000 から余裕）
"""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscordNotifierConfig:
    """DiscordNotifier 設定。"""

    webhook_signal: str
    webhook_alert: str
    webhook_summary: str
    webhook_error: str
    dedup_window_seconds: int = 300
    request_timeout_seconds: float = 10.0
    max_message_length: int = 1900


class DiscordNotifier:
    """Discord Webhook 経由の通知実装。

    使用例::

        notifier = DiscordNotifier(config)
        await notifier.send_signal("LONG BTC entered")
        await notifier.close()  # 終了時に必ず

    Args:
        config: DiscordNotifierConfig
        session: 外部から差し込む aiohttp.ClientSession（テスト用・
            None なら内部で作成して close 時に閉じる）
    """

    def __init__(
        self,
        config: DiscordNotifierConfig,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.config = config
        self._owned_session = session is None
        self._session = session
        self._recent_sends: dict[str, float] = {}

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """内部で作った session のみ閉じる（外部 session は触らない）。"""
        if self._owned_session and self._session is not None:
            await self._session.close()
            self._session = None

    # ─── Notifier Protocol 実装 ─────────────

    async def send_signal(
        self,
        message: str,
        dedup_key: str | None = None,
    ) -> None:
        await self._send(
            self.config.webhook_signal,
            channel="SIGNAL",
            message=message,
            dedup_key=dedup_key,
            exception=None,
        )

    async def send_alert(
        self,
        message: str,
        dedup_key: str | None = None,
    ) -> None:
        await self._send(
            self.config.webhook_alert,
            channel="ALERT",
            message=message,
            dedup_key=dedup_key,
            exception=None,
        )

    async def send_summary(self, message: str) -> None:
        await self._send(
            self.config.webhook_summary,
            channel="SUMMARY",
            message=message,
            dedup_key=None,
            exception=None,
        )

    async def send_error(
        self,
        message: str,
        exception: Exception | None = None,
    ) -> None:
        await self._send(
            self.config.webhook_error,
            channel="ERROR",
            message=message,
            dedup_key=None,
            exception=exception,
        )

    # ─── 内部実装 ──────────────────────────

    async def _send(
        self,
        webhook_url: str,
        channel: str,
        message: str,
        dedup_key: str | None,
        exception: Exception | None,
    ) -> None:
        if dedup_key is not None and self._is_recently_sent(dedup_key):
            logger.debug(
                "discord notification suppressed (dedup): %s", dedup_key
            )
            return

        formatted = self._format_message(channel, message, exception)
        formatted = self._truncate(formatted)

        try:
            await self._post_webhook(webhook_url, formatted)
        except Exception:
            logger.exception(
                "discord webhook POST failed (channel=%s)", channel
            )
            logger.warning("[%s] %s", channel, formatted)

    def _is_recently_sent(self, dedup_key: str) -> bool:
        """dedup_window 以内に同じ key を送ったか。
        新規 / 期限切れなら現在時刻を記録して False を返す。"""
        now = time.time()
        last = self._recent_sends.get(dedup_key, 0.0)
        if now - last < self.config.dedup_window_seconds:
            return True
        self._recent_sends[dedup_key] = now
        return False

    @staticmethod
    def _format_message(
        channel: str, message: str, exception: Exception | None
    ) -> str:
        parts = [f"**[{channel}]** {message}"]
        if exception is not None:
            tb = "".join(
                traceback.format_exception(
                    type(exception), exception, exception.__traceback__
                )
            )
            parts.append(f"```\n{tb}\n```")
        return "\n".join(parts)

    def _truncate(self, content: str) -> str:
        limit = self.config.max_message_length
        if len(content) <= limit:
            return content
        suffix = "\n... (truncated)"
        return content[: limit - len(suffix)] + suffix

    async def _post_webhook(self, url: str, content: str) -> None:
        session = await self._ensure_session()
        timeout = aiohttp.ClientTimeout(
            total=self.config.request_timeout_seconds
        )
        async with session.post(
            url, json={"content": content}, timeout=timeout
        ) as response:
            if response.status >= 400:
                body = await response.text()
                raise aiohttp.ClientResponseError(
                    request_info=response.request_info,
                    history=response.history,
                    status=response.status,
                    message=f"webhook returned {response.status}: {body}",
                )
