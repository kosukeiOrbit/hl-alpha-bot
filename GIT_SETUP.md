# Git セットアップコマンド

このファイルは `hl-alpha-bot` リポジトリの初期化手順です。

## 1. リポジトリ初期化

```bash
# プロジェクトディレクトリ作成
mkdir hl-alpha-bot
cd hl-alpha-bot

# Git初期化
git init
git config user.name "kosukeiOrbit"  # 既存BOTと同じ
git config user.email "your-email@example.com"

# main ブランチに改名（古いgitだとmasterになる）
git branch -m main
```

## 2. 設計書ブランチ作成

```bash
# 開発統合ブランチ
git checkout -b develop

# 設計ブランチ
git checkout -b feature/initial-design

# ファイル配置
mkdir -p docs secrets/.gitkeep data/.gitkeep logs/.gitkeep
mv hl-alpha-bot-design.md docs/design.md

# コミット対象ファイル
# ├── docs/design.md          ← 設計書
# ├── README.md
# ├── .gitignore
# └── secrets/                ← 空ディレクトリ（後で実装時に中身追加）

git add docs/design.md
git add README.md
git add .gitignore

# ファイルなしディレクトリ用に .gitkeep を作成
touch data/.gitkeep
touch logs/.gitkeep
mkdir -p secrets
touch secrets/.gitkeep

git add data/.gitkeep logs/.gitkeep secrets/.gitkeep

# コミット
git commit -m "docs: add initial design document for hl-alpha-bot

- 26章構成・5944行の包括的設計書
- 既存BOT（auto-daytrade/moomoo-trader）の知見を統合
- HyperLiquid API実調査ベース
- TDD前提のアーキテクチャ
- 段階的ロールアウト計画（Phase 0〜4）

Co-authored-by: Claude <noreply@anthropic.com>"
```

## 3. リモートリポジトリ設定

GitHub で `hl-alpha-bot` リポジトリを作成後：

```bash
git remote add origin git@github.com:kosukeiOrbit/hl-alpha-bot.git

# main ブランチをpush
git checkout main
git push -u origin main

# develop ブランチをpush
git checkout develop
git push -u origin develop

# feature/initial-design をpush
git checkout feature/initial-design
git push -u origin feature/initial-design
```

## 4. ブランチ保護設定（GitHub）

Settings → Branches → Branch protection rules で：

### main ブランチ
- ✅ Require pull request reviews before merging
- ✅ Require status checks to pass (CI green)
- ✅ Include administrators
- ❌ Allow force pushes
- ❌ Allow deletions

### develop ブランチ
- ✅ Require pull request reviews before merging
- ❌ Allow force pushes

## 5. 次のブランチを切る

設計が確定したら、PRをマージして実装を始める：

```bash
# feature/initial-design を develop にマージ（PR経由推奨）
gh pr create --base develop --head feature/initial-design \
  --title "feat: initial design document" \
  --body "Closes #1"

# develop に切り替え
git checkout develop
git pull

# Week 1 の実装ブランチ
git checkout -b feature/core-models
```

## 6. ブランチ命名規約

```
feature/<scope>-<short-description>

例:
- feature/core-entry-judge
- feature/infra-hl-client
- feature/app-reconciliation

緊急修正:
- hotfix/<issue-number>-<short-description>

ドキュメント:
- docs/<topic>

リファクタ:
- refactor/<scope>
```

## 7. コミットメッセージ規約（Conventional Commits）

```
<type>: <subject>

<body>

<footer>
```

### type一覧

- `feat`: 新機能
- `fix`: バグ修正
- `docs`: ドキュメント
- `style`: フォーマット
- `refactor`: リファクタ
- `test`: テスト
- `chore`: ビルド・補助ツール
- `perf`: パフォーマンス改善

### 例

```
feat: implement 4-layer AND entry judgment

- core/entry_judge.py with judge_long_entry/judge_short_entry
- 100% test coverage with parametric and property-based tests
- All thresholds load from config.trading.long/short

Closes #5
```

## 8. PR テンプレート

`.github/PULL_REQUEST_TEMPLATE.md` を作成：

```markdown
## 変更内容
<!-- 何を変更したか -->

## 設計書との対応
<!-- docs/design.md のどの章を実装したか -->
- 章 X.Y: ...

## テスト
- [ ] 単体テスト追加
- [ ] カバレッジ100%（CORE層の場合）
- [ ] 統合テスト（APPLICATION層の場合）
- [ ] testnet動作確認（INFRASTRUCTURE層の場合）

## チェックリスト
- [ ] mypy strict 通過
- [ ] ruff check 通過
- [ ] pytest 緑
- [ ] 設計書（docs/design.md）の更新が必要なら反映済み
```
