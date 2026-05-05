# hl-alpha-bot 設計案（HyperLiquid仮想通貨デイトレBOT）

**作成日：2026-04-27**
**最終更新：2026-04-27（章構成全面リファクタ）**
**ベース：** auto-daytrade（日本株）+ moomoo-trader（米株）の知見統合
**運用先：** HyperLiquid（オンチェーンPerp DEX）
**言語：** Python 3.13
**環境：** VPS常駐運用（24時間365日）

---

## 目次

### Part 1: 全体像
- [1. 既存BOTからの抽出（冷徹な評価）](#1-既存botからの抽出冷徹な評価)
- [2. HyperLiquidを選ぶ意味](#2-hyperliquidを選ぶ意味使い切るべき優位性)
- [3. システムアーキテクチャ](#3-システムアーキテクチャ)

### Part 2: コアロジック設計
- [4. エントリーロジック：4層AND条件](#4-エントリーロジック4層and条件)
- [5. 価格基準・過熱フィルター](#5-価格基準過熱フィルターの定義)
- [6. VWAP計算と活用](#6-vwap計算方式と活用設計)
- [7. SENTIMENT判定詳細](#7-sentiment判定の詳細仕様sentiment_analyzerpy)

### Part 3: 運用基盤設計
- [8. データロギング設計（DB主体）](#8-データロギング設計db主体--csvエクスポート)
- [9. 障害時の振る舞い](#9-障害時の振る舞い設計)
- [10. アカウント・資金管理](#10-アカウント資金管理設計)

### Part 4: 実装方針
- [11. TDDアーキテクチャ](#11-tddアーキテクチャと実装方針)
- [12. ウォッチリスト戦略](#12-ウォッチリスト戦略)
- [13. ポジション・リスク管理](#13-ポジションリスク管理)
- [14. 執行戦略：Maker-First](#14-執行戦略maker-first)
- [15. 段階的ロールアウト計画](#15-段階的ロールアウト計画)

### Part 5: 補足情報
- [16. 技術スタック](#16-技術スタック)
- [17. 月次想定コスト](#17-月次想定コスト)
- [18. 既存BOTからの流用率](#18-既存botからの流用率)
- [19. 主要フロー（1ループ）](#19-主要フロー1ループ10秒間隔)
- [20. 「限界を超えた」追加提案](#20-限界を超えた追加提案)
- [21. Claude Code向け実装メモ](#21-claude-code向け実装メモ)
- [22. HyperLiquid API仕様（実調査ベース）](#22-hyperliquid-api仕様実調査ベース)
- [23. 設定管理設計](#23-設定管理設計)
- [24. バックテスト基盤](#24-バックテスト基盤)
- [25. 通知設計（Discord 4チャンネル統合）](#25-通知設計discord-4チャンネル統合)
- [26. ロギング・メトリクス設計](#26-ロギングメトリクス設計)

---

## 1. 既存BOTからの抽出（冷徹な評価）

### auto-daytrade（日本株）から学ぶこと

**勝ったロジック（持ち込む）：**
- 気配値GAPフィルター（GAP 1〜2%のみ通す → 3W0L +23,600円）
- 保有時間120分以上の勝率87.5%（早すぎる損切りがいかに悪かったか）
- 仮想モード→実弾の段階的検証フロー
- ブラックリスト機構（損切り後の同銘柄当日再エントリー禁止）
- Discord通知の3チャンネル分離
- **パターンBエントリー判定の進化（重要実証データ）：**
  - 旧：「直近5本中3本上昇」のみ → 勝率低
  - 新：VWAP乖離1%以内 + 始値+3%以内 + 5本前比+0.3%以上 + 出来高急増 → 勝率改善
  - **教訓：単純な上昇本数カウントではノイズを拾う。「位置情報（VWAP基準）」+「モメンタム」+「過熱回避」の組み合わせが効く**

**負けたロジック（持ち込まない）：**
- 寄り付き成行エントリー → スリッページで-2〜3%損切り常態化
- 損切り-1%固定 → ノイズで刈られすぎ（MFE +0.17%が示す「入った瞬間下がる」パターン）
- パターンA全体PF 0.44 → 朝の寄り付き勝負は構造的に不利
- **「直近N本のうちM本上昇」のみによる単純モメンタム判定** → ノイズで負ける

### moomoo-trader（米株）から学ぶこと

**勝ったロジック（持ち込む）：**
- **flow（大口） × sentiment（Claude）のAND二重ロック** ← これが本質
- ATRベースSL/TP（ボラに応じて伸縮）
- 固定額ポジション（$600）でリスク均一化
- センチメント0.70台が最強（0.80+はむしろ負ける ← 過剰反応の罠）
- 動的スクリーニング × 固定ウォッチリストの併用
- flow=BUY時のみClaude API呼び出し（API節約）

**未解決の課題（持ち込んで解決すべき）：**
- 強制決済率92% / TP到達率6% ← TP/SL設計が現状維持バイアス
- VWAPが記録のみで未活用
- bid/askスプレッド未チェック

---

## 2. HyperLiquidを選ぶ意味（使い切るべき優位性）

HyperLiquidは他のCEX/DEXにない以下の特性があり、これがBOT設計の出発点になる。

### a) オンチェーン板情報がフルに公開
全注文・全約定・全清算がリアルタイムで取得可能。CEXでは「大口の動き」を推測するしかないが、HyperLiquidなら**実際に見える**。これはmoomooの`get_capital_flow()`の上位互換。

### b) 清算データがリアルタイム取得可能
他人のロスカット価格帯を事前に把握できる → 「清算カスケード」を予測してエントリー/逆張りができる。これは株では絶対にできない。

### c) Funding Rate が1時間ごとに精算
HyperLiquidは8時間レートを1時間ごとに分割（1/8）支払い。極端に偏ったFundingは「ポジション一方向集中」のシグナル。逆張りエッジになる。

### d) Maker手数料が低い（条件付きリベートあり）
Maker基本0.015%・Taker 0.045%（perps Tier 0）。Maker volume share ≥0.5%でリベート（-0.001%）に転じる。寄り付き成行で死んだauto-daytradeの教訓を活かし、原則すべて指値（Post-Only/ALO）運用にする。

### e) 24/7取引・板薄時間がある
逆に言うと「板が薄い時間帯」を避ける/狙う設計が必要。

---

## 3. システムアーキテクチャ

```
┌──────────────────────────────────────────────────┐
│  DATA LAYER                                      │
│  hyperliquid_client.py  ← WebSocket常時接続       │
│  liquidation_feed.py    ← 清算ストリーム          │
│  funding_monitor.py     ← Funding Rate 1h監視     │
│  orderbook_analyzer.py  ← 板の偏り検出（Imbalance）│
│  news_feed.py           ← Crypto系RSS+CT scrape   │
├──────────────────────────────────────────────────┤
│  SIGNAL ENGINE（4層フィルター）                    │
│  ① flow_detector.py     ← 大口買いフロー検出       │
│  ② sentiment_analyzer   ← Claude API（節約呼び）   │
│  ③ liquidation_pred     ← 清算カスケード予測       │
│  ④ regime_filter        ← BTCトレンド/ボラ環境判定 │
│  → AND条件で統合（and_filter.py）                  │
├──────────────────────────────────────────────────┤
│  RISK MANAGER                                    │
│  position_sizer.py      ← Kelly基準ベース          │
│  dynamic_stop.py        ← ATR×ボラ調整型SL        │
│  circuit_breaker.py     ← 日次/週次/連敗トリガー   │
│  funding_aware.py       ← Funding支払い前の手仕舞い│
├──────────────────────────────────────────────────┤
│  EXECUTION ENGINE                                │
│  maker_first_router.py  ← Post-Only指値優先       │
│  iceberg_executor.py    ← 大口注文の分割執行       │
├──────────────────────────────────────────────────┤
│  DASHBOARD                                       │
│  pnl_tracker / discord_notifier / web_dashboard  │
└──────────────────────────────────────────────────┘
```

---

## 4. エントリーロジック：4層AND条件

moomooの「flow × sentiment」を、暗号資産では4層に拡張する。各層でフィルタすることで、ノイズエントリーを徹底的に排除。

### 重要な実証知見（auto-daytrade パターンBの教訓）

**【失敗パターン】**「直近5本中3本上昇」だけで判定 → 勝率低い
- **理由：** 単純な「上昇本数」はノイズで上がっている動きも拾ってしまう
- **問題：** 価格の「位置」を見ていないため、過熱した高値圏でもエントリーする

**【成功パターン（現在運用中）】**位置情報 + モメンタムの二段階判定 → 勝率改善
```
条件1：現在値 > VWAP（ただしVWAPから+1%以内）  ← 位置情報
条件2：現在値が始値から+3%以内              ← 過熱回避
条件3：直近5本でモメンタムあり（5本前比+0.3%以上） ← モメンタム
条件4：出来高急増                           ← 関心度
```

**設計原則：**
1. 「価格が今どこにいるか（VWAP基準）」+ 「直近の動きの方向と強さ」の組み合わせが効く
2. VWAPから乖離しすぎた銘柄は追わない（既に動いた後＝負けやすい）
3. 始値からの上昇率に上限を設ける（過熱回避）

これをHyperLiquid BOTのLONG/SHORT両方の判定ロジックに移植する。

---

### LONGエントリー条件（4層AND）

```
① MOMENTUM + POSITION（auto-daytrade教訓を移植）
   現在価格 > VWAP（ただしVWAPから+0.5%以内）
   AND 現在価格 < 当日始値 × 1.05（暗号資産は変動大なので+5%まで許容）
   AND 直近5本（5分足）でモメンタムあり（5本前比 +0.3%以上）

② FLOW（HyperLiquid大口検出）★要WS trades実装
   直近5分の買い約定額 / 売り約定額 > 1.5（flow_buy_sell_ratio）
   AND 大口約定（>$50k）が買い優勢（flow_large_order_count > 0）
   AND 出来高が直近20本平均の1.5倍以上（volume_surge_ratio > 1.5）

   【重要】このシグナルは WebSocket trades チャンネルからの
   約定ストリーム集計が必要。REST API では取得不可。
   詳細は章11.6.3「FLOWシグナルの実装方針」を参照。

③ SENTIMENT（Claude API）
   Claude APIスコア > 0.6 AND confidence > 0.7
   ※flow=BUYかつ① momentumクリア時のみ呼ぶ（API節約）
   ※将来的に過熱除外（0.85+）の導入をPhase 2以降で検証する

④ REGIME（マクロ環境フィルター）
   BTCの15分EMA20 > EMA50（上昇トレンド）
   AND BTC ATR%が極端に高くない（ボラ崩壊回避）
   AND Funding Rate が中立〜やや低（< 0.01%/8h相当・実精算は1h単位）
   AND OI（建玉）の急変なし（過去1hで±10%以下）
   　→ OI履歴は APPLICATION 層で Repository 経由保持・章13.5参照
```

**注意：** 当初設計の「清算クラスター予測」は、HyperLiquid公式APIで他人の清算データを直接取得できないため**断念**。代替として上記のFunding Rate + OI変動でレジーム判定する。詳細は章13.5の「清算データの扱いについて」を参照。

### SHORTエントリー条件（4層AND）

HyperLiquidなら最初から本番可能（信用口座申請不要）。LONGと対称。

```
① MOMENTUM + POSITION
   現在価格 < VWAP（ただしVWAPから-0.5%以内）
   AND 現在価格 > 当日始値 × 0.95
   AND 直近5本（5分足）で下降モメンタムあり（5本前比 -0.3%以下）

② FLOW
   売り約定優勢 + 大口売り（>$50k）
   AND 出来高が直近20本平均の1.5倍以上

③ SENTIMENT
   スコア < -0.3 AND confidence > 0.7
   ※将来的に下限除外（-0.85以下）の導入をPhase 2以降で検証する

④ REGIME（マクロ環境フィルター）
   BTC下降 OR Funding > 0.03%/8h相当（買い過熱）
   AND OI急増なし（過熱）
```

moomooで信用口座待ちで`SHORT_DRY_RUN`していたのが嘘のように、**HyperLiquidなら初日からSHORT本番運用可能**。これがDEXパーペチュアル最大の優位。

---

### 判定の評価順序（コスト最適化）

API呼び出しコスト・計算コストを考慮した推奨順序：

```
1. ① MOMENTUM + POSITION  （ローカル計算・最速）
   ↓ クリア
2. ② FLOW                 （HL APIのみ・速い）
   ↓ クリア
3. ④ REGIME + LIQUIDATION （HL APIのみ・中速）
   ↓ クリア
4. ③ SENTIMENT            （Claude API・遅い・有料）★最後
```

この順序により、Claude API呼び出し回数を**90%以上削減**できる見込み（moomooの「flow=BUY時のみ呼ぶ」の発展形）。

---

## 5. 価格基準・過熱フィルターの定義

章4の①MOMENTUM + POSITIONで使う「始値」「24h変化率」「過熱判定」の正確な定義。
仮想通貨は24時間取引のため「始値」概念が曖昧であり、明確に定義しないと実装でバグる。

### 5.1 株 vs 仮想通貨の根本的違い

| 項目 | 株（auto-daytrade） | 仮想通貨（HyperLiquid） |
|---|---|---|
| 寄り付き | 9:00で明確 | 存在しない（24/7） |
| 1日の境界 | 9:00〜15:30 | 設計者が決める必要あり |
| 始値の絶対性 | 全員共通 | 任意性が残る |
| 「+3%」の意味 | 全員同じ値 | 基準次第で異なる |

### 5.2 採用する3重チェック方式

「始値」を1つに絞らず、**3つの異なる時間軸基準を組み合わせて過熱を判定する**。

```
基準A: UTC 00:00時点の価格（utc_open_price）       ← 「当日始値」
基準B: 24時間前の価格（rolling_24h_open）          ← ローリング基準
基準C: 24時間レンジ内の現在位置（0.0〜1.0）        ← レンジ位置
```

**なぜ3重か：**
- 基準Aだけ：UTC 00:00直後はほぼゼロ%で常時通過してしまう
- 基準Bだけ：緩やかな上昇トレンドを検出しにくい
- 基準Cだけ：絶対値の動きを見落とす
- → 3つを組み合わせることで「位置」「直近変化」「レンジ感」を立体的に把握

### 5.3 PriceContextデータ構造

```python
from dataclasses import dataclass

@dataclass
class PriceContext:
    """銘柄ごとの価格基準コンテキスト"""
    symbol: str
    current_price: float            # 現在値
    utc_open_price: float           # 直近UTC 00:00時点の価格
    rolling_24h_open: float         # 24時間前の価格（HL APIのprevDayPx）
    high_24h: float                 # 24時間高値
    low_24h: float                  # 24時間安値
    timestamp: datetime             # コンテキスト生成時刻

    @property
    def utc_day_change_pct(self) -> float:
        """UTC基準・当日始値からの変化率（基準A）"""
        return (self.current_price - self.utc_open_price) / self.utc_open_price

    @property
    def rolling_24h_change_pct(self) -> float:
        """ローリング24h変化率（基準B）"""
        return (self.current_price - self.rolling_24h_open) / self.rolling_24h_open

    @property
    def position_in_24h_range(self) -> float:
        """24h高安レンジ内の位置 0.0=安値・1.0=高値（基準C）"""
        if self.high_24h == self.low_24h:
            return 0.5
        return (self.current_price - self.low_24h) / (self.high_24h - self.low_24h)
```

### 5.4 過熱フィルターのロジック

```python
def is_not_overheated_long(ctx: PriceContext) -> bool:
    """LONG過熱フィルター：3重チェック全クリアでTrue"""
    return (
        ctx.utc_day_change_pct < 0.05          # 基準A: UTC始値+5%以内
        and ctx.rolling_24h_change_pct < 0.10  # 基準B: 24h前から+10%以内
        and ctx.position_in_24h_range < 0.85   # 基準C: 24h高値圏(85%以上)でない
    )

def is_not_overheated_short(ctx: PriceContext) -> bool:
    """SHORT過熱フィルター（下落の追随を回避）"""
    return (
        ctx.utc_day_change_pct > -0.05         # UTC始値-5%以内
        and ctx.rolling_24h_change_pct > -0.10 # 24h前から-10%以内
        and ctx.position_in_24h_range > 0.15   # 24h安値圏(15%以下)でない
    )
```

### 5.5 株BOTからの数値変更理由

| 項目 | auto-daytrade | hl-alpha-bot | 理由 |
|---|---|---|---|
| 始値からの上限 | +3% | +5% | 仮想通貨はボラ3〜5倍 |
| 24h変化率上限 | （概念なし） | +10% | 株は1日の値幅が小さい |
| レンジ位置上限 | （概念なし） | 0.85 | 高値追いを防ぐ |
| VWAP乖離 | +1%以内 | +0.5%以内 | 流動性高い分タイト |

仮想通貨のボラ補正で「+3% → +5%」と緩めているが、24h変化率＋レンジ位置の追加チェックで全体としては**より厳しい**フィルターになっている。

### 5.6 utc_open_priceの取得・記録方法

HyperLiquid APIは`prevDayPx`（24h前価格）は返すが、UTC 00:00価格は直接取得できない。
**3層フォールバック方式で取得する**：

#### 取得方法の優先順位

```
優先1: candleSnapshot 1h 足の「UTC 00:00 を含む最初のローソクの open」
         → 最も簡単で堅牢
         → 例: today_utc = 2026-04-27 のとき、
              start=2026-04-27T00:00:00Z, end=2026-04-27T01:00:00Z
              で candle_snapshot を呼ぶ → response[0]["o"]

優先2: 自前スナップショット（utc_open_prices テーブル）
         → 起動中に UTC 00:00 + 5秒で記録した値
         → BOT が UTC 00:00 を跨いで稼働している場合に有効

優先3: prevDayPx で代用（最終手段）
         → ログに警告を出して継続
```

#### 採用方法（PR6.2 実装）

INFRASTRUCTURE層の `_get_utc_day_open_price()` は **優先1** を採用：

```python
# src/infrastructure/hyperliquid_client.py
async def _get_utc_day_open_price(self, symbol: str) -> Decimal:
    """当日 UTC 00:00 の始値を1h足から取得"""
    now_utc = datetime.now(timezone.utc)
    utc_midnight = datetime(
        now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc,
    )
    utc_midnight_ms = int(utc_midnight.timestamp() * 1000)
    end_ms = utc_midnight_ms + 3_600_000  # +1h

    response = await asyncio.to_thread(
        self.info.candles_snapshot,
        symbol, "1h", utc_midnight_ms, end_ms,
    )
    if not response:
        raise ExchangeError(f"No UTC 00:00 candle for {symbol}")
    return Decimal(str(response[0]["o"]))
```

#### 自前スナップショット（オプション・優先2 用）

長期的なフォールバックとして、UTC 00:00直後にスナップショットを記録：

```python
# scripts/snapshot_utc_open.py
async def snapshot_utc_open():
    """UTC 00:00 + 5秒に全銘柄の価格を記録"""
    while True:
        now = datetime.now(timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=5, microsecond=0
        )
        wait_seconds = (next_midnight - now).total_seconds()
        await asyncio.sleep(wait_seconds)

        prices = fetch_all_mark_prices()
        save_to_db(
            table="utc_open_prices",
            date=next_midnight.date(),
            prices=prices
        )
        notify_discord("📅 UTC 00:00 価格スナップショット完了")
```

**SQLiteテーブル：**
```sql
CREATE TABLE utc_open_prices (
    date DATE,
    symbol TEXT,
    open_price REAL,
    snapshot_at DATETIME,
    PRIMARY KEY (date, symbol)
);
```

**起動時の grace period（章9.8 DataReadinessGate）：**
- UTC 00:00 直後の数分は 1h 足が未生成 → `_get_utc_day_open_price` が失敗する
- DataReadinessGate で UTC 00:00 + 10分 まで grace period を設ける
- それでも失敗したら `prevDayPx` で代用（優先3）+ ログ警告

### 5.7 HyperLiquid APIでの取得方法

```python
def fetch_market_context(symbol: str) -> PriceContext:
    """HL APIから1銘柄の価格コンテキストを構築"""
    response = requests.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "metaAndAssetCtxs"}
    )
    universe, asset_ctxs = response.json()

    # 該当銘柄のctxを取得
    idx = next(i for i, a in enumerate(universe["universe"])
               if a["name"] == symbol)
    ctx = asset_ctxs[idx]

    # UTC 00:00価格はDBから取得
    today_utc = datetime.now(timezone.utc).date()
    utc_open = get_utc_open_from_db(symbol, today_utc)

    # 24h高安は1m足から計算
    candles = fetch_1m_candles(symbol, since=now - timedelta(hours=24))
    high_24h = max(c["high"] for c in candles)
    low_24h = min(c["low"] for c in candles)

    return PriceContext(
        symbol=symbol,
        current_price=float(ctx["markPx"]),
        utc_open_price=utc_open,
        rolling_24h_open=float(ctx["prevDayPx"]),
        high_24h=high_24h,
        low_24h=low_24h,
        timestamp=datetime.now(timezone.utc)
    )
```

### 5.8 章4のエントリー条件への適用（更新版）

章4の①MOMENTUM + POSITION条件は、PriceContextを使って以下に書き換える：

```
【LONG ① MOMENTUM + POSITION】
   現在価格 > VWAP（VWAPから+0.5%以内）
   AND is_not_overheated_long(ctx) == True
       ※ UTC始値+5%以内 AND 24h+10%以内 AND レンジ位置<0.85
   AND 直近5本（5分足）でモメンタムあり（5本前比 +0.3%以上）

【SHORT ① MOMENTUM + POSITION】
   現在価格 < VWAP（VWAPから-0.5%以内）
   AND is_not_overheated_short(ctx) == True
       ※ UTC始値-5%以内 AND 24h-10%以内 AND レンジ位置>0.15
   AND 直近5本（5分足）で下降モメンタムあり（5本前比 -0.3%以下）
```

### 5.9 ロギング（チューニング用データ蓄積）

エントリー判定時、3基準の値を必ず記録：

```sql
ALTER TABLE entry_logs ADD COLUMN utc_day_change_pct REAL;
ALTER TABLE entry_logs ADD COLUMN rolling_24h_change_pct REAL;
ALTER TABLE entry_logs ADD COLUMN position_in_24h_range REAL;
```

これにより後日「UTC始値+3%超のエントリーは負けやすい」等のチューニングが可能。
auto-daytradeでGAPフィルター閾値を後から+2%に決定できたのと同じ手法。

---

## 6. VWAP計算方式と活用設計

moomoo-traderの未解決課題「VWAPは記録のみで未活用」を解消するため、
仮想通貨BOTでは**計算方式を明確化し、エントリー条件・損切りロジック・分析の3用途で活用する**。

### 6.1 VWAPの選択肢

仮想通貨は24時間取引のため、VWAPの「リセットタイミング」を設計者が決める必要がある。
3種類の選択肢がある：

| 種別 | 計算範囲 | 特徴 | 採用 |
|---|---|---|---|
| **当日VWAP** | UTC 00:00からの累積 | 章5の「UTC始値」と整合 | ✅ メイン採用 |
| **24h ローリングVWAP** | 直近24時間累積 | 連続的・ノイズ少 | △ 補助指標 |
| **セッションVWAP** | 直近のレンジから | 短期向き・実装複雑 | ❌ 不採用 |

**採用理由：**
- 章5の3基準（UTC始値・24h変化率・レンジ位置）と時間軸が揃う
- 株BOT踏襲で実装メンタルモデルを共有できる
- moomooの`turnover / volume`計算ロジックがそのまま流用可能

### 6.2 VWAP計算式

```python
def calculate_vwap(symbol: str) -> float:
    """当日VWAP（UTC 00:00からの累積）を計算

    HyperLiquid APIから取得できる以下を使う：
    - dayNtlVlm: 当日累積取引代金（USD）
    - dayBaseVlm: 当日累積取引数量（コイン）

    VWAP = 累積取引代金 / 累積取引数量
    """
    ctx = fetch_asset_ctx(symbol)
    day_volume_usd = float(ctx["dayNtlVlm"])
    day_volume_base = float(ctx["dayBaseVlm"])

    if day_volume_base == 0:
        return float(ctx["markPx"])  # フォールバック

    return day_volume_usd / day_volume_base
```

**精度検証：** moomoo-traderで`vwap_approx = turnover / volume`の精度は確認済み
（last_price $273.43 vs vwap $273.56・差異0.05%）。同じ式で問題なく動く。

### 6.3 エントリー条件での使い方（章4再掲）

```python
# LONG
vwap = calculate_vwap(symbol)
distance_pct = (current_price - vwap) / vwap * 100

if 0 < distance_pct < 0.5:           # VWAPから+0.5%以内
    layer1_position_pass = True
else:
    layer1_position_pass = False
    # signalsに記録：rejection_reason='VWAP_TOO_FAR' or 'BELOW_VWAP'

# SHORT
if -0.5 < distance_pct < 0:          # VWAPから-0.5%以内
    layer1_position_pass = True
```

### 6.4 保有中VWAP挙動の追跡（重要）

moomooの教訓「VWAP記録のみで未活用」を解決するため、
**保有中もVWAPを継続的に観察し、tradesテーブルに集計記録する**。

章11のTDD原則に従い、**純関数で実装**する。状態は不変データクラスで保持し、
更新は新インスタンスを返す関数で行う。

```python
# src/core/vwap.py
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class VWAPState:
    """保有中VWAP挙動の状態（不変）"""
    cross_count: int = 0
    above_seconds: int = 0
    below_seconds: int = 0
    min_distance_pct: float = float('inf')
    max_distance_pct: float = float('-inf')
    last_above: bool | None = None


def update_vwap_state(
    state: VWAPState,
    current_price: float,
    vwap: float,
    elapsed_sec: int,
) -> VWAPState:
    """純関数：新しい状態を返す（元のstateは変更しない）

    テストはinput→outputの組み合わせで網羅すれば良い。
    モック不要・依存なし。
    """
    distance_pct = (current_price - vwap) / vwap * 100
    is_above = current_price > vwap

    # クロス検出
    new_cross_count = state.cross_count
    if state.last_above is not None and is_above != state.last_above:
        new_cross_count += 1

    # 累積時間
    new_above = state.above_seconds + (elapsed_sec if is_above else 0)
    new_below = state.below_seconds + (0 if is_above else elapsed_sec)

    return replace(
        state,
        cross_count=new_cross_count,
        above_seconds=new_above,
        below_seconds=new_below,
        min_distance_pct=min(state.min_distance_pct, distance_pct),
        max_distance_pct=max(state.max_distance_pct, distance_pct),
        last_above=is_above,
    )


def vwap_state_to_record(state: VWAPState) -> dict:
    """tradesテーブル保存用のレコード形式に変換（純関数）"""
    total = state.above_seconds + state.below_seconds
    return {
        "vwap_cross_count": state.cross_count,
        "vwap_held_above_pct": state.above_seconds / total if total > 0 else 0,
        "min_vwap_distance_pct": (
            state.min_distance_pct
            if state.min_distance_pct != float('inf') else None
        ),
        "max_vwap_distance_pct": (
            state.max_distance_pct
            if state.max_distance_pct != float('-inf') else None
        ),
    }
```

**APPLICATION層での使い方：**

```python
# src/application/position_monitor.py
class PositionMonitor:
    """ポジション監視のユースケース層

    VWAPStateの保持はここで行うが、
    更新ロジックは純関数update_vwap_state()に委譲する。
    """
    def __init__(self, exchange, repo):
        self.exchange = exchange
        self.repo = repo
        # trade_id → VWAPState のマッピング
        self.vwap_states: dict[int, VWAPState] = {}

    async def tick(self, trade_id: int) -> None:
        """3秒ごと等の周期で呼ばれる"""
        snapshot = await self.exchange.get_market_snapshot(...)
        current_state = self.vwap_states.get(trade_id, VWAPState())

        # 純関数で新状態を計算
        new_state = update_vwap_state(
            current_state,
            current_price=snapshot.price,
            vwap=snapshot.vwap,
            elapsed_sec=3,
        )
        self.vwap_states[trade_id] = new_state

    async def on_close(self, trade_id: int) -> None:
        """ポジションクローズ時"""
        state = self.vwap_states.pop(trade_id, None)
        if state:
            record = vwap_state_to_record(state)
            await self.repo.update_trade_vwap_metrics(trade_id, record)
```

**テストは超シンプル：**

```python
# tests/core/test_vwap.py
def test_update_vwap_state_increments_above_seconds_when_above():
    state = VWAPState()
    new_state = update_vwap_state(state, current_price=100.5, vwap=100.0, elapsed_sec=3)
    assert new_state.above_seconds == 3
    assert new_state.below_seconds == 0
    assert new_state.last_above is True

def test_detects_cross():
    state = VWAPState(last_above=True)
    new_state = update_vwap_state(state, current_price=99.5, vwap=100.0, elapsed_sec=1)
    assert new_state.cross_count == 1

def test_no_false_cross_on_first_update():
    state = VWAPState(last_above=None)
    new_state = update_vwap_state(state, current_price=100.5, vwap=100.0, elapsed_sec=1)
    assert new_state.cross_count == 0  # 初回はクロスなし
```

**この設計の利点：**
- ロジックを変えるたびにモックを書き直す必要がない
- 状態の遷移を純関数のテストだけで網羅できる
- APPLICATION層は「いつ呼ぶか」だけに責務が集中する

### 6.5 VWAPベース動的損切り（Phase 3以降検証）

実データが集まった段階で導入を検討する追加ロジック：

```python
# 案：LONG中にVWAPを下抜けたら早期撤退
def should_exit_on_vwap_break(position, current_price, vwap):
    """LONG中にVWAP割れ＋直後反発なし → 早期撤退"""
    if position.direction != "LONG":
        return False
    if current_price >= vwap:
        return False
    # VWAP割れから60秒経過しても戻らない場合のみ撤退
    if position.below_vwap_since is None:
        position.below_vwap_since = now()
        return False
    if (now() - position.below_vwap_since).seconds > 60:
        return True
    return False
```

ただしこれは**Phase 1〜2のデータで効果検証してから**実装する。
最初から組み込むと「auto-daytradeで損切り早すぎて負けた」と同じ罠にハマる可能性。

### 6.6 VWAP分析クエリ例

実データが集まった段階で以下のような検証ができる：

```sql
-- VWAP乖離率別の勝率（章4のVWAP+0.5%の妥当性検証）
SELECT
    CASE
        WHEN vwap_distance_at_entry_pct < 0.1 THEN '1: <0.1%'
        WHEN vwap_distance_at_entry_pct < 0.3 THEN '2: 0.1-0.3%'
        WHEN vwap_distance_at_entry_pct < 0.5 THEN '3: 0.3-0.5%'
        ELSE '4: >0.5%'
    END AS bucket,
    COUNT(*) AS n,
    SUM(CASE WHEN net_pnl_usd > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS win_rate,
    AVG(net_pnl_usd) AS avg_pnl
FROM trades
WHERE direction = 'LONG' AND is_real = 1
GROUP BY bucket;

-- 保有中VWAPクロス回数別の成績
SELECT
    vwap_cross_count,
    COUNT(*) AS n,
    AVG(net_pnl_usd) AS avg_pnl,
    AVG(hold_minutes) AS avg_hold_min
FROM trades
WHERE is_real = 1
GROUP BY vwap_cross_count;

-- 「VWAP上を保ってた割合」と勝敗の相関（LONG）
SELECT
    CASE
        WHEN vwap_held_above_pct > 0.9 THEN 'A: 90%+'
        WHEN vwap_held_above_pct > 0.7 THEN 'B: 70-90%'
        WHEN vwap_held_above_pct > 0.5 THEN 'C: 50-70%'
        ELSE 'D: <50%'
    END AS bucket,
    COUNT(*) AS n,
    SUM(CASE WHEN net_pnl_usd > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS win_rate
FROM trades
WHERE direction = 'LONG' AND is_real = 1
GROUP BY bucket;
```

これらのクエリで「VWAPフィルターが本当に効いているか」「保有中のVWAP割れが負け要因になっているか」を検証し、ロジック改善の根拠データにする。

### 6.7 既存BOTからの差分

| 項目 | moomoo-trader | hl-alpha-bot |
|---|---|---|
| VWAP記録 | エントリー時のみ | エントリー時+決済時+保有中継続 |
| 乖離率カラム | なし | vwap_distance_at_entry/exit_pct |
| 保有中挙動 | なし | クロス回数・上方滞在率・極値 |
| エントリー条件 | 記録のみ・判定未使用 | layer1のpass判定に使用 |
| 損切りロジック | VWAP無関係 | Phase 3以降でVWAP割れ撤退検証 |
| 分析クエリ | なし | 6.6に3種類定義 |

---

## 7. SENTIMENT判定の詳細仕様（sentiment_analyzer.py）

章4の③SENTIMENTを実装する際の具体仕様。moomoo-trader/src/signals/sentiment_analyzer.pyを暗号資産用に書き換える前提。

### 7.1 基本フロー

```
銘柄ティッカー（例: BTC）
   ↓
ニュース・SNS・掲示板テキストを収集（直近1〜6時間分）
   ↓
複数のテキストをまとめてClaude APIに投げる（バッチ）
   ↓
JSON形式で「score」「confidence」「key_factors」「reasoning」を取得
   ↓
score > 0.6 AND confidence > 0.7 → エントリー候補
```

### 7.2 テキスト収集ソース

moomooではYahoo Finance + Google News RSS + moomoo掲示板を使用。暗号資産では以下に置き換え：

| ソース | 内容 | 取得方法 | 優先度 |
|---|---|---|---|
| **CoinDesk RSS** | プロ記者の市況記事 | RSS無料 | ★★★ |
| **CoinTelegraph RSS** | 同上 | RSS無料 | ★★★ |
| **The Block RSS** | 機関投資家向け | RSS無料 | ★★ |
| **CryptoPanic API** | 集約ニュース＋センチメント付 | 無料枠あり | ★★★ |
| **Reddit r/cryptocurrency** | コミュニティ熱量 | API無料 | ★★ |
| **X（Twitter）** | リアルタイム熱量 | 有料・回避推奨 | ☆ |
| **HyperLiquid Discord** | プロジェクト固有 | Webhook scrape | ★ |

**Phase 1での推奨構成：** CoinDesk + CoinTelegraph + CryptoPanic + Reddit の4ソース

**Phase 4以降で追加検討：** X API（コスト$200/月だが影響大）

### 7.3 テキスト前処理

各ソースから取得後、以下のフィルタを適用：

```python
# 1. 銘柄関連性フィルタ
texts = [t for t in raw_texts 
         if symbol.upper() in t.upper() 
         or symbol_aliases[symbol] in t.upper()]
# 例: ETH の aliases = ["ETHEREUM", "VITALIK", "ETH"]

# 2. 時間フィルタ
texts = [t for t in texts 
         if (now - t.published_at) < timedelta(hours=6)]

# 3. 重複除去（タイトル類似度0.8以上は同一視）
texts = deduplicate_by_similarity(texts, threshold=0.8)

# 4. 長さ調整（本文先頭500文字でカット）
texts = [t[:500] for t in texts]

# 5. 件数上限（最大10件・古い順から削除）
texts = texts[:10]
```

### 7.4 Claude APIプロンプト

```python
SYSTEM_PROMPT = """あなたは暗号資産市場のセンチメント分析エキスパートです。
ニュースとコミュニティ投稿から、短期（数時間〜1日）の価格影響を数値化します。
必ずJSON形式のみで回答し、他のテキストは一切含めないでください。"""

USER_PROMPT = """以下は{symbol}に関する直近{hours}時間のニュース・コメントです。
{symbol}の短期（数時間〜1日）の価格に対するセンチメントを分析してください。

【テキスト（{count}件）】
{texts}

【出力形式（JSON厳守・他のテキスト不要）】
{{
  "score": -1.0〜1.0の数値,
  "confidence": 0.0〜1.0の数値,
  "key_factors": ["要因1", "要因2", ...],
  "reasoning": "1〜2文の根拠",
  "flags": {{
    "has_hack": bool,
    "has_etf_news": bool,
    "has_listing": bool,
    "has_regulation": bool
  }}
}}

【スコアリング基準】
score: 価格への影響度
  +1.0 = 強烈な買い材料（メジャー上場・大型提携・規制緩和承認）
  +0.7 = 明確なポジティブ（パートナーシップ・好決算・テーマ性）
  +0.3 = ややポジティブ（小ニュース・地合い良好）
   0.0 = 中立・ノイズ
  -0.3 = ややネガティブ
  -0.7 = 明確なネガティブ（ハッキング・上場廃止・規制強化）
  -1.0 = 致命的（破綻・大規模流出・SECエンフォース）

confidence: 判定の確信度
  高い（>0.8）: 複数ソースが同方向 / 明確な事実ベース
  中（0.5〜0.8）: 単一だが信頼ソース / やや確定的
  低い（<0.5）: 噂レベル / 矛盾する情報 / 推測ばかり

【暗号資産特有の判定ルール】
ポジティブ要因（score上げる）：
- メジャー取引所上場（Binance/Coinbase/HyperLiquid）
- ETF承認・申請進展
- ステーキング・エアドロップ発表
- 機関投資家採用（BlackRock/Fidelity等）
- メインネット稼働・大型アップグレード

ネガティブ要因（score下げる）：
- ハッキング・エクスプロイト・流出
- SEC訴訟・規制対象指定
- 取引所上場廃止・取引停止
- バリデーター/ノード問題
- ステーブルコインのデペッグ
- ファウンダー逮捕・スキャンダル

中立扱い（score=0）：
- 価格の振り返り記事
- 「○○ドル突破」のみの記事
- 一般的な相場予想
- インフルエンサーの願望ツイート

【重要な除外ルール】
- 過去の振り返り記事 → score=0
- 「〜の可能性」「〜と予想」のみ → confidenceを0.3以下に
- 全テキストが価格チャート解説のみ → score=0, confidence<0.3
"""
```

### 7.5 レスポンス例

**ポジションティブケース（エントリー候補）：**
```json
{
  "score": 0.72,
  "confidence": 0.81,
  "key_factors": [
    "BlackRockがETHステーキング機能をETFに追加申請",
    "オンチェーン出来高24h+45%",
    "Funding rateは中立維持"
  ],
  "reasoning": "機関採用拡大の明確なポジティブ材料。既に織り込み始めている可能性あり。",
  "flags": {
    "has_hack": false,
    "has_etf_news": true,
    "has_listing": false,
    "has_regulation": false
  }
}
```
→ score=0.72, confidence=0.81 → **③クリア**

**過熱ケース（除外）：**
```json
{
  "score": 0.91,
  "confidence": 0.85,
  "key_factors": ["価格急騰の連鎖報道", "FOMOコメント多数"],
  "reasoning": "既に大きく上昇後の追随報道のみ。新規材料なし。"
}
```
→ score=0.91 > 0.6 → **③クリア**（ただし過熱気味のためログに記録し、Phase 2以降で除外検証）

**確信度低ケース（除外）：**
```json
{
  "score": 0.65,
  "confidence": 0.42,
  "key_factors": ["噂レベルのパートナーシップ報道のみ"],
  "reasoning": "単一ソースの未確認情報。公式発表なし。"
}
```
→ confidence=0.42 < 0.7 → **③不通過**

### 7.6 エントリー判定ロジック

**Phase 1〜2（初期運用）：シンプル版**

```python
def is_sentiment_pass_long(response: dict) -> bool:
    """LONGエントリーのSENTIMENT判定（初期版）"""
    return (
        response["score"] > 0.6
        and response["confidence"] > 0.7
        and not response["flags"]["has_hack"]
        and not response["flags"]["has_regulation"]
    )

def is_sentiment_pass_short(response: dict) -> bool:
    """SHORTエントリーのSENTIMENT判定（初期版）"""
    return (
        response["score"] < -0.3
        and response["confidence"] > 0.7
    )
```

**Phase 3以降（データ蓄積後）：過熱除外の検討**

サンプル数が50件を超えた段階で、`sentiment_logs`テーブルから
スコア帯別の実勝率を分析し、上限/下限除外の閾値を決定する。

```python
# 検討予定の改良版（Phase 3以降）
def is_sentiment_pass_long_v2(response: dict) -> bool:
    return (
        response["score"] > 0.6
        and response["score"] < SCORE_UPPER_LIMIT  # 実データから決定
        and response["confidence"] > 0.7
        and not response["flags"]["has_hack"]
        and not response["flags"]["has_regulation"]
    )
```

moomooの知見（0.85以上は負けやすい）はあくまで米株での実績であり、
仮想通貨で同じ傾向になるかは別途検証が必要。先入観を持たずデータで判断する。

### 7.7 コスト最適化（重要）

全銘柄を毎ループAPI呼び出しすると月$500超になる。以下4つの工夫で月$30〜50に抑える。

**a) 4層AND評価順序の徹底**
```
① MOMENTUM（ローカル計算・無料）
② FLOW（HL API・無料）
④ REGIME + LIQUIDATION（HL API・無料）
↓ ここまで全クリアした銘柄だけ
③ SENTIMENT（Claude API・有料）
```

**b) キャッシュ戦略**
```python
# 同じ銘柄の評価は5分間キャッシュ
SENTIMENT_CACHE_TTL = 300  # 秒

if symbol in cache and (now - cache[symbol].timestamp) < SENTIMENT_CACHE_TTL:
    # 新規テキストが追加されてなければキャッシュ使用
    if not has_new_texts(symbol, since=cache[symbol].timestamp):
        return cache[symbol].response
```

**c) バッチ処理**
複数銘柄を1リクエストにまとめる（Claude APIはマルチ銘柄解析可能）。
ただしレスポンス品質が落ちる場合は単発に戻す。

**d) Prompt Caching活用**
Claude APIのprompt cachingでスコアリング基準部分（システムプロンプト＋ルール）をキャッシュ。
キャッシュヒット時はトークン単価が90%引き。

### 7.8 moomoo実績からの知見

moomoo-traderの71件の実績データから判明した重要事実：

| sentiment | 件数 | 勝率 | 合計PnL |
|---|---|---|---|
| 0.60〜0.69 | 7 | 71% | -$1.62（サンプル少） |
| 0.70〜0.79 | 50 | 62% | **+$51.98** ← スイートスポット |
| 0.80〜0.89 | 14 | 43% | +$9.03（過熱気味） |

**結論：** moomoo（米株）では0.7〜0.8がスイートスポットで0.85以上は負ける傾向があった。
ただし**仮想通貨で同じ傾向になる保証はない**ため、初期は `score > 0.6` のみで運用し、
50件以上のサンプルが蓄積された時点でhl-alpha-bot独自のスイートスポットを決定する。

採用方針：
- **Phase 1〜2：** `score > 0.6 AND confidence > 0.7`（上限なし・データ収集優先）
- **Phase 3以降：** `sentiment_logs`の実勝率から上限を決定して導入

### 7.9 ロギング項目（必須）

各API呼び出し結果はSQLite `sentiment_logs` テーブルに保存：

```sql
CREATE TABLE sentiment_logs (
    id INTEGER PRIMARY KEY,
    timestamp DATETIME,
    symbol TEXT,
    score REAL,
    confidence REAL,
    key_factors TEXT,
    reasoning TEXT,
    flags JSON,
    text_count INTEGER,
    text_sources TEXT,
    api_cost_usd REAL,
    cached BOOL,
    entered BOOL,           -- このsentimentでエントリーしたか
    pnl_if_entered REAL     -- 後で結果を埋める
);
```

これにより：
- 「sentiment 0.X台の実勝率」を継続検証
- スコアと実損益の相関を月次で再評価
- プロンプトの調整根拠データになる

### 7.10 PR7.5b 実装で確定した SentimentResult 仕様

PR7.5b (FixedSentimentProvider) で `SentimentResult` の実型を確認した結果：

```python
@dataclass(frozen=True)
class SentimentResult:
    score: Decimal       # 範囲 [-1, 1]
    confidence: Decimal  # 範囲 [0, 1]
    direction: Literal["bullish", "bearish", "neutral"]
    reasoning: str
    source_count: int
    cached: bool
```

- `score` は **`[-1, 1]`** 範囲（仕様書本文で曖昧だった部分の確定）
  - `-1.0` 〜 `-0.3` : bearish 寄り
  - `-0.3` 〜 `+0.6` : neutral
  - `+0.6` 〜 `+1.0` : bullish 寄り
- 1 つのスコアで LONG/SHORT 両方向を表現（符号で方向、絶対値で強さ）
- `direction` の判定は CORE 層 entry_judge と整合させる（章 11.4）：
  - `score > 0.6` AND `confidence > 0.7` → LONG SENTIMENT 通過
  - `score < -0.3` AND `confidence > 0.7` → SHORT SENTIMENT 通過

`fetch_sources` / `judge` / `judge_cached_or_fresh` の 3 メソッドを揃えて
`SentimentProvider` Protocol を満たす。

---

## 8. データロギング設計（DB主体 + CSVエクスポート）

両既存BOT（auto-daytrade・moomoo）はCSV主体だったが、hl-alpha-botではSQLite主体に切り替える。
理由は以下：

- 取引数が桁違い（24h取引のため月100〜500件想定）
- スキーマ進化が頻繁（カラム追加でCSVは破綻しやすい）
- 月跨ぎ集計・分析がSQLで一発になる
- BOT稼働中に別プロセスから安全に閲覧可能（WAL mode）
- 税務エクスポートはCSVに自動変換すればよい

### 8.1 ストレージ構成

```
プライマリ：data/hl_bot.db（SQLite + WAL mode）
  ├─ trades                 メイン取引記録
  ├─ signals                4層AND判定の全評価ログ
  ├─ dryrun                 Phase 0-1の仮想取引
  ├─ sentiment_logs         Claude API詳細（章7.9）
  ├─ funding_payments       Funding精算記録
  ├─ deposits_withdrawals   入出金（オンチェーン）
  ├─ utc_open_prices        UTC 00:00価格スナップショット（章5.6）
  ├─ oi_history             OI時系列（章13.5・1h前OI参照用）
  ├─ usdjpy_rates           USD/JPYレート（税務用）
  └─ blacklist              当日エントリー禁止銘柄

エクスポート：data/exports/（毎日23:59 UTCに自動生成）
  ├─ trades_YYYY-MM.csv     月別取引（運用分析用）
  ├─ trades_YYYY.csv        年次累積（税務用・JPY換算済み）
  ├─ funding_YYYY.csv       年次Funding（税務用）
  └─ tax_summary_YYYY.csv   年次総括（税務申告補助）

ログ：logs/bot_YYYYMMDD.log（テキスト・常時）
  - エントリー判定の全試行
  - スキップ理由
  - APIエラー
  - 例外スタックトレース
```

### 8.2 trades テーブル（メイン取引記録）

確定した取引のみ記録。クローズ時に1行追加。

```sql
CREATE TABLE trades (
    -- 基本情報
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT NOT NULL,          -- HL注文ID（OID）
    symbol          TEXT NOT NULL,          -- "BTC" "ETH" 等
    direction       TEXT NOT NULL,          -- 'LONG' / 'SHORT'
    size            REAL NOT NULL,          -- コイン枚数（小数）
    leverage_used   REAL,                   -- 使用レバ倍率

    -- 価格・タイミング（USD建て）
    entry_price     REAL NOT NULL,
    exit_price      REAL NOT NULL,
    opened_at       DATETIME NOT NULL,      -- UTC
    closed_at       DATETIME NOT NULL,      -- UTC
    hold_minutes    REAL NOT NULL,
    position_value_usd REAL,                -- エントリー時約定金額

    -- 損益
    pnl_usd         REAL NOT NULL,          -- 売買損益（USD）
    commission_taker_usd REAL DEFAULT 0,    -- Taker手数料
    commission_maker_usd REAL DEFAULT 0,    -- Maker手数料（条件達成時のみリベート=負）
    funding_total_usd REAL DEFAULT 0,       -- 保有中Funding合計（受取+/支払-）
    net_pnl_usd     REAL NOT NULL,          -- 純損益

    -- JPY換算（税務用）
    entry_price_jpy     REAL,
    exit_price_jpy      REAL,
    pnl_jpy             REAL,
    net_pnl_jpy         REAL,
    usdjpy_rate_at_entry REAL,
    usdjpy_rate_at_exit  REAL,

    -- 決済理由
    exit_reason     TEXT NOT NULL,          -- 'TP' / 'SL' / 'FORCE_CLOSE' /
                                            -- 'FUNDING_EXIT' / 'TIMEOUT' / 'MANUAL'

    -- スリッページ（執行検証用）
    slippage_entry_pct REAL,                -- 想定vs実約定価格差
    slippage_exit_pct  REAL,
    is_maker_entry  INTEGER,                -- 1=Maker, 0=Taker
    is_maker_exit   INTEGER,

    -- リスク指標（エントリー時スナップショット）
    atr_value_usd   REAL,                   -- ATR絶対値
    atr_pct         REAL,                   -- ATR%
    sl_price        REAL,                   -- 設定したSL価格
    tp_price        REAL,                   -- 設定したTP価格

    -- VWAP情報（エントリー時）
    vwap_at_entry_price         REAL,       -- VWAP価格（エントリー時）
    vwap_distance_at_entry_pct  REAL,       -- VWAPからの乖離率(%) ★チューニング用
    vwap_above_at_entry         INTEGER,    -- 1=上, 0=下

    -- VWAP情報（決済時）
    vwap_at_exit_price          REAL,
    vwap_distance_at_exit_pct   REAL,
    vwap_above_at_exit          INTEGER,

    -- VWAP保有中挙動（章6のVWAPフィルター効果検証用）
    vwap_cross_count            INTEGER,    -- 保有中VWAPを跨いだ回数
    vwap_held_above_pct         REAL,       -- 保有時間中VWAP上にいた割合(0〜1)
                                            -- LONGなら高い方が良い
    min_vwap_distance_pct       REAL,       -- 保有中の最小乖離（最接近）
    max_vwap_distance_pct       REAL,       -- 保有中の最大乖離

    -- 4層判定値（エントリー時）
    momentum_5bar_pct  REAL,                -- 5本前比モメンタム
    flow_buy_sell_ratio REAL,               -- 買い/売り約定比
    flow_strength   REAL,                   -- 大口フロー強度
    sentiment_score REAL,
    sentiment_confidence REAL,
    btc_regime      TEXT,                   -- 'UPTREND' / 'DOWNTREND' / 'CHOP'

    -- 章5 価格基準3点
    utc_day_change_pct      REAL,
    rolling_24h_change_pct  REAL,
    position_in_24h_range   REAL,

    -- 仮想通貨特有
    btc_change_24h          REAL,           -- BTCの24h変化率（地合い）
    btc_dominance           REAL,           -- BTC.D（アルト分析用）
    funding_rate_at_entry   REAL,
    next_funding_minutes    INTEGER,        -- 次回Fundingまで分
    oi_change_1h_pct        REAL,           -- OI 1h変化率
    liquidation_zone_above_usd REAL,        -- 上方清算クラスター
    liquidation_zone_below_usd REAL,        -- 下方清算クラスター
    spread_at_entry_pct     REAL,           -- bid-ask spread
    orderbook_imbalance     REAL,           -- 板の偏り

    -- MFE/MAE（保有中の最大/最小）
    mfe_pct         REAL,                   -- 最大含み益%
    mae_pct         REAL,                   -- 最大含み損%
    mfe_usd         REAL,
    mae_usd         REAL,

    -- メタ情報
    entry_pattern   TEXT,                   -- 'MOMENTUM_BREAKOUT' /
                                            -- 'LIQUIDATION_HUNT' 等
    entry_source    TEXT,                   -- 'fixed' / 'dynamic' / 'both'
    sentiment_summary TEXT,                 -- センチメント要約（参照用）
    flag_has_etf_news    INTEGER,
    flag_has_listing     INTEGER,
    flag_has_hack        INTEGER,
    flag_has_regulation  INTEGER,

    -- フェーズ管理
    phase           TEXT,                   -- 'PHASE_2' / 'PHASE_3' / 'PHASE_4'
    is_real         INTEGER NOT NULL,       -- 1=実弾, 0=仮想

    -- インデックス用
    notes           TEXT
);

CREATE INDEX idx_trades_symbol ON trades(symbol);
CREATE INDEX idx_trades_opened_at ON trades(opened_at);
CREATE INDEX idx_trades_exit_reason ON trades(exit_reason);
```

### 8.3 signals テーブル（4層AND評価ログ）

エントリーした/しなかったに関わらず、watchlistスキャン中の全評価を記録。
**重要：** auto-daytradeで「なぜエントリーしなかったか」が後から追えず苦労した教訓。

```sql
CREATE TABLE signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       DATETIME NOT NULL,      -- UTC
    symbol          TEXT NOT NULL,
    direction       TEXT,                   -- 'LONG' / 'SHORT' / NULL（評価のみ）

    -- 4層判定結果
    layer1_momentum_pass    INTEGER,        -- 0/1
    layer2_flow_pass        INTEGER,
    layer3_sentiment_pass   INTEGER,
    layer4_regime_pass      INTEGER,
    final_decision          TEXT,           -- 'ENTRY' / 'REJECTED' / 'BLACKLISTED'
    rejection_reason        TEXT,           -- どの層で何が原因で落ちたか

    -- 各層の詳細値（チューニング用）
    momentum_5bar_pct       REAL,
    vwap_price              REAL,            -- VWAP値そのもの
    vwap_above              INTEGER,         -- 1=現在値がVWAP上
    vwap_distance_pct       REAL,            -- VWAPからの乖離率
    utc_day_change_pct      REAL,
    rolling_24h_change_pct  REAL,
    position_in_24h_range   REAL,

    flow_buy_sell_ratio     REAL,
    flow_large_order_count  INTEGER,        -- >$50k約定数
    volume_surge_ratio      REAL,           -- 直近20本平均比

    sentiment_score         REAL,
    sentiment_confidence    REAL,
    sentiment_text_count    INTEGER,
    sentiment_cached        INTEGER,        -- キャッシュ使用か

    btc_change_24h          REAL,
    btc_ema_trend           TEXT,
    funding_rate            REAL,
    liquidation_above_usd   REAL,
    liquidation_below_usd   REAL,

    -- 価格スナップショット
    current_price           REAL,
    spread_pct              REAL,

    phase                   TEXT,
    led_to_trade_id         INTEGER,        -- エントリーした場合のtrades.id
    FOREIGN KEY (led_to_trade_id) REFERENCES trades(id)
);

CREATE INDEX idx_signals_timestamp ON signals(timestamp);
CREATE INDEX idx_signals_symbol ON signals(symbol);
CREATE INDEX idx_signals_decision ON signals(final_decision);
```

### 8.4 dryrun テーブル（Phase 0-1仮想取引）

```sql
CREATE TABLE dryrun (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            DATE NOT NULL,
    symbol          TEXT NOT NULL,
    pattern         TEXT NOT NULL,          -- 'LONG_MOMENTUM' /
                                            -- 'SHORT_MOMENTUM' / 'LIQ_HUNT'
    entry_time      DATETIME NOT NULL,
    entry_price     REAL NOT NULL,
    sl_price        REAL,
    tp_price        REAL,
    virtual_size    REAL,                   -- 仮想ポジションサイズ

    -- 4層判定値
    momentum_5bar_pct       REAL,
    flow_buy_sell_ratio     REAL,
    sentiment_score         REAL,
    sentiment_confidence    REAL,
    btc_regime              TEXT,
    funding_rate            REAL,
    liquidation_above_usd   REAL,
    liquidation_below_usd   REAL,

    -- VWAP情報（エントリー時）
    vwap_at_entry_price         REAL,
    vwap_distance_at_entry_pct  REAL,
    vwap_above_at_entry         INTEGER,

    -- 章5 3基準
    utc_day_change_pct      REAL,
    rolling_24h_change_pct  REAL,
    position_in_24h_range   REAL,

    -- 結果（決済時に更新）
    close_time              DATETIME,
    close_price             REAL,
    exit_reason             TEXT,           -- 'TP' / 'SL' / 'FORCE_CLOSE' / 'TIMEOUT'
    virtual_pnl_usd         REAL,
    virtual_pnl_pct         REAL,
    virtual_hold_minutes    REAL,
    virtual_mfe_pct         REAL,
    virtual_mae_pct         REAL,

    -- VWAP情報（決済時）
    vwap_at_close_price         REAL,
    vwap_distance_at_close_pct  REAL,

    -- 後付け検証用
    would_trigger_real      INTEGER,        -- 実弾モードならエントリーしたか
    phase                   TEXT
);

CREATE INDEX idx_dryrun_date ON dryrun(date);
CREATE INDEX idx_dryrun_symbol ON dryrun(symbol);
```

### 8.5 funding_payments テーブル（税務必須）

HyperLiquidは1時間ごとにFunding精算（8h rateを1/8ずつ毎時支払い）。1ポジション保有中に多数回発生する。

```sql
CREATE TABLE funding_payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       DATETIME NOT NULL,      -- UTC精算時刻
    related_trade_id INTEGER,
    symbol          TEXT NOT NULL,
    funding_rate    REAL NOT NULL,
    position_size   REAL NOT NULL,          -- 精算時の保有数量
    amount_usd      REAL NOT NULL,          -- +受取 / -支払い
    usdjpy_rate     REAL NOT NULL,
    amount_jpy      REAL NOT NULL,
    is_received     INTEGER NOT NULL,       -- 1=受取, 0=支払い
    FOREIGN KEY (related_trade_id) REFERENCES trades(id)
);

CREATE INDEX idx_funding_timestamp ON funding_payments(timestamp);
CREATE INDEX idx_funding_trade ON funding_payments(related_trade_id);
```

### 8.5b oi_history テーブル（章13.5 OI急変検出用）

`open_interest_1h_ago` を取得するための時系列保存。
HL公式API には「1h前のOI」を直接返すエンドポイントがないため、自前で記録する。

```sql
CREATE TABLE oi_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    timestamp   DATETIME NOT NULL,
    oi          REAL NOT NULL,              -- 建玉総額
    UNIQUE(symbol, timestamp)
);
CREATE INDEX idx_oi_symbol_time ON oi_history(symbol, timestamp);
```

**運用ルール：**
- ループ毎（10秒間隔・章19）に各銘柄の OI を記録
- 起動から1h経過するまでの grace period は OI変動 = 0%扱い（章9.8）
- 直近24時間以前のデータは日次で削除（容量管理）

**Repository メソッド（章11.5 ADAPTERS）：**
- `record_oi(symbol, timestamp, oi)` - 記録
- `get_oi_at(symbol, timestamp, tolerance_minutes=5)` - 指定時刻に最も近い OI を返す
- `prune_old_oi(keep_hours=24)` - 古いデータを削除

### 8.6 deposits_withdrawals テーブル（税務必須）

```sql
CREATE TABLE deposits_withdrawals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       DATETIME NOT NULL,
    type            TEXT NOT NULL,          -- 'deposit' / 'withdrawal'
    asset           TEXT NOT NULL,          -- 'USDC' / 'ETH' 等
    amount          REAL NOT NULL,
    amount_usd      REAL NOT NULL,
    gas_fee_usd     REAL DEFAULT 0,
    tx_hash         TEXT,
    from_address    TEXT,
    to_address      TEXT,
    usdjpy_rate     REAL NOT NULL,
    amount_jpy      REAL NOT NULL,
    gas_fee_jpy     REAL,
    notes           TEXT
);
```

### 8.7 usdjpy_rates テーブル（税務根拠）

確定申告に使うため、毎日17:00（JST）にみずほTTM等を記録。

```sql
CREATE TABLE usdjpy_rates (
    date            DATE PRIMARY KEY,
    ttm             REAL NOT NULL,          -- 仲値（みずほ銀行TTM）
    ttb             REAL,                   -- 買値
    tts             REAL,                   -- 売値
    source          TEXT,                   -- 'mizuho' / 'mufg' 等
    fetched_at      DATETIME
);
```

**実装メモ：**
- リアルタイム取引時はCoinGecko/Yahoo FinanceのレートをUSD/JPY換算に使用
- 毎日JST 17:00にみずほ銀行公示レートを別途取得して`usdjpy_rates`に保存
- 年次税務エクスポート時はこの`usdjpy_rates.ttm`で再計算
- これにより税務署に「みずほTTM基準で計算してます」と正当に主張できる

### 8.8 自動CSVエクスポート

毎日23:59 UTCに以下を自動生成：

| ファイル | 内容 | 用途 |
|---|---|---|
| `trades_YYYY-MM.csv` | 当月の確定取引 | 月次運用分析 |
| `signals_YYYY-MM.csv` | 当月のシグナル評価（圧縮版） | フィルタチューニング |
| `dryrun_YYYY-MM.csv` | 当月のドライラン結果 | Phase検証 |

### 8.9 年次税務エクスポート

毎年12月31日（or 翌年1月1日）に自動生成、または手動コマンドで生成可能：

**`tax_summary_YYYY.csv`：**

```
売買損益（雑所得）：
  - 確定取引数：XXX件
  - 売買損益合計（USD）：$X,XXX
  - 売買損益合計（JPY換算・取引時レート）：¥X,XXX,XXX
  - 売買損益合計（JPY換算・年末みずほTTM）：¥X,XXX,XXX

Funding損益（雑所得）：
  - Funding受取合計（USD/JPY）：
  - Funding支払合計（USD/JPY）：
  - Funding純額（JPY）：

手数料：
  - Taker手数料合計（JPY）：
  - Maker手数料合計（JPY、条件達成時のみ負）：

ガス代：
  - 入出金ガス代合計（JPY）：

【雑所得合計（JPY）】：¥X,XXX,XXX
```

**`trades_YYYY.csv`（税務用詳細）：**

各取引について以下を1行で：
```
取引日, 銘柄, 方向, 数量, エントリー価格(USD), 決済価格(USD),
売買損益(USD), USD/JPYレート(エントリー), USD/JPYレート(決済),
売買損益(JPY), 手数料(USD), 手数料(JPY), Funding(USD), Funding(JPY),
純損益(JPY)
```

これをそのままCryptactや確定申告ソフトに食わせられる形式にする。

### 8.10 補助：sentiment_logs テーブル（章7.9・再掲）

章7.9で定義済み。ここでは構造のみ参照。

```sql
CREATE TABLE sentiment_logs (
    id INTEGER PRIMARY KEY,
    timestamp DATETIME,
    symbol TEXT,
    score REAL,
    confidence REAL,
    key_factors TEXT,                       -- JSON文字列
    reasoning TEXT,
    flags TEXT,                             -- JSON文字列
    text_count INTEGER,
    text_sources TEXT,
    api_cost_usd REAL,
    cached INTEGER,
    led_to_trade_id INTEGER,
    FOREIGN KEY (led_to_trade_id) REFERENCES trades(id)
);
```

### 8.11 blacklist テーブル

auto-daytrade踏襲。当日エントリー禁止銘柄を管理。

```sql
CREATE TABLE blacklist (
    symbol          TEXT NOT NULL,
    blacklisted_at  DATETIME NOT NULL,      -- UTC
    expires_at      DATETIME NOT NULL,      -- 通常24時間後
    reason          TEXT NOT NULL,          -- 'STOP_LOSS' / 'TAKE_PROFIT' /
                                            -- 'LIQUIDATION' / 'MANUAL'
    related_trade_id INTEGER,
    PRIMARY KEY (symbol, blacklisted_at)
);
```

### 8.12 SQLite運用上の注意点

```python
# WAL mode有効化（同時アクセス対応）
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
conn.execute("PRAGMA foreign_keys=ON")

# 定期VACUUM（月1回・サイズ削減）
conn.execute("VACUUM")

# バックアップ（毎日）
import shutil
shutil.copy("data/hl_bot.db", f"data/backups/hl_bot_{date}.db")
# 30日以上前のバックアップは削除
```

### 8.13 分析クエリ例（後の改善で使う）

設計書を実装後、以下のようなクエリで運用改善できる：

```sql
-- センチメントスコア帯別の勝率
SELECT
    CAST(sentiment_score * 10 AS INT) / 10.0 AS score_bucket,
    COUNT(*) AS trades,
    SUM(CASE WHEN net_pnl_usd > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS win_rate,
    SUM(net_pnl_usd) AS total_pnl
FROM trades
WHERE is_real = 1
GROUP BY score_bucket
ORDER BY score_bucket;

-- 過熱フィルター3基準別の勝率（章5チューニング用）
SELECT
    CASE
        WHEN utc_day_change_pct < 0.02 THEN '1: <2%'
        WHEN utc_day_change_pct < 0.05 THEN '2: 2-5%'
        WHEN utc_day_change_pct < 0.10 THEN '3: 5-10%'
        ELSE '4: >10%'
    END AS gap_bucket,
    COUNT(*) AS trades,
    AVG(net_pnl_usd) AS avg_pnl,
    SUM(net_pnl_usd) AS total_pnl
FROM trades
WHERE direction = 'LONG' AND is_real = 1
GROUP BY gap_bucket;

-- どの層で落ちているか（フィルター詰まり分析）
SELECT
    rejection_reason,
    COUNT(*) AS count,
    COUNT(*) * 100.0 / (SELECT COUNT(*) FROM signals WHERE final_decision = 'REJECTED') AS pct
FROM signals
WHERE final_decision = 'REJECTED'
GROUP BY rejection_reason
ORDER BY count DESC;

-- スリッページ常態化のチェック（auto-daytrade教訓）
SELECT
    DATE(opened_at) AS date,
    AVG(slippage_entry_pct) AS avg_slippage,
    AVG(CASE WHEN is_maker_entry = 1 THEN 1.0 ELSE 0.0 END) AS maker_ratio
FROM trades
GROUP BY DATE(opened_at)
ORDER BY date DESC;

-- 月次税務サマリー
SELECT
    strftime('%Y-%m', closed_at) AS month,
    COUNT(*) AS trades,
    SUM(net_pnl_jpy) AS pnl_jpy,
    SUM(commission_taker_usd * usdjpy_rate_at_exit) AS commission_jpy,
    SUM(funding_total_usd * usdjpy_rate_at_exit) AS funding_jpy
FROM trades
WHERE is_real = 1
GROUP BY month;
```

### 8.14 既存BOTからの差分まとめ

| 項目 | auto-daytrade | moomoo-trader | hl-alpha-bot |
|---|---|---|---|
| プライマリストレージ | CSV | CSV | **SQLite** |
| シグナル評価ログ | なし | なし | **signals テーブル** |
| 税務情報 | 簡易 | 簡易 | **JPY換算・USD/JPY記録完備** |
| Funding記録 | N/A | N/A | **funding_payments完備** |
| 入出金記録 | N/A | N/A | **deposits_withdrawals** |
| MFE/MAE | あり | あり | あり（強化） |
| スキーマ進化 | カラム追加困難 | 同左 | **ALTER TABLEで安全** |
| 自動CSV出力 | プライマリ | プライマリ | **エクスポート（補助）** |
| バックアップ | 手動 | 手動 | **日次自動** |

### 8.15 PR7.5a 実装で確定した事項

PR7.5a (SQLiteRepository 実装) で確定した、設計書記述と実装の差分。

#### 8.15.1 schema.sql の配置と適用

```
src/infrastructure/migrations/schema.sql
```

- `migrations/` 配下に配置
- 全テーブル `CREATE TABLE IF NOT EXISTS` で冪等
- `schema_version` テーブルを併設（将来のマイグレーション用）
- `SQLiteRepository.initialize()` で `executescript` 一発適用

#### 8.15.2 trades テーブルの追加フィールド（PR7.2/7.3 の状態管理用）

章 8.2 の trades テーブル DDL に以下のフィールドが実装で追加されている：

| フィールド | 型 | 用途 | 出所 |
|---|---|---|---|
| `is_filled` | INTEGER (0/1) | entry 約定済みフラグ | PR7.2 |
| `actual_entry_price` | REAL | 実約定価格（指定価格と異なり得る） | PR7.2 |
| `tp_order_id` / `sl_order_id` | TEXT | grouped 発注後に紐付けられる HL OID | PR7.2・章 14.6 |
| `is_external` | INTEGER (0/1) | 外部発生（手動取引等）フラグ | PR7.3・章 9.3 |
| `resumed_at` | DATETIME | 再開済みマーク時刻 | PR7.3・章 9.3 |
| `is_manual_review` | INTEGER (0/1) | 手動確認要フラグ | PR7.3・章 9.3 |
| `fill_time` | DATETIME | entry 約定検知時刻 | PR7.2・章 14.6 |
| `vwap_metrics` | TEXT (JSON) | 章 6 VWAP State の JSON 保存 | PR7.5a |

#### 8.15.3 balance_history テーブル（新規）

口座残高スナップショットを記録（章 10.5 / 13.5 / 15.4 サマリー用）：

```sql
CREATE TABLE IF NOT EXISTS balance_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     DATETIME NOT NULL,
    balance_usd   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_balance_history_time
    ON balance_history(timestamp);
```

`Repository.record_balance_snapshot(timestamp, balance_usd)` で記録、
`Repository.get_account_balance_history(days)` で参照。

#### 8.15.4 incidents テーブル（章 9.11 ∋ 詳細化）

章 9.11 で言及していた障害ログテーブルを実装で具体化：

```sql
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   DATETIME NOT NULL,
    severity    TEXT NOT NULL,    -- INFO / WARNING / ERROR / CRITICAL
    event       TEXT NOT NULL,    -- イベント種別キー
    details     TEXT NOT NULL     -- JSON
);
```

`Repository.log_incident(IncidentLog)` で記録。

#### 8.15.5 Decimal の保存方針

SQLite は Decimal をネイティブサポートしないため、REAL（float）として保存し
読み出し時に `Decimal(str(row[...]))` で復元する。トレード規模なら 15 桁精度で
十分。等値比較や厳密な金額計算は CORE 層側 (Decimal) で行う。

#### 8.15.6 datetime の保存方針

ISO8601 文字列で保存。tzinfo がない datetime は UTC とみなして付与する：

```python
def _dt_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()
```

---

## 9. 障害時の振る舞い設計

実弾運用を始める前に必ず固める章。
auto-daytradeで「逆指値の二重発動」「APIエラーで監視停止」が起きた教訓から、
**起こり得る全障害について事前に挙動を決めておく**。

### 9.1 設計原則

```
原則1: 「分からない時は何もしない」より「分からない時は最も安全な行動」を取る
       → 不明な状態 = 全ポジションを成行クローズして停止

原則2: 「冪等性」を全注文で担保する
       → 同じclient_order_idで2回送っても二重発注にならない設計

原則3: 「真実の源（Source of Truth）はHyperLiquid側」
       → BOTの内部状態は常にHL APIで突合する。乖離があればHL側を信じる

原則4: 「監視できないポジションは持たない」
       → WS切断・API障害が一定時間続いたら全クローズ
```

### 9.2 障害シナリオ一覧と対処

| # | シナリオ | 検出方法 | 対処 | 重要度 |
|---|---|---|---|---|
| 1 | BOTクラッシュ・再起動 | 起動時チェック | 状態復元プロセス（4.5.3） | 🔴最重要 |
| 2 | WebSocket切断 | ハートビート停止 | 30秒超で全クローズ＋停止 | 🔴最重要 |
| 3 | HL API応答なし | タイムアウト | リトライ後・全クローズ | 🔴最重要 |
| 4 | 注文送信失敗 | エラーレスポンス | 冪等リトライ最大3回 | 🟡重要 |
| 5 | 約定通知欠損 | 定期突合 | get_fillsで突合・補正 | 🟡重要 |
| 6 | ポジション情報乖離 | 内部状態vs HL状態 | HL側を真として補正 | 🟡重要 |
| 7 | フラッシュクラッシュ | 価格5%以上の急変 | サーキットブレーカー | 🔴最重要 |
| 8 | ネットワーク完全断 | 全APIタイムアウト | 復旧後の状態突合 | 🔴最重要 |
| 9 | DB書き込み失敗 | SQLiteエラー | ローカル一時ファイル | 🟢通常 |
| 10 | Claude API障害 | タイムアウト | エントリー停止・既存ポジションは継続 | 🟢通常 |
| 11 | ガス代不足 | 入出金時 | 通知のみ・取引には影響なし | 🟢通常 |
| 12 | 起動直後のデータ不足 | ループ初回 | データ蓄積までエントリー禁止 | 🟡重要 |

### 9.3 BOT再起動時の状態復元プロセス

最も重要な障害対応。以下の順序で復元する。

**実装は`application/reconciliation.py`に配置**（章11のTDDアーキテクチャ参照）。
`ExchangeProtocol` `Repository` `Notifier` を依存注入することでテスト可能に。

```python
# src/application/reconciliation.py
from dataclasses import dataclass
from src.adapters.exchange import ExchangeProtocol
from src.adapters.repository import Repository
from src.adapters.notifier import Notifier


@dataclass
class StateReconciler:
    """起動時・定期突合の責務を持つユースケース

    依存はProtocolのみ（テスト時はモック化可能）。
    """
    exchange: ExchangeProtocol
    repo: Repository
    notifier: Notifier

    async def restore_on_startup(self) -> RestoreResult:
        """起動時の状態復元（必ず実行）"""

        # Step 1: HyperLiquidの真実の状態を取得
        hl_positions = await self.exchange.get_positions()
        hl_open_orders = await self.exchange.get_open_orders()
        hl_recent_fills = await self.exchange.get_fills(
            since=now() - timedelta(hours=24)
        )

        # Step 2: DBの状態を取得
        db_open_trades = await self.repo.get_open_trades()

        # Step 3: 突合と補正（純関数reconcile_positions()に委譲）
        result = reconcile_positions(
            hl_positions=hl_positions,
            db_trades=db_open_trades,
            hl_fills=hl_recent_fills,
        )

        # Step 4: 結果に応じて副作用を実行
        for action in result.actions:
            await self._apply_action(action)

        # Step 5: 古い未約定注文をキャンセル
        for order in hl_open_orders:
            if (now() - order.timestamp).seconds > 30:
                await self.exchange.cancel_order(order.id)

        # Step 6: 復元完了通知
        await self.notifier.send_signal(
            f"✅ 状態復元完了: {len(hl_positions)}ポジション監視中"
        )
        return result

    async def _apply_action(self, action: ReconcileAction) -> None:
        """突合結果のアクションを実行"""
        if action.type == "REGISTER_EXTERNAL":
            await self.repo.register_external_position(action.hl_pos)
            await self.notifier.send_alert(
                f"⚠️ 外部ポジション検出: {action.hl_pos.symbol}"
            )
        elif action.type == "RESUME_MONITORING":
            await self.repo.mark_resumed(action.db_trade.id)
        elif action.type == "CORRECT_DB":
            await self.repo.update_position(action.db_trade.id, action.hl_pos)
            await self.notifier.send_alert(
                f"⚠️ ポジション乖離補正: {action.hl_pos.symbol}"
            )
        elif action.type == "CLOSE_FROM_FILL":
            await self.repo.close_trade(action.db_trade.id, action.fill)
        elif action.type == "MANUAL_REVIEW":
            await self.repo.mark_manual_review(action.db_trade.id)
            await self.notifier.send_alert(
                f"🆘 手動確認要: {action.db_trade.symbol}"
            )
```

**核心の判定ロジックは純関数化（CORE層）：**

```python
# src/core/reconciliation.py
from dataclasses import dataclass


@dataclass(frozen=True)
class ReconcileAction:
    type: str   # 'REGISTER_EXTERNAL' / 'RESUME_MONITORING' /
                # 'CORRECT_DB' / 'CLOSE_FROM_FILL' / 'MANUAL_REVIEW'
    hl_pos: Position | None = None
    db_trade: Trade | None = None
    fill: Fill | None = None


@dataclass(frozen=True)
class ReconcileResult:
    actions: list[ReconcileAction]
    positions_resumed: int
    external_detected: int
    corrections_made: int
    manual_review_needed: int


def reconcile_positions(
    hl_positions: list[Position],
    db_trades: list[Trade],
    hl_fills: list[Fill],
) -> ReconcileResult:
    """純関数：突合の判定だけ行う・I/O一切なし

    入出力が決定的なのでテスト容易。
    """
    actions = []

    # HL側にあるポジションをチェック
    for hl_pos in hl_positions:
        db_match = next(
            (t for t in db_trades if t.symbol == hl_pos.symbol), None
        )

        if db_match is None:
            actions.append(ReconcileAction(
                type="REGISTER_EXTERNAL", hl_pos=hl_pos
            ))
        elif positions_match(db_match, hl_pos):
            actions.append(ReconcileAction(
                type="RESUME_MONITORING", db_trade=db_match, hl_pos=hl_pos
            ))
        else:
            actions.append(ReconcileAction(
                type="CORRECT_DB", db_trade=db_match, hl_pos=hl_pos
            ))

    # DBにあるがHLにないポジション
    for db_trade in db_trades:
        if not any(p.symbol == db_trade.symbol for p in hl_positions):
            fill = find_matching_fill(hl_fills, db_trade)
            if fill:
                actions.append(ReconcileAction(
                    type="CLOSE_FROM_FILL", db_trade=db_trade, fill=fill
                ))
            else:
                actions.append(ReconcileAction(
                    type="MANUAL_REVIEW", db_trade=db_trade
                ))

    return ReconcileResult(
        actions=actions,
        positions_resumed=sum(1 for a in actions if a.type == "RESUME_MONITORING"),
        external_detected=sum(1 for a in actions if a.type == "REGISTER_EXTERNAL"),
        corrections_made=sum(1 for a in actions if a.type == "CORRECT_DB"),
        manual_review_needed=sum(1 for a in actions if a.type == "MANUAL_REVIEW"),
    )
```

**この分離の利点（章11のTDD原則準拠）：**
- 判定ロジック（`reconcile_positions`）は純関数なので**テストが書きやすい**
- 副作用（DB書き込み・通知）は`StateReconciler`に分離
- モック作成不要で大量のテストパターンを書ける

**重要：** 状態復元中は**新規エントリーを禁止**する。復元完了フラグが立つまで章4の判定をスキップ。

### 9.4 WebSocket切断時の対処

```python
class WebSocketManager:
    HEARTBEAT_TIMEOUT = 30           # 30秒応答なしで切断判定
    RECONNECT_MAX_ATTEMPTS = 3       # 再接続3回失敗で諦める
    EMERGENCY_CLOSE_TIMEOUT = 30     # 切断後30秒で強制クローズ

    async def monitor_connection(self):
        while True:
            if self.last_message_at < now() - HEARTBEAT_TIMEOUT:
                await self.handle_disconnect()
            await asyncio.sleep(5)

    async def handle_disconnect(self):
        notify_alert("⚠️ WS切断検出")

        # 再接続試行
        for attempt in range(self.RECONNECT_MAX_ATTEMPTS):
            if await self.try_reconnect():
                notify_signal(f"✅ WS再接続成功 (試行{attempt+1}回)")
                return
            await asyncio.sleep(5 * (2 ** attempt))  # 指数バックオフ

        # 再接続失敗 → 緊急クローズモード
        notify_alert("🆘 WS再接続失敗・全ポジション緊急クローズ開始")
        await self.emergency_close_all()
        await self.shutdown_bot()
```

**判断基準：**
- **5秒未満の切断**：ログのみ・取引継続
- **30秒以内に復旧**：ログ＋通知・取引継続
- **30秒超または再接続失敗**：全クローズ＋BOT停止

理由：監視できないポジションを持つことが最大のリスク。短時間ならスリッページのほうが安く済む。

### 9.5 注文の冪等性設計

二重発注を構造的に防ぐ：

```python
def generate_client_order_id(symbol: str, action: str) -> str:
    """冪等キー生成

    Format: hlbot_{symbol}_{action}_{timestamp_ms}_{random_4digit}
    例: hlbot_BTC_entry_1704067200000_3847

    HyperLiquidは同じclient_order_idの注文を拒否するため、
    リトライ時も同じIDを使えば二重発注にならない
    """
    ts = int(time.time() * 1000)
    nonce = random.randint(1000, 9999)
    return f"hlbot_{symbol}_{action}_{ts}_{nonce}"

async def place_order_with_retry(
    exchange: ExchangeProtocol,    # 依存注入（Protocolベース）
    symbol: str,
    side: str,
    size: float,
    price: float,
    action: str = "entry",
    max_retries: int = 3,
) -> OrderResult:
    """冪等性を保ったリトライ付き注文

    `exchange`はExchangeProtocolなのでテスト時はモック注入可能。
    `infrastructure/hyperliquid_client.py`が実装を提供する。
    """
    client_oid = generate_client_order_id(symbol, action)

    for attempt in range(max_retries):
        try:
            result = await exchange.place_order(
                symbol=symbol,
                side=side,
                size=size,
                price=price,
                client_order_id=client_oid,  # ★毎回同じID
                timeout=10,
            )
            return result

        except DuplicateOrderError:
            # 既に同じIDで注文済み → 状態確認
            existing = await exchange.get_order_by_client_id(client_oid)
            return OrderResult(success=True, order=existing)

        except TimeoutError:
            # タイムアウト時は約定したか不明
            await asyncio.sleep(2)
            existing = await exchange.get_order_by_client_id(client_oid)
            if existing:
                return OrderResult(success=True, order=existing)
            # まだ無いなら同じIDで再送

        except RateLimitError:
            await asyncio.sleep(5 * (2 ** attempt))

    raise OrderFailedError(f"注文失敗: {symbol}")
```

### 9.6 ポジション突合（定期実行）

BOT稼働中も5分ごとに突合し、内部状態とHL状態の乖離を検出。
9.3の`StateReconciler`と同じロジックを再利用する：

```python
# src/application/reconciliation.py（章9.3の続き）

class StateReconciler:
    # ... restore_on_startup() は省略

    async def reconcile_periodic(self) -> None:
        """5分ごとにポジション突合（バックグラウンドタスク）"""
        while True:
            await asyncio.sleep(300)  # 5分

            try:
                hl_positions = await self.exchange.get_positions()
                hl_fills = await self.exchange.get_fills(
                    since=now() - timedelta(hours=1)
                )
                db_open = await self.repo.get_open_trades()

                # 純関数reconcile_positions()を再利用
                result = reconcile_positions(
                    hl_positions=hl_positions,
                    db_trades=db_open,
                    hl_fills=hl_fills,
                )

                if result.actions:
                    await self.notifier.send_alert(
                        f"⚠️ 突合不一致: {len(result.actions)}件"
                    )
                    for action in result.actions:
                        await self._apply_action(action)

            except Exception as e:
                await self.notifier.send_error(f"突合エラー: {e}")
```

**重要：** 起動時の`restore_on_startup()`と定期実行の`reconcile_periodic()`は**同じ純関数`reconcile_positions()`を呼ぶ**ため、ロジックの一貫性が保証される。テストも一度書けば両方カバーできる。


### 9.7 サーキットブレーカー（多層防御）

複数レベルで防御：

```python
class CircuitBreaker:
    """多層サーキットブレーカー"""

    # 既存：日次・週次・連敗（章6）
    # 追加：価格急変・API異常・ポジション異常を検出

    def check_all(self) -> Optional[BreakReason]:
        # Level 1: 日次損失（既存）
        if self.daily_loss_pct < -3.0:
            return BreakReason.DAILY_LOSS

        # Level 2: 週次損失（既存）
        if self.weekly_loss_pct < -8.0:
            return BreakReason.WEEKLY_LOSS

        # Level 3: 連敗（既存）
        if self.consecutive_losses >= 3:
            return BreakReason.CONSECUTIVE_LOSS

        # Level 4: フラッシュクラッシュ検出（新規）
        for symbol in self.watched_symbols:
            change_1min = self.get_1min_change(symbol)
            if abs(change_1min) > 5.0:  # 1分で5%変動
                return BreakReason.FLASH_CRASH

        # Level 5: BTCの異常変動（新規）
        btc_change_5min = self.get_5min_change("BTC")
        if abs(btc_change_5min) > 3.0:
            return BreakReason.BTC_ANOMALY

        # Level 6: APIエラー率（新規）
        if self.api_error_rate_5min > 0.30:  # 30%以上
            return BreakReason.API_INSTABILITY

        # Level 7: 想定外の同時保有数（新規）
        if len(self.positions) > MAX_POSITIONS * 1.5:
            return BreakReason.POSITION_OVERFLOW

        return None
```

**発動時の挙動：**

| 発動レベル | 挙動 | 復帰 |
|---|---|---|
| Level 1-3（損失系） | 全クローズ・当日エントリー禁止 | 翌日UTC 00:00で自動復帰 |
| Level 4（フラッシュクラッシュ） | 該当銘柄のみクローズ・他継続 | 1時間後に該当銘柄も復帰 |
| Level 5（BTC異常） | 全クローズ・全エントリー停止 | 手動復帰のみ |
| Level 6（API異常） | 新規エントリー停止・既存は監視継続 | API回復で自動復帰 |
| Level 7（オーバーフロー） | 全クローズ＋BOT停止 | 手動調査必須 |

### 9.8 起動直後のデータ不足問題

```python
class DataReadinessGate:
    """データ蓄積完了までエントリーを禁止"""

    REQUIRED_5MIN_BARS = 6     # 5本前比モメンタム計算用 + 余裕1本
    REQUIRED_VWAP_MINUTES = 30  # VWAP安定化に最低30分の累積データ

    def is_ready(self, symbol: str) -> tuple[bool, str]:
        # 5分足データ
        bar_count = self.get_5min_bar_count(symbol)
        if bar_count < self.REQUIRED_5MIN_BARS:
            return False, f"5分足データ不足({bar_count}/{self.REQUIRED_5MIN_BARS})"

        # VWAP安定性（UTC始値からの経過時間）
        utc_now = datetime.now(timezone.utc)
        utc_open = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
        if utc_now < utc_open + timedelta(minutes=self.REQUIRED_VWAP_MINUTES):
            return False, "UTC始値後30分未満（VWAP不安定）"

        # 24h関連データ
        if not self.has_24h_high_low(symbol):
            return False, "24h高安データ未取得"

        return True, "OK"
```

**起動シーケンス：**
```
1. BOT起動
2. 状態復元（4.5.3）
3. WebSocket接続・データ蓄積開始
4. DataReadinessGate待機（最大30分）
5. 全銘柄レディ → エントリー判定開始
```

### 9.9 ガス代・入出金障害

HyperLiquidは取引そのものはガス不要だが、入出金時はArbitrum等のガスが必要：

```python
class GasMonitor:
    MIN_GAS_USD = 5.0        # 最低保持額
    GAS_ALERT_USD = 10.0     # アラート閾値

    async def check_periodic(self):
        while True:
            await asyncio.sleep(3600)  # 1時間ごと
            balance = await get_arbitrum_eth_balance()
            balance_usd = balance * await get_eth_price()

            if balance_usd < self.MIN_GAS_USD:
                notify_alert(f"🆘 ガス代枯渇: ${balance_usd:.2f}")
            elif balance_usd < self.GAS_ALERT_USD:
                notify_alert(f"⚠️ ガス代低下: ${balance_usd:.2f}")
```

**重要：** ガス代不足は取引に影響しない（HL内取引はガス不要）。出金できなくなるだけなので緊急度は低い。

### 9.10 Claude API障害時の挙動

```python
async def get_sentiment_with_fallback(symbol: str) -> Optional[SentimentResult]:
    try:
        return await claude_client.analyze(symbol, timeout=15)
    except (TimeoutError, RateLimitError, APIError) as e:
        log_warning(f"Claude API障害: {e}")
        notify_alert(f"⚠️ Claude API障害: 既存ポジは継続、新規エントリー停止")

        # 既存ポジションの監視は継続
        # 新規エントリーは停止（sentinment無しでエントリーはしない）
        return None
```

**センチメント取得失敗時の方針：**
- 新規エントリー：**禁止**（4層ANDを満たせない）
- 既存ポジション：監視継続（決済はsentiment不要）
- 復帰：API応答が戻ったら自動再開

### 9.11 障害ログとインシデント記録

全障害をDB記録し、後で分析可能にする：

```sql
CREATE TABLE incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       DATETIME NOT NULL,
    severity        TEXT NOT NULL,          -- 'INFO' / 'WARN' / 'CRITICAL'
    category        TEXT NOT NULL,          -- 'WS_DISCONNECT' / 'API_ERROR' /
                                            -- 'POSITION_MISMATCH' / 'CIRCUIT_BREAK'
    title           TEXT NOT NULL,
    details         TEXT,                   -- JSON文字列
    affected_symbol TEXT,
    affected_trade_id INTEGER,
    auto_resolved   INTEGER,                -- 1=自動復旧, 0=手動対応必要
    resolved_at     DATETIME,
    resolution_notes TEXT,
    FOREIGN KEY (affected_trade_id) REFERENCES trades(id)
);

CREATE INDEX idx_incidents_severity ON incidents(severity);
CREATE INDEX idx_incidents_category ON incidents(category);
```

これにより月次振り返りで「今月WS切断が10回あった」「ポジション乖離が3件」等の障害傾向を把握できる。

### 9.12 緊急停止コマンド（手動操作）

人間が即座に止める手段を用意：

```python
# scripts/emergency_stop.py
"""
緊急停止スクリプト
使い方: python scripts/emergency_stop.py [--close-all]

--close-all: 全ポジションを成行クローズしてから停止
（指定なし: 新規エントリー停止のみ・既存ポジは保持）
"""

async def emergency_stop(close_all: bool = False):
    # PIDファイルからBOTプロセスにシグナル送信
    pid = read_pid_file()

    if close_all:
        # SIGUSR1: 全クローズ＋停止
        os.kill(pid, signal.SIGUSR1)
        notify_alert("🆘 緊急停止（全クローズ）実行")
    else:
        # SIGUSR2: 新規停止のみ（既存ポジは継続）
        os.kill(pid, signal.SIGUSR2)
        notify_alert("⚠️ 新規エントリー停止")
```

**Discord経由の停止コマンドも実装可能：**
```
!hlbot stop          # 新規停止のみ
!hlbot panic         # 全クローズ＋停止
!hlbot status        # 現状確認
```

### 9.13 障害対応チェックリスト（運用前確認）

実弾運用開始前に以下が全て✅であること：

- [ ] BOT再起動テスト：保有ポジション中に再起動しても正しく復元できるか
- [ ] WS切断テスト：手動でWS切ってから30秒で全クローズ動くか
- [ ] 二重発注テスト：同じclient_order_idで連送して2件入らないか
- [ ] ポジション突合テスト：DBを意図的に壊してから突合で復旧できるか
- [ ] サーキットブレーカーテスト：日次-3%到達で全クローズ・停止するか
- [ ] フラッシュクラッシュテスト：価格5%急変シミュレートで該当銘柄クローズか
- [ ] Claude API障害テスト：APIキーを無効にして既存ポジション監視継続するか
- [ ] 緊急停止テスト：emergency_stop.pyで全クローズできるか
- [ ] DB破損テスト：DBファイル破損時にバックアップから復旧できるか
- [ ] ガス代枯渇テスト：ガス低下時に通知が飛ぶか

### 9.14 既存BOTからの差分

| 項目 | auto-daytrade | moomoo-trader | hl-alpha-bot |
|---|---|---|---|
| 再起動時の状態復元 | なし（毎日新規起動） | 簡易あり | **完全自動復元** |
| WS切断対応 | N/A | あり | **タイムアウト型強制クローズ** |
| 冪等性 | なし | なし | **client_order_idベース** |
| ポジション突合 | なし | 一部あり | **5分ごと自動突合** |
| サーキットブレーカー | 時刻ベースのみ | 損失ベース | **7段階多層防御** |
| 障害ログDB | なし | なし | **incidents テーブル** |
| 緊急停止コマンド | なし | なし | **手動 + Discord** |
| データ準備チェック | なし | なし | **DataReadinessGate** |

### 9.15 PR7.3 / PR7.4 実装で確定した事項

#### 9.15.1 ReconcileAction の実装名（章 9.3 補強）

CORE 層の関数名と type 列挙体は実装で以下に確定：

```python
# src/core/reconciliation.py
class ActionType(StrEnum):
    REGISTER_EXTERNAL = "REGISTER_EXTERNAL"
    RESUME_MONITORING = "RESUME_MONITORING"
    CORRECT_DB        = "CORRECT_DB"
    CLOSE_FROM_FILL   = "CLOSE_FROM_FILL"
    MANUAL_REVIEW     = "MANUAL_REVIEW"
```

Repository 側のメソッド名は **`correct_position`**（仕様書本文の `update_position` は誤り）。
シグネチャ：

```python
async def correct_position(
    self, trade_id: int, actual_size: Decimal, actual_entry: Decimal
) -> None: ...
```

`register_external_position` は新規 trade を作って `int`（trade_id）を返す。

#### 9.15.2 CORE と ADAPTERS の Fill 型差異と復元（章 9.3 補強）

CORE 層の `HLFill` には `closed_pnl` / `fee_usd` がない（突合判定に最小データ）。
APPLICATION 層の `_close_from_fill` で元の ADAPTERS `Fill` を症候的にマッチして取り戻す：

```python
def _find_adapter_fill(
    hl_fill: HLFill, adapter_fills: tuple[Fill, ...]
) -> Fill | None:
    for f in adapter_fills:
        if (f.symbol == hl_fill.symbol
                and f.side == hl_fill.side
                and f.size == hl_fill.size
                and f.price == hl_fill.price
                and f.timestamp_ms == hl_fill.timestamp):
            return f
    return None
```

#### 9.15.3 run_periodic_check の実装（章 9.6 補強）

`restore_on_startup` と `run_periodic_check` は内部で同じ `_reconcile` ヘルパーを呼び、
スイッチ引数で挙動を変える：

```python
async def restore_on_startup(self) -> ReconcileSummary:
    return await self._reconcile(cleanup_enabled=True, notify_completion=True)

async def run_periodic_check(self) -> ReconcileSummary:
    return await self._reconcile(cleanup_enabled=False, notify_completion=False)
```

定期実行モードでは stale order cleanup を行わず、完了 signal も送らない。
差分があった場合のみ alert で通知する。

#### 9.15.4 CircuitBreaker CORE 実装の名前（章 9.7 補強）

CORE 層の実装は以下の名前で確定。仕様書本文と乖離していた箇所の正しい呼称：

| 仕様書本文の名前 | CORE 実装の実体 |
|---|---|
| `CircuitBreakerInputs` | `BreakerInput`（単数形） |
| `check_all_breakers` | `check_circuit_breaker` |
| `result.is_active` | `result.triggered` |
| `result.active_reasons`（複数） | `result.reason`（単一・最初の発動のみ） |

`check_circuit_breaker` は **最初に発動した 1 つだけ**を返す（複数同時発動は表現しない）。
レイヤー優先度は順序で表現される（DAILY_LOSS > WEEKLY_LOSS > CONSECUTIVE_LOSS > ...）。

---

## 10. アカウント・資金管理設計

実装より前に決めるべき最重要項目。
ここでミスると**資金そのものが飛ぶ**ため、コードの設計より優先する。

### 10.1 設計原則

```
原則1: 「BOTのキー漏洩 = 全資金喪失」にしない
       → Agent Wallet（取引のみ・出金不可）を使う

原則2: 「BOTが扱える資金 = 失っても良い額」だけにする
       → SubAccount分離で物理的に上限を設ける

原則3: 「自動出金は実装しない」
       → 資金移動は必ず人間が判断・署名

原則4: 「3層防御で資金を守る」
       → SubAccount制限 → レバレッジ上限 → サーキットブレーカー
```

### 10.2 アカウント構成

HyperLiquidのMaster + SubAccount構造を活用し、**SubAccount分離方式**を採用する。

```
Master Account（人間専用・コールドウォレット連携）
├─ 全資金保管（例: $10,000）
├─ ハードウェアウォレット署名で操作
└─ SubAccountへの資金移動のみ実施
        │
        ▼ 手動移動
SubAccount "hl-alpha-bot"（BOT運用専用）
├─ 運用資金 $2,000のみ
├─ Agent Walletを承認登録
├─ 損失上限を物理的に制限（ここに入れた額しか失わない）
└─ Master側へは手動でのみ戻す
        │
        ▼ Agent承認
Agent Wallet（BOT用・VPS環境変数）
├─ 取引のみ（place_order / cancel_order / query）
├─ 出金不可
└─ 資金移動不可
```

**選択肢比較（採用前検討済み）：**

| パターン | メリット | デメリット | 採用 |
|---|---|---|---|
| Single Master | シンプル | キー漏洩で全損 | ❌ |
| **SubAccount分離** | 損失隔離・段階運用可 | やや複雑 | ✅ |
| 公開Vault運用 | 他人資金預かれる | 運用責任・税務複雑 | ❌ |

### 10.3 キー階層と権限管理

| キー種別 | 権限 | 保管場所 | リスク |
|---|---|---|---|
| **Master Wallet秘密鍵** | 全権限（出金含む） | ハードウェアウォレット + 紙バックアップ | 最高 |
| **API Wallet（Agent）** | 取引のみ・出金不可 | VPS上で sops暗号化 | 中 |
| **Approval Signature** | 一回限りの承認 | 操作時生成・保存しない | 低 |

**Agent Walletの権限設定（必須）：**

```yaml
agent_wallet_permissions:
  allowed:
    - place_order        # 注文発注
    - cancel_order       # 注文キャンセル
    - modify_order       # 注文変更
    - query_positions    # ポジション照会
    - query_orders       # 注文照会
    - query_fills        # 約定照会

  forbidden:
    - withdraw           # 出金（明示的に禁止）
    - transfer_subaccount # サブアカウント間移動
    - approve_agent      # 別Agentの承認
    - update_leverage    # レバレッジ変更（手動設定済みのため）
```

これにより「BOTのキーが漏れても、攻撃者は出金できない」状態になる。

### 10.4 秘密鍵の保管方法

**採用：sops + age 暗号化**

```bash
# ディレクトリ構造
secrets/
├── secrets.enc.yaml      # 暗号化済み（git commit可）
├── .age-key.example      # サンプル（実鍵ではない）
└── .gitignore            # .age-keyを除外

# 実鍵は別管理
~/.config/sops/age/keys.txt   # 復号鍵（gitignore・別途バックアップ）
```

**起動シーケンス：**
```bash
# systemdサービスから起動時に復号
ExecStartPre=/usr/bin/sops -d /opt/hlbot/secrets/secrets.enc.yaml > /tmp/hlbot.env
ExecStart=/usr/bin/env $(cat /tmp/hlbot.env | xargs) python -m src.main
ExecStartPost=/bin/rm /tmp/hlbot.env
```

**比較検討した選択肢：**

| 方法 | セキュリティ | 利便性 | 採否 |
|---|---|---|---|
| .envファイル（平文） | ★ | ★★★ | ❌論外 |
| .env + ファイル権限600 | ★★ | ★★★ | △最低限 |
| 環境変数（systemd） | ★★ | ★★ | ◯バックアップ用 |
| **sops + age** | ★★★ | ★★ | ✅ **採用** |
| AWS Secrets Manager | ★★★★ | ★★ | ◯クラウド時 |
| HashiCorp Vault | ★★★★★ | ★ | △オーバースペック |

**sops採用の理由：**
- gitに暗号化済みファイルをcommitできる（バックアップになる）
- 復号鍵だけ別管理すれば良い
- AWS依存なし・無料
- VPS盗難時も復号鍵がなければ無意味

### 10.5 資金移動・出金ポリシー

#### HyperLiquid mainnet の最低入金額（重要・実機検証済み）

```
HL mainnet の最低入金額: 5 USDC
- これより少ないと入金 UI で拒否される
- アカウント活性化の最小コスト
- testnet も同様（mock USDC で）
```

testnet で取引するには、まず **mainnet で 5 USDC 入金してアカウントを活性化**する必要がある（章22.12 参照）。これは Sybil攻撃対策で、testnet Faucet の利用条件にも mainnet 活性化が含まれる。

#### 入金（Master → SubAccount）

```
頻度: 月1回 or 必要時のみ
方法: 手動（HyperLiquid UI から）
金額: 運用想定額のみ（過剰入金しない）
記録: deposits_withdrawals テーブル（章8.6）
```

#### 出金（SubAccount → Master）

```
頻度: 利益が積み上がった時のみ
方法: 完全手動（BOTには出金権限なし）
タイミング: 月初に前月利益を引き上げ
記録: deposits_withdrawals テーブル
```

#### 自動引き上げ（Sweep）

**実装しない。** Agent Walletに出金権限がないため技術的にも不可能。

代わりに**月次引き上げ推奨通知**を実装：

```python
def monthly_sweep_recommendation():
    """月次サマリーで引き上げ推奨額を通知"""
    initial = INITIAL_FUNDING_USD
    current = get_subaccount_balance()
    profit = current - initial

    if profit > initial * 0.20:  # 20%超の利益
        notify_summary(
            f"💰 月次引き上げ推奨\n"
            f"初期資金: ${initial:,.0f}\n"
            f"現在残高: ${current:,.0f}\n"
            f"利益: ${profit:+,.0f} (+{profit/initial*100:.1f}%)\n"
            f"推奨アクション: ${profit:.0f} を Master Account へ移動"
        )
```

**重要：** 自動出金は実装しない。資金移動は必ず人間が判断。

### 10.6 段階的資金投入計画

| Phase | 投入額 | 目的 |
|---|---|---|
| Phase 0 | $0 | データ収集のみ（取引なし） |
| Phase 1 | $0 | ドライラン（仮想取引） |
| **Phase 2** | **$200〜500** | 実装バグ発見・最小サイズ実弾 |
| **Phase 3** | **$1,000〜2,000** | フルサイズ運用 |
| **Phase 4** | **$2,000〜5,000** | 安定運用後の増資 |

**設計原則：**
- いきなり大きく入れない
- Phase 2は「実装バグの発見が目的」なので少額でOK
- Phase 4でも最大$5,000程度
- **「無くなっても生活に影響しない額」を厳守**

### 10.7 増資・減資のルール

| 条件 | アクション |
|---|---|
| 月次PF > 1.5（3ヶ月連続） | 投入額を1.5倍まで増やしてOK |
| 月次PF 1.0〜1.5 | 現状維持 |
| 月次PF < 1.0（2ヶ月連続） | 投入額を半減 |
| 月次PF < 0.5 | 全額引き上げ・原因究明 |
| 大きな仕様変更後 | Phase 2に戻す |

**自動判定の実装案：**

```python
def monthly_capital_review():
    """月初にPF判定して通知（実行は手動）"""
    last_3months_pf = calculate_pf(months=3)
    last_month_pf = calculate_pf(months=1)

    if last_3months_pf > 1.5 and last_month_pf > 1.0:
        notify_summary("📈 増資検討可: 過去3ヶ月PF=1.5超")
    elif last_month_pf < 1.0:
        consecutive = count_consecutive_negative_months()
        if consecutive >= 2:
            notify_alert(f"⚠️ 減資検討: 直近{consecutive}ヶ月PF<1.0")
    elif last_month_pf < 0.5:
        notify_alert("🆘 全額引き上げ推奨: 月次PF<0.5")
```

### 10.8 リスク限界の3層物理防御

ソフトウェアロジックだけでなく、**口座構造で物理的にリスクを制限**する：

```
┌──────────────────────────────────────────┐
│ Layer 1: SubAccount入金額制限             │
│   入金額 = $2,000                        │
│   → どんなにBOTがバグっても$2,000で止まる │
└────────────────┬─────────────────────────┘
                 │
┌────────────────▼─────────────────────────┐
│ Layer 2: レバレッジ上限（HL設定）          │
│   設定: 3倍                              │
│   → 最大エクスポージャ $6,000             │
│   → 全清算でも$2,000損失で止まる          │
└────────────────┬─────────────────────────┘
                 │
┌────────────────▼─────────────────────────┐
│ Layer 3: BOTサーキットブレーカー（章9.7）│
│   日次-3% / 週次-8% / 連敗3回             │
│   → 早期警告システム                      │
└──────────────────────────────────────────┘
```

**3層で守る思想：** 何かが破綻しても、最後はLayer 1で止まる。
ソフトウェアバグでLayer 3が機能しなくても、Layer 1の物理制約は破れない。

### 10.9 入出金経路（オンチェーン）

HyperLiquidへの資金移動はArbitrum経由のUSDCが基本：

```
日本の取引所（bitFlyer / GMOコイン等）
  ↓ JPY → USDT/USDC
海外取引所（Bybit / Binance等）
  ↓ Withdraw to Arbitrum
Arbitrum Wallet（自分のEOA）
  ↓ Bridge to HyperLiquid
HyperLiquid Master Account
  ↓ Internal transfer (gas不要)
SubAccount "hl-alpha-bot"
```

**各段階の記録（deposits_withdrawals テーブル・章8.6）：**

| ステップ | 記録項目 | 課税 |
|---|---|---|
| JPY → USDT/USDC | 取得時USD/JPY・購入手数料 | 取得価額 |
| Bybit → Arbitrum | 出金手数料・gas | 経費 |
| Arbitrum → HyperLiquid | bridge手数料・gas | 経費 |
| Master → Sub | 内部振替（gas不要・記録のみ） | 課税対象外 |

**重要：** Master⇔Sub間は**同一所有者の振替**のため課税対象外。
ただし全段階を`deposits_withdrawals`テーブルに記録し、税務調査時に正当性を示せるようにする。

### 10.10 キー紛失・盗難時の対応

#### 想定シナリオと対処

| シナリオ | 対処 | 緊急度 |
|---|---|---|
| **Agent秘密鍵の漏洩** | Master側からAgent revoke・新Agentを作成 | 🟡中 |
| **VPS盗難** | sops復号鍵を変更・新VPSへ移行 | 🟡中 |
| **Master秘密鍵の漏洩** | 即座に全資金を新Walletへ移動・旧Wallet放棄 | 🔴最高 |
| **2FA端末紛失** | バックアップコードで復旧 | 🟡中 |
| **BOTの暴走（バグ）** | 緊急停止コマンド（章9.12）で全クローズ | 🔴最高 |
| **HyperLiquid自体の障害** | 全クローズ後にBOT停止・公式情報待ち | 🔴最高 |

#### 事前準備チェックリスト

実弾運用開始前に以下を全て✅にする：

- [ ] Master秘密鍵のシードフレーズを物理紙で2箇所別保管（金庫等）
- [ ] sops復号鍵のバックアップを2箇所に分散
- [ ] Agent revokeの手順書を作成（操作手順・所要時間）
- [ ] 緊急時連絡用Discord webhook URLも別保管
- [ ] VPS再構築手順を文書化（Ansible/Terraform推奨）
- [ ] Agentキーローテーション手順を文書化
- [ ] 事業継続計画（BCP）：1日停止しても問題ないか
- [ ] 緊急連絡先リスト（家族・税理士・弁護士）

### 10.11 キーローテーション計画

定期的なキー更新でリスクを低減：

| キー | ローテーション頻度 | 手順 |
|---|---|---|
| Agent Wallet | **3ヶ月ごと**または漏洩疑い時 | 4.6.12参照 |
| sops復号鍵 | 6ヶ月ごと | 全secretsを新鍵で再暗号化 |
| Master Wallet | 原則永続 | 漏洩時のみ全資金移動して移行 |
| Discord Webhook | 1年ごと | URL再生成・設定更新 |

### 10.12 Agent Wallet ローテーション手順

```
1. 新Agent Walletを生成
2. Master側で新Agentを承認（旧Agentと併存可能）
3. BOTを停止
4. sopsで新Agent秘密鍵を暗号化・配置
5. 動作確認（テスト注文）
6. 24時間問題なければ旧Agent revoke
7. ローテーション履歴をaudit_logに記録
```

### 10.13 監査・ログトレーサビリティ

「いつ・誰が・何を」を全て追えるようにする：

```sql
CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       DATETIME NOT NULL,
    actor           TEXT NOT NULL,          -- 'BOT' / 'HUMAN_VIA_UI' /
                                            -- 'HUMAN_VIA_CLI' / 'HUMAN_VIA_DISCORD'
    action          TEXT NOT NULL,          -- 'DEPOSIT' / 'WITHDRAW' /
                                            -- 'CONFIG_CHANGE' / 'EMERGENCY_STOP' /
                                            -- 'KEY_ROTATION' / 'AGENT_APPROVED'
    details         TEXT,                   -- JSON
    ip_address      TEXT,                   -- 操作元IP（人間操作時）
    user_agent      TEXT,
    notes           TEXT
);

CREATE INDEX idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX idx_audit_action ON audit_log(action);
```

これで「先月の3万ドル損失は何だったか」を時系列で追える。
税務調査が来た時にも有用。

### 10.14 推奨設定値まとめ

```yaml
# config/account_settings.yaml
account_structure:
  master_account:
    role: "資金保管・人間専用"
    initial_balance_usd: 10000
    key_storage: "ハードウェアウォレット + シードフレーズ物理保管"

  sub_account:
    name: "hl-alpha-bot"
    role: "BOT運用・損失隔離"
    initial_funding_usd: 2000
    max_funding_usd: 5000
    leverage_limit: 3

  agent_wallet:
    role: "BOT取引専用"
    permissions:
      - place_order
      - cancel_order
      - modify_order
      - query
    forbidden:
      - withdraw
      - transfer
      - approve
    key_storage: "VPS上で sops + age 暗号化"
    rotation_months: 3

risk_limits:
  layer1_physical_usd: 2000
  layer2_max_leverage: 3
  layer3_circuit_breaker:
    daily_loss_pct: 3.0
    weekly_loss_pct: 8.0
    consecutive_loss_count: 3

deployment_phases:
  phase_2_funding: [200, 500]
  phase_3_funding: [1000, 2000]
  phase_4_funding: [2000, 5000]

withdrawal_policy:
  automatic: false
  manual_frequency: "monthly"
  recommendation_threshold_pct: 20
  recommendation_channel: "summary_discord"

monitoring:
  reconciliation_interval_minutes: 5
  audit_log: true
  monthly_capital_review: true
```

### 10.15 既存BOTからの差分

| 項目 | auto-daytrade | moomoo-trader | hl-alpha-bot |
|---|---|---|---|
| アカウント分離 | なし（証券口座1つ） | なし | **Master + SubAccount** |
| 取引専用キー | なし | なし | **Agent Wallet（出金不可）** |
| 秘密鍵管理 | .env平文 | .env平文 | **sops + age暗号化** |
| 物理リスク制限 | 投入額のみ | 投入額のみ | **3層防御** |
| 自動出金 | N/A | N/A | **実装しない（推奨通知のみ）** |
| キーローテーション | なし | なし | **3ヶ月ごと** |
| 監査ログ | なし | なし | **audit_log テーブル** |
| 段階投入 | なし | なし | **Phase別$200→$5000** |
| BCP | なし | なし | **チェックリスト整備** |

---

## 11. TDDアーキテクチャと実装方針

実装はTest-Driven Development（TDD）で進める。
シンプルさとテスタビリティを最優先し、章7〜4.6で設計した機能を3層アーキテクチャに整理する。

### 11.1 設計原則

```
原則1: COREは100%純関数・100%テスト必須
       → ビジネスロジックは外部I/Oに依存させない

原則2: 外部I/OはすべてProtocol/ABC経由
       → モック化を構造的に保証

原則3: ビジネスロジックとI/Oを混ぜない
       → 関数1つに「判定」と「DB書き込み」を同居させない

原則4: 1機能 = 1テストファイル（並走可能）
       → テストの実行が速い・並列化できる

原則5: 「動く小さなものを積み上げる」
       → 一気に全部作らず、コアから外側へ
```

### 11.2 3層アーキテクチャ

```
┌──────────────────────────────────────────────────┐
│  CORE（純粋ロジック・依存なし・100%テスト可）       │
│  - entry_judge.py        4層AND判定               │
│  - price_context.py      過熱フィルター（章5）   │
│  - vwap.py               VWAP計算+Tracker（章6） │
│  - position_sizer.py     サイズ計算                │
│  - stop_loss.py          SL/TP計算（ATRベース）    │
│  - circuit_breaker.py    7段階防御判定（章9.7）  │
│  - models.py             MarketSnapshot等          │
└─────────────────┬────────────────────────────────┘
                  │ 呼び出すだけ
                  ▼
┌──────────────────────────────────────────────────┐
│  ADAPTERS（外部I/Oインターフェース定義のみ）        │
│  - exchange.py           Protocol: 取引所操作      │
│  - sentiment.py          Protocol: Sentiment取得   │
│  - repository.py         Protocol: DB操作          │
│  - notifier.py           Protocol: 通知            │
└─────────────────┬────────────────────────────────┘
                  │ 実装される
                  ▼
┌──────────────────────────────────────────────────┐
│  INFRASTRUCTURE（実装・テスト時はモック）           │
│  - hyperliquid_client.py  HL SDK実装               │
│  - claude_provider.py     Claude API実装           │
│  - sqlite_repo.py         SQLite実装               │
│  - discord_notifier.py    Discord webhook実装      │
└──────────────────────────────────────────────────┘
                  ▲
                  │ 組み立て
                  │
┌──────────────────────────────────────────────────┐
│  APPLICATION（ユースケース層・薄い）                │
│  - entry_flow.py         エントリー判定→発注        │
│  - position_monitor.py   ポジション監視             │
│  - reconciliation.py     状態復元・突合（章9.3）  │
└──────────────────────────────────────────────────┘
```

**依存方向の絶対ルール：**
- CORE は何にも依存しない（標準ライブラリのみ）
- ADAPTERSはCOREのみに依存
- APPLICATIONはCOREとADAPTERSに依存
- INFRASTRUCTUREはADAPTERSに依存（実装する側）
- **逆方向の依存は絶対禁止**

### 11.3 ディレクトリ構成（簡素版）

章10の構成を本章で更新。**6ディレクトリのみ**にする。

```
hl-alpha-bot/
├── src/
│   ├── core/                    # 純粋ロジック（100%テスト）
│   │   ├── __init__.py
│   │   ├── models.py            # MarketSnapshot, EntryDecision等
│   │   ├── entry_judge.py       # 4層AND判定
│   │   ├── price_context.py     # 章5の過熱フィルター
│   │   ├── vwap.py              # 章6のVWAP計算+Tracker
│   │   ├── position_sizer.py    # サイズ計算
│   │   ├── stop_loss.py         # ATR SL/TP計算
│   │   └── circuit_breaker.py   # 章9.7の7段階防御
│   │
│   ├── adapters/                # Protocol定義のみ
│   │   ├── __init__.py
│   │   ├── exchange.py          # ExchangeProtocol
│   │   ├── sentiment.py         # SentimentProvider
│   │   ├── repository.py        # Repository
│   │   └── notifier.py          # Notifier
│   │
│   ├── infrastructure/          # 実装
│   │   ├── __init__.py
│   │   ├── hyperliquid_client.py
│   │   ├── claude_provider.py
│   │   ├── sqlite_repo.py
│   │   └── discord_notifier.py
│   │
│   ├── application/             # ユースケース（薄く）
│   │   ├── __init__.py
│   │   ├── entry_flow.py
│   │   ├── position_monitor.py
│   │   └── reconciliation.py
│   │
│   └── main.py                  # 組み立てのみ
│
├── tests/
│   ├── core/                    # 純関数テスト（高速・大量）
│   ├── adapters/                # Protocol準拠テスト
│   ├── application/             # 統合テスト（モック使用）
│   ├── e2e/                     # testnetでの疎通テスト
│   └── fixtures/                # ゴールデンテスト用データ
│
├── scripts/                     # 補助スクリプト
├── data/                        # SQLite + エクスポート
├── logs/
└── secrets/
```

**6ディレクトリのみ：** core / adapters / infrastructure / application / scripts / tests
これ以上は分けない。

### 11.4 CORE層の具体例：entry_judge

```python
# src/core/models.py
from dataclasses import dataclass

@dataclass(frozen=True)
class MarketSnapshot:
    """エントリー判定に必要な全データを束ねたDTO

    このオブジェクトさえあれば判定可能。
    外部I/Oは事前に済ませてここに集約する。
    """
    symbol: str
    current_price: float

    # VWAP（章6）
    vwap: float

    # モメンタム
    momentum_5bar_pct: float

    # 章5 価格基準3点
    utc_open_price: float
    rolling_24h_open: float
    high_24h: float
    low_24h: float

    # フロー
    flow_buy_sell_ratio: float
    flow_large_order_count: int
    volume_surge_ratio: float

    # センチメント
    sentiment_score: float
    sentiment_confidence: float
    sentiment_flags: dict

    # レジーム
    btc_ema_trend: str
    funding_rate: float
    liquidation_above_usd: float
    liquidation_below_usd: float

    @property
    def vwap_distance_pct(self) -> float:
        return (self.current_price - self.vwap) / self.vwap * 100

    @property
    def utc_day_change_pct(self) -> float:
        return (self.current_price - self.utc_open_price) / self.utc_open_price

    @property
    def rolling_24h_change_pct(self) -> float:
        return (self.current_price - self.rolling_24h_open) / self.rolling_24h_open

    @property
    def position_in_24h_range(self) -> float:
        if self.high_24h == self.low_24h:
            return 0.5
        return (self.current_price - self.low_24h) / (self.high_24h - self.low_24h)

@dataclass(frozen=True)
class EntryDecision:
    """判定結果"""
    should_enter: bool
    direction: str | None       # 'LONG' / 'SHORT' / None
    rejection_reason: str | None
    layer_results: dict[str, bool]
```

```python
# src/core/entry_judge.py
from .models import MarketSnapshot, EntryDecision

def judge_long_entry(snap: MarketSnapshot) -> EntryDecision:
    """LONGエントリー判定（純関数・I/O一切なし）"""
    layers = {
        "momentum": _check_momentum_long(snap),
        "flow": _check_flow_long(snap),
        "sentiment": _check_sentiment_long(snap),
        "regime": _check_regime_long(snap),
    }

    if all(layers.values()):
        return EntryDecision(
            should_enter=True,
            direction="LONG",
            rejection_reason=None,
            layer_results=layers,
        )

    failed = next(k for k, v in layers.items() if not v)
    return EntryDecision(
        should_enter=False,
        direction=None,
        rejection_reason=f"layer_{failed}_failed",
        layer_results=layers,
    )

def _check_momentum_long(snap: MarketSnapshot) -> bool:
    """章4の①MOMENTUM + POSITION（LONG）"""
    return (
        0 < snap.vwap_distance_pct < 0.5
        and snap.utc_day_change_pct < 0.05
        and snap.rolling_24h_change_pct < 0.10
        and snap.position_in_24h_range < 0.85
        and snap.momentum_5bar_pct > 0.3
    )

def _check_flow_long(snap: MarketSnapshot) -> bool:
    """章4の②FLOW（LONG）

    注: WS trades 実装まで暫定的に flow_layer_enabled=false で
    呼び出し側がスキップする想定（章11.6.3 / 章23）。
    """
    return (
        snap.flow_buy_sell_ratio > 1.5
        and snap.flow_large_order_count > 0
        and snap.volume_surge_ratio > 1.5
    )

def _check_sentiment_long(snap: MarketSnapshot) -> bool:
    """章7の③SENTIMENT（LONG）"""
    return (
        snap.sentiment_score > 0.6
        and snap.sentiment_confidence > 0.7
        and not snap.sentiment_flags.get("has_hack", False)
        and not snap.sentiment_flags.get("has_regulation", False)
    )

def _check_regime_long(snap: MarketSnapshot) -> bool:
    """章4の④REGIME + LIQUIDATION（LONG）"""
    return (
        snap.btc_ema_trend == "UPTREND"
        and snap.funding_rate < 0.01
        and snap.liquidation_above_usd > snap.liquidation_below_usd
    )

# SHORT用は対称的に_check_*_short関数を実装
```

**この設計のポイント：**
- 引数はMarketSnapshot 1つだけ
- 戻り値はEntryDecision 1つだけ
- I/Oは一切なし（async/awaitもなし）
- グローバル変数なし
- 純粋関数なので**何度呼んでも同じ結果**

### 11.5 ADAPTERS層の具体例：Protocol定義

```python
# src/adapters/exchange.py
from typing import Protocol
from decimal import Decimal

class ExchangeProtocol(Protocol):
    """取引所操作のインターフェース

    実装はinfrastructure/hyperliquid_client.pyで提供。
    テスト時はモックで差し替え可能。
    """

    async def get_positions(self) -> list[Position]: ...
    async def get_open_orders(self) -> list[Order]: ...
    async def get_market_snapshot(self, symbol: str) -> dict: ...

    async def place_order(
        self,
        symbol: str,
        side: str,
        size: Decimal,
        price: Decimal | None,
        client_order_id: str,
        order_type: str,
        post_only: bool = True,
    ) -> OrderResult: ...

    async def cancel_order(self, order_id: str) -> bool: ...
```

```python
# src/adapters/sentiment.py
from typing import Protocol

class SentimentProvider(Protocol):
    async def analyze(
        self,
        symbol: str,
        texts: list[str],
    ) -> SentimentResult: ...
```

**Protocolを使う理由：**
- Pythonのstructural typing活用
- 継承不要（duck typingベース）
- テスト時はMagicMock等で簡単差し替え

### 11.6 APPLICATION層の具体例：entry_flow

ユースケース層は**薄く**書く。判定ロジックはCOREに任せる。

```python
# src/application/entry_flow.py
from dataclasses import dataclass
from src.core.entry_judge import judge_long_entry, judge_short_entry
from src.core.models import MarketSnapshot
from src.adapters.exchange import ExchangeProtocol
from src.adapters.sentiment import SentimentProvider
from src.adapters.repository import Repository
from src.adapters.notifier import Notifier

@dataclass
class EntryFlow:
    """エントリー判定→発注のユースケース

    依存はProtocolのみ。実装は注入される。
    """
    exchange: ExchangeProtocol
    sentiment: SentimentProvider
    repo: Repository
    notifier: Notifier

    async def evaluate_and_enter(self, symbol: str) -> None:
        # 1. データ収集（I/O）
        snap = await self._build_snapshot(symbol)

        # 2. 判定（純粋ロジック・CORE）
        long_decision = judge_long_entry(snap)
        short_decision = judge_short_entry(snap)

        # 3. ロギング（I/O）
        await self.repo.log_signal(snap, long_decision, short_decision)

        # 4. 発注（I/O）
        if long_decision.should_enter:
            await self._place_long(symbol, snap)
        elif short_decision.should_enter:
            await self._place_short(symbol, snap)

    async def _build_snapshot(self, symbol: str) -> MarketSnapshot:
        """各ソースから情報を集めてSnapshotを組み立てる

        【責務分担】
        Exchange (ExchangeProtocol.get_market_snapshot) で埋まるフィールド：
          - symbol, current_price, vwap
          - utc_open_price, rolling_24h_open
          - low_24h, high_24h
          - momentum_5bar_pct
          - volume_5min_recent, volume_5min_avg_20bars
          - flow_buy_sell_ratio（WS trades 実装後）★
          - flow_large_order_count（WS trades 実装後）★

        APPLICATION層で埋めるフィールド（Exchange 単独では取れない）：
          - sentiment_score, sentiment_confidence ← SentimentProvider
          - btc_ema_trend, btc_atr_pct ← BTCのMarketSnapshotから別途集計
          - open_interest_1h_ago ← Repository（OI履歴）から取得
          - open_interest（現在値）← Exchange.get_open_interest

        ★ WS trades 未実装フェーズでは flow_layer_enabled=False で
        FLOW判定をbypass（章23参照）。
        """
        # Exchange 由来のフィールド
        market = await self.exchange.get_market_snapshot(symbol)

        # SENTIMENT 集約
        sentiment = await self.sentiment.judge_cached_or_fresh(
            symbol=symbol, direction="LONG"
        )

        # BTC レジーム情報（別 snapshot から導出）
        btc_snap = await self.exchange.get_market_snapshot("BTC")
        btc_ema_trend = self._calc_btc_ema_trend(btc_snap)  # 簡易実装（後述）
        btc_atr_pct = self._calc_btc_atr_pct(btc_snap)

        # OI履歴（Repository 経由）
        oi_now = await self.exchange.get_open_interest(symbol)
        oi_1h_ago = await self.repo.get_oi_at(
            symbol, now() - timedelta(hours=1)
        )

        # MarketSnapshot を「APPLICATION層で完成」させる
        return market.with_sentiment_and_regime(
            sentiment_score=sentiment.score,
            sentiment_confidence=sentiment.confidence,
            btc_ema_trend=btc_ema_trend,
            btc_atr_pct=btc_atr_pct,
            open_interest=oi_now,
            open_interest_1h_ago=oi_1h_ago or oi_now,
        )

    @staticmethod
    def _calc_btc_ema_trend(btc_snap: MarketSnapshot) -> bool:
        """BTC EMA20 > EMA50 の判定（章4 ④REGIME）

        【PR7.1 簡易実装】
        rolling_24h_open との比較で「24h 上昇傾向」を代理指標とする。
        - 軽量・追加API呼び出し不要
        - 精度は粗い

        【後続PRで精緻化】
        CORE 層に純関数 calculate_ema(prices, period) を追加し、
        ローソク足から EMA20 と EMA50 を実際に計算する：

            from src.core.indicators import calculate_ema
            candles = await exchange._fetch_recent_candles("BTC", "15m", 50)
            closes = [c["c"] for c in candles]
            ema20 = calculate_ema(closes, 20)
            ema50 = calculate_ema(closes, 50)
            return ema20 > ema50
        """
        return btc_snap.current_price > btc_snap.rolling_24h_open

    @staticmethod
    def _calc_btc_atr_pct(btc_snap: MarketSnapshot) -> float:
        """BTC ATR% の判定（章4 ④REGIME）

        【PR7.1 簡易実装】
        24h 高安幅を ATR の代理として使う。
        - 軽量・即時計算可能
        - 真の ATR ではない

        【後続PRで精緻化】
        CORE 層に純関数 calculate_atr(highs, lows, closes, period) を追加：

            from src.core.indicators import calculate_atr
            candles = await exchange._fetch_recent_candles("BTC", "15m", 14)
            atr = calculate_atr(
                highs=[c["h"] for c in candles],
                lows=[c["l"] for c in candles],
                closes=[c["c"] for c in candles],
                period=14,
            )
            return atr / btc_snap.current_price
        """
        if btc_snap.current_price == 0:
            return 0.0
        return (btc_snap.high_24h - btc_snap.low_24h) / btc_snap.current_price
```

**この設計のポイント：**
- ロジックは`judge_*_entry`に丸投げ
- I/Oはアダプター経由
- テスト時は4つのアダプターをモック化するだけ
- `MarketSnapshot.with_sentiment_and_regime()` のような「部分上書き」メソッドで責務を明示化

### 11.6.3 FLOWシグナルの実装方針（重要）

**問題：** 章4の②FLOW判定（`flow_buy_sell_ratio > 1.5` AND `flow_large_order_count > 0`）は、HyperLiquid REST API では実装不可。**WebSocket trades チャンネルの集計が必須**。

**段階的実装方針：**

```
Phase A（PR6.2 完了時点）: FLOW 層 bypass
  - settings.yaml: trading.long.flow_layer_enabled: false
  - entry_judge は flow_layer_enabled=false の時 ②をスキップ
  - 暫定実装で誤判定するより、明示的に飛ばす方が安全

Phase B（PR6.5 想定・WS trades 実装後）: FLOW 層 enable
  - WebSocketで trades をストリーム購読
  - 直近5分の約定を集計 → flow_buy_sell_ratio / flow_large_order_count
  - settings.yaml: trading.long.flow_layer_enabled: true
  - Phase 0 でドライラン → 閾値検証
```

**判定スキップの実装（PR7.1 で確定・APPLICATION 層）：**

CORE 層の `judge_long_entry` は config を受け取らない純関数のままにする。
代わりに **APPLICATION 層 (entry_flow.py) で bypass を行う**：

```python
# src/application/entry_flow.py
class EntryFlow:
    def _judge(self, snap, direction) -> EntryDecision:
        # CORE層の純関数（config非依存）
        if direction == "LONG":
            decision = judge_long_entry(snap)
        else:
            decision = judge_short_entry(snap)

        # APPLICATION 層で bypass
        if not self.config.flow_layer_enabled:
            decision = self._bypass_flow(decision)

        return decision

    @staticmethod
    def _bypass_flow(decision: EntryDecision) -> EntryDecision:
        """layer_results.flow を True 上書き → should_enter 再計算"""
        new_layer_results = dict(decision.layer_results)
        new_layer_results["flow"] = True
        all_pass = all(new_layer_results.values())
        new_rejection = decision.rejection_reason
        if new_rejection and "flow" in new_rejection.lower():
            new_rejection = None
        return replace(
            decision,
            should_enter=all_pass,
            layer_results=new_layer_results,
            rejection_reason=new_rejection,
        )
```

**設計判断：** CORE 層は純関数原則（config非依存）を維持し、
動的な bypass は呼び出し側 (APPLICATION) に集約する。
これにより：
- CORE層のテストは config 不要で通せる
- bypass の有効/無効切替は config 設定の変更だけで済む
- 将来 bypass 条件を増やす時も CORE 層を変更不要

**WS trades 実装の概要（PR6.5 想定）：**

```python
# src/infrastructure/hyperliquid_ws.py
class HyperLiquidWebSocket:
    """WebSocket trades 集計

    直近5分の rolling window で約定を保持し、
    flow_buy_sell_ratio と大口count を算出する。
    """
    async def subscribe_trades(self, symbol: str) -> None:
        # WSサブスクライブ → 5分rolling window更新

    def get_flow_metrics(self, symbol: str) -> FlowMetrics:
        # buy_usd_5min / sell_usd_5min / large_buy_count / large_sell_count
```

これは章22.8 の WebSocket仕様に沿って実装する。

### 11.7 TDDワークフロー

#### Red-Green-Refactorサイクル

```
1. Red: 失敗するテストを書く
   tests/core/test_entry_judge.py

   def test_long_passes_when_all_conditions_met():
       snap = make_snapshot()  # ヘルパー
       decision = judge_long_entry(snap)
       assert decision.should_enter is True

2. Green: 最小限の実装でテストを通す
   src/core/entry_judge.py

   def judge_long_entry(snap):
       return EntryDecision(should_enter=True, ...)

3. Red: 次のテスト
   def test_fails_when_below_vwap():
       snap = make_snapshot(current_price=99, vwap=100)
       assert judge_long_entry(snap).should_enter is False

4. Green: 失敗ケースに対応
   def judge_long_entry(snap):
       if snap.vwap_distance_pct <= 0:
           return EntryDecision(should_enter=False, ...)
       return EntryDecision(should_enter=True, ...)

5. Refactor: 構造を整える（テストは緑のまま）

6. 繰り返し
```

#### テストヘルパーの作成（最初に必須）

```python
# tests/core/helpers.py
from src.core.models import MarketSnapshot

def make_snapshot(**overrides) -> MarketSnapshot:
    """全フィールドにデフォルト値を持つSnapshot生成ヘルパー

    テストでは「変えたい部分だけ」指定すれば良い。
    """
    defaults = dict(
        symbol="BTC",
        current_price=100.0,
        vwap=99.8,
        momentum_5bar_pct=0.5,
        utc_open_price=98.0,
        rolling_24h_open=97.0,
        high_24h=101.0,
        low_24h=96.0,
        flow_buy_sell_ratio=2.0,
        flow_large_order_count=3,
        volume_surge_ratio=1.8,
        sentiment_score=0.7,
        sentiment_confidence=0.8,
        sentiment_flags={},
        btc_ema_trend="UPTREND",
        funding_rate=0.005,
        liquidation_above_usd=1_000_000,
        liquidation_below_usd=500_000,
    )
    return MarketSnapshot(**{**defaults, **overrides})
```

このヘルパーを書けば、各テストは1行になる：
```python
def test_xxx():
    snap = make_snapshot(sentiment_score=0.3)
    assert judge_long_entry(snap).should_enter is False
```

### 11.8 効くテストパターン3種

#### パターン1: パラメトリックテスト（境界値網羅）

```python
@pytest.mark.parametrize("vwap_distance,expected", [
    (-0.5, False),  # VWAP下
    (0.0, False),   # VWAP同値
    (0.1, True),    # 通常範囲
    (0.49, True),   # 上限ギリギリ
    (0.5, False),   # 上限超え
    (1.0, False),   # 大きく逸脱
])
def test_vwap_distance_thresholds(vwap_distance, expected):
    snap = make_snapshot(
        current_price=100.0 * (1 + vwap_distance / 100),
        vwap=100.0,
    )
    assert judge_long_entry(snap).should_enter == expected
```

#### パターン2: プロパティベーステスト（hypothesis）

```python
from hypothesis import given, strategies as st

@given(
    score=st.floats(min_value=-1, max_value=1, allow_nan=False),
    confidence=st.floats(min_value=0, max_value=1, allow_nan=False),
)
def test_judgment_is_total_function(score, confidence):
    """どんな入力でも例外を出さない（全域定義）"""
    snap = make_snapshot(
        sentiment_score=score,
        sentiment_confidence=confidence,
    )
    decision = judge_long_entry(snap)  # 例外出ないこと
    assert isinstance(decision.should_enter, bool)
```

#### パターン3: ゴールデンテスト（実データ回帰）

```python
def test_real_market_data_regression():
    """過去の実データで判定が変わらないことを保証"""
    fixtures = load_json("tests/fixtures/btc_2026_01_signals.json")

    for case in fixtures:
        snap = MarketSnapshot(**case["snapshot"])
        decision = judge_long_entry(snap)
        assert decision.should_enter == case["expected_enter"], \
            f"Regression at {case['timestamp']}"
```

### 11.9 統合テスト戦略（モック使用）

APPLICATION層のテストは**4つのアダプターを全部モック化**する：

```python
# tests/application/test_entry_flow.py
import pytest
from unittest.mock import AsyncMock
from src.application.entry_flow import EntryFlow

@pytest.fixture
def mock_dependencies():
    return {
        "exchange": AsyncMock(),
        "sentiment": AsyncMock(),
        "repo": AsyncMock(),
        "notifier": AsyncMock(),
    }

@pytest.mark.asyncio
async def test_places_order_when_judgment_passes(mock_dependencies):
    # Arrange: モックが返す値を設定
    mock_dependencies["exchange"].get_market_snapshot.return_value = {...}
    mock_dependencies["sentiment"].analyze.return_value = SentimentResult(
        score=0.7, confidence=0.8
    )

    flow = EntryFlow(**mock_dependencies)

    # Act
    await flow.evaluate_and_enter("BTC")

    # Assert: 適切なアダプターが呼ばれたか
    mock_dependencies["exchange"].place_order.assert_called_once()
    mock_dependencies["repo"].log_signal.assert_called_once()

@pytest.mark.asyncio
async def test_does_not_place_order_when_sentiment_low(mock_dependencies):
    mock_dependencies["sentiment"].analyze.return_value = SentimentResult(
        score=0.3, confidence=0.8  # 閾値未満
    )

    flow = EntryFlow(**mock_dependencies)
    await flow.evaluate_and_enter("BTC")

    mock_dependencies["exchange"].place_order.assert_not_called()
```

### 11.10 E2Eテスト戦略（testnet）

HyperLiquid testnetを使った疎通テスト。**少量のみ**：

```python
# tests/e2e/test_hyperliquid_real.py
import pytest

@pytest.mark.e2e
@pytest.mark.skipif(not HL_TESTNET_KEY, reason="No testnet key")
async def test_can_place_and_cancel_order_on_testnet():
    """testnet で実際に注文してキャンセルできるか"""
    client = HyperLiquidSDKClient(network="testnet")

    order = await client.place_order(
        symbol="BTC",
        side="buy",
        size=Decimal("0.001"),
        price=Decimal("1"),  # 約定しない安値
        client_order_id=f"test_{int(time.time())}",
        post_only=True,
    )
    assert order.success

    cancelled = await client.cancel_order(order.id)
    assert cancelled is True
```

E2Eは**CIで毎回回さない**。手動 or 週次のみ。

### 11.11 Phase別TDD実装ロードマップ

| Week | 実装 | テスト戦略 | 完了基準 |
|---|---|---|---|
| 1 | core/models.py + helpers | データクラス・ヘルパー | テストヘルパー使える |
| 1-2 | core/entry_judge.py | パラメトリックテスト多数 | 全層判定100%カバレッジ |
| 2 | core/price_context.py | 章5の3基準テスト | 過熱フィルター完成 |
| 2 | core/vwap.py | VWAPTracker含む | 保有中追跡できる |
| 3 | core/position_sizer.py + stop_loss.py | ATRベースSL/TP | サイズ計算完成 |
| 3 | core/circuit_breaker.py | 7段階全テスト | 章9.7完成 |
| 4 | adapters/ Protocol定義 | Protocol準拠テスト | I/F確定 |
| 4-5 | infrastructure/hyperliquid_client.py | testnetでE2E | 疎通確認 |
| 5 | infrastructure/claude_provider.py | モック化テスト | API呼び出せる |
| 5 | infrastructure/sqlite_repo.py | DBスキーマ・CRUD | DB操作完成 |
| 6 | application/entry_flow.py | モック使った統合テスト | エントリー流れ完成 |
| 6 | application/position_monitor.py | 同上 | 監視ロジック完成 |
| 6 | application/reconciliation.py | 章9.3の状態復元 | 突合完成 |
| 7 | main.py 組み立て | E2Eテスト | BOT起動可能 |
| 7 | Phase 0データ収集開始 | - | データ蓄積開始 |
| 8 | ドライラン（Phase 1） | - | 仮想取引動作 |

**Definition of Done（DoD）：**

各Weekで以下が満たされていること：
- [ ] テストが全て緑（pytest通過）
- [ ] CORE層は100%カバレッジ
- [ ] APPLICATION層は主要パスをカバー
- [ ] linterエラーなし（ruff/black/mypy）
- [ ] PRレビュー（自分でDiff確認）
- [ ] CIが緑

### 11.12 開発支援ツール構成

```toml
# pyproject.toml
[project]
dependencies = [
    "hyperliquid-python-sdk",
    "anthropic",
    "discord-webhook",
    "feedparser",  # RSS
    "aiohttp",
    "pydantic",
    "pyyaml",
]

[project.optional-dependencies]
dev = [
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "hypothesis",      # プロパティベーステスト
    "ruff",            # リンター
    "black",           # フォーマッター
    "mypy",            # 型チェック
    "pre-commit",      # コミット前フック
]

[tool.pytest.ini_options]
addopts = "-v --cov=src --cov-report=term-missing"
markers = [
    "e2e: testnet使用のE2Eテスト（手動実行）",
    "slow: 時間のかかるテスト",
]

[tool.ruff]
line-length = 100
target-version = "py313"
select = ["E", "F", "I", "N", "B", "UP"]

[tool.mypy]
strict = true
```

### 11.13 既存設計章のリファクタ指針

各章で書いた内容を、本TDDアーキテクチャに沿って実装する際のマッピング：

| 章 | 設計内容 | 配置先 |
|---|---|---|
| 4 | 4層AND判定 | `core/entry_judge.py` |
| 5 | PriceContext + 過熱フィルター | `core/price_context.py` |
| 6 | VWAP計算 + 純関数Tracker | `core/vwap.py` |
| 7 | sentiment_analyzer | `infrastructure/claude_provider.py` |
| 8 | DB操作 | `infrastructure/sqlite_repo.py` |
| 9.3, 9.6 | 状態復元・突合 | `application/reconciliation.py` + `core/reconciliation.py` |
| 9.5 | 注文発注（リトライ・冪等） | `infrastructure/hyperliquid_client.py` |
| 9.7 | サーキットブレーカー判定 | `core/circuit_breaker.py` |
| 10 | アカウント・キー管理 | コードではなく運用設計 |

**設計方針の徹底：**

- 章6のVWAP挙動追跡はすでに純関数版（`VWAPState` + `update_vwap_state()`）で定義済み
- 章9の状態復元も判定ロジック（`reconcile_positions()`）と副作用（`StateReconciler`）が分離済み
- 上記の章のコード例は全て本アーキテクチャ準拠

### 11.14 Claude Code向け実装指示

このアーキテクチャでClaude Codeに実装させる時のプロンプトテンプレート：

```
hl-alpha-bot を以下の方針で実装してください：

1. アーキテクチャ：core / adapters / infrastructure / application の4層
2. CORE層は純関数のみ・I/O禁止・100%テストカバレッジ
3. ADAPTERSはProtocolのみ
4. INFRASTRUCTUREでProtocol実装
5. APPLICATIONはCOREを呼び出すだけの薄い層

実装順：
1. core/models.py を最初に書く（テストヘルパー含む）
2. core/entry_judge.py をTDDで書く（赤→緑→リファクタ）
3. 残りのcore/* も同様に
4. adapters/* のProtocol定義
5. infrastructure/* の実装（testnet で動作確認）
6. application/* の組み立て
7. main.py で全部繋げる

各テストは：
- パラメトリックテストで境界値を網羅
- ヘルパー関数 make_snapshot() を活用
- モックは AsyncMock を使う

設計書の章11を厳守してください。
```

### 11.15 既存BOTからの差分

| 項目 | auto-daytrade | moomoo-trader | hl-alpha-bot |
|---|---|---|---|
| アーキテクチャ | フラット（src/直下） | 軽い層分け | **4層厳格** |
| 純関数の比率 | 低い（I/O混在） | 中 | **CORE層100%純関数** |
| テスト | なし or 少 | あり | **TDD・100%カバレッジ目標** |
| 依存逆転 | なし | 部分的 | **Protocol必須** |
| テストヘルパー | なし | なし | **make_snapshot等** |
| プロパティテスト | なし | なし | **hypothesis活用** |
| ゴールデンテスト | なし | なし | **fixtures/で実データ回帰** |
| E2E分離 | なし | なし | **testnet・手動のみ** |
| 開発フロー | 動かしながら修正 | 動かしながら修正 | **TDD: Red→Green→Refactor** |

### 11.16 PR7.x 実装で確定した APPLICATION 層 Config 仕様

PR7.1 〜 PR7.5c の実装で各 Config dataclass のフィールドが確定。
本文中で散発していた仮置きフィールド名と乖離しているので、ここで**唯一の真実**として整理する。

#### 11.16.1 EntryFlowConfig（PR7.1）

```python
@dataclass(frozen=True)
class EntryFlowConfig:
    is_dry_run: bool
    leverage: int
    flow_layer_enabled: bool
    position_size_pct: Decimal           # 章 13.2 SizingInput と同名
    sl_atr_mult: Decimal
    tp_atr_mult: Decimal
    oi_lookup_tolerance_minutes: int
```

仕様書本文との差分:
- ❌ `risk_per_trade_pct` は存在しない → ✅ `position_size_pct`
- ❌ `max_position_usd` は EntryFlowConfig に持たない（CORE position_sizer の引数として注入）
- ❌ `consecutive_losses_threshold` / `losing_streak_size_multiplier` も同様に持たない

#### 11.16.2 PositionMonitorConfig（PR7.2）

```python
@dataclass(frozen=True)
class PositionMonitorConfig:
    funding_close_minutes_before: int
    funding_close_enabled: bool
    fills_lookback_seconds: int
    force_close_slippage_tolerance_pct: Decimal
```

#### 11.16.3 ReconciliationConfig（PR7.3）

```python
@dataclass(frozen=True)
class ReconciliationConfig:
    fills_lookback_hours: int
    stale_order_cleanup_seconds: int
```

仕様書本文との差分:
- ❌ `enable_stale_order_cleanup` フィールドは存在しない
  → cleanup の有無は `restore_on_startup` (有効) vs `run_periodic_check` (無効) の
    呼び出し側スイッチで制御（章 9.6）

#### 11.16.4 SchedulerConfig（PR7.4）

```python
@dataclass(frozen=True)
class SchedulerConfig:
    watchlist: tuple[str, ...]
    directions: tuple[Literal["LONG", "SHORT"], ...]
    cycle_interval_seconds: float
    reconcile_interval_seconds: float
    circuit_breaker_enabled: bool
    max_position_count: int
    daily_loss_limit_pct: Decimal
    weekly_loss_limit_pct: Decimal
    consecutive_loss_limit: int
    flash_crash_threshold_pct: Decimal
    btc_anomaly_threshold_pct: Decimal
    api_error_rate_max: Decimal
    position_overflow_multiplier: Decimal
```

仕様書本文との差分:
- ❌ `shutdown_timeout_seconds` フィールドは存在しない
  → graceful shutdown は `_shutdown_requested` フラグで cycle 完了を待つだけ

### 11.17 PR7.x 実装で確定した APPLICATION 層実装方針

#### 11.17.1 FLOW bypass は APPLICATION 層に集約（PR7.1・章 11.6.3 補強）

CORE 層 `judge_long_entry` / `judge_short_entry` は config 引数を持たない純関数のまま維持。
FLOW bypass は APPLICATION 層 `EntryFlow._bypass_flow` で処理する：

```python
@staticmethod
def _bypass_flow(decision: EntryDecision) -> EntryDecision:
    new_layer_results = dict(decision.layer_results)
    new_layer_results["flow"] = True
    all_pass = all(new_layer_results.values())
    new_rejection = decision.rejection_reason
    if new_rejection and "flow" in new_rejection.lower():
        new_rejection = None
    return replace(
        decision,
        should_enter=all_pass,
        layer_results=new_layer_results,
        rejection_reason=new_rejection,
    )
```

ご利益:
- CORE のテストは config 不要で済む（高速）
- bypass の有効/無効は config だけで切替
- 将来 bypass 条件を増やしても CORE 層は不変

#### 11.17.2 EntryFlow の `_dispatch_fill` パターン（PR7.2）

PositionMonitor で fill を分類するロジックは `closed_pnl` で entry / 決済を分離：

```python
async def _dispatch_fill(self, fill: Fill, open_trades: tuple[Trade, ...]) -> str:
    if fill.closed_pnl == 0:
        # entry の可能性
        for trade in open_trades:
            if not trade.is_filled and self._fill_matches_entry(trade, fill):
                await self._on_entry_filled(trade, fill)
                return "entry"
        return "ignored"
    # closed_pnl != 0 → 決済の可能性
    for trade in open_trades:
        if trade.is_filled and self._fill_matches_close(trade, fill):
            await self._on_trade_closed(trade, fill)
            return "close"
    return "ignored"
```

#### 11.17.3 BreakerInput の Phase 0 placeholder（PR7.4）

CircuitBreaker の入力には Phase 0 で取れない指標がある。
**安全側ゼロで埋める**ことで段階的に埋めていく：

| フィールド | Phase 0 | 後続実装 |
|---|---|---|
| `daily_loss_pct` | ✅ Repository から計算 | — |
| `consecutive_losses` | ✅ Repository から取得 | — |
| `position_count` | ✅ exchange.get_positions | — |
| `weekly_loss_pct` | ⏳ ゼロ | Phase 1 で残高履歴から |
| `symbol_1min_changes_pct` | ⏳ 空 tuple | PR6.5 WS trades で |
| `btc_5min_change_pct` | ⏳ ゼロ | 同上 |
| `api_error_rate_5min` | ⏳ ゼロ | エラー追跡器を別途実装 |

Phase 0 中に発火するのは **DAILY_LOSS / CONSECUTIVE_LOSS / POSITION_OVERFLOW** のみ。

#### 11.17.4 Scheduler の `_wait_or_shutdown` で `await asyncio.sleep(0)` 必須（PR7.4）

`cycle_interval_seconds=0` の場合、`_wait_or_shutdown(0)` が即 return すると
他タスク（shutdown 要求等）が実行できないタイトループになる：

```python
async def _wait_or_shutdown(self, seconds: float) -> None:
    await asyncio.sleep(0)  # ← 必須・他タスクへの yield 機会
    if seconds <= 0:
        return
    end = asyncio.get_event_loop().time() + seconds
    while True:
        if self._shutdown_requested:
            return
        remaining = end - asyncio.get_event_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.1, remaining))
```

#### 11.17.5 BTC レジーム判定の本実装とフォールバック（PR7.7）

Phase 0 観察 48 時間で REGIME 通過率 47.5% と判明し、PR7.1 簡易実装
（`current_price > rolling_24h_open`）が値動きで頻繁に false に振れるためと
分かった。15 分足ローソク足ベースの本実装に置き換え：

```python
# src/application/entry_flow.py 抜粋
_BTC_REGIME_INTERVAL = "15m"
_BTC_EMA_LIMIT = 60          # EMA50 のシード(50) + マージン
_BTC_EMA_SHORT_PERIOD = 20
_BTC_EMA_LONG_PERIOD = 50
_BTC_ATR_LIMIT = 30          # ATR(14) は 15 本以上必要、マージン込み
_BTC_ATR_PERIOD = 14

async def _calc_btc_ema_trend(self, btc_snap: MarketSnapshot) -> str:
    try:
        candles = await self.exchange.get_candles(
            symbol="BTC", interval=_BTC_REGIME_INTERVAL, limit=_BTC_EMA_LIMIT,
        )
    except ExchangeError:
        return self._fallback_btc_ema_trend(btc_snap)
    if len(candles) < _BTC_EMA_LONG_PERIOD:
        return self._fallback_btc_ema_trend(btc_snap)

    closes = [c.close for c in candles]
    ema_short = calculate_ema(closes, period=_BTC_EMA_SHORT_PERIOD)
    ema_long  = calculate_ema(closes, period=_BTC_EMA_LONG_PERIOD)
    if ema_short > ema_long:
        return "UPTREND"
    if ema_short < ema_long:
        return "DOWNTREND"
    return "NEUTRAL"
```

`_calc_btc_atr_pct` も同様に 30 本ローソク足から `calculate_atr_pct` を呼ぶ。

確定した実装方針:
- **取得本数**: EMA 用 60 本（EMA50 のシード + 余裕）、ATR 用 30 本（ATR(14) + 余裕）
- **インターバル**: 15 分足固定（章 4 ④ REGIME の判定軸）
- **NEUTRAL 分岐**: `Decimal` の有限精度のため定数 close でも厳密一致しない
  → 実走では事実上 UPTREND/DOWNTREND の二択になる。テストは `monkeypatch`
   で `calculate_ema` を固定し NEUTRAL 分岐到達のみ検証する。
- **戻り値型**: `_calc_btc_atr_pct` は内部 `Decimal` で計算後 `float` に cast
  （`MarketSnapshot.btc_atr_pct: float` を維持。Decimal 化は別 PR）。
- **フォールバック**: ローソク足取得失敗・本数不足時は `_fallback_btc_ema_trend`
  / `_fallback_btc_atr_pct` で 24h 比較（PR7.1 ロジック）に戻る。
  API 一時障害で BOT が止まらないよう必ず保つ。

72 時間観察で REGIME 通過率 78.1% に改善（章15.4.2 で詳述）。

### 11.18 PR7.7 実装で確定した CORE indicators モジュール

CORE 層に純関数のテクニカル指標モジュール `src/core/indicators.py` を新設。
ローソク足の取得は呼び出し側（INFRASTRUCTURE / APPLICATION）の責務、
本モジュールは数値計算のみ：

```python
# src/core/indicators.py（抜粋）
from decimal import Decimal


def calculate_ema(prices: list[Decimal], period: int) -> Decimal:
    """指数移動平均（EMA）。

    seed = 最初の period 件の SMA、以降は alpha=2/(period+1) で平滑化:
        EMA(t) = price(t) * alpha + EMA(t-1) * (1 - alpha)
    """

def calculate_atr(
    highs: list[Decimal], lows: list[Decimal], closes: list[Decimal],
    period: int = 14,
) -> Decimal:
    """Average True Range。Wilder's smoothing 採用:
        TR = max(h-l, |h-pc|, |l-pc|)
        ATR(t) = (ATR(t-1) * (period-1) + TR(t)) / period
    最初の TR には前足の close が必要なので period+1 本以上必要。
    """

def calculate_atr_pct(
    highs: list[Decimal], lows: list[Decimal], closes: list[Decimal],
    period: int = 14,
) -> Decimal:
    """ATR / latest_close * 100。latest_close=0 の場合は 0 を返す。"""
```

設計判断:
- **すべて純関数**: 副作用なし、ローソク足取得は呼び出し側
- **Decimal 統一**: 浮動小数点誤差回避のため `alpha = Decimal(2) / (Decimal(period) + Decimal(1))`
- **EMA seed**: 最初の period 件の SMA（最も標準的）
- **ATR smoothing**: Wilder's smoothing（オリジナル定義）。EMA 平滑化版は採用しない
- **入力検証**: `period < 1`、長さ不一致、本数不足は ValueError を上げる
  → APPLICATION 層側で `try ... except (ExchangeError, ValueError)` で
   フォールバックに落とす

将来の `dynamic_stop`（ATR ベース SL/TP）でも同じ純関数を再利用できる。

---

## 12. ウォッチリスト戦略

固定+動的のハイブリッド（moomoo踏襲）。

### 固定リスト（流動性最重視・8銘柄）
```
BTC, ETH, SOL, BNB, XRP, DOGE, AVAX, LINK
```

### 動的リスト（毎4時間更新・最大8銘柄）

**スクリーニング条件：**
- 24h出来高 > $50M（流動性確保）
- 板深度 > $200k（±1%以内）
- 直近24h Funding Rateが極端（清算狙い候補）
- 直近1hで価格変動率が標準偏差の2σ超（動意あり）

**除外条件：**
- ミームコイン極小流動性銘柄
- 上場直後（1週間以内）の銘柄
- スプレッドが0.1%以上の銘柄

**合計最大16銘柄を10秒間隔でスキャン。**

---

## 13. ポジション・リスク管理

### 13.1 重要な認識：暗号資産は株より3〜5倍ボラが大きい

auto-daytradeの「-1%損切り」をそのまま持ち込むと一瞬で消える。**ATRベース必須**。

| 項目 | 値 | 根拠 | 設定key |
|---|---|---|---|
| 1ポジション固定額 | 口座の5% | 20分割で連敗耐性 | `risk.position_size_pct_of_capital` |
| 同時保有上限 | LONG 3 / SHORT 2 | レバ込みの実エクスポージャ管理 | `risk.max_positions_long/short` |
| レバレッジ | 最大3倍 | HL最大50倍だが自殺行為 | `risk.max_leverage` |
| SL | ATR(1h, 14) × 1.5 | 株より広く | `trading.stop_loss.atr_sl_multiplier` |
| TP | ATR(1h, 14) × 2.5〜3.0 | リスクリワード1:2 | `trading.stop_loss.atr_tp_multiplier` |
| トレーリング | +1ATR含み益後発動 | 利を伸ばす | `trading.stop_loss.trailing_after_atr` |
| 日次損失上限 | -3% | サーキットブレーカー | `risk.daily_loss_limit_pct` |
| 週次損失上限 | -8% | 週次強制停止 | `risk.weekly_loss_limit_pct` |
| 連敗 | 3連敗でサイズ半減 | moomoo踏襲 | `risk.consecutive_loss_count` |

### 13.2 ポジションサイジングの計算（純関数）

```python
# src/core/position_sizer.py
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SizingInput:
    account_balance_usd: Decimal
    entry_price: Decimal
    sl_price: Decimal
    leverage: int
    position_size_pct: Decimal
    sz_decimals: int                  # HL銘柄のszDecimals
    consecutive_losses: int = 0


@dataclass(frozen=True)
class SizingResult:
    size_coins: Decimal
    notional_usd: Decimal
    risk_per_unit: Decimal
    risk_total_usd: Decimal
    rejected_reason: str | None = None


def calculate_position_size(input: SizingInput) -> SizingResult:
    """ポジションサイズ計算（純関数）

    1. 基本サイズ = 口座 × position_size_pct × leverage / entry_price
    2. 連敗ペナルティ：3連敗以上でサイズ半減
    3. szDecimalsで丸める
    4. リスク額（entry-SL の差 × size）を返す
    """
    size_multiplier = Decimal("0.5") if input.consecutive_losses >= 3 else Decimal("1.0")

    notional = (
        input.account_balance_usd
        * input.position_size_pct
        * input.leverage
        * size_multiplier
    )
    raw_size = notional / input.entry_price

    quantizer = Decimal("0.1") ** input.sz_decimals
    size_coins = raw_size.quantize(quantizer, rounding="ROUND_DOWN")

    if size_coins <= 0:
        return SizingResult(
            size_coins=Decimal("0"),
            notional_usd=Decimal("0"),
            risk_per_unit=Decimal("0"),
            risk_total_usd=Decimal("0"),
            rejected_reason="size_too_small_after_rounding",
        )

    risk_per_unit = abs(input.entry_price - input.sl_price)
    risk_total = risk_per_unit * size_coins

    return SizingResult(
        size_coins=size_coins,
        notional_usd=size_coins * input.entry_price,
        risk_per_unit=risk_per_unit,
        risk_total_usd=risk_total,
        rejected_reason=None,
    )
```

### 13.3 SL/TP価格計算（ATRベース・純関数）

```python
# src/core/stop_loss.py
@dataclass(frozen=True)
class StopLossInput:
    direction: str                    # 'LONG' / 'SHORT'
    entry_price: Decimal
    atr_value: Decimal
    sl_multiplier: Decimal
    tp_multiplier: Decimal
    tick_size: Decimal


@dataclass(frozen=True)
class StopLossResult:
    sl_price: Decimal
    tp_price: Decimal


def calculate_sl_tp(input: StopLossInput) -> StopLossResult:
    """ATRベースのSL/TP計算（純関数）"""
    sl_distance = input.atr_value * input.sl_multiplier
    tp_distance = input.atr_value * input.tp_multiplier

    if input.direction == "LONG":
        sl_raw = input.entry_price - sl_distance
        tp_raw = input.entry_price + tp_distance
    else:  # SHORT
        sl_raw = input.entry_price + sl_distance
        tp_raw = input.entry_price - tp_distance

    sl_price = _round_to_tick(sl_raw, input.tick_size)
    tp_price = _round_to_tick(tp_raw, input.tick_size)

    # 最低1tick差を保証（auto-daytrade教訓）
    min_diff = input.tick_size
    if input.direction == "LONG":
        sl_price = min(sl_price, input.entry_price - min_diff)
        tp_price = max(tp_price, input.entry_price + min_diff)
    else:
        sl_price = max(sl_price, input.entry_price + min_diff)
        tp_price = min(tp_price, input.entry_price - min_diff)

    return StopLossResult(sl_price=sl_price, tp_price=tp_price)


def _round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).quantize(Decimal("1")) * tick
```

### 13.4 Funding Rate 連動の自動手仕舞い

1時間ごとのFunding精算前（章23の`risk.funding_exit_minutes_before: 5`）に：

```python
# src/core/funding_judge.py
@dataclass(frozen=True)
class FundingExitInput:
    direction: str                    # 'LONG' / 'SHORT'
    funding_rate: Decimal             # 8h相当値
    unrealized_pnl_pct: Decimal
    minutes_to_funding: int
    threshold_minutes: int


def should_exit_before_funding(input: FundingExitInput) -> bool:
    """Funding精算前の手仕舞い判定（純関数）"""
    if input.minutes_to_funding > input.threshold_minutes:
        return False

    is_paying_funding = (
        (input.direction == "LONG" and input.funding_rate > 0)
        or (input.direction == "SHORT" and input.funding_rate < 0)
    )

    if not is_paying_funding:
        return False  # 受取側なら維持

    # 支払い側で含み益が薄い場合は手仕舞い
    return input.unrealized_pnl_pct < Decimal("0.5")
```

これは株BOTには存在しない暗号資産特有のロジック。

### 13.5 清算データの扱いについて（重要）

**当初設計の制約：** 章4の④で「清算カスケード予測」を計画していたが、HyperLiquid公式APIでは：

- **自分の清算データ：** `WsUserNonFundingLedgerUpdates`（`type: "ledgerLiquidation"`）で取得可能
- **他人の清算データ：** **公式API直接取得不可**
- **過去データ：** S3アーカイブ（`hyperliquid-archive`）で月次更新・遅延あり
- **third-party：** Quicknode SQL Explorer等で清算指標を提供（要有料プラン）

#### 採用した代替アプローチ

清算カスケード予測の代わりに、以下の指標で「ポジションの偏り」「過熱」を検出：

```python
# src/core/regime.py
@dataclass(frozen=True)
class RegimeInput:
    funding_rate_8h: Decimal          # 8時間相当のFunding
    open_interest: Decimal            # 現在のOI
    open_interest_1h_ago: Decimal     # 1h前のOI
    btc_ema_short: Decimal            # BTC EMA(20)
    btc_ema_long: Decimal             # BTC EMA(50)
    btc_atr_pct: Decimal              # BTC ATR%


def judge_regime_long(input: RegimeInput) -> tuple[bool, str | None]:
    """LONGエントリーのレジーム判定（純関数）

    清算予測の代わり：Funding + OI変動で「ポジション偏り」を検出
    """
    if input.btc_ema_short <= input.btc_ema_long:
        return False, "btc_downtrend"
    if input.btc_atr_pct > Decimal("5.0"):
        return False, "btc_volatility_extreme"
    if input.funding_rate_8h >= Decimal("0.0001"):  # 0.01%
        return False, "funding_overheated"

    oi_change_pct = (
        (input.open_interest - input.open_interest_1h_ago)
        / input.open_interest_1h_ago * 100
    )
    if abs(oi_change_pct) > 10:
        return False, "oi_extreme_change"

    return True, None
```

#### OI履歴の管理責務（重要・APPLICATION層）

`open_interest_1h_ago` は HL公式 API の単発呼び出しでは取れない。
**APPLICATION 層で履歴を保持する必要がある**：

```python
# src/application/entry_flow.py（抜粋）

async def _build_snapshot(self, symbol: str) -> MarketSnapshot:
    # 現在のOIを取得
    oi_now = await self.exchange.get_open_interest(symbol)

    # 1h前のOIを Repository から取得（履歴管理）
    oi_1h_ago = await self.repo.get_oi_at(
        symbol, now() - timedelta(hours=1)
    )

    # OI を spawn のたびに記録（後で 1h前として参照される）
    await self.repo.record_oi(symbol, now(), oi_now)

    # 1h前データがまだ無い場合は現在値を代用（OI変動 = 0%扱い）
    return market.with_regime(
        open_interest=oi_now,
        open_interest_1h_ago=oi_1h_ago or oi_now,
        ...
    )
```

**Repository に必要なメソッド（章8 に追加）：**

```python
class Repository(Protocol):
    async def record_oi(
        self, symbol: str, timestamp: datetime, oi: Decimal
    ) -> None:
        """OI を時系列で記録"""

    async def get_oi_at(
        self, symbol: str, timestamp: datetime, tolerance_minutes: int = 5
    ) -> Decimal | None:
        """指定時刻に最も近いOIを返す（±tolerance内に無ければNone）"""
```

**oi_history テーブル設計：**

```sql
CREATE TABLE oi_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    timestamp   DATETIME NOT NULL,
    oi          REAL NOT NULL,
    UNIQUE(symbol, timestamp)
);
CREATE INDEX idx_oi_symbol_time ON oi_history(symbol, timestamp);
```

**保持期間：** 直近24時間で十分（古いデータは日次で削除）。

**起動直後の grace period：** OI履歴が1h分溜まるまでは `open_interest_1h_ago=open_interest` で OI変動0%扱い。これは章9.8 の DataReadinessGate に追加する。

#### 将来の拡張（章20.A 自分の清算データFB）

Phase 4以降、**自分が清算された履歴**を蓄積し、機械学習なしの単純ヒューリスティクスで活用：

```
ルール例：
- 銘柄Xで過去30日に清算された → 翌7日間その銘柄でLONG禁止
- BTCの大暴落時に清算された → BTC ATR%閾値を厳しく
```

詳細は章20.A。Phase 4安定後の拡張機能。

### 13.6 強制決済タイミング（暗号資産特有）

株BOTと違い**24時間市場のため時間制限は緩く設定**：

```yaml
risk:
  max_holding_hours: null              # デフォルト時間無制限

trading:
  hard_exit:
    loss_pct_threshold: -10.0          # -10%超で問答無用決済
    volume_drop_pct: -50.0             # 出来高半減で決済（流動性低下）
```

これは章9.7のサーキットブレーカーとは別レイヤー（個別ポジション保護）。

### 13.7 PR7.2 実装で確定した Funding 強制決済の実装（章 13.4 補強）

`PositionMonitor._force_close` の実装は **reduce_only IOC + slippage tolerance** を採用：

```python
async def _force_close(self, pos, reason):
    side = "sell" if pos.size > 0 else "buy"
    book = await self.exchange.get_l2_book(pos.symbol)
    tolerance = self.config.force_close_slippage_tolerance_pct
    if side == "buy":
        best = book.asks[0].price
        limit_price = best * (Decimal("1") + tolerance)  # ask 上に余裕
    else:
        best = book.bids[0].price
        limit_price = best * (Decimal("1") - tolerance)  # bid 下に余裕

    request = OrderRequest(
        symbol=pos.symbol, side=side, size=abs(pos.size),
        price=limit_price, tif="Ioc", reduce_only=True,
    )
```

選択理由:
- HL に純粋な market 注文はないため limit + IOC で代用
- `slippage_tolerance` で最悪値を制限
- `reduce_only=True` で誤って増し玉にならない保証
- IOC なので残った場合は自動キャンセル

---

## 14. 執行戦略：Maker-First

### 14.1 設計原則

auto-daytradeのスリッページ問題（-1%設定が-2〜3%で約定）の根本原因は成行注文。
HyperLiquidの**Post-Only（ALO）注文**を最大限活用する。

### 14.2 執行フロー全体

```
エントリー：Post-Only指値（best_bid - 1tick・LONG）
           → 30秒未約定ならキャンセル
           → 価格を1tick調整して再評価（最大3回）
           → 全リトライ失敗ならエントリー断念

SL：原則ストップマーケット（確実な損切り優先）
    オプション：流動性高い時はストップリミット（slippage上限0.1%）

TP：指値（Post-Only/ALO）
    → 約定するまで板に置く
    → エントリーと同時にgrouping="normalTpsl"で発注
```

### 14.3 ALO拒否時の再評価アルゴリズム

HyperLiquidの罠（章22.11）：**ALO注文がマッチしそうな場合は即座に拒否**される。
これに対応する再評価ループ：

```python
# src/application/maker_first_router.py
async def place_post_only_with_retry(
    exchange: ExchangeProtocol,
    symbol: str,
    side: str,             # 'buy' or 'sell'
    size: Decimal,
    config: ExecutionConfig,
) -> OrderResult:
    """Post-Only注文の再評価ループ"""

    for attempt in range(config.post_only_retry_attempts):
        # 1. 現在の板情報取得
        book = await exchange.get_l2_book(symbol)
        best_bid = Decimal(book.bids[0].price)
        best_ask = Decimal(book.asks[0].price)
        tick_size = await exchange.get_tick_size(symbol)

        # 2. Post-Only価格計算
        if side == "buy":
            target_price = best_bid - tick_size  # マッチしないギリギリ
        else:
            target_price = best_ask + tick_size

        # 3. 注文発注（章9.5の冪等性設計を継承）
        client_oid = generate_client_order_id(symbol, f"entry_attempt{attempt}")
        try:
            result = await exchange.place_order(
                symbol=symbol,
                side=side,
                size=size,
                price=target_price,
                client_order_id=client_oid,
                tif="Alo",
            )
            order_id = result.order_id

        except OrderRejectedError as e:
            if "ALO" in e.message:
                continue  # 価格が動いた → retry
            else:
                raise

        # 4. 約定待機
        await asyncio.sleep(config.post_only_retry_wait_sec)

        # 5. 約定確認
        status = await exchange.get_order_status(order_id)
        if status == "filled":
            return OrderResult(success=True, order_id=order_id)

        # 6. 未約定 → キャンセルしてretry
        await exchange.cancel_order(order_id)

    return OrderResult(
        success=False,
        rejected_reason="post_only_all_retries_failed",
    )
```

### 14.4 経済効果の試算

```
Taker回避でTaker 0.045% → Maker 0.015% に削減
→ 往復で 0.06% の節約
→ 年間500回取引なら 30% の手数料圧縮
→ Phase 4で月100トレードなら年間 $200〜300 節約（運用額$2000の場合）
```

### 14.5 SL執行の特例

SL発動時はスリッページよりも**確実な決済**を優先：

```python
# 通常はストップマーケット
sl_order = {
    "type": "trigger",
    "trigger": {
        "isMarket": True,
        "triggerPx": str(sl_price),
        "tpsl": "sl",
    },
}

# ストップリミット（オプション）：流動性が高い時のみ
if spread_pct < 0.05 and book_depth_usd > 100000:
    sl_order["trigger"]["isMarket"] = False
    sl_order["limit_px"] = str(sl_price * Decimal("0.999"))
```

設定値：
```yaml
trading:
  execution:
    sl_use_market: true
    sl_max_slippage_pct: 0.1
    sl_use_limit_when:
      spread_max_pct: 0.05
      book_depth_min_usd: 100000
```

### 14.6 TP連結（HyperLiquid独自機能）

エントリー注文とTP/SL注文を `grouping: "normalTpsl"` で連結すると、エントリー約定時に自動でTP/SLが発注される：

```python
{
    "action": {
        "type": "order",
        "orders": [
            entry_order,
            tp_order,    # reduceOnly + trigger
            sl_order,    # reduceOnly + trigger
        ],
        "grouping": "normalTpsl",  # ★これが鍵
    },
}
```

**メリット：** エントリー約定とSL/TP発注の時間差ゼロ → スリッページ最小化
**注意：** SL/TPは事前に決定する必要があるため、エントリー時点でATR等が計算済みであること

#### 実機検証で判明した挙動（重要・PR6.4.3）

testnet で `place_orders_grouped` を実行すると、3 つの `OrderResult` が返るが、
**動作は非対称**：

```python
results = await client.place_orders_grouped(entry, tp, sl)
# results[0] (entry): success=True, order_id=12345  ← 板に乗る
# results[1] (tp):    success=True, order_id=None   ← entry 約定まで保留
# results[2] (sl):    success=True, order_id=None   ← entry 約定まで保留
```

**仕様：**
1. entry のみ通常通り板に乗る（即時 `order_id` 取得可）
2. tp/sl は HL 側で「entry 約定待ち」状態で保留される
3. entry が約定すると tp/sl が**自動発注**される（その時点で order_id 確定）
4. entry を `cancel_order` すると、tp/sl も自動的に無効化される
5. tp/sl の `order_id` を後で取得するには、約定後に `open_orders` で照会

**実装上の影響：**

| 項目 | 対応 |
|---|---|
| `OrderResult.success=True && order_id=None` | 「保留中」として正常扱い（失敗ではない） |
| Repository.open_trade のタイミング | entry 発注時点で `trade_id` 確定可能 |
| tp_price / sl_price の保存 | entry 発注時点で予定価格を Repository に記録 |
| tp/sl の `order_id` 紐付け | **約定検知後**（PR7.2 position_monitor）で実施 |

**PR7.2 で実装すべきフロー：**

```python
# position_monitor の擬似コード
async def _on_entry_filled(trade_id: int):
    """entry 約定検知時の処理"""
    # 1. trade をマーク（is_filled=True）
    await repo.mark_trade_filled(trade_id)

    # 2. 自動発注された TP/SL の order_id を取得
    open_orders = await exchange.get_open_orders()
    trade = await repo.get_trade(trade_id)
    related = [o for o in open_orders
               if o.symbol == trade.symbol and o.reduce_only]

    # 3. trade に tp_order_id / sl_order_id を紐付け
    for order in related:
        if 価格が tp 側:
            await repo.update_tp_order_id(trade_id, order.order_id)
        elif 価格が sl 側:
            await repo.update_sl_order_id(trade_id, order.order_id)
```

これにより、章9.3 の状態復元（reconciliation）で trade と order_id が一貫して扱える。

### 14.7 PR7.2 実装で確定した TP/SL order_id 紐付けロジック（章 14.6 詳細化）

実装で確定したのは **「価格近接マッチング」** 方式。`reduce_only` フィールドが
ADAPTERS 層 `Order` に無いため、**価格が tp_price と sl_price のどちらに近いか**で識別する：

```python
async def _find_tp_sl_order_ids(self, trade: Trade) -> tuple[int | None, int | None]:
    open_orders = await self.exchange.get_open_orders()
    tp_oid: int | None = None
    sl_oid: int | None = None
    for order in open_orders:
        if order.symbol != trade.symbol:
            continue
        diff_to_tp = abs(order.price - trade.tp_price)
        diff_to_sl = abs(order.price - trade.sl_price)
        if diff_to_tp < diff_to_sl:
            if tp_oid is None:
                tp_oid = order.order_id
        elif sl_oid is None:
            sl_oid = order.order_id
    return tp_oid, sl_oid
```

PR6.4.3 testnet 検証で判明した HL grouped 仕様を反映:
- `place_orders_grouped` 直後の `results` の中で **entry のみ即 order_id を持つ**
- TP/SL は entry 約定まで `order_id=None` で保留状態
- entry が約定すると HL が自動で TP/SL を発注し `open_orders` に出る
- そこから上記ロジックで紐付ける（PR7.2 position_monitor `_on_entry_filled`）

---

## 15. 段階的ロールアウト計画

### 15.1 全体タイムライン

auto-daytradeで「いきなり実弾→苦戦」した反省を活かし、4段階で。

| フェーズ | 期間 | 内容 | 投入額 | 目標 |
|---|---|---|---|---|
| **Phase 0** | 1〜2週間 | データ収集のみ・取引なし | $0 | データ蓄積 |
| **Phase 1** | 2〜3週間 | 全シグナルをドライラン両方向 | $0 | ロジック検証 |
| **Phase 2** | 2〜3週間 | 最小サイズ実弾・LONGのみ | $200〜500 | 実装バグ発見 |
| **Phase 3** | 2〜4週間 | フルサイズLONG + ドライランSHORT | $1000〜2000 | 統計的検証 |
| **Phase 4** | 〜 | フル運用（LONG + SHORT） | $2000〜5000 | 安定運用 |

### 15.2 Phase 遷移基準（数値で明確化）

「想定通りの成績」を数値化し、各Phase完了の判定を明確に：

#### Phase 0 → Phase 1
- [ ] 7日以上連続でデータ収集動作
- [ ] WS切断・再接続が正常動作
- [ ] 16銘柄×24時間のデータが`signals`/`utc_open_prices`に記録されている
- [ ] サーキットブレーカーが誤動作していない

#### Phase 1 → Phase 2
- [ ] **ドライラン仮想取引が30件以上**
- [ ] 仮想勝率が35%以上（PFは未計測でOK）
- [ ] 4層AND各層のrejection ratioが妥当（どれか1層が90%以上落としていない）
- [ ] sentiment APIコストが想定内（月$50以下）
- [ ] ドライランで重大な仕様バグなし

#### Phase 2 → Phase 3
- [ ] **実弾10件以上 完了（決済済）**
- [ ] スリッページ平均が-0.1%以下（章14のMaker-Firstが効いている）
- [ ] サーキットブレーカー発動なし、または正常動作確認
- [ ] 状態復元（章9.3）が再起動で正常動作
- [ ] 実弾と仮想の損益乖離が±20%以内

#### Phase 3 → Phase 4
- [ ] **実弾30件以上**
- [ ] 月次PF >= 1.0（最低限ブレークイーブン）
- [ ] 月次最大ドローダウン -8%以内
- [ ] SHORTドライランで個別vs マクロの傾向が見える
- [ ] auto-daytradeで起きた既知の罠（章21.6）が発生していない

### 15.3 Phase ロールバック条件

各Phaseで以下に該当したら**前段階に戻す**：

| 条件 | 対応 |
|---|---|
| 月次PF < 0.5 | Phase 2に戻す |
| 重大なバグで$100超の損失 | Phase停止・原因究明 |
| WS切断で監視できなかったポジションが生じた | Phase 1に戻す |
| 設計書通りに動かない動作が3件以上 | Phase停止・設計書見直し |
| サーキットブレーカーが誤動作 | Phase停止・閾値調整 |

### 15.4 各Phaseでの監視指標

毎日Discord summary chに以下を通知：

```
[Phase X 日次サマリー]
シグナル評価数: XXX件
エントリー件数: XX件（実弾: X / ドライラン: XX）
クローズ件数: XX件
日次PnL: $X.XX (X.XX%)
累積PnL: $XX.XX
最大ドローダウン: -X.XX%

各層 rejection ratio:
  ① momentum: XX%
  ② flow: XX%
  ③ sentiment: XX%
  ④ regime: XX%

次回Phase遷移基準達成: X / Y 項目
```

**Phase遷移は人間の判断で行う**（自動遷移しない）。設計書の達成基準を満たしたら手動でPhaseアップ。

#### 15.4.1 PR7.5c-fix 確定: Phase 0 SENTIMENT 観察戦略

Phase 0 で `FixedSentimentProvider` を中立値 (`fixed_score=0.0`) のまま使うと、
SENTIMENT 層が常に弾かれて 4 層 AND が成立せず、**MOMENTUM/REGIME の現実の通過率が
観察できない**。

`profile_phase0.yaml` で **強制 bullish** (`fixed_score=0.8 / fixed_confidence=0.9`) に
設定し、SENTIMENT を素通りさせて他 3 層の素の通過率を観察する：

```yaml
# config/profile_phase0.yaml
sentiment:
  fixed_score: 0.8                       # CORE LONG 閾値 0.6 を超える
  fixed_confidence: 0.9                  # CORE confidence 閾値 0.7 を超える
  reasoning: "Phase 0 forced bullish for observation"
```

`is_dry_run=true` のままなので実発注リスクはゼロ。
PR7.5e で `ClaudeSentimentProvider` に差し替えた後は実 sentiment ベースの観察に移行。

#### 15.4.2 PR7.7 効果検証: 72 時間観察ベースライン（2026-05-02 〜 05）

PR7.7（BTC EMA/ATR 本実装）デプロイ後、72 時間連続観察で REGIME 通過率が
劇的に改善。`scripts/analyze_phase0.py --window 72h --json` で計測した
ベースライン値（`logs/pr7_6_5_baseline.json` に保存済み）：

```
観察期間:
  earliest_signal: 2026-05-02T13:53:27+00:00
  latest_signal:   2026-05-05T13:53:11+00:00
  estimated_cycles: 51,763 (= MOMENTUM 層シグナル総数)

層別通過率:
  MOMENTUM   1,023 /  51,763  ( 1.98%)
  FLOW      51,763 /  51,763  (100.00%)
  SENTIMENT 51,763 /  51,763  (100.00%) ← profile_phase0 forced bullish
  REGIME    40,420 /  51,763  (78.09%)  ← PR7.7 効果

PR7.1 簡易実装期間（48h・参考値）:
  REGIME 47.5%  /  MOMENTUM 1.9% (589 件)
PR7.7 本実装期間（72h・実測）:
  REGIME 78.1%  /  MOMENTUM 2.0% (1,023 件)

差分:
  - REGIME +30.6pt（簡易実装の値動きノイズ除去）
  - MOMENTUM 通過の絶対数 +73%（589 → 1,023、観察時間も +50% なので
    時間あたりも改善）
```

判定軸の置き換え:
- 簡易実装: `current_price > rolling_24h_open`（24h 前比）
  → 値動きで頻繁に false に振れる
- 本実装: `EMA20 > EMA50` on 15 分足 60 本
  → トレンドベースで安定

#### 15.4.3 PR7.7 期間で観察された 4 層通過クラスター

DRYRUN シグナルが短時間に集中する「クラスター」を観察。dedup_key
（章 25.8.3）が無いと Discord 通知が滞留する根拠データ：

```
[2026-05-05 01:20:52 〜 01:24:52] BTC LONG  21 件 / 4 分（最大）
[2026-05-05 02:04:44 〜 02:08:34] ETH+BTC LONG 17 件 / 4 分
[2026-05-05 10:28:31 〜 10:44:22] ETH LONG  7 件 / 16 分

観察特徴:
- モメンタム発生時は数分〜十数分続く
- 1 サイクル(10 秒)ごとに記録されるので秒〜分単位で連発
- dedup_key=dryrun:{symbol}:{direction} で 5 分窓に圧縮
  → 21 件のクラスターでも Discord 通知は最初の 1 件のみ
- DB（signals テーブル）には全件残るので後から analyze_phase0 で集計可能
```

dedup window（既定 300 秒）の選定根拠は本データ。短いと連発が漏れ、
長いと「次クラスター開始」が見逃される。5 分は経験則として妥当。

#### 15.4.4 PR7.7 期間で観察された 24 時間サイクル数の安定性

`by_hour` ヒストグラムで、各時刻のシグナル評価数（= サイクル数 × 銘柄数）が
ほぼフラットになっていることを確認：

```
[実測値・PR7.7 期間 72h]
  00:00台: 8616
  01:00台: 8632
  04:00台: 8640 (最大)
  10:00台: 8572 (最小)
  20:00台: 8580
  ...
  範囲: 8572 〜 8640（最大 0.79% の差）

理論値: 24h × 60min × 6 cycles/min = 8640 cycles/symbol
       = 8640 × 2 銘柄 / 2 (BTC は両方の slot で記録) = 8640
       ※実装は watchlist=("BTC","ETH")で 2 銘柄評価のため、
         サイクル数と signals 数は分母設定により近似
```

含意:
- BOT は 24h 連続稼働で API レート制限に当たらない
- サイクル時間が安定（10 秒インターバル + 軽い揺らぎ）
- 起動初期の偏り（PR7.1 期間 48h で 06:00台 6860 vs 07:00台 3848 などの
  起動・停止跡）は 3 日連続稼働で平準化される
- 長期観察に耐えうることを実測で確認

PR7.5e（ClaudeSentimentProvider）デプロイ後も同じ計測スクリプトで
回帰確認を行う運用とする。

### 15.5 Phase別の設定差分（章23.9参照）

```yaml
# config/profile_phase2.yaml
phase: PHASE_2
account:
  initial_funding_usd: 300

risk:
  position_size_pct_of_capital: 0.10  # 少額なので大きめ
  daily_loss_limit_pct: 5.0           # 緩め

trading:
  long:
    enabled: true
  short:
    enabled: false                    # SHORT封印

# config/profile_phase4.yaml
phase: PHASE_4
account:
  initial_funding_usd: 3000

risk:
  position_size_pct_of_capital: 0.05
  daily_loss_limit_pct: 3.0

trading:
  long:
    enabled: true
  short:
    enabled: true                     # 解禁
```

---

## 16. 技術スタック

| 項目 | 選定 | 理由 |
|---|---|---|
| 言語 | Python 3.13 | 既存BOT資産流用 |
| HL接続 | `hyperliquid-python-sdk`（公式）+ WebSocket直接 | レイテンシ重要箇所はWS直 |
| 実行環境 | VPS（東京 or HL APIに近いリージョン） | 24/7稼働 |
| データ保存 | SQLite + JSONL | ローカル完結 |
| センチメント | Claude Sonnet 4 | moomoo踏襲 |
| 監視 | Discord Webhook 4ch | signal/alert/summary/error |

**VPS推奨スペック：** 2vCPU / 4GB RAM / 月$10前後（Vultr/Hetzner）

---

## 17. 月次想定コスト

| 項目 | 金額 |
|---|---|
| VPS | 約$10 |
| Claude API | 約$30〜50（flow=BUY時のみ呼ぶので節約効く） |
| HyperLiquid手数料 | Maker 0.015%（リベート条件達成時 -0.001%）/ Taker 0.045% |
| データソース | 全部無料（HL API・CoinGecko・CT scrape） |
| **合計** | **約$40〜60/月**（moomooの半分以下） |

---

## 18. 既存BOTからの流用率

| コンポーネント | 流用元 | 流用率 |
|---|---|---|
| sentiment_analyzer | moomoo | 90% |
| circuit_breaker | moomoo | 95% |
| position_sizer | moomoo（Kelly追加） | 70% |
| pnl_tracker | moomoo | 95% |
| notifier | moomoo | 100% |
| 動的スクリーナー骨格 | moomoo | 60% |
| ドライラン仕組み | auto-daytrade | 80% |
| ブラックリスト | auto-daytrade | 100% |
| 強制決済タイミング | 両方 | コンセプトのみ |

**流用ベースで全体の60%以上は最初から動く想定**。新規開発の主戦場は以下4箇所：
1. HyperLiquid接続
2. 清算予測
3. Maker-First執行
4. Funding連動

---

## 19. 主要フロー（1ループ・10秒間隔）

章11のアーキテクチャに沿った全体フロー：

```
[起動時]
0. main.py が依存組み立て
   ├─ ExchangeProtocol → HyperLiquidSDKClient
   ├─ SentimentProvider → ClaudeAPIProvider
   ├─ Repository → SQLiteRepo
   └─ Notifier → DiscordNotifier

1. StateReconciler.restore_on_startup() ← 章9.3
   ├─ HL側のポジション・注文・約定取得
   ├─ DBの未決済取引取得
   ├─ reconcile_positions()で純関数判定
   └─ 結果のアクションを実行

2. DataReadinessGate待機（最大30分） ← 章9.8
   └─ VWAP安定 + 5分足6本 + 24h高安取得まで

3. メインループ開始

[毎10秒・定常運用]
4. CircuitBreaker.check_all() ← 章9.7
   └─ 7段階チェック・発動時は緊急処理

5. ウォッチリストスキャン（最大16銘柄）← 章12
   各銘柄について EntryFlow.evaluate_and_enter():
   ├─ 既存ポジションあり？ → スキップ
   ├─ ブラックリスト？ → スキップ
   │
   ├─ build_snapshot()でMarketSnapshot構築
   │   ├─ ExchangeProtocol.get_market_snapshot()
   │   ├─ flow_detector（HL大口検出）
   │   ├─ price_context計算（章5）
   │   └─ レジーム判定
   │
   ├─ ① MOMENTUM + POSITION判定（章4）← 純関数
   │   └─ 不通過 → signals記録・スキップ
   ├─ ② FLOW判定 ← 純関数
   │   └─ 不通過 → signals記録・スキップ
   ├─ ④ REGIME + LIQUIDATION判定 ← 純関数
   │   └─ 不通過 → signals記録・スキップ
   ├─ ③ SENTIMENT判定（Claude API）← 章7
   │   └─ 不通過 → signals記録・スキップ
   │
   ├─ judge_long/short_entry() ← 純関数（章11.4）
   │   ├─ 全クリア → エントリー実行
   │   │   └─ place_order_with_retry()（章9.5・冪等）
   │   └─ 未達 → ドライラン記録（Phase 0-1時）

[並行: ポジション監視]
6. PositionMonitor.tick() （3秒ごと）
   ├─ SL/TP到達チェック
   ├─ update_vwap_state()でVWAP挙動追跡 ← 章6.4
   ├─ MFE/MAE更新
   ├─ トレーリングストップ更新
   ├─ Funding精算5分前 → 手仕舞い判定（章13）
   └─ クローズ時: vwap_state_to_record()でDB保存

[並行: 5分ごと]
7. StateReconciler.reconcile_periodic() ← 章9.6
   └─ HL側とDBの突合・乖離補正

[並行: WS監視]
8. WebSocketManager.monitor_connection() ← 章9.4
   └─ 30秒切断で全クローズ

[並行: 1時間ごと]
9. GasMonitor.check_periodic() ← 章9.9
   └─ Arbitrum ETH残高チェック
```

**この流れで章4〜11すべてが繋がる。**
新規エントリー判定は全て純関数化されており、APPLICATION層は薄い。

### 19.1 PR7.4 / PR7.5c-fix 実装で確定したログ・組み立て事項

#### 19.1.1 cycle ログのフォーマット（PR7.5c-fix）

旧 `entries=%d` 形式は entry_executed の値だけ表示していて状況把握しづらかった。
PR7.5c-fix で以下に拡張：

```
cycle done filled=N closed=N attempts=N executed=N dryrun=N errors=N cb=off|active duration=Xs
```

各カウンタの意味:
- `filled` : 当サイクルで entry 約定検知した数
- `closed` : 当サイクルで TP/SL 約定検知した数
- `attempts` : evaluate_and_enter を呼んだ回数（watchlist × directions）
- `executed` : 実際に grouped 発注した回数（dry_run 時はゼロ）
- `dryrun` : dry_run で 4 層通過した数（観察モードで signals テーブルに記録）
- `errors` : entry_flow が例外を出した回数
- `cb` : サーキットブレーカー状態（active なら entry スキップ済み）

#### 19.1.2 Scheduler の `_wait_or_shutdown` ハング対策（PR7.4）

`cycle_interval_seconds=0` でタイトループにならないよう、関数冒頭で
`await asyncio.sleep(0)` を必ず実行する（章 11.17.4 詳述）。
これが無いとテストの `request_shutdown` が走れずスケジューラが停止しない。

#### 19.1.3 main.py の依存組み立てと SIGTERM/SIGINT（PR7.5c）

```python
# src/main.py の async_main 起動シーケンス
1. load_settings(base, profile)        # YAML → AppSettings (pydantic)
2. setup_logging(settings)             # TimedRotatingFileHandler + stdout
3. load_secrets()                      # sops 復号
4. build_scheduler(settings, secrets)  # 全 INFRASTRUCTURE + APPLICATION 組み立て
5. await repo.initialize()             # SQLite schema 適用
6. install_signal_handlers(scheduler)  # SIGINT (+ SIGTERM Linux/Mac)
7. await scheduler.run()               # メインループ
8. (finally) await repo.close()
```

`install_signal_handlers` は SIGINT を必ず登録、SIGTERM は `hasattr(signal, "SIGTERM")`
で Linux/Mac のみ登録（Windows は SIGTERM 非対応のためスキップ）。

ハンドラの中身は単に `scheduler.request_shutdown()` を呼ぶだけ。
graceful shutdown は scheduler 側のループで現在のサイクル完了を待つ。

### 19.2 PR7.7 で増えた API コール数（1 サイクルあたり）

PR7.7 で BTC レジーム判定がローソク足ベースになり、1 サイクル
（10 秒間隔）あたりの REST API コール数が 7 → 9 に増加：

```
[PR7.6 時点 = 7 calls/cycle]
- get_market_snapshot("BTC")    # current_price + rolling_24h_open 等
- get_market_snapshot("ETH")
- get_open_interest("BTC")      # OI 履歴
- get_open_interest("ETH")
- get_fills(...)                # PositionMonitor の fill 検知
- get_positions()               # 突合 + ブレーカー入力
- get_open_orders()             # 突合

[PR7.7 以降 = 9 calls/cycle]
- 上記 7 calls
- get_candles("BTC", "15m", 60) # ★追加: EMA 用
- get_candles("BTC", "15m", 30) # ★追加: ATR 用
```

レート制限への影響:
- HL の REST レート制限は IP ベース 1200 req/min（章 22.2）
- 9 calls × 6 cycles/min = **54 req/min**（4.5% 消費）→ 余裕

将来最適化候補:
- 同じ cycle 内で BTC ローソク足を 2 回取らない（60 本取って 30 本分を再利用）
- 結果キャッシュ（次サイクル冒頭の interval 境界判定）
- 観察データから問題化したら別 PR で対応。Phase 0 の 9 calls/cycle で問題は出ていない。

---

## 20. 将来拡張機能の優先度マトリクス

ここに記載する機能は**Phase 4安定運用後**に検討する拡張案。
本体の4層AND判定が安定稼働してから着手する。

### 20.1 拡張機能一覧と優先度

| 機能 | 期待効果 | 実装難易度 | 仕様確認状況 | 推奨着手時期 |
|---|---|---|---|---|
| A. 自分の清算データFB | 中 | 低 | △ HL API確認必要 | Phase 4 + 1ヶ月 |
| B. BTC.D レジームフィルター | 高 | 低 | ✅ 外部API（CoinGecko）で取得可能 | Phase 4直後 |
| C. センチメントソース別重み | 中 | 中 | ✅ 設計可能 | データ100件蓄積後 |
| D. Whale Wallet追跡 | 中 | 中 | △ HL API確認必要 | 余裕があれば |
| E. Claudeマルチモーダル板分析 | 不明 | 高 | △ コスト未検証 | 実験段階 |

### 20.2 各機能の詳細

#### A. 自分の清算データをフィードバック

**やること：**
- 過去の清算履歴を `clearinghouseState` から定期取得
- 同銘柄を24時間ブラックリスト化（章9のblacklist機構を流用）

**実装場所：** `src/application/self_liquidation_monitor.py`

**未確認事項：**
- HL APIで自分の清算履歴を取得するエンドポイント
- 清算と通常のSL約定の区別方法

#### B. BTC支配率（Dominance）レジームフィルター ★最優先

**やること：**
- CoinGecko APIから BTC.D を取得（無料・15分更新）
- BTC.D 上昇局面 → アルトコインLONGを禁止
- BTC.D 下落局面 → アルト解禁

**判定ロジック（純関数）：**
```python
def should_allow_alt_long(btc_d_current: float, btc_d_24h_ago: float) -> bool:
    btc_d_change_pct = (btc_d_current - btc_d_24h_ago) / btc_d_24h_ago * 100
    return btc_d_change_pct < 0.5  # +0.5%以上は警戒
```

**設定値：** `trading.alt_filter.btc_d_24h_threshold: 0.5`

#### C. センチメントのソース別重み付け

**やること：**
- `sentiment_logs`の実勝率データから、ソース別に補正係数を学習
- 例：CoinTelegraph由来は0.85倍・CoinDesk由来は1.0倍

**前提：** 100件以上のサンプル必要（Phase 3〜4で蓄積）

**実装：** `core/sentiment_weighting.py`の純関数

#### D. Whale Wallet追跡

**やること：**
- 著名トレーダーのアドレスを監視リストに登録
- 彼らのポジション開閉をシグナルに加える

**注意：**
- 個人を特定するアドレスを直書きしない
- 公開情報（X/HypurrScan等）から手動収集
- Phase 4安定後の実験機能

#### E. Claudeマルチモーダル板分析

**やること：**
- 板情報をPNG画像化してClaude Vision APIに渡す

**懸念：**
- API呼び出しコスト増（画像はテキストの数倍）
- 効果が未検証

**判断：** プロトタイプを別ブランチで試作・効果が出たら統合検討

### 20.3 拡張機能の追加方針

```
1. 本体（章4の4層AND）が安定稼働している
2. 設定値（章23）に新機能のON/OFFフラグを追加
3. デフォルトはOFF（既存ロジックに影響しない）
4. ドライランで効果検証（章15のPhase 1相当）
5. データで効果が確認できたらON
```

これにより本体の安定性を損なわずに段階的に拡張できる。


---

## 21. Claude Code向け実装メモ

このMDをClaude Codeに渡して実装する際の最終チェックリスト。

### 21.1 設計上の最重要ポイント（妥協しないこと）

1. **TDDで進める（章11参照）**
   - CORE層は純関数・100%テスト・I/O禁止
   - ADAPTERSはProtocolのみ
   - INFRASTRUCTUREでProtocol実装
   - APPLICATIONは薄いユースケース層
   - **判定ロジックを副作用と混ぜない**（章9.3の例参照）

2. **エントリー判定は必ず4層AND（章4参照）**
   - 単純な「N本中M本上昇」のような判定は絶対に作らない（auto-daytradeで実証済みの失敗パターン）
   - VWAP乖離・始値乖離・モメンタム・出来高の4要素は省略しない
   - 章4.4のClaude API呼び出し順序を厳守（コスト最適化）

3. **執行は必ずMaker-First（章14参照）**
   - 成行注文はSL発動時のみ
   - エントリー・TPは原則Post-Only指値
   - スリッページがauto-daytrade最大の損失要因だったことを忘れない

4. **段階的ロールアウト厳守（章15参照）**
   - Phase 0でのデータ収集を飛ばさない
   - いきなりPhase 4に行かない

5. **障害対応を後付けしない（章9参照）**
   - 状態復元・冪等性・突合は最初から実装
   - サーキットブレーカーは7段階で

### 21.2 実装順序（章11.11のロードマップ）

詳細は章11.11参照。要約：

```
Week 1-3: CORE層全部（純関数・100%テスト）
Week 4:   ADAPTERS Protocol定義
Week 4-5: INFRASTRUCTURE（HL接続・testnet確認）
Week 5:   INFRASTRUCTURE（Claude・SQLite）
Week 6:   APPLICATION（entry_flow・position_monitor・reconciliation）
Week 7:   main.py組み立て・E2Eテスト・Phase 0開始
Week 8:   Phase 1（ドライラン）
```

### 21.3 既存BOTから流用するファイル（章11.13マッピング）

```
moomoo-trader/src/signals/sentiment_analyzer.py
  → hl-alpha-bot/src/infrastructure/claude_provider.py
  （ニュースソースをCrypto系に・章7のプロンプトに差し替え）

moomoo-trader/src/risk/circuit_breaker.py
  → hl-alpha-bot/src/core/circuit_breaker.py
  （純関数化・章9.7の7段階対応）

moomoo-trader/src/risk/position_sizer.py
  → hl-alpha-bot/src/core/position_sizer.py
  （Kelly基準を追加・純関数化）

moomoo-trader/src/monitor/pnl_tracker.py
  → hl-alpha-bot/src/infrastructure/sqlite_repo.py の一部
  （章8のスキーマで再実装）

moomoo-trader/src/monitor/notifier.py
  → hl-alpha-bot/src/infrastructure/discord_notifier.py
  （Notifier Protocolを実装する形に）

auto-daytrade/src/utils/（ブラックリスト機構部分）
  → hl-alpha-bot/src/core/blacklist.py + sqlite_repo
  （純関数化）
```

### 21.4 環境変数（.env）の想定

```bash
# HyperLiquid（章10参照・Agent Walletキーを使用）
HL_AGENT_PRIVATE_KEY=
HL_MAIN_ADDRESS=
HL_SUB_ACCOUNT=hl-alpha-bot
HL_NETWORK=mainnet  # testnet / mainnet

# Claude
ANTHROPIC_API_KEY=

# Discord Webhooks（4チャンネル）
DISCORD_WEBHOOK_SIGNAL=
DISCORD_WEBHOOK_ALERT=
DISCORD_WEBHOOK_SUMMARY=
DISCORD_WEBHOOK_ERROR=

# 運用モード
TRADE_MODE=PHASE_0  # PHASE_0 / PHASE_1 / PHASE_2 / PHASE_3 / PHASE_4
INITIAL_FUNDING_USD=2000
```

**重要：** 上記は`.env`ではなく`secrets/secrets.enc.yaml`にsops暗号化して保存（章10.4参照）。

### 21.5 必須テスト項目（運用前チェックリスト）

章9.13と11.11のDoDを統合：

#### CORE層テスト（Week 1-3）
- [ ] 全純関数のカバレッジ100%
- [ ] パラメトリックテストで境界値網羅
- [ ] hypothesisでプロパティベーステスト

#### 統合テスト（Week 6）
- [ ] EntryFlowが4アダプターを正しく呼ぶ
- [ ] StateReconciler判定→アクションの一貫性
- [ ] PositionMonitorのVWAP状態遷移

#### E2Eテスト（Week 7・testnet）
- [ ] testnet で注文発注・キャンセル
- [ ] WS切断・再接続の動作
- [ ] DBへの記録確認

#### 障害テスト（Phase 1前）
- [ ] BOT再起動でポジション復元（章9.3）
- [ ] WS切断30秒で全クローズ（章9.4）
- [ ] 同じclient_order_idで二重発注しない（章9.5）
- [ ] サーキットブレーカー発動で全クローズ（章9.7）
- [ ] Claude API障害で既存ポジション継続（章9.10）
- [ ] 緊急停止コマンド動作（章9.12）

### 21.6 既知の罠（auto-daytrade/moomooで踏んだもの）

1. **約定価格の取得バグ**（auto-daytrade 4/10修正）
   - 注文時のexec_priceではなく、保有ポジション情報のpriceを使う
   - HL実装時も同じ罠に注意

2. **小数点処理**（auto-daytrade 4/17修正）
   - SL/TP価格はint切り捨てではなくround四捨五入 + 最低1tick差を保証
   - 暗号資産はtick sizeが銘柄ごとに違うので要注意

3. **強制決済率92%問題**（moomoo未解決）
   - TP/SL到達前に時間切れになるケースが多発
   - HL実装ではトレーリングストップ + 動的TP調整で対処
   - **MFE/MAEログ（章8.2）でTP乗数最適化を継続**

4. **連続エントリー防止**（auto-daytrade 4/15）
   - 損切り直後の同銘柄リエントリーは負けやすい
   - ブラックリスト機構を必ず移植（章9参照）

5. **VWAPは記録するだけでは効かない**（moomoo未解決）
   - エントリー条件・保有中追跡・分析クエリの3点で活用（章6）

6. **段階投入を飛ばすと痛い目を見る**（両BOT共通）
   - Phase 2を$200から始める意味を理解（章10.6）

### 21.7 Claude Codeへのプロンプトテンプレート

```
hl-alpha-bot を以下の方針で実装してください。

【設計書】
hl-alpha-bot-design.md を参照してください。
特に章11（TDDアーキテクチャ）を厳守。

【実装方針】
1. TDDで進める（Red→Green→Refactor）
2. CORE層は純関数のみ・I/O禁止・テスト100%
3. ADAPTERSはProtocolのみ定義
4. INFRASTRUCTUREでProtocol実装
5. APPLICATIONは薄いユースケース層

【実装順】
Week 1: core/models.py（MarketSnapshot等）+ テストヘルパー
Week 1-2: core/entry_judge.py + core/price_context.py
Week 2: core/vwap.py（VWAPState + update_vwap_state）
Week 2-3: core/{position_sizer,stop_loss,circuit_breaker}.py
Week 3: core/reconciliation.py
Week 4: adapters/* Protocol定義
Week 4-5: infrastructure/hyperliquid_client.py（testnet確認）
Week 5: infrastructure/claude_provider.py + sqlite_repo.py
Week 6: application/{entry_flow,position_monitor,reconciliation}.py
Week 7: main.py + E2Eテスト

【テスト】
- パラメトリックテストで境界値網羅
- ヘルパー関数 make_snapshot() を活用
- AsyncMockで統合テスト
- testnet/E2Eは @pytest.mark.e2e で分離

各Weekの完了基準：
- 全テスト緑
- mypy strict 通過
- ruff・blackエラーなし
- カバレッジ目標達成
```

### 21.8 設計書の使い方ガイド

**章を読む順序の推奨：**

```
初読: 1 → 2 → 3 → 4 → 11
  全体像 + 4層AND + アーキテクチャを把握

詳細実装時:
  CORE実装 → 4, 5, 6, 11
  INFRASTRUCTURE → 7（Claude）, 8（DB）, 9（HL）
  APPLICATION → 9, 11
  運用設計 → 10, 15

実弾運用前: 9（障害対応）+ 10（資金管理）を再読
```

---

## 22. HyperLiquid API仕様（実調査ベース）

公式ドキュメントとSDKリポジショントリを実調査して得た仕様。設計書の他の章はこれを前提としている。

### 22.1 エンドポイント

| 種類 | URL（mainnet） | URL（testnet） |
|---|---|---|
| Info（読み取り） | `https://api.hyperliquid.xyz/info` | `https://api.hyperliquid-testnet.xyz/info` |
| Exchange（書き込み） | `https://api.hyperliquid.xyz/exchange` | `https://api.hyperliquid-testnet.xyz/exchange` |
| WebSocket | `wss://api.hyperliquid.xyz/ws` | `wss://api.hyperliquid-testnet.xyz/ws` |

すべて**HTTP POST**。GETは存在しない。リクエストボディはJSON。

### 22.2 レート制限（重要）

#### IPベース制限（厳しめ）

```
REST: 1200 weight/分（リセットは1分単位）

Weight計算：
- exchange API: weight = 1 + floor(batch_length / 40)
- info API:
  - weight 2:  l2Book / allMids / clearinghouseState / orderStatus /
              spotClearinghouseState / exchangeStatus
  - weight 60: userRole
  - その他:    weight 20

WebSocket:
- 最大接続数: 10/IP
- 新規接続: 30/分まで
- 最大subscriptions: 1000
- ユーザー固有subscriptionsの最大ユニーク数: 10
- 送信メッセージ: 2000/分（全接続合算）
- 同時inflight POST: 100
```

#### アドレスベース制限

```
基本: 1 USDC取引で1リクエスト
初期buffer: 10000リクエスト
制限到達時: 10秒に1リクエストのみ許可

例: $100の注文1件で +1リクエスト権利
   $50,000の取引で +50,000リクエスト権利

cancelの累積制限: min(limit + 100000, limit * 2)
→ 制限到達後もcancelは緩い
```

**設計への影響：**
- 16銘柄を10秒間隔でスキャン → 1分間に96リクエスト → IP制限内
- ただし各スキャンでweight 2のl2Book + weight 20のmetaなどを呼ぶと逼迫
- **WebSocket subscribe + 差分更新で大幅にリクエスト数を削減**すべき

### 22.3 アドレス・キー体系

```
Master Wallet（人間のEOA）
  │ 署名でAgent Wallet承認
  ▼
Agent Wallet（API Wallet）
  ├─ 名前付きAgentは最大3個（Master）+ 2個（Subaccount毎）
  ├─ 取引のみ可能
  ├─ 出金不可
  └─ 期限切れあり（再承認必要）

SubAccount
  ├─ 秘密鍵を持たない（Master署名で操作）
  ├─ 独立したアドレスを持つ
  └─ 別ユーザーとして扱われる（rate limit別計算）
```

**設計への影響：**
- 章10.3のAgent Wallet方式は実装可能（`approveAgent`アクション）
- SubAccount方式も実装可能（vaultAddress指定で署名）
- **Agentキーは期限切れがある**ので章10.11のローテーション計画は妥当

### 22.4 注文タイプ（重要）

#### TIF（Time-In-Force）

| 値 | 名称 | 動作 |
|---|---|---|
| `Alo` | **Add Liquidity Only** | Post-Only。即時マッチしそうなら**拒否**（キャンセル） |
| `Ioc` | Immediate Or Cancel | 即時約定可能分のみ・残りキャンセル |
| `Gtc` | Good Till Cancel | 板に乗って約定or明示的キャンセルまで |

#### Trigger注文（TP/SL）

```
Stop Market: 価格到達でmarket order発火
Stop Limit:  価格到達でlimit order発火
Take Market: 利確用market（LONG時 trigger < mid・SHORT時 trigger > mid）
Take Limit:  利確用limit
```

#### 構造（exchange APIの`order`アクション）

```python
{
    "action": {
        "type": "order",
        "orders": [{
            "a": 0,              # asset index（universeのインデックス）
            "b": True,            # is_buy
            "p": "50000",         # price (string・trailing zeros禁止)
            "s": "0.01",          # size (string)
            "r": False,           # reduce_only
            "t": {
                "limit": {"tif": "Alo"}   # Post-Only
                # または "trigger": {"isMarket": True, "triggerPx": "...", "tpsl": "tp" or "sl"}
            }
        }],
        "grouping": "na",         # "normalTpsl"でエントリーとTP/SLを連結可能
    },
    "nonce": 1705234567890,       # 現在timestamp(ms)
    "signature": {...},
    "vaultAddress": null,         # subaccount/vault時に設定
}
```

**設計への影響：**
- 章14のMaker-First執行は**TIF=Alo**で実現
- ALO拒否時は`status: error`が返るので再評価ロジック必須
- 章9.5の冪等注文設計：HLには`client_order_id`相当として`cloid`がある（任意）

#### 最小注文額（実機検証済み）

```
最小名目額: $10 USDC
```

これより小さい注文（size × price < $10）は HL 側で拒否される：

```
"Order must have minimum value of $10. asset=N"
```

サイズ計算（章13）では `size_coins × price >= $10` を保証する必要がある。
特に Phase 2-4 の小額運用時は、leverage と価格次第で**最小サイズが大きくなる**ため事前検算必須。

#### SDK 経由の数値型（罠あり）

公式 hyperliquid-python-sdk を使う場合、`order_type` dict 内の数値フィールドは
**float で渡す必要がある**：

```python
# ❌ NG（SDK の signing.py で TypeError）
{"trigger": {"triggerPx": "70000", ...}}  # str はダメ

# ✅ OK
{"trigger": {"triggerPx": float(trigger_price), ...}}
```

理由：SDK 内部で `f"{x:.8f}"` のような書式指定で wire 形式（文字列）に変換する。
公式 REST API の最終形式は文字列だが、SDK が内部で float→string 変換するため、
Python 側からは float を渡すべき。

これは `place_order` の `limit_px` や `trigger_order` の `triggerPx` 共通の慣習。

#### grouped 注文（normalTpsl）の挙動（実機検証済み）

```python
# entry + TP + SL を1コマンドで連結（章14.6）
results = await client.place_orders_grouped(entry, tp, sl)
# → tuple[OrderResult, ...] 順番は (entry, tp, sl)
```

**重要な仕様：**
1. `entry` は通常通り板に乗る → `order_id` 取得可能
2. `tp` / `sl` は **entry が約定するまで HL 側で保留** → 即時 `order_id` は **None**
3. entry 約定で tp/sl が自動発注される（その時点で order_id 確定）
4. entry をキャンセルすれば tp/sl も自動的に無効化
5. tp/sl の order_id を取得するには、約定後に `open_orders` で再取得

**実装上の影響：**
- `OrderResult.success=True` でも `order_id=None` のケースは正常動作
- これは「失敗」ではなく「保留中」
- Repository で `open_trade` する時点では tp/sl の order_id は紐付け不可
- **約定検知（PR7.2 position_monitor）で order_id を後付けで紐付ける**

### 22.5 価格・サイズの精度（必須知識）

```
Perps:
  MAX_DECIMALS = 6
  価格は最大5 significant figures
  かつ MAX_DECIMALS - szDecimals 桁の小数点以下まで

Spot:
  MAX_DECIMALS = 8
  同上計算

szDecimals: 銘柄ごとに異なる（meta endpointで取得）
  例: BTC szDecimals=5 → サイズは0.00001刻み

整数価格は常に有効（5SF制限を超えても）
  例: 123456 はOK・12345.6 はNG
```

**実装上の罠：**
- 価格・サイズの`p`/`s`フィールドは**文字列**（floatではない）
- **trailing zerosは禁止**（`"50000.0"`はNG・`"50000"`が正しい）
- SDKが自動処理する（公式Python SDKは`price_to_str()`等を提供）
- 章9.6既知の罠「小数点処理」はHLでも要注意

### 22.6 手数料体系（重要・章14更新）

#### Perps Tier 0（基本）

| 種別 | レート | 備考 |
|---|---|---|
| Maker | 0.015% | 基本は課金（リベートではない！） |
| Taker | 0.045% | |

#### Maker rebate（条件付き）

14日間のmaker volume shareに応じて：

| Maker Share | レート |
|---|---|
| ≥ 0.5% | -0.001%（リベート開始） |
| ≥ 1.5% | -0.002% |
| ≥ 3.0% | -0.003% |

**個人運用ではほぼ届かない。** 設計上はMaker手数料0.015%で計算する。

#### スタッキング割引

HYPEトークンステーキングで5%〜40%割引（>10 HYPE で5%、>500K HYPEで40%）。

#### Funding費用

```
8時間レートを1時間ごとに 1/8 ずつ精算
精算時刻: 毎時00分（UTC）
方向: 正→Long が Short に支払う
      負→Short が Long に支払う
プロトコル手数料なし（peer-to-peer）
```

**設計への影響：**
- 章9.5・章13の「Funding時刻30分前の手仕舞い」は**「精算5分前」に変更**（毎時なので頻繁になりすぎないよう短く）
- 章8.5の`funding_payments`テーブルは1時間ごとに記録（保有24h で最大24件）

### 22.7 重要なInfo APIエンドポイント

| type | weight | 用途 |
|---|---|---|
| `meta` | 20 | 全銘柄メタ情報（universe/szDecimals） |
| `metaAndAssetCtxs` | 20 | meta + 各銘柄の市況情報（一括） |
| `allMids` | 2 | 全銘柄のmid price（軽量） |
| `l2Book` | 2 | 板情報（指定銘柄） |
| `clearinghouseState` | 2 | ユーザーポジション・残高 |
| `openOrders` | 20 | ユーザーの未約定注文 |
| `orderStatus` | 2 | 注文状態 |
| `userFills` | 20+ | 約定履歴（200件単位で重み増加） |
| `userFundingHistory` | 20+ | Funding履歴 |
| `candleSnapshot` | 20+ | Kline（60本単位で重み増加） |

**サポートする candle interval：**
`1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 8h, 12h, 1d, 3d, 1w, 1M`

#### candleSnapshot を使う公開 API（PR7.7）

PR7.7 で BTC レジーム判定（章11.17.5）が `candleSnapshot` を使うようになり、
ADAPTERS / INFRASTRUCTURE 層に公開 API として `Candle` dataclass と
`ExchangeProtocol.get_candles` を追加した：

```python
# src/adapters/exchange.py
@dataclass(frozen=True)
class Candle:
    """ローソク足（章22.7 candleSnapshot）。timestamp_ms は開始時刻。"""
    symbol: str
    interval: str
    timestamp_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class ExchangeProtocol(Protocol):
    async def get_candles(
        self, symbol: str, interval: str, limit: int = 100
    ) -> tuple[Candle, ...]:
        """直近 limit 本のローソク足を返す（古い → 新しい順）。"""
        ...
```

実装 (`HyperLiquidClient.get_candles`) は既存の private `_fetch_recent_candles`
（dict のリストを返す）をラップして `Candle` tuple に変換する：

```python
async def get_candles(
    self, symbol: str, interval: str, limit: int = 100
) -> tuple[Candle, ...]:
    raw = await self._fetch_recent_candles(symbol, interval, limit)
    return tuple(
        Candle(
            symbol=symbol,
            interval=interval,
            timestamp_ms=int(c["t"]),
            open=Decimal(str(c["o"])),
            high=Decimal(str(c["h"])),
            low=Decimal(str(c["l"])),
            close=Decimal(str(c["c"])),
            volume=Decimal(str(c["v"])),
        )
        for c in raw
    )
```

interval 文字列はそのまま HL に渡す（`"15m"` 等）。
未対応 interval（例: `"7m"`）は `_INTERVAL_MS` の lookup で
`ExchangeError("Unsupported interval: ...")` を上げる。

#### SDK レスポンスのフィールド仕様（実機検証済み）

##### side フィールドの表現混在

`info.open_orders()` と `info.user_fills_by_time()` のレスポンスで、
`side` フィールドの表現が**箇所により異なる**：

| ソース | 値 |
|---|---|
| HL公式 REST API（生） | `"B"` / `"A"` (Bid/Ask) |
| SDK 経由（場所により） | `"B"` / `"A"` または `"buy"` / `"sell"` |

**INFRASTRUCTURE 層で正規化必須：**

```python
side: Literal["buy", "sell"] = "buy" if side_raw in ("B", "buy") else "sell"
```

##### orderStatus の状態文字列リスト

`info.query_order_by_oid()` レスポンスの `order.status` で実際に観測される文字列：

| HL の値 | Protocol の Literal |
|---|---|
| `"open"` | `"pending"` |
| `"triggered"` | `"pending"` |
| `"filled"` | `"filled"` |
| `"canceled"` または `"cancelled"`（綴り両方あり） | `"cancelled"` |
| `"rejected"` | `"rejected"` |

未知の状態は保守的に `"pending"` 扱いにする。

##### liquidationPx は null 可

`assetPositions[].position.liquidationPx` は以下の場合 `null`：
- ポジションサイズが小さい
- レバレッジが低い
- 含み益が大きく清算されにくい

実装では `Decimal | None` 型で扱う。

#### ALO 拒否のメッセージ実例（実機検証済み）

ALO 注文が即マッチしそうな価格で発注されると拒否される。
実際のメッセージ：

```
"Post only order would have immediately matched, bbo was 76382@76387. asset=3"
```

**`_raise_inner_error` の判定ロジック：**
- `"post only"` を含む → `OrderRejectedError(code="ALO_REJECT")`
- `"would have matched"` を含む → 同上
- `"would have immediately matched"` を含む → 同上
- `"ALO"` を含む（大文字小文字問わず）→ 同上

判定は `error_msg.lower()` で行い、複数キーワードを許容する。

### 22.8 重要なWebSocketサブスクリプション

| type | 内容 | 用途 |
|---|---|---|
| `allMids` | 全銘柄mid price更新 | 軽量な価格監視 |
| `l2Book` | 指定銘柄の板差分 | 板分析・VWAP計算 |
| `trades` | 指定銘柄の約定 | フロー検出 |
| `candle` | Kline更新 | 5分足モメンタム |
| `userEvents` | 自分の約定/注文 | ポジション管理 |
| `userFills` | 自分のFill | 約定通知 |
| `userFundings` | 自分のFunding精算 | コスト記録 |
| `notification` | 清算通知等 | アラート |

**重要：** 切断時の挙動は公式が明言：
> "All automated users should handle disconnects from the server side and gracefully reconnect.
> Disconnection from API servers may happen periodically and without announcement.
> Missed data during the reconnect will be present in the snapshot ack on reconnect."

**設計への影響：**
- 章9.4のWS切断対応は必須（公式が「定期的に切れる」と明言）
- 再接続時のsnapshot ackで**欠損データを補完**できる

### 22.9 SDK選定

| SDK | 言語 | 公式/コミュニティ | 推奨度 |
|---|---|---|---|
| **hyperliquid-python-sdk** | Python | **公式** | ★★★ 採用 |
| nktkas/hyperliquid | TypeScript | コミュニティ | ★★ |
| nomeida/hyperliquid | TypeScript | コミュニティ | ★★ |
| sonirico/go-hyperliquid | Go | コミュニティ | ★ |
| ccxt | 多言語 | サードパーティ | △ |

**hl-alpha-botは公式Python SDK採用：**
- リポジショントリ: https://github.com/hyperliquid-dex/hyperliquid-python-sdk
- 署名・wallet管理を自動化
- Info/Exchange両方カバー
- WebSocketサポート

ただし以下の点に注意：
- `infrastructure/hyperliquid_client.py`でラップして`ExchangeProtocol`を実装
- 内部でSDKを使うが、テストではモック化できる
- 章11.5のProtocol準拠で書く

### 22.10 署名・nonce

```
署名方式: EIP-712 typed data signing
nonce: 現在timestamp（ミリ秒）
expiresAfter: オプション・タイムスタンプ後は拒否
```

**重複nonce対策：**
- 同じnonceの再送は拒否される
- リトライ時は**新しいnonce**で送る必要がある
- ただしAgentキーで送ると**Master側でnonce消費**しないので影響少

### 22.11 Hyperliquid独自の拒否ケース（罠集）

実装で踏みやすい罠：

| エラー | 原因 | 対処 |
|---|---|---|
| `Post only order would have immediately matched, bbo was X@Y. asset=N` | Alo注文がマッチしそう（実機検証済みメッセージ） | 価格を1tick下げる/上げる・再発注 |
| `Order must have minimum value of $10. asset=N` | 最小注文額（$10）未満 | サイズを増やす |
| `Insufficient margin` | 証拠金不足 | サイズ縮小 or 別ポジションクローズ |
| `Tick size mismatch` | 価格精度違反 | szDecimalsベースで丸める |
| `User or API Wallet 0x... does not exist` | アドレス未初期化 or Agent未承認 or Agent秘密鍵とアドレス不一致 | 1 USDCを入金して活性化・Agent承認・整合性検証 |
| `Reduce only order would not reduce position` | reduce_onlyで増ポジションになる | order作成ロジック修正 |
| `Trigger price wrong side` | TP/SL価格がmid側を間違っている | 方向検証ロジック追加 |
| `Order limit reached` | 同時open order数超過（1000+5M USDCあたり1） | 古い注文をキャンセル |
| `Cannot claim drip because user 0x... does not exist on mainnet` | testnet Faucet が mainnet 活性化を要求 | mainnet で 5 USDC 入金後リトライ |

### 22.12 アクティベーションgas代

新規アドレスは **mainnet で 5 USDC** の入金で活性化される（実機検証済み・2026年現在）。
これを払わないと注文できない。

**重要な仕様（実機検証で判明）：**

```
HL mainnet 最低入金額: 5 USDC（活性化と兼用）
testnet Faucet の利用条件: アドレスが mainnet で活性化されていること
```

つまり **testnet で取引するためにも、まず mainnet で 5 USDC 入金が必要**。
これは Sybil 攻撃対策と思われる。

**testnet での開発フロー：**

```
1. Master Wallet（EOA）を準備
2. Arbitrum One で 5+ USDC を保有
3. HL mainnet (https://app.hyperliquid.xyz/) で 5 USDC 入金
   → アドレスが mainnet で活性化される
4. HL testnet (https://app.hyperliquid-testnet.xyz/) で Faucet
   → 1000 mock USDC 取得（mainnet 残高はそのまま）
5. testnet で開発・検証
6. mainnet 本番運用は別途追加入金
```

**設計への影響：**
- 章10.5 の Phase 0-2 投入額は **5 USDC + α** から始まる
- testnet 開発のためだけに 5 USDC のコストがかかる
- 実費約 $5（≒ ¥750）が開発前提

### 22.13 testnet利用方法

```
1. mainnet で 5 USDC 入金（活性化）← 必須前提
2. https://app.hyperliquid-testnet.xyz/ にアクセス
3. ウォレット接続（Mainnetと同じMetaMask等）
4. 「Faucet」から testnet USDC を取得（無料・1000 mock USDC/回）
5. 通常通り取引可能
```

**注意：** testnetのデータはmainnetと完全に分離。
testnet APIエンドポイント（`api.hyperliquid-testnet.xyz`）を使う。
ただし**活性化条件は mainnet 共通**（章22.12 参照）。

### 22.14 設計書への反映済み修正

本章調査の結果、以下を訂正済み：

| 修正前 | 修正後 |
|---|---|
| Funding 8時間ごと | **1時間ごとに精算（8h rateの1/8）** |
| Maker手数料 -0.01%（リベート） | **基本0.015%課金・条件達成時のみリベート** |
| 章14 Maker rebate ~0.01% | 0.015%課金・volume share≥0.5%でリベート |
| 章9 Funding30分前手仕舞い | **5分前**に変更（精算が頻繁なため） |
| funding_monitor 8h監視 | 1h監視 |
| 月次コスト Maker -0.01% | Maker 0.015%（リベート達成時 -0.001%） |

### 22.15 残るPENDING調査項目

実装着手後に詳細確認すべき項目：

- [ ] HIP-3 perp（新規上場銘柄）の取扱い・除外フィルター実装
- [ ] WebSocket post requestの活用（REST RPSを節約）
- [ ] STP（Self-Trade Prevention）の設定方法
- [ ] Vault（公開ファンド）と SubAccountの違いの正確な確認
- [ ] Agent Wallet期限切れの実時間
- [ ] 高congestion時の「2x maker share percentage」の意味
- [ ] HIP-3 growth modeの活用可否

これらは実装中にtestnetで検証する。

---

## 23. 設定管理設計

設計書の章4〜22で散在している全設定値を一元管理する仕組み。
**この章が「設定値の真実の源（Source of Truth）」となる。**

### 23.1 設計原則

```
原則1: 全設定値は1つの設定ファイルに集約
       → コードに直接ハードコードしない

原則2: 機密情報と通常設定を分離
       → secrets/ は暗号化・config/ はgit管理

原則3: 設定はpydanticで型・範囲検証
       → 不正値で起動しない

原則4: 環境別プロファイルで切り替え可能
       → dev/staging/prod の差分管理

原則5: 整合性制約は起動時にチェック
       → SL < TP・max_positions ≤ レバ等

原則6: 動的変更は安全な範囲のみ
       → 重大設定の変更はBOT再起動必須
```

### 23.2 ファイル階層構造

```
hl-alpha-bot/
├── secrets/                          # 機密情報（sops暗号化）
│   ├── secrets.enc.yaml              # 暗号化済み（git管理可）
│   ├── .age-key.example              # サンプル鍵
│   └── .gitignore                    # 実鍵除外
│
├── .sops.yaml                        # sops の設定（プロジェクトルート）
│
├── config/                           # 通常設定（git管理）
│   ├── settings.yaml                 # 基本設定（全環境共通）
│   ├── profile_dev.yaml              # 開発環境差分
│   ├── profile_staging.yaml          # ステージング差分
│   ├── profile_prod.yaml             # 本番環境差分
│   └── schema.py                     # pydanticスキーマ定義
│
├── src/
│   └── core/
│       └── config.py                 # 設定ローダー（純関数）
│
└── pyproject.toml                    # Python依存・lintツール（コード関連のみ）
```

#### .sops.yaml の正しい書き方（実機検証済み・罠あり）

`.sops.yaml` の `path_regex` は **入力ファイル名で判定される**。
出力先のファイル名（`.enc.yaml`）にだけマッチさせると `sops -e` で
「no matching creation rules found」エラーになる。

**推奨形：**

```yaml
# プロジェクトルートの .sops.yaml
creation_rules:
  - path_regex: secrets/.*\.yaml$       # 入力・出力両方にマッチ
    age: "age1abc..."                    # age 公開鍵（1行・必ずクォート）
```

**よくあるNG例：**

```yaml
# ❌ NG: secrets/secrets.yaml を sops -e する時マッチしない
creation_rules:
  - path_regex: secrets/.*\.enc\.yaml$  # .enc を含むパターン
    age: "..."
```

**Windows PowerShell での作成：**

```powershell
# Bash の heredoc は使えない。代わりに:
$content = @'
creation_rules:
  - path_regex: secrets/.*\.yaml$
    age: "age1..."
'@
[System.IO.File]::WriteAllText("$PWD\.sops.yaml", $content,
    [System.Text.UTF8Encoding]::new($false))   # BOM なし UTF-8
```

`Set-Content -Encoding utf8` は環境によって BOM を付けるので、
sops のパーサーで失敗することがある。`UTF8Encoding($false)` で BOM なし指定。

### 23.3 secrets.enc.yaml（暗号化機密情報）

```yaml
# secrets/secrets.enc.yaml（sops -e で暗号化）
hyperliquid:
  agent_private_key: "0x..."        # Agent Wallet秘密鍵（必ずクォート！）
  master_address: "0x..."           # Master Walletアドレス（必ずクォート！）
  agent_address: "0x..."            # Agent Walletアドレス（必ずクォート！）
  network: "testnet"                # mainnet / testnet

claude:
  api_key: "sk-ant-..."

discord:
  webhook_signal: "https://discord.com/api/webhooks/..."
  webhook_alert: "https://discord.com/api/webhooks/..."
  webhook_summary: "https://discord.com/api/webhooks/..."
  webhook_error: "https://discord.com/api/webhooks/..."

# 外部データソース（必要に応じて）
external:
  cryptopanic_api_key: ""
  reddit_client_id: ""
  reddit_secret: ""
```

#### YAML クォートの罠（実機検証で判明・必須対応）

`0x...` で始まるアドレスや秘密鍵は **必ずダブルクォートで囲む**：

```yaml
# ❌ NG: PyYAML が 16進数の整数として解釈してしまう
master_address: 0x910571363855665c9511f06ed7b691ab32fc1bd5

# ✅ OK
master_address: "0x910571363855665c9511f06ed7b691ab32fc1bd5"
```

**さらに罠：** `sops -e` / `sops -d` は **YAMLのクォートを剥がす**。
このため、復号後の値が int になってしまうケースが頻発する。

**対処：pydantic 側で int → hex 文字列への自動復元**

```python
class HyperLiquidSecrets(BaseModel):
    master_address: str
    agent_private_key: str
    agent_address: str
    network: Literal["mainnet", "testnet"]

    @field_validator("master_address", "agent_address", mode="before")
    @classmethod
    def coerce_address(cls, v: object) -> object:
        """int 化されたアドレスを 40桁 hex に復元"""
        if isinstance(v, int):
            return "0x" + format(v, "040x")  # ゼロパディング必須
        return v

    @field_validator("agent_private_key", mode="before")
    @classmethod
    def coerce_private_key(cls, v: object) -> object:
        """int 化された秘密鍵を 64桁 hex に復元"""
        if isinstance(v, int):
            return "0x" + format(v, "064x")
        return v
```

これで sops が int に変換してしまった値も正常に扱える。

#### Agent Wallet 整合性検証（起動時に必須）

`agent_address` と `agent_private_key` が**別の Wallet のもの**だと、
発注時に `User or API Wallet 0x... does not exist` エラーが出る。
これは過去に複数回 Generate した際の取り違えで起きやすい。

**起動時の自動検証ロジック：**

```python
def _validate_consistency(secrets: HyperLiquidSecrets) -> None:
    """agent_private_key から導出される address と
    agent_address の一致確認（起動時に必ず実行）"""
    from eth_account import Account
    derived = Account.from_key(secrets.agent_private_key).address
    if derived.lower() != secrets.agent_address.lower():
        raise SecretsLoadError(
            f"agent_address mismatch: "
            f"specified={secrets.agent_address}, derived={derived}. "
            f"Either fix agent_address in secrets.yaml or "
            f"approve {derived} in HyperLiquid UI."
        )

# load_secrets() の最後で必ず呼ぶ
def load_secrets(...) -> HyperLiquidSecrets:
    # ... 復号・パース・pydantic検証 ...
    secrets = HyperLiquidSecrets(...)
    _validate_consistency(secrets)  # ← 必須
    return secrets
```

これにより、設定ミスを起動時に即座に検出できる。

### 23.4 settings.yaml（通常設定・基本値）

設計書の章4〜22で定義した全設定値をここに集約：

```yaml
# config/settings.yaml
# ═══════════════════════════════════════════════════════════
# Phase管理
# ═══════════════════════════════════════════════════════════
phase: PHASE_0   # PHASE_0 / PHASE_1 / PHASE_2 / PHASE_3 / PHASE_4

# ═══════════════════════════════════════════════════════════
# トレーディング設定（章4・5・6・13・14）
# ═══════════════════════════════════════════════════════════
trading:
  # エントリー判定（4層AND・章4）
  long:
    # ① MOMENTUM + POSITION（章4・5・6）
    vwap_min_distance_pct: 0.0      # VWAPより上が必要
    vwap_max_distance_pct: 0.5      # VWAP+0.5%以内
    utc_day_change_max_pct: 0.05    # 章5: UTC始値+5%以内
    rolling_24h_change_max_pct: 0.10 # 章5: 24h前から+10%以内
    position_in_24h_range_max: 0.85 # 章5: 24h高値圏(85%)以下
    momentum_5bar_min_pct: 0.3      # 5本前比+0.3%以上

    # ② FLOW（章4・章11.6.3）
    flow_layer_enabled: false       # WS trades 実装まで bypass（PR6.5想定でtrueに）
    flow_buy_sell_ratio_min: 1.5
    flow_large_order_size_usd: 50000  # >$50k 約定を「大口」とする
    volume_surge_ratio_min: 1.5     # 直近20本平均比1.5倍

    # ③ SENTIMENT（章7）
    sentiment_score_min: 0.6
    sentiment_confidence_min: 0.7
    # sentiment_score_max: null      # Phase 3以降にデータで決定（章7.6）

    # ④ REGIME + LIQUIDATION（章4）
    btc_ema_short_period: 20
    btc_ema_long_period: 50
    funding_rate_max_8h: 0.01
    btc_atr_max_pct: 5.0            # ATR%が極端に高い時は除外

  short:
    # LONGと対称（章4 SHORT条件）
    vwap_min_distance_pct: -0.5
    vwap_max_distance_pct: 0.0
    utc_day_change_min_pct: -0.05
    rolling_24h_change_min_pct: -0.10
    position_in_24h_range_min: 0.15
    momentum_5bar_max_pct: -0.3

    flow_layer_enabled: false       # WS trades 実装まで bypass
    flow_sell_buy_ratio_min: 1.5
    flow_large_order_size_usd: 50000
    volume_surge_ratio_min: 1.5

    sentiment_score_max: -0.3
    sentiment_confidence_min: 0.7

    funding_rate_min_8h: 0.03       # 0.03%超で買い過熱判定

  # SL/TP（章13）
  stop_loss:
    atr_period: 14
    atr_timeframe: "1h"
    atr_sl_multiplier: 1.5          # SL = ATR × 1.5
    atr_tp_multiplier: 2.5          # TP = ATR × 2.5
    trailing_after_atr: 1.0         # +1ATR含み益後トレーリング発動
    min_tick_buffer: 1              # SL/TP価格の最低1tick差保証

  # 執行（章14）
  execution:
    use_post_only: true             # Post-Only(ALO)優先
    post_only_retry_attempts: 3
    post_only_retry_wait_sec: 30    # 30秒未約定でキャンセル
    sl_use_market: true             # SL発動時はmarket
    sl_max_slippage_pct: 0.1        # ストップリミット時の上限

# ═══════════════════════════════════════════════════════════
# リスク管理（章13・章9.7）
# ═══════════════════════════════════════════════════════════
risk:
  # ポジションサイジング
  position_size_pct_of_capital: 0.05  # 口座の5%
  max_leverage: 3                      # HL設定値と一致させる
  max_positions_long: 3
  max_positions_short: 2

  # サーキットブレーカー（章9.7・7段階）
  daily_loss_limit_pct: 3.0            # Layer 1
  weekly_loss_limit_pct: 8.0           # Layer 2
  consecutive_loss_count: 3            # Layer 3
  consecutive_loss_size_halve: true    # 連敗時サイズ半減

  flash_crash_threshold_pct: 5.0       # Layer 4: 1分5%変動
  btc_anomaly_threshold_pct: 3.0       # Layer 5: BTC 5分3%変動
  api_error_rate_max: 0.30             # Layer 6: 5分30%エラー
  position_overflow_multiplier: 1.5    # Layer 7: 上限の1.5倍

  # Funding管理（章13・章22）
  funding_exit_minutes_before: 5       # 精算5分前手仕舞い判定（章22.6で1h精算反映）

  # 強制決済
  max_holding_hours: null              # 24時間で強制決済する場合は数値
                                       # 仮想通貨は時間制限なし設計（株BOTと違う）

# ═══════════════════════════════════════════════════════════
# ウォッチリスト（章12）
# ═══════════════════════════════════════════════════════════
watchlist:
  fixed:
    - BTC
    - ETH
    - SOL
    - BNB
    - XRP
    - DOGE
    - AVAX
    - LINK

  dynamic:
    enabled: true
    refresh_hours: 4
    max_count: 8
    volume_24h_min_usd: 50000000
    book_depth_min_usd: 200000
    spread_max_pct: 0.1
    age_min_days: 7                    # 上場後1週間以上経過
    extreme_funding_threshold: 0.05    # 極端Funding（候補）

  exclude:
    - "USDC-PERP"
    # ミームコイン等を必要に応じて追加

# ═══════════════════════════════════════════════════════════
# SENTIMENT（章7）
# ═══════════════════════════════════════════════════════════
sentiment:
  cache_ttl_seconds: 300
  text_lookback_hours: 6
  text_max_count: 10
  text_max_chars: 500
  similarity_dedup_threshold: 0.8

  sources:
    - coindesk
    - cointelegraph
    - cryptopanic
    - reddit
    # - twitter  # Phase 4以降の検討

  claude:
    model: "claude-sonnet-4-5-20250929"
    max_tokens: 1000
    timeout_seconds: 15
    use_prompt_caching: true

# ═══════════════════════════════════════════════════════════
# データロギング（章8）
# ═══════════════════════════════════════════════════════════
logging:
  database:
    path: "data/hl_bot.db"
    journal_mode: WAL
    backup_daily: true
    backup_retention_days: 30
    vacuum_monthly: true

  csv_export:
    enabled: true
    daily_export_time_utc: "23:59"
    monthly_summary: true
    yearly_tax_export: true

  application_log:
    level: INFO                        # DEBUG / INFO / WARNING / ERROR
    rotation: daily
    retention_days: 30
    format: json                       # json / text

# ═══════════════════════════════════════════════════════════
# 障害対応（章9）
# ═══════════════════════════════════════════════════════════
fault_tolerance:
  websocket:
    heartbeat_timeout_sec: 30
    reconnect_max_attempts: 3
    emergency_close_after_disconnect_sec: 30
    backoff_initial_sec: 5
    backoff_multiplier: 2

  reconciliation:
    startup_required: true
    periodic_interval_sec: 300

  data_readiness:
    required_5min_bars: 6
    required_vwap_minutes: 30

  order_retry:
    max_attempts: 3
    timeout_sec: 10

# ═══════════════════════════════════════════════════════════
# HyperLiquid API（章22）
# ═══════════════════════════════════════════════════════════
hyperliquid:
  endpoints:
    mainnet:
      info: "https://api.hyperliquid.xyz/info"
      exchange: "https://api.hyperliquid.xyz/exchange"
      websocket: "wss://api.hyperliquid.xyz/ws"
    testnet:
      info: "https://api.hyperliquid-testnet.xyz/info"
      exchange: "https://api.hyperliquid-testnet.xyz/exchange"
      websocket: "wss://api.hyperliquid-testnet.xyz/ws"

  fees:
    maker_rate_pct: 0.015              # 章22.6: 基本Maker
    taker_rate_pct: 0.045              # 章22.6: 基本Taker
    expected_rebate_rate_pct: 0.0      # 個人運用ではほぼ0

  rate_limit:
    rest_weight_per_minute: 1200
    ws_max_subscriptions: 1000
    ws_max_connections: 10

# ═══════════════════════════════════════════════════════════
# メインループ
# ═══════════════════════════════════════════════════════════
loop:
  scan_interval_sec: 10                # ウォッチリストスキャン間隔
  position_monitor_interval_sec: 3     # ポジション監視間隔
  gas_monitor_interval_sec: 3600       # ガス代チェック間隔

# ═══════════════════════════════════════════════════════════
# アカウント・資金管理（章10）
# ═══════════════════════════════════════════════════════════
account:
  initial_funding_usd: 2000
  max_funding_usd: 5000
  monthly_sweep_threshold_pct: 20      # 20%超で引き上げ推奨通知

  capital_review:
    enabled: true
    schedule: "monthly_first"
    increase_pf_threshold: 1.5
    decrease_pf_threshold: 1.0
    panic_pf_threshold: 0.5

```

### 23.5 環境別プロファイル（profile_*.yaml）

settings.yamlを基準として、環境ごとに**差分のみ**記述：

```yaml
# config/profile_dev.yaml（開発環境）
# settings.yamlからの差分のみ

phase: PHASE_0

hyperliquid_network: testnet         # 本番ではmainnet

trading:
  long:
    sentiment_score_min: 0.5         # 開発時は緩めにしてエントリー試行を増やす

logging:
  application_log:
    level: DEBUG                     # 開発時はDEBUGレベル

risk:
  daily_loss_limit_pct: 1.0          # 開発時は厳しく
```

```yaml
# config/profile_staging.yaml（ステージング・testnet）
phase: PHASE_2
hyperliquid_network: testnet

account:
  initial_funding_usd: 100           # testnetなので小額
```

```yaml
# config/profile_prod.yaml（本番）
phase: PHASE_4
hyperliquid_network: mainnet

# 本番固有の厳格な設定
risk:
  daily_loss_limit_pct: 3.0          # 標準値
```

**起動時の指定：**
```bash
HL_PROFILE=prod python -m src.main
# settings.yaml + profile_prod.yaml をマージ
```

### 23.6 pydanticスキーマ定義

`config/schema.py`：

```python
# config/schema.py
from pydantic import BaseModel, Field, validator
from enum import Enum


class Phase(str, Enum):
    PHASE_0 = "PHASE_0"
    PHASE_1 = "PHASE_1"
    PHASE_2 = "PHASE_2"
    PHASE_3 = "PHASE_3"
    PHASE_4 = "PHASE_4"


class LongTradingConfig(BaseModel):
    """LONG エントリー条件（章4・5・6・7）"""
    vwap_min_distance_pct: float = Field(ge=0, le=2)
    vwap_max_distance_pct: float = Field(gt=0, le=2)
    utc_day_change_max_pct: float = Field(gt=0, le=0.2)
    rolling_24h_change_max_pct: float = Field(gt=0, le=0.5)
    position_in_24h_range_max: float = Field(gt=0, le=1.0)
    momentum_5bar_min_pct: float = Field(gt=0, le=2)
    flow_buy_sell_ratio_min: float = Field(ge=1.0, le=10)
    flow_large_order_size_usd: float = Field(gt=1000)
    volume_surge_ratio_min: float = Field(ge=1.0, le=10)
    sentiment_score_min: float = Field(ge=-1, le=1)
    sentiment_confidence_min: float = Field(ge=0, le=1)
    sentiment_score_max: float | None = None
    btc_ema_short_period: int = Field(ge=5, le=200)
    btc_ema_long_period: int = Field(ge=10, le=500)
    funding_rate_max_8h: float = Field(ge=0, le=1)
    btc_atr_max_pct: float = Field(gt=0, le=20)

    @validator("vwap_max_distance_pct")
    def vwap_max_gt_min(cls, v, values):
        if "vwap_min_distance_pct" in values:
            if v <= values["vwap_min_distance_pct"]:
                raise ValueError("vwap_max_distance_pct must be > vwap_min_distance_pct")
        return v

    @validator("btc_ema_long_period")
    def long_gt_short(cls, v, values):
        if "btc_ema_short_period" in values:
            if v <= values["btc_ema_short_period"]:
                raise ValueError("btc_ema_long_period must be > btc_ema_short_period")
        return v


class StopLossConfig(BaseModel):
    atr_period: int = Field(ge=5, le=100)
    atr_timeframe: str = Field(regex=r"^[0-9]+[mhd]$")
    atr_sl_multiplier: float = Field(gt=0, le=10)
    atr_tp_multiplier: float = Field(gt=0, le=20)
    trailing_after_atr: float = Field(gt=0, le=10)
    min_tick_buffer: int = Field(ge=1)

    @validator("atr_tp_multiplier")
    def tp_gt_sl(cls, v, values):
        if "atr_sl_multiplier" in values:
            if v <= values["atr_sl_multiplier"]:
                raise ValueError("TP multiplier must be > SL multiplier")
        return v


class RiskConfig(BaseModel):
    position_size_pct_of_capital: float = Field(gt=0, le=0.5)
    max_leverage: int = Field(ge=1, le=10)
    max_positions_long: int = Field(ge=0, le=20)
    max_positions_short: int = Field(ge=0, le=20)
    daily_loss_limit_pct: float = Field(gt=0, le=20)
    weekly_loss_limit_pct: float = Field(gt=0, le=50)
    consecutive_loss_count: int = Field(ge=1, le=20)
    flash_crash_threshold_pct: float = Field(gt=0, le=50)
    btc_anomaly_threshold_pct: float = Field(gt=0, le=20)
    api_error_rate_max: float = Field(ge=0, le=1)
    funding_exit_minutes_before: int = Field(ge=0, le=60)

    @validator("weekly_loss_limit_pct")
    def weekly_gt_daily(cls, v, values):
        if "daily_loss_limit_pct" in values:
            if v <= values["daily_loss_limit_pct"]:
                raise ValueError("weekly limit must be > daily limit")
        return v


class AccountConfig(BaseModel):
    initial_funding_usd: float = Field(gt=0, le=100000)
    max_funding_usd: float = Field(gt=0, le=1000000)
    monthly_sweep_threshold_pct: float = Field(gt=0, le=100)

    @validator("max_funding_usd")
    def max_ge_initial(cls, v, values):
        if "initial_funding_usd" in values:
            if v < values["initial_funding_usd"]:
                raise ValueError("max_funding must be >= initial_funding")
        return v


class FullConfig(BaseModel):
    """全設定の親スキーマ"""
    phase: Phase
    trading: TradingConfig
    risk: RiskConfig
    watchlist: WatchlistConfig
    sentiment: SentimentConfig
    logging: LoggingConfig
    fault_tolerance: FaultToleranceConfig
    hyperliquid: HyperliquidConfig
    loop: LoopConfig
    account: AccountConfig

    @validator("risk")
    def position_count_vs_leverage(cls, v, values):
        # max_positions_long * size_pct * leverage <= 1.0 (口座100%以下)
        total_long = v.max_positions_long * v.position_size_pct_of_capital * v.max_leverage
        total_short = v.max_positions_short * v.position_size_pct_of_capital * v.max_leverage
        if total_long + total_short > 1.5:  # 余裕を持って150%まで
            raise ValueError(
                f"Total position exposure too high: "
                f"long={total_long:.2%}, short={total_short:.2%}"
            )
        return v
```

### 23.7 設定ローダー（CORE層・純関数）

```python
# src/core/config.py
import os
import yaml
from pathlib import Path
from config.schema import FullConfig


def load_config(profile: str | None = None) -> FullConfig:
    """設定をロードして検証

    純関数：環境変数とファイルパスから設定を組み立てるだけ。
    副作用なし・テスト可能。
    """
    # 1. base settings.yamlをロード
    base_path = Path("config/settings.yaml")
    with open(base_path) as f:
        config_dict = yaml.safe_load(f)

    # 2. プロファイル指定があれば差分マージ
    profile = profile or os.getenv("HL_PROFILE", "dev")
    profile_path = Path(f"config/profile_{profile}.yaml")
    if profile_path.exists():
        with open(profile_path) as f:
            profile_diff = yaml.safe_load(f) or {}
        config_dict = deep_merge(config_dict, profile_diff)

    # 3. pydantic検証（不正値ならここで例外）
    return FullConfig(**config_dict)


def deep_merge(base: dict, override: dict) -> dict:
    """辞書を深くマージ（純関数）"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_secrets() -> dict:
    """sopsで暗号化されたsecretsをロード

    起動時のみ呼ぶ。テストではモックする。
    """
    import subprocess
    result = subprocess.run(
        ["sops", "-d", "secrets/secrets.enc.yaml"],
        capture_output=True,
        text=True,
        check=True,
    )
    return yaml.safe_load(result.stdout)
```

### 23.8 ホットリロード設計（限定的）

運用中の閾値調整を可能にする。ただし**安全な範囲のみ**：

```python
# 動的変更可能な設定（ホットリロード対応）
HOT_RELOADABLE = {
    "trading.long.sentiment_score_min",
    "trading.long.vwap_max_distance_pct",
    "trading.short.sentiment_score_max",
    "watchlist.dynamic.refresh_hours",
    "logging.application_log.level",
}

# 動的変更不可（再起動必須）
RESTART_REQUIRED = {
    "phase",                                 # Phase切替は影響大
    "hyperliquid_network",                   # mainnet/testnet切替
    "account.initial_funding_usd",
    "risk.max_leverage",                     # 既存ポジに影響
    "risk.max_positions_long",
}
```

**変更フロー：**

```
1. config/settings.yaml を編集
2. python scripts/validate_config.py で検証
3. SIGUSR1 でBOTに再読込シグナル
   → ホットリロード可能な値のみ更新
   → 再起動必須の値が変わっていたら警告ログ + 再起動指示
4. audit_logテーブルに変更を記録
```

**Discord経由の操作（将来）：**

```
!hlbot config get                      # 現在の設定表示
!hlbot config set trading.long.sentiment_score_min=0.65
!hlbot config validate                 # 検証のみ
!hlbot config reload                   # ホットリロード実行
```

### 23.9 Phase別設定差分

各Phaseで自動的に切り替わる設定値：

| 設定キー | Phase 0 | Phase 1 | Phase 2 | Phase 3 | Phase 4 |
|---|---|---|---|---|---|
| `account.initial_funding_usd` | 0 | 0 | 200-500 | 1000-2000 | 2000-5000 |
| `risk.position_size_pct_of_capital` | - | - | 0.10 | 0.05 | 0.05 |
| `risk.daily_loss_limit_pct` | - | - | 5.0 | 4.0 | 3.0 |
| `trading.long.sentiment_score_min` | 0.5 | 0.5 | 0.55 | 0.6 | 0.6 |
| 実弾発注 | ❌ | ❌ | ✅ | ✅ | ✅ |
| ドライラン記録 | ✅ | ✅ | ✅ | ✅ | ❌ |
| SHORT実弾 | ❌ | ❌ | ❌ | ❌ | ✅ |

これは`profile_phaseN.yaml`として実装可能。

### 23.10 設定値マッピング表（章ごとの参照）

設計書の各章で出てくる設定値が、settings.yamlのどこに対応するか：

| 章 | 設定値 | settings.yaml内の場所 |
|---|---|---|
| 4 | VWAP+0.5%以内 | `trading.long.vwap_max_distance_pct` |
| 4 | 5本前比+0.3% | `trading.long.momentum_5bar_min_pct` |
| 4 | 売買比 > 1.5 | `trading.long.flow_buy_sell_ratio_min` |
| 5.5 | UTC始値+5%以内 | `trading.long.utc_day_change_max_pct` |
| 5.5 | 24h前+10%以内 | `trading.long.rolling_24h_change_max_pct` |
| 5.5 | レンジ位置<0.85 | `trading.long.position_in_24h_range_max` |
| 7.4 | sentiment > 0.6 | `trading.long.sentiment_score_min` |
| 7.4 | confidence > 0.7 | `trading.long.sentiment_confidence_min` |
| 7.7a | キャッシュ300秒 | `sentiment.cache_ttl_seconds` |
| 9.4 | WS切断30秒 | `fault_tolerance.websocket.heartbeat_timeout_sec` |
| 9.7 | 日次-3% | `risk.daily_loss_limit_pct` |
| 9.7 | 週次-8% | `risk.weekly_loss_limit_pct` |
| 9.7 | 連敗3回 | `risk.consecutive_loss_count` |
| 10.6 | 投入額$2,000 | `account.initial_funding_usd` |
| 10.8 | レバ3倍 | `risk.max_leverage` |
| 12 | watchlist固定 | `watchlist.fixed` |
| 13 | ATR×1.5 | `trading.stop_loss.atr_sl_multiplier` |
| 13 | ATR×2.5 | `trading.stop_loss.atr_tp_multiplier` |
| 13 | Funding 5分前 | `risk.funding_exit_minutes_before` |
| 14 | Post-Only30秒 | `trading.execution.post_only_retry_wait_sec` |
| 19 | スキャン10秒 | `loop.scan_interval_sec` |
| 22.6 | Maker 0.015% | `hyperliquid.fees.maker_rate_pct` |

**この表により、設計書の数値変更時にどこを直すか即座に分かる。**

### 23.11 設定変更時の運用フロー

```
[閾値調整したい時]

1. SQLクエリで実績分析
   SELECT vwap_distance_pct, win_rate FROM ...
   → "VWAP+0.3%以下の方が勝率高い"と判明

2. config/settings.yaml を編集
   trading.long.vwap_max_distance_pct: 0.5 → 0.3

3. 検証
   python scripts/validate_config.py

4. テストで影響確認
   pytest tests/core/test_entry_judge.py -k "vwap"

5. git commit + PR
   主要な閾値変更はPRレビュー必須

6. ステージング適用（testnet）
   HL_PROFILE=staging で1日動作確認

7. 本番反映
   ホットリロード or 再起動

8. audit_log に記録
   何をいつ何故変更したかDB保存

9. 1週間後に効果検証
   変更前後の勝率比較クエリ実行
```

**auto-daytradeのGAPフィルター閾値変更（4/22）と同じ手法を、構造化したフロー。**

### 23.12 設定ファイル検証スクリプト

```python
# scripts/validate_config.py
"""設定ファイル検証スクリプト

CIでも実行する。設定変更が安全か事前にチェック。
"""
import sys
from src.core.config import load_config


def main():
    profiles = ["dev", "staging", "prod"]
    errors = []

    for profile in profiles:
        try:
            config = load_config(profile=profile)
            print(f"✅ {profile}: OK")

            # 追加の整合性チェック
            check_phase_funding_consistency(config)
            check_position_exposure(config)

        except Exception as e:
            errors.append(f"❌ {profile}: {e}")

    if errors:
        for err in errors:
            print(err)
        sys.exit(1)

    print("\n✅ All profiles validated")


def check_phase_funding_consistency(config):
    """Phaseと投入額の整合性"""
    phase_min_funding = {
        "PHASE_2": 200,
        "PHASE_3": 1000,
        "PHASE_4": 2000,
    }
    if config.phase.value in phase_min_funding:
        min_required = phase_min_funding[config.phase.value]
        if config.account.initial_funding_usd < min_required:
            raise ValueError(
                f"{config.phase.value} requires >= ${min_required}, "
                f"got ${config.account.initial_funding_usd}"
            )


def check_position_exposure(config):
    """ポジション総額が口座を超えないか"""
    total = (
        (config.risk.max_positions_long + config.risk.max_positions_short)
        * config.risk.position_size_pct_of_capital
        * config.risk.max_leverage
    )
    if total > 1.5:
        raise ValueError(f"Total exposure {total:.2%} too high")


if __name__ == "__main__":
    main()
```

### 23.13 既存BOTからの差分

| 項目 | auto-daytrade | moomoo-trader | hl-alpha-bot |
|---|---|---|---|
| 設定ファイル | settings.py（Python） | settings.py（Python） | **YAML + pydantic** |
| 環境別切替 | なし | なし | **profile_*.yaml** |
| 検証 | 起動時エラー頼み | 同左 | **pydantic + 整合性チェック** |
| 機密情報 | .env平文 | .env平文 | **sops + age暗号化** |
| ホットリロード | 不可 | 不可 | **限定的に可能** |
| 設定変更履歴 | gitログのみ | gitログのみ | **audit_log DB記録** |
| マッピング表 | なし | なし | **章23.10で全章を一覧化** |

### 23.14 Claude Code向け実装指示

```
設定管理は章23の方針で実装してください：

1. config/settings.yaml に全設定値を集約
   - コードに数値をハードコードしない
   - 章4-22の数値はすべて章23.10のマッピング表を参照

2. config/schema.py にpydanticスキーマを定義
   - Field()で範囲制約
   - @validatorで整合性制約（SL<TP等）

3. src/core/config.py にローダーを実装
   - 純関数として書く
   - profile_*.yamlの差分マージ対応

4. 起動時に必ず load_config() → 検証通過 → 使用

5. テスト時は make_config(**overrides) ヘルパーで差分指定

6. ハードコードされた数値を見つけたら設定化する
```

### 23.15 PR7.5c 実装で確定した AppSettings 構造

PR7.5c 実装で pydantic v2 + `extra="forbid"` で各セクションを 1:1 で APPLICATION 層
Config dataclass にマッピングする形で確定（章 11.16 参照）。

#### 23.15.1 AppSettings ルート

```python
# config/schema.py
class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: Literal["phase_0", "phase_1", "phase_2", "phase_3", "phase_4"] = "phase_0"
    exchange:        ExchangeSettings        = Field(default_factory=...)
    trading:         TradingSettings         = Field(default_factory=...)
    watchlist:       WatchlistSettings       = Field(default_factory=...)
    sentiment:       SentimentSettings       = Field(default_factory=...)
    storage:         StorageSettings         = Field(default_factory=...)
    scheduler:       SchedulerSettings       = Field(default_factory=...)
    position_monitor: PositionMonitorSettings = Field(default_factory=...)
    reconciliation:  ReconciliationSettings  = Field(default_factory=...)
    entry_flow:      EntryFlowSettings       = Field(default_factory=...)
    logging:         LoggingSettings         = Field(default_factory=...)
```

`extra="forbid"` で未知の YAML キーは即エラー（タイポを早期発見）。

#### 23.15.2 settings.yaml と Config dataclass の対応表

| 設定セクション | 対応する dataclass |
|---|---|
| `trading` | `EntryFlowConfig`（PR7.1）の dry_run / leverage / flow_layer_enabled / position_size_pct / sl_atr_mult / tp_atr_mult |
| `entry_flow` | `EntryFlowConfig` の oi_lookup_tolerance_minutes |
| `position_monitor` | `PositionMonitorConfig`（PR7.2）の 4 フィールド全部 |
| `reconciliation` | `ReconciliationConfig`（PR7.3）の 2 フィールド |
| `scheduler` | `SchedulerConfig`（PR7.4）の閾値含む 13 フィールド |
| `sentiment` | `FixedSentimentProvider`（PR7.5b）の score / confidence / reasoning |
| `storage` | `SQLiteRepository`（PR7.5a）の db_path |
| `logging` | `setup_logging`（PR7.5c）の log_file / rotation_when / rotation_backup_count / level |

#### 23.15.3 profile_*.yaml の最小構成（Phase 0 例）

```yaml
# config/profile_phase0.yaml
phase: phase_0

trading:
  is_dry_run: true             # 必ず true（観察モード）
  position_size_pct: 0.01      # Phase 0 は 1% に絞る

watchlist:
  directions:
    - LONG                     # Phase 0 は LONG のみで観察

sentiment:
  fixed_score: 0.8             # bullish 強制（章 15.4.1 参照）
  fixed_confidence: 0.9
  reasoning: "Phase 0 forced bullish for observation"
```

`load_settings(base, profile)` は `deep_merge` で再帰マージ：
- dict 同士は再帰マージ
- 片方が dict でなければ profile が完全上書き
- profile に書いていないキーは base のまま

---

## 24. バックテスト基盤

実装段階で「Phase 0開始前に過去データで4層ANDを検証する」ための基盤設計。
auto-daytradeで「いきなり仮想モード→負けた」ような事態を防ぐ。

### 24.1 設計原則

```
原則1: 本番コードと同じCORE層を使う
       → 純関数なので過去データを食わせるだけで動く

原則2: I/Oをモック化して実行
       → SentimentProvider・ExchangeProtocolを過去データfixtureで差し替え

原則3: 結果は本番と同じスキーマで保存
       → backtests テーブルにtradesとほぼ同じ列で記録

原則4: 過去データの取得方法を明確に
       → S3アーカイブ + candleSnapshot で構築
```

### 24.2 過去データ取得方針

| データ種別 | ソース | 粒度 | 取得方法 |
|---|---|---|---|
| OHLCV（5分・1h） | candleSnapshot API | 5m, 1h | 章22.7参照・60本単位で重み増 |
| 過去板スナップショット | S3 hyperliquid-archive | 不定期 | `aws s3 cp` |
| 過去Funding rate | userFundingHistory | 1h | 章22.7 |
| 過去clearinghouseState | 取得不可（自分のみ） | - | 自前で記録するしかない |
| センチメント過去データ | 取得不可（外部） | - | 過去ニュースは取れるが当時のClaudeスコアは再現不可 |

**重要：** SENTIMENTは過去データ再現が困難。バックテストでは**sentiment=固定値**でシミュレートする（章24.5）。

### 24.3 backtest_results テーブル

```sql
-- 章8の trades と類似だが backtest 専用
CREATE TABLE backtest_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_id     TEXT NOT NULL,         -- バックテスト実行ID（YYYYMMDD_NNN）
    config_hash     TEXT NOT NULL,         -- 設定値ハッシュ（再現性）
    symbol          TEXT NOT NULL,
    direction       TEXT,
    entry_time      DATETIME,
    entry_price     REAL,
    exit_time       DATETIME,
    exit_price      REAL,
    size_coins      REAL,
    pnl_usd         REAL,
    exit_reason     TEXT,                  -- 'TP' / 'SL' / 'TIMEOUT' / 'FUNDING'
    hold_minutes    REAL,
    mfe_pct         REAL,
    mae_pct         REAL,
    -- バックテスト固有
    sentiment_score_used REAL,             -- 固定値の場合
    skipped_reason  TEXT,                  -- エントリーしなかった理由
    layer_results   TEXT                   -- 4層判定のJSON
);

CREATE INDEX idx_backtest_id ON backtest_results(backtest_id);
CREATE INDEX idx_backtest_symbol ON backtest_results(symbol);
```

### 24.4 バックテスト実行スクリプト

```python
# scripts/backtest.py
"""バックテスト実行

使い方:
  python scripts/backtest.py --symbols BTC,ETH --start 2026-03-01 --end 2026-03-31
"""
import asyncio
from datetime import datetime
from src.core.entry_judge import judge_long_entry
from src.core.config import load_config


async def run_backtest(symbols: list[str], start: datetime, end: datetime):
    config = load_config(profile="dev")
    backtest_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    for symbol in symbols:
        # 1. 過去5分足データ取得
        candles_5m = await fetch_historical_candles(symbol, "5m", start, end)
        candles_1h = await fetch_historical_candles(symbol, "1h", start, end)

        # 2. 5分ごとにスナップショットを構築して判定
        for i in range(20, len(candles_5m)):  # 最低20本必要
            snap = build_market_snapshot_from_history(
                symbol=symbol,
                candles_5m=candles_5m[:i+1],
                candles_1h=get_1h_at_time(candles_1h, candles_5m[i].time),
                sentiment_score=0.7,         # 固定値（章24.5）
                sentiment_confidence=0.8,
            )

            # 3. 本番と同じ純関数で判定
            decision = judge_long_entry(snap, config)

            if decision.should_enter:
                # 4. 仮想エントリー → 後続ローソクでSL/TP判定
                result = simulate_position(
                    candles=candles_5m[i:],
                    entry_price=snap.current_price,
                    direction="LONG",
                    config=config,
                )

                # 5. backtest_results に保存
                save_backtest_result(backtest_id, symbol, snap, decision, result)


def simulate_position(candles, entry_price, direction, config):
    """ポジションシミュレーション（純関数）"""
    sl_price = calculate_sl(...)
    tp_price = calculate_tp(...)

    for candle in candles:
        if direction == "LONG":
            if candle.low <= sl_price:
                return {"exit_reason": "SL", "exit_price": sl_price, ...}
            if candle.high >= tp_price:
                return {"exit_reason": "TP", "exit_price": tp_price, ...}
    return {"exit_reason": "TIMEOUT", ...}
```

### 24.5 SENTIMENTの扱い

過去のClaude APIスコアは再現できないため、3パターンでシミュレート：

```yaml
# config/backtest.yaml
backtest:
  sentiment_modes:
    - name: "optimistic"
      score: 0.75
      confidence: 0.85
    - name: "neutral"
      score: 0.65
      confidence: 0.75
    - name: "pessimistic"
      score: 0.55      # 閾値ぎりぎり
      confidence: 0.70
```

各モードで実行して、結果のばらつきを確認。

### 24.6 バックテスト結果の分析

```sql
-- バックテストの全体成績
SELECT
    backtest_id,
    COUNT(*) AS trades,
    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS win_rate,
    SUM(pnl_usd) AS total_pnl,
    AVG(pnl_usd) AS avg_pnl,
    MAX(mae_pct) AS max_drawdown
FROM backtest_results
GROUP BY backtest_id
ORDER BY backtest_id DESC;

-- 銘柄別の成績比較
SELECT symbol, COUNT(*), AVG(pnl_usd), SUM(pnl_usd)
FROM backtest_results
WHERE backtest_id = '20260427_120000'
GROUP BY symbol;
```

### 24.7 Phase 0開始前の検証手順

```
1. 過去30日分のデータを取得
   python scripts/backtest.py --symbols BTC,ETH,SOL --start 30daysAgo

2. 4層ANDで何件エントリーしたか確認
   想定: 月20-50件（多すぎor少なすぎは設定見直し）

3. 各層のrejection ratioを確認
   どれか1層で90%+落ちていたら閾値調整

4. 仮想PnLが極端でないか確認
   月+10%や-10%超は設定が偏ってる兆候

5. SL/TP到達率を確認
   TP到達率5%未満なら章23のATR乗数を見直し

6. OK なら Phase 0 開始
```

### 24.8 制約事項（明示）

バックテストは万能ではない：

- **過去データの隙間：** S3アーカイブは月次更新で抜けあり
- **センチメント再現不可：** 当時のClaudeスコアは保存していない
- **板情報の欠如：** L2 book履歴は限定的
- **約定モデルの単純化：** 実際のスリッページや拒否は再現しきれない
- **手数料・Fundingの近似：** 当時の正確な値は取れない

**バックテストはあくまで参考値。** 実弾運用での実証を最終判断とする。

---

## 25. 通知設計（Discord 4チャンネル統合）

設計書全体に散在する通知パターンを一覧化・整理する章。

### 25.1 4チャンネル構成

| チャンネル | 用途 | 通知頻度 |
|---|---|---|
| **mt-signal** | エントリー・決済シグナル | リアルタイム |
| **mt-alert** | サーキットブレーカー・障害 | リアルタイム |
| **mt-summary** | 日次・月次サマリー | 1日1回 |
| **mt-error** | エラー・例外スタックトレース | リアルタイム |

### 25.2 通知パターン全一覧

| イベント | チャンネル | フォーマット例 | 章 |
|---|---|---|---|
| エントリー実行 | signal | 🔵 LONG BTC @ $X / size: X / SL: $X / TP: $X | 4 |
| エントリー失敗 | signal | ⚠️ Entry rejected: BTC (ALO retry exhausted) | 14 |
| TP約定 | signal | 🟢 TP hit: BTC +X.XX% (+$X) / hold: Xmin | 13 |
| SL約定 | signal | 🔴 SL hit: BTC -X.XX% (-$X) / hold: Xmin | 13 |
| ドライラン記録 | signal | 📊 [DRY] LONG BTC @ $X / score: X.XX | 8 |
| 状態復元完了 | signal | ✅ State restored: X positions monitored | 9.3 |
| 外部ポジション検出 | alert | ⚠️ External position detected: BTC | 9.3 |
| ポジション乖離補正 | alert | ⚠️ Position mismatch corrected: BTC | 9.6 |
| WS切断 | alert | ⚠️ WS disconnected | 9.4 |
| WS再接続成功 | signal | ✅ WS reconnected (attempt N) | 9.4 |
| WS切断→緊急クローズ | alert | 🆘 WS reconnect failed・emergency close | 9.4 |
| サーキットブレーカー発動 | alert | 🆘 Circuit breaker: <reason> | 9.7 |
| フラッシュクラッシュ検出 | alert | ⚠️ Flash crash: BTC -X% in 1min | 9.7 |
| Claude API障害 | alert | ⚠️ Claude API down・new entries paused | 9.10 |
| ガス代低下 | alert | ⚠️ Gas balance low: $X | 9.9 |
| ガス代枯渇 | alert | 🆘 Gas depleted: $X | 9.9 |
| 緊急停止実行 | alert | 🆘 Emergency stop executed | 9.12 |
| Funding精算（受取/支払） | summary | 💰 Funding: BTC +$X / ETH -$X | 13.4 |
| 日次サマリー | summary | [Phase X 日次サマリー...] | 15.4 |
| 月次引き上げ推奨 | summary | 💰 Monthly sweep recommended: $X | 10.5 |
| 増減資推奨 | summary | 📈/⚠️ Capital review: ... | 10.7 |
| UTC 00:00価格スナップショット | summary | 📅 UTC open snapshot done | 5.6 |
| API例外 | error | ❌ API error: <stack trace> | - |
| DB例外 | error | ❌ DB error: <stack trace> | - |
| 想定外の例外 | error | 💥 Uncaught exception: ... | - |

### 25.3 通知レート制限

同じエラーが連発した時の対策：

```python
# src/infrastructure/discord_notifier.py
class DiscordNotifier:
    def __init__(self):
        self._recent_messages = {}  # message_hash → last_sent_at
        self._dedup_window = 300    # 5分以内の同じメッセージは抑制

    async def send(self, channel: str, message: str, dedup_key: str | None = None):
        if dedup_key:
            now = time.time()
            last = self._recent_messages.get(dedup_key, 0)
            if now - last < self._dedup_window:
                return  # 重複抑制
            self._recent_messages[dedup_key] = now

        # 実際の送信
        await self._send_webhook(channel, message)
```

### 25.4 緊急時の連絡経路

Discord自体がDownしている場合の冗長化：

| Priority | 経路 | 用途 |
|---|---|---|
| 1 | Discord Webhook | 通常 |
| 2 | Telegram BOT | Discord不通時のフォールバック（Phase 4で検討） |
| 3 | Email（SMTP） | 最後の手段（最終Phase 4で検討） |
| 4 | ログファイルのみ | 全部Downした時の最終手段 |

Phase 1〜3はDiscord 1経路で運用。Phase 4で必要に応じて追加。

### 25.5 通知メッセージのフォーマット規約

```python
# 絵文字ガイドライン
🔵 LONG関連
🔴 SHORT関連 / 損失
🟢 利益確定
⚠️ 警告（処理は継続）
🆘 重大警告（人間の介入必要）
✅ 正常完了
📊 ドライラン・統計
💰 利益・資金
📈 上昇・好調
📉 下降・不調
📅 スケジュール
❌ エラー
💥 例外
```

```python
# メッセージテンプレート
ENTRY_TEMPLATE = "🔵 {direction} {symbol} @ ${entry_price:.2f}\nSize: {size} | SL: ${sl:.2f} | TP: ${tp:.2f}\nLayer scores: M={m:.2f} F={f:.2f} S={s:.2f} R={r:.2f}"

EXIT_TEMPLATE = "{emoji} {reason}: {symbol} {pnl_pct:+.2%} ({pnl_usd:+.2f})\nHold: {hold_minutes:.0f}min | MFE: {mfe:.2f}% | MAE: {mae:.2f}%"
```

### 25.6 Notifier Protocol（章11踏襲）

```python
# src/adapters/notifier.py
from typing import Protocol


class Notifier(Protocol):
    async def send_signal(self, message: str, dedup_key: str | None = None) -> None: ...
    async def send_alert(self, message: str, dedup_key: str | None = None) -> None: ...
    async def send_summary(self, message: str) -> None: ...
    async def send_error(self, message: str, exception: Exception | None = None) -> None: ...
```

実装時はこのProtocolを満たすクラスを`infrastructure/discord_notifier.py`に作る。
テスト時はモック化して通知内容を検証可能。

### 25.7 PR7.5b 実装で確定した ConsoleNotifier 仕様

Phase 0 観察モード用の最小実装。Discord 実装まで `ConsoleNotifier` で凌ぐ：

```
[SIGNAL]   logger INFO     ─ エントリー / 決済 / 状態復元
[ALERT]    logger WARNING  ─ サーキットブレーカー / 障害
[SUMMARY]  logger INFO     ─ 日次・月次サマリー
[ERROR]    logger ERROR    ─ 例外（exception 渡しで traceback 付与）
```

実装上の確定事項：
- `dedup_key` は受け取って捨てる（Console では実 dedupe しない）
- `exception` は traceback を末尾に付与
- 書き込み失敗（stream クローズ等）は `logger.exception` で握りつぶす
  → メインループを通知障害で落とさない
- 2 モード切替: `use_logging=True`（本番・logger 経由）/ `False`（テスト・stream 直接）

PR7.5d で DiscordNotifier に差し替え予定。同じ Protocol を満たすので
`build_scheduler` 側の差し替えは 1 行。

### 25.8 PR7.5d-fix で確定した Notifier 周りの仕様

DiscordNotifier 本実装後、48 時間 Phase 0 観察で **シグナルクラスター発生時に
5 分間で 18 件単位の同一 DRYRUN 通知が滞留** する事象が発生。
`dedup_key` を「受け取れる引数」から「実際にすべての通知箇所で使う」運用に
切り替えた。同時に Notifier Protocol の kwargs 構造を再整理した。

#### 25.8.1 Notifier Protocol の kwargs はメソッドごとに異なる

`send_summary` だけ `dedup_key` / `exception` を持たない。
日次・週次の定期サマリは「同じ key を抑制すると逆に困る」ので意図的な非対称：

| メソッド | dedup_key | exception | チャンネル |
|---|---|---|---|
| `send_signal` | あり | なし | mt-signal |
| `send_alert` | あり | なし | mt-alert |
| `send_summary` | **なし** | **なし** | mt-summary |
| `send_error` | なし | あり | mt-error |

実装は章25.6 のとおり：

```python
class Notifier(Protocol):
    async def send_signal(self, message: str, dedup_key: str | None = None) -> None: ...
    async def send_alert(self, message: str, dedup_key: str | None = None) -> None: ...
    async def send_summary(self, message: str) -> None: ...
    async def send_error(self, message: str, exception: Exception | None = None) -> None: ...
```

注記: `exception` は `send_error` 専用。エラー以外で例外を「ついでに付ける」
仕組みは作らない（チャンネル分離の意味がなくなる）。
代わりに alert メッセージ本文に `f": {e}"` を埋め込む運用。

#### 25.8.2 Scheduler の `_safe_notify` 振り分けパターン

`_safe_notify(method_name, message, *, dedup_key=None, exception=None)` を
`send_summary` 含む全メソッドから呼べるように、`method_name` で kwargs を
振り分ける。これは将来通知メソッドが増えた時にも踏襲する基盤パターン：

```python
async def _safe_notify(
    self,
    method_name: str,
    message: str,
    *,
    dedup_key: str | None = None,
    exception: Exception | None = None,
) -> None:
    try:
        method = getattr(self.notifier, method_name)
        kwargs: dict[str, object] = {}
        if method_name in ("send_signal", "send_alert"):
            kwargs["dedup_key"] = dedup_key
        elif method_name == "send_error":
            kwargs["exception"] = exception
        # send_summary は kwargs を渡さない（メッセージのみ）
        await method(message, **kwargs)
    except Exception:
        logger.exception("notification failed: %s", method_name)
```

#### 25.8.3 dedup_key 命名規則表（PR7.5d-fix で確定）

各通知箇所で使う `dedup_key` の規則を一覧化。`window` は
`DiscordNotifierConfig.dedup_window_seconds`（既定 300 秒）に従う：

| 通知 | dedup_key | 理由 |
|---|---|---|
| BOT 起動 | なし | 1 回のみ発生 |
| 状態復元完了 | なし | 起動時 1 回 |
| エントリー実行 | `entry:{trade_id}` | trade_id は一意 |
| エントリー失敗 | `entry_fail:{symbol}:{direction}` | 同銘柄連続失敗を抑制 |
| **DRYRUN シグナル** | `dryrun:{symbol}:{direction}` | **クラスター対策・最重要** |
| FILL 通知 | `fill:{trade_id}` | trade_id は一意 |
| CLOSE 通知 | `close:{trade_id}` | trade_id は一意 |
| 強制決済 | `force_close:{symbol}:{reason}` | 連発防止 |
| 強制決済失敗 | `force_close_fail:{symbol}` | 連発防止 |
| 外部ポジション検出 | `external:{symbol}` | 同銘柄の連続検出を抑制 |
| ポジション乖離補正 | `correct:{symbol}` | 連発防止 |
| 起動時決済記録 | `close_from_fill:{trade_id}` | trade_id は一意 |
| 手動確認要 | `manual:{trade_id}` | trade_id は一意 |
| ブレーカー発動 | `cb_active:{reason}` | reason は単一 Enum（章9.15.4） |
| ブレーカー解除 | `cb_clear` | 解除は同イベント |
| サイクル例外 | `cycle_error` | 連発抑制 |
| step 失敗 | `step_fail:{step_name}` | 同 step の連発抑制 |

注記: `cb_active` の reason は単一 Enum 値（`BreakReason.value`）であって、
複数 reason の sort+join ではない。`check_circuit_breaker` の戻り値が
最初の発動 1 つだけ（章 9.15.4）なので。

`ConsoleNotifier` は `dedup_key` を受け取って捨てる。
`DiscordNotifier` だけが実際に dedup する設計。
プロファイル切替で挙動が変わるが運用上問題なし。

---

## 26. ロギング・メトリクス設計

「BOTが今健康かどうか」を可視化する章。

### 26.1 3層ロギング構造

```
Layer 1: Application Log（ファイル）
  - 用途: 障害再現・デバッグ
  - 形式: JSON Lines
  - 出力先: logs/bot_YYYYMMDD.log
  - 保持: 30日

Layer 2: 構造化メトリクス（DB）
  - 用途: 成績分析・チューニング
  - テーブル: trades / signals / sentiment_logs / funding_payments / incidents
  - 章8で定義済み

Layer 3: リアルタイム通知（Discord）
  - 用途: 人間への即時通知
  - 章25で定義済み
```

### 26.2 ログレベル基準

| レベル | 用途 | 例 |
|---|---|---|
| DEBUG | 開発時の詳細情報 | API request/response の中身、判定の中間値 |
| INFO | 通常運用での記録 | エントリー・決済・状態遷移 |
| WARNING | 想定外だが処理継続 | API リトライ、ALO拒否1回 |
| ERROR | 処理失敗・要対応 | 注文発注失敗、状態復元エラー |
| CRITICAL | 緊急・即時対応必要 | サーキットブレーカー発動、データ破損 |

### 26.3 ログフォーマット（JSON Lines）

```python
# 例：エントリー時のログ
{
  "timestamp": "2026-04-27T12:34:56.789Z",
  "level": "INFO",
  "event": "entry_executed",
  "symbol": "BTC",
  "direction": "LONG",
  "size": "0.005",
  "entry_price": "65432.10",
  "sl_price": "64100.00",
  "tp_price": "67200.00",
  "layer_scores": {
    "momentum": 0.45,
    "flow": 1.8,
    "sentiment": 0.72,
    "regime": "OK"
  },
  "trade_id": 12345,
  "phase": "PHASE_2"
}
```

JSON形式の利点：
- ログ集約ツール（Loki/Datadog等）に流せる
- jq/SQL with DuckDB で簡単に分析可能
- 機械可読で人間も読める

### 26.4 ログ出力すべきイベント一覧

| イベント | レベル | 含めるべき情報 |
|---|---|---|
| BOT起動 | INFO | version, phase, profile |
| 状態復元完了 | INFO | restored_count, corrections, time_taken |
| ウォッチリスト更新 | INFO | symbols, source |
| シグナル評価開始 | DEBUG | symbol |
| 4層AND判定 | DEBUG | layer_results, snapshot_excerpt |
| エントリー判定通過 | INFO | symbol, direction, layer_scores |
| エントリー判定失敗 | DEBUG | symbol, rejection_reason |
| 注文発注 | INFO | symbol, side, size, price, client_oid |
| 注文約定 | INFO | order_id, fill_price, slippage |
| 注文キャンセル | INFO | order_id, reason |
| ALO拒否 | WARNING | symbol, attempt_count |
| ポジション監視更新 | DEBUG | trade_id, current_pnl, mfe, mae |
| TP/SL約定 | INFO | trade_id, exit_reason, pnl |
| Funding精算 | INFO | symbol, amount_usd, direction |
| WS切断 | WARNING | duration_sec |
| WS再接続成功 | INFO | attempt_count |
| サーキットブレーカー | CRITICAL | reason, action_taken |
| API エラー | ERROR | endpoint, status_code, error_message |
| 想定外例外 | ERROR | exception_type, traceback |

### 26.5 メトリクス監視

#### 必須メトリクス（DB or ログから集計）

```python
# src/monitor/metrics.py
async def collect_metrics() -> dict:
    """5分ごとに収集するメトリクス"""
    return {
        # 取引関連
        "active_positions_count": ...,
        "today_trades_count": ...,
        "today_pnl_usd": ...,
        "today_pnl_pct": ...,
        "current_drawdown_pct": ...,

        # API関連
        "claude_api_calls_today": ...,
        "claude_api_cost_today_usd": ...,
        "hl_api_weight_used_minute": ...,
        "hl_ws_connected": True/False,

        # システム関連
        "memory_used_mb": ...,
        "cpu_percent": ...,
        "disk_used_pct": ...,
        "log_file_size_mb": ...,

        # 障害関連
        "incidents_today": ...,
        "ws_disconnects_today": ...,
        "alo_rejections_today": ...,
    }
```

#### 異常検知のしきい値

| メトリクス | WARNING | CRITICAL |
|---|---|---|
| memory_used_mb | > 1500 | > 3000 |
| cpu_percent | > 70 | > 90 |
| disk_used_pct | > 70 | > 90 |
| claude_api_cost_today_usd | > $5 | > $10 |
| ws_disconnects_today | > 5 | > 20 |
| alo_rejections_today | > 50 | > 200 |
| current_drawdown_pct | < -3% | < -5% |

これらを超えたらDiscord alertを送信。

### 26.6 ヘルスチェックエンドポイント（Phase 4以降）

外部監視ツール（Uptimerobot等）から叩ける状態確認URL：

```python
# scripts/healthcheck.py
"""
シンプルなFastAPIで /health エンドポイントを公開
$ python scripts/healthcheck.py
$ curl http://localhost:8000/health
"""
from fastapi import FastAPI

app = FastAPI()

@app.get("/health")
async def health():
    metrics = await collect_metrics()
    is_healthy = all([
        metrics["hl_ws_connected"],
        metrics["memory_used_mb"] < 3000,
        metrics["incidents_today"] < 10,
    ])
    return {
        "status": "healthy" if is_healthy else "degraded",
        "metrics": metrics,
    }
```

Phase 4で必要に応じて導入。

### 26.7 ログローテーション

```python
# logging_config.py
import logging.handlers

handler = logging.handlers.TimedRotatingFileHandler(
    filename="logs/bot.log",
    when="midnight",          # 毎日0時にローテーション
    interval=1,
    backupCount=30,           # 30日分保持
    encoding="utf-8",
)
handler.suffix = "%Y%m%d"     # logs/bot.log.20260427
```

### 26.8 機密情報のログ漏洩対策

```python
# 絶対にログに出さないキー
SENSITIVE_KEYS = {
    "agent_private_key",
    "api_key",
    "ANTHROPIC_API_KEY",
    "webhook",
    "private_key",
}

def sanitize_for_log(data: dict) -> dict:
    """機密情報を伏せ字化"""
    return {
        k: ("***REDACTED***" if any(s in k.lower() for s in SENSITIVE_KEYS) else v)
        for k, v in data.items()
    }
```

ログ出力時は必ずこの関数を通す。

### 26.9 ログ分析クエリ例（DuckDBで集計）

```bash
# ログをDuckDBでSQL検索
duckdb -c "
SELECT level, COUNT(*) FROM 'logs/bot_2026*.log'
GROUP BY level
ORDER BY level;
"

duckdb -c "
SELECT
    json_extract_string(_msg, '\$.event') as event,
    COUNT(*) as count
FROM 'logs/bot_*.log'
WHERE level = 'ERROR'
GROUP BY event
ORDER BY count DESC;
"
```

DuckDBは無料・シングルバイナリで超高速。BOT稼働中でも安全に分析可能。

### 26.10 OS依存の運用Tips（実機検証で判明）

#### Windows cp932 と subprocess

Windows のデフォルトエンコーディングは **cp932 (Shift-JIS)**。
`subprocess.run` で UTF-8 出力を読むと `UnicodeDecodeError` になる：

```
UnicodeDecodeError: 'cp932' codec can't decode byte 0x90 in position 84
```

**対処：必ず `encoding="utf-8"` を明示する：**

```python
result = subprocess.run(
    ["sops", "-d", "secrets/secrets.enc.yaml"],
    capture_output=True,
    text=True,
    check=True,
    encoding="utf-8",  # ← Windows必須
)
```

`text=True` だけでは OS デフォルト（Windows なら cp932）が使われるため、
クロスプラットフォームで動かしたいなら `encoding` 明示が必須ルール。

#### Python モジュールパス

`scripts/foo.py` から `from src.infrastructure.X import ...` する場合、
**プロジェクトルートが PYTHONPATH に含まれていない**と `ModuleNotFoundError`：

```
ModuleNotFoundError: No module named 'src'
```

**対処1（恒久・推奨）：** スクリプト先頭で sys.path を追加

```python
# scripts/foo.py
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# 以降、from src... が動く
```

**対処2（一時的）：** 環境変数で指定

```bash
# bash
PYTHONPATH=. python scripts/foo.py

# PowerShell
$env:PYTHONPATH = "$PWD"
python scripts/foo.py
```

**対処3：** `python -m scripts.foo` でモジュールとして実行

恒久対応として **対処1** を推奨。これが無いと運用環境で起動失敗する。

