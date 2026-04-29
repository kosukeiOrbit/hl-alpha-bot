"""ConsoleNotifier: 全通知を logger / 標準出力に流す Phase 0 用実装。

Discord / Slack 等への通知の代わりに、
[SIGNAL] / [ALERT] / [SUMMARY] / [ERROR] の 4 チャンネルプレフィックス付き
メッセージとして logger（または直接 stream）に出す。

Phase 0 の動作確認や CI 環境ではこれで十分。
PR7.5d で DiscordNotifier に差し替える想定。

設計上の注意:
- ANSI エスケープによる色付けは Windows ターミナルで化けるリスクがあるので
  プレフィックスのみで識別。
- write 失敗時は logger.exception するだけで例外は伝播させない
  （メインループを通知障害で落とさない）。
"""

from __future__ import annotations

import logging
import sys
import traceback
from typing import TextIO

logger = logging.getLogger(__name__)


class ConsoleNotifier:
    """標準出力ベースの Notifier 実装。

    Args:
        stream: 出力先（デフォルト sys.stdout）。テスト時に StringIO を渡す。
        use_logging: True なら logger 経由（main.py で集約管理可能）、
            False なら stream に直接書き込み（テスト用）。
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        use_logging: bool = True,
    ) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.use_logging = use_logging

    async def send_signal(
        self, message: str, dedup_key: str | None = None
    ) -> None:
        """[SIGNAL] チャンネル: エントリー / 決済 / 状態復元（章25.2）。

        dedup_key は Console では実質無視（受け取って捨てる）。
        DiscordNotifier 等では同 key の連続送信を抑制する想定。
        """
        del dedup_key
        await self._write("SIGNAL", message, level=logging.INFO)

    async def send_alert(
        self, message: str, dedup_key: str | None = None
    ) -> None:
        """[ALERT] チャンネル: サーキットブレーカー / 障害（章25.2）。"""
        del dedup_key
        await self._write("ALERT", message, level=logging.WARNING)

    async def send_summary(self, message: str) -> None:
        """[SUMMARY] チャンネル: 日次・月次サマリー（章25.2）。"""
        await self._write("SUMMARY", message, level=logging.INFO)

    async def send_error(
        self,
        message: str,
        exception: Exception | None = None,
    ) -> None:
        """[ERROR] チャンネル: エラー・例外（章25.2）。

        exception が渡されたら traceback を末尾に付与する。
        """
        if exception is not None:
            tb = "".join(
                traceback.format_exception(
                    type(exception), exception, exception.__traceback__
                )
            )
            full = f"{message}\n{tb}"
        else:
            full = message
        await self._write("ERROR", full, level=logging.ERROR)

    # ─── 内部 ───────────────────────────────

    async def _write(self, channel: str, message: str, level: int) -> None:
        """共通の書き出しロジック（例外を握りつぶす）。"""
        formatted = f"[{channel}] {message}"
        if self.use_logging:
            logger.log(level, formatted)
            return
        try:
            print(formatted, file=self.stream, flush=True)
        except Exception:
            logger.exception("ConsoleNotifier write failed")
