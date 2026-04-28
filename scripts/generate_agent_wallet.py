"""Agent Wallet の秘密鍵生成スクリプト（章10.3）。

HyperLiquid の Agent Wallet（API Wallet）として使う EOA を新規作成する。

使い方:
    python scripts/generate_agent_wallet.py

出力:
    - Agent Wallet Address（公開してOK）
    - Agent Private Key（絶対秘密！）

セキュリティ原則:
- 出力は標準出力にのみ表示。ファイルには書き出さない
- 生成された秘密鍵は手動でパスワードマネージャー等に保管すること
- スクリプト本体（ロジック）はコミット可。実行結果は絶対コミットしない
"""

from __future__ import annotations

import secrets

from eth_account import Account


def generate_agent_wallet() -> tuple[str, str]:
    """新規 Agent Wallet の秘密鍵とアドレスを生成（純関数）。

    Returns:
        (address, private_key) のタプル。
        address: チェックサム付きの 0x プレフィックス42文字。
        private_key: 0x プレフィックス66文字。
    """
    # 暗号学的に安全な乱数で 32 bytes の秘密鍵を生成
    private_key = "0x" + secrets.token_hex(32)
    account = Account.from_key(private_key)
    return account.address, private_key


def main() -> None:
    address, private_key = generate_agent_wallet()

    print("=" * 70)
    print("  HyperLiquid Agent Wallet 生成完了")
    print("=" * 70)
    print()
    print(f"  Address      : {address}")
    print(f"  Private Key  : {private_key}")
    print()
    print("=" * 70)
    print()
    print("⚠️  この秘密鍵は一度しか表示されません。")
    print("⚠️  以下を必ず実行してください：")
    print()
    print("   1. 上記の Private Key をパスワードマネージャーに保存")
    print("      (1Password / Bitwarden / KeePass 等)")
    print()
    print("   2. ターミナルの履歴・スクロールバッファをクリア")
    print("      bash:       history -c && clear")
    print("      PowerShell: Clear-History; Clear-Host")
    print()
    print("   3. このターミナルのウィンドウを閉じる")
    print()
    print("⚠️  絶対にやってはいけないこと：")
    print("   - 秘密鍵をファイルに保存（一時的でも禁止）")
    print("   - 秘密鍵を git add・commit・push する")
    print("   - 秘密鍵を Slack / Discord / メール等で送信する")
    print("   - 秘密鍵をスクショに含める")
    print()


if __name__ == "__main__":
    main()
