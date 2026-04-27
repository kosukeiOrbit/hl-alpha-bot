# hl-alpha-bot

HyperLiquid上で動作する仮想通貨デイトレードBOT。

既存のBOT資産（[auto-daytrade](https://github.com/kosukeiOrbit/auto-daytrade)・[moomoo-trader](https://github.com/kosukeiOrbit/moomoo-trader)）の実証データに基づき、HyperLiquid特有の優位性（Agent Wallet・オンチェーン板情報・Maker-First執行）を活用する。

## 設計書

詳細な設計は [docs/design.md](./docs/design.md) を参照。

### 主要章

| 章 | 内容 |
|---|---|
| 1-3 | 全体像・既存BOT知見・HL選定理由 |
| 4-7 | コアロジック（4層AND・価格基準・VWAP・SENTIMENT） |
| 8-10 | 運用基盤（DB・障害対応・資金管理） |
| 11-15 | 実装方針（TDD・ウォッチリスト・リスク管理・執行・ロールアウト） |
| 22-23 | HL API仕様・設定管理 |
| 24-26 | バックテスト・通知・ロギング |

## 実装ステータス

🔵 **Phase 0**: データ収集（未着手）
⚪ **Phase 1**: ドライラン（未着手）
⚪ **Phase 2**: 最小実弾（未着手）
⚪ **Phase 3**: フルサイズLONG（未着手）
⚪ **Phase 4**: フル運用（未着手）

## アーキテクチャ

```
src/
├── core/          # 純粋ロジック（100%テスト）
├── adapters/      # Protocol定義
├── infrastructure/# 実装（HL/Claude/SQLite/Discord）
├── application/   # ユースケース層
└── main.py        # エントリーポイント
```

詳細は設計書 章11参照。

## 開発

```bash
# 環境構築
poetry install

# テスト
pytest

# リント
ruff check
mypy src/

# 設定検証
python scripts/validate_config.py

# バックテスト
python scripts/backtest.py --symbols BTC,ETH --start 2026-03-01

# BOT起動
HL_PROFILE=dev python -m src.main
```

## ライセンス

Private repository.
