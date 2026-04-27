"""Notifier Protocol（章25）。

Discord 4チャンネル構成での通知抽象化。
INFRASTRUCTURE層では DiscordNotifier 実装を入れる。
将来 Telegram 等に拡張する場合もこの Protocol を実装すれば良い（章25.4）。
"""

from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    """通知プロバイダ。"""

    async def send_signal(self, message: str, dedup_key: str | None = None) -> None:
        """mt-signal: エントリー・決済・状態復元（章25.2）。"""
        ...

    async def send_alert(self, message: str, dedup_key: str | None = None) -> None:
        """mt-alert: サーキットブレーカー・障害（章25.2）。"""
        ...

    async def send_summary(self, message: str) -> None:
        """mt-summary: 日次・月次サマリー（章25.2）。"""
        ...

    async def send_error(
        self,
        message: str,
        exception: Exception | None = None,
    ) -> None:
        """mt-error: エラー・例外（章25.2）。"""
        ...
