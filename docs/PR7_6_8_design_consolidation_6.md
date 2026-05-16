# PR7.6.8 - 設計書集約 #6 (Phase A+B 修正知見 + 5/15 mainnet incident + ALO lifecycle)

## ドキュメントの目的

このドキュメントは「**事後分析と判断保留のための素材**」である。
2026-05-15 mainnet 初日に同時露呈した 5 件の重大バグの記録、それを構造的に潰した 5 PR (Phase A: A1/A2/A3, Phase B: B1/B2) の詳細、および集約過程で判明した ALO post-only 注文の lifecycle 問題、4 層 AND の構造的トレードオフを、中立的に並列で記録する。

実装判断は読み手が後日するためのもので、ここでは結論を急がない。各節の最後に「未決の論点」を明記する。

- スコープ: docs-only。コード変更なし。既存 851 + B2 で +8 = **859 passed / 100% coverage** を維持。
- 想定読者: 将来の自分または引き継ぎ先。
- 編集方針: 事実 → 分析 → 未決論点 の 3 段構成を各節で踏襲。

---

## 1. 2026-05-15 mainnet incident: 全 5 バグの完全記録

### 1.1 経緯

2026-05-15、Phase 2 (mainnet 実弾) 初日。残高 $295.00 で稼働開始。BOT は短時間に 8 件のエントリーを実弾モードで連発し、その後手動停止された。最終残高 $294.92 (損失 $0.08、Position 0、Open Orders 0)。

最終残高はほぼ無傷だが、「想定外の連発」「DB と HL 状態の乖離」「reconciler の誤クローズ」が同時発生しており、4 層 AND ゲートだけでは不十分な仕様の穴が一気に露呈した。

### 1.2 5 件のバグ (発見順)

| # | 症状 | 構造的原因 | 修正 PR |
|---|---|---|---|
| 1 | ATR が 0.0001 にフロアされ SL/TP スプレッドが \$1 まで縮退 | `_estimate_atr` がローソク足ベースでなく `recent_price * 0.0001` の placeholder のまま放置 | **PR A2** |
| 2 | `max_position_count=1` でも 8 件連続発注 | CircuitBreaker Layer 7 が `position_overflow_multiplier=1.5` 適用かつ HL position 数ベース。ALO resting は計上されない | **PR A1** |
| 3 | ID 1 の TP fill (buy, 05:36:10) が ID 2-7 (05:43:28 以降の entry) に伝播し MANUAL クローズ | `_find_matching_fill` が symbol + side + size のみで判定。`fill.timestamp >= entry_time` の下限制約と `is_filled=1` フィルタが無い | **PR A3** |
| 4 | MANUAL クローズした trade の TP/SL が HL 側で resting し続ける | reconciliation の `_close_from_fill` / `_mark_manual_review` が DB のみ更新し HL を cancel しない | **PR B2** |
| 5 | 同一 HL fill が複数 cycle で再処理され、別 trade に誤って属性化 | `position_monitor._detect_fills` に dedup なし。`fills_lookback_seconds=300` の窓に古い fill が居座る | **PR B1** |

### 1.3 連鎖メカニズム

5 件は独立ではなく、互いに前提を提供しあっていた。

```
バグ #2 (ゲート無し) ── 連続 ALO 8 件発注
   │
   ├── バグ #1 (ATR placeholder)
   │     └── SL/TP $1 スプレッド
   │
   └── ALO の一部 fill / 一部未約定
         │
         ├── バグ #5 (fill dedup 無し)
         │     └── 同 timestamp の fill が次 cycle で再処理
         │           └── ID N の is_filled=1 と actual_entry_price が
         │               隣接 trade の fill で上書き
         │
         └── ALO の未約定残 + バグ #3 (timestamp lower bound 無し)
               └── ID 1 の TP fill が後続 ID 2-7 にマッチ
                     └── reconciler が ID 2-7 を MANUAL_REVIEW で
                         「決済済み」扱いに → バグ #4
                               └── HL の TP/SL が宙ぶらりん
```

最終残高の小ささは偶然 (ATR floor で SL/TP がほぼ即時マッチして損益が打ち消されたため)。SL/TP が機能していれば損失はもっと大きくなる可能性があり、逆に SL/TP が機能しなければ放置リスクが拡大していた。**「無事だった」という観測は再現性のあるリスク評価にならない**。

### 1.4 未決の論点

- **incident response process の欠如**: BOT が暴走した時に 1-click で全 cancel + 状態凍結する手順 (runbook) が無い。事後手動停止に依存。
- **Phase 2 リリース基準**: mainnet 切り替えは「testnet で 3 日緑」だけが基準だった。本件は testnet では検出不可能 (実弾モードでしか走らない経路) が含まれており、リリース基準そのものを別途見直す必要がある。

---

## 2. Phase A: 必須修正 (再稼働前に必要)

### 2.1 PR A1: max_position_count 発注時ゲート (#2)

**コミット**: `c0944fc`

**変更**: [`src/application/scheduler.py:_run_entry_flow_pass`](src/application/scheduler.py#L283) の冒頭に DB ベースのポジション数ゲートを追加。`repo.get_open_trades()` (= `WHERE exit_time IS NULL`) の件数が `config.max_position_count` 以上なら entry pass 全体を skip。

**なぜ CircuitBreaker (Layer 7) で十分でなかったか**:

| 観点 | CircuitBreaker Layer 7 | PR A1 ゲート |
|---|---|---|
| ソース | `exchange.get_positions()` (HL) | `repo.get_open_trades()` (DB) |
| ALO resting の計上 | × (HL position 数に出ない) | ○ (exit_time NULL で出る) |
| 閾値倍率 | `max_position_count × 1.5` (overflow_multiplier) | `>=` で完全強制 |
| 発火タイミング | breaker トリガー後の判定 | entry pass 入口で先回り skip |

**観測される不変条件**: PR A1 適用後は「resting ALO 1 件で全 watchlist × 全 direction の entry pass が skip される」。次節 5 で見るように、これは別の論点 (resting 永続) を発生させる。

### 2.2 PR A2: ATR 実装化 (#1)

**コミット**: `139cc37`

**変更**: [`src/application/entry_flow.py:_calc_atr_for_sizing`](src/application/entry_flow.py#L378) を新設。`exchange.get_candles(symbol, "1h", limit=15)` で 14 期間 True Range を計算する真の ATR に置換。`recent_price * 0.0001` の placeholder を撤去。

**取得失敗時の挙動**: ATR が候補ローソク足・24h レンジともに取得不能なら `ExchangeError` を `entry_fail:{symbol}:{direction}` alert に転送して entry 停止 (silent rejection を回避)。

**実機検証**: PR A2 適用後の live BTC ATR(1h, 14) ≈ \$518 → SL 距離 ~\$777 (1.5x) を確認。5/15 事故時の \$1 スプレッドとは別世界。

### 2.3 PR A3: reconciler マッチング厳格化 (#3)

**コミット**: `18d0645`

**変更**: 2 箇所。

1. [`src/core/reconciliation.py:_find_matching_fill`](src/core/reconciliation.py#L187) に **timestamp 下限制約** (`fill.timestamp >= db_trade.entry_time_ms`) を追加。これに伴い `DBTrade` dataclass に `entry_time_ms: int` を新設。
2. [`src/application/reconciliation.py:_run_core_reconcile`](src/application/reconciliation.py#L185) で `is_filled=0` の trade (ALO resting 中) を reconciliation 対象から除外。

**保護される不変条件**:

- 5/15 のように ID 1 の TP fill (05:36:10) が ID 2-7 (05:43:28〜) にマッチすることは構造的に不可能。
- ALO resting 中の trade を reconciler が "HL に position 無し" として CLOSE_FROM_FILL に流す事故も発生しない。

**意図的に設けていない制約**:

- `_find_matching_fill` の **上限 (max_age)** は意図的に未設定。`is_filled=1` フィルタ + PR A1 ゲート (max_position_count=1) の組み合わせで、同 symbol/side/size の同時 open は構造的に発生しないため、低ボラ局面での長保有 trade の正規 fill を取りこぼすリスクのほうが大きいと判断。

### 2.4 Phase A の総合効果

5/15 事故の必要条件 (大量 entry + placeholder ATR + 緩い fill マッチ) のうち、Phase A だけで 3 件すべてを構造的に閉じている。「再稼働前に必要」というレベル分けは妥当だった。

---

## 3. Phase B: 重要修正 (Phase A 完了後)

### 3.1 PR B1: fill 冪等化 (#5)

**コミット**: `0b9f95e`

**変更**: [`src/application/position_monitor.py:_detect_fills`](src/application/position_monitor.py#L115) で DB の `fill_time` から既処理 entry fill の timestamp set を構築し、同 timestamp の HL fill を skip。

**設計判断**:

| 案 | 内容 | 採否 |
|---|---|---|
| A | in-memory set で dedup | ✗ 再起動で消える |
| **B** | **DB の `fill_time` ベース** | **○ 採用** |
| C | 別 repo method (`get_recent_fill_times`) | △ 抽象漏れ |

**副次的な技術的負債返済**: PR7.2 以降、`mark_trade_filled` は `fill_time` を DB に書いていたが、`Trade` dataclass にフィールドが無く `_row_to_trade` も読んでいなかった。PR B1 で `Trade.fill_time: datetime | None = None` を追加し、`_row_to_trade` で `_iso_to_dt(row["fill_time"])` を populate するように修正。

**狭い dedup 範囲の根拠**:

- dedup は **entry 側のみ**。close fill は trade の `exit_time` 設定で open_trades から外れるので、`_dispatch_fill` の close 経路が自然と "ignored" になる。
- `max_position_count=1` (PR A1) のもとでは同 timestamp で別 trade に正規割当されることは構造的に発生しない → timestamp 単独 dedup で十分。

### 3.2 PR B2: HL 注文 cleanup + entry_order_id 保存 (#4)

**コミット**: `c2c4f04`

**変更**: 3 つの層に分かれる。

1. **Trade 側 (DB):**
   - `Trade` dataclass に `entry_order_id: int | None = None` を追加。
   - `TradeOpenRequest` にも同フィールドを追加。
   - `schema.sql` に `entry_order_id TEXT` 列を追加。
   - 既存 DB 向け idempotent 後付け migration: [`_ensure_trades_columns`](src/infrastructure/sqlite_repository.py#L77) で `PRAGMA table_info` を見て無ければ `ALTER TABLE`。
2. **entry_flow 側 (記録):**
   - [`_execute_entry`](src/application/entry_flow.py#L543) で `open_trade(TradeOpenRequest(..., entry_order_id=results[0].order_id))` を呼ぶ。
3. **reconciliation 側 (cleanup):**
   - [`_close_from_fill`](src/application/reconciliation.py#L292) と [`_mark_manual_review`](src/application/reconciliation.py#L327) の手前で `_cancel_known_orders(symbol, adapter_trade, errors, reason=...)` を呼ぶ。
   - 対象 oid: entry → tp → sl の順。`ExchangeError` は warn ログ + `errors` に積んで continue (close 本体は止めない)。
   - CORE 純度維持のため、`_apply_action` のシグネチャに `db_trades_by_id: dict[int, Trade]` を渡す形で実装 (CORE DBTrade に HL 注文 ID を入れない)。

**この PR の射程外 (意図的)**:

- 通常運用中の resting ALO 自動 cancel は実装しない。これは別論点として §5 で議論する。

### 3.3 Phase A + B の総合到達点

| 項目 | 5/15 以前 | Phase A 完了後 | Phase A+B 完了後 |
|---|---|---|---|
| 同時 entry 数 | 最大 8 件 (ゲート不在) | 最大 1 件 (DB ゲート) | 同左 |
| ATR | placeholder $1 | 真の 14 期間 TR | 同左 |
| reconciler 誤マッチ | symbol+side+size のみ | + timestamp 下限 + is_filled | 同左 |
| 同 fill の cycle 間再処理 | 起きる | 起きる | 起きない (DB dedup) |
| MANUAL クローズ時の HL 注文 | resting し続ける | resting し続ける | best-effort cancel |
| 859 passed / 100% coverage | (n/a) | 851 | **859** |

5 バグはすべて構造的に閉じている。5/15 シナリオの再発は不可能。

### 3.4 未決の論点

- **PR B2 で追加した `entry_order_id` の活用**: 現状 cleanup 経路でのみ参照。§5 案 1〜3 のいずれを採るかで活用度が変わる。
- **PR A3 の max_age 上限**: 意図的に未設定だが、極端な長保有 (週単位) で正規 fill 以外の意図しないマッチが起きる可能性は理論的にゼロではない。実機データを蓄積してから判断。

---

## 4. 2026-05-15 BTC ATR 実観測値 (Phase A2 検証データ)

PR A2 適用後の live BTC で計測:

| 指標 | 値 |
|---|---|
| ATR (1h, 14 期間) | 約 $518 |
| 1.5× ATR (SL 距離) | 約 $777 |
| 1.0× ATR (TP 距離) | 約 $518 |
| 当時の BTC 価格 | 約 $80,400 |
| 相対比 | ATR/price ≈ 0.64% |

**含意**:

- SL は 1% 弱の値動きで発動する設計になっている。これは中期トレンドフォロー戦略としては妥当な水準。
- 5/15 事故時の \$1 スプレッドは ATR の **約 0.2%** に縮退していた。即時マッチして実質「成行＋マイクロ TP」になっていた可能性が高い。

---

## 5. ALO post-only 注文 lifecycle (2026-05-16 調査結果)

### 5.1 HL 側の仕様

公式ドキュメント ([Order types](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-types), [Exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)) を 2026-05-16 時点で確認:

- ALO は **発注時のみ** post-only 制約 (`即マッチしそうなら拒否`)。一度 book に rest した後は通常の limit order と同じ挙動。
- GTC は明示的に「filled or cancelled まで rest」と記載 (= 無期限)。
- ALO は「added to the order book」のみで lifetime に明示記載なし → **実質 GTC 相当**と理解するのが妥当。
- API の `expiresAfter` は action call 側の制約であり、resting order の有効期限ではない。
- 結論: **ALO は明示 cancel か約定までは無期限**。

### 5.2 BOT 側の cancel 経路 (5 経路の現状)

| 経路 | 発動条件 | 対象範囲 | 実装場所 | 通常運用中の自動発火 |
|---|---|---|---|---|
| ① startup cleanup | BOT 再起動 + `now - order.timestamp_ms > 30s` | HL 全 open order (entry/TP/SL 無区別、全 symbol) | [`reconciliation._cleanup_stale_orders`](src/application/reconciliation.py#L411) | × (起動時のみ) |
| ② 定期 reconcile (5 分) | なし (`cleanup_enabled=False` 固定) | — | [`reconciliation.run_periodic_check`](src/application/reconciliation.py#L94) | × |
| ③ PR B2 cleanup | DB が CLOSE_FROM_FILL / MANUAL_REVIEW に流れた時のみ | trade.entry/tp/sl_order_id | [`reconciliation._cancel_known_orders`](src/application/reconciliation.py#L357) | × (例外パスのみ) |
| ④ position_monitor | 無し | — | — | — |
| ⑤ §14.3 maker_first_router | 30 秒未約定で再発注ループ | entry のみ | **未実装** | — |

**含意**: 通常運用中の resting ALO に対して、自動 cancel する経路は実質的に **無い**。

### 5.3 PR A1 ゲートとの相互作用 (硬直化リスク)

PR A1 のゲートは `repo.get_open_trades()` (= `exit_time IS NULL`) の件数で発動する。entry 発注成功時点で DB に INSERT (is_filled=0, exit_time=NULL) されるので、**resting ALO 1 件だけでゲートが閉まる**。

新たな構造的リスク:

```
強い価格急変 → entry が一度も約定しない resting ALO
   ↓
PR A1 ゲート閉鎖 (全 symbol / 全 direction が skip)
   ↓
価格が戻ってから遅延約定 → 既に判定材料が陳腐化した position を保有
```

5/15 incident は「8 件連発」だったが、Phase A+B 適用後は逆向きの「**1 件居座って次の機会も逃す**」が新たな構造リスクとして登場する。

### 5.4 改善余地 (案 1〜3)

事実だけ並べ、判定保留。

| 案 | 実装場所 | 内容 | 既存資産の活用 | 副作用 |
|---|---|---|---|---|
| **1** | position_monitor | `now - entry_time > N秒` かつ `is_filled=0` の trade を検出 → `cancel_order(entry_order_id)` + DB `exit_time` set | PR B2 の `entry_order_id` をそのまま活用 | TP/SL は同時発注なので別途 cleanup 必要 |
| **2** | reconciliation.run_periodic_check | `cleanup_enabled` の部分的有効化 (entry ALO のみ N 秒、TP/SL 除外) | 既存基盤を流用、変更小 | 既存テストへの波及あり |
| **3** | application/maker_first_router (新規) | 設計書 §14.3 の `place_post_only_with_retry` を本実装 (30 秒未約定で価格を 1 tick 動かして再発注、最大 N 回) | settings.yaml の `post_only_retry_wait_sec: 30` が既に定義済 | 本来の Maker-First 設計に整合・実装規模最大 |

### 5.5 未決の論点

- **どの案を採るか**: 4 層通過頻度が極めて低い (mainnet 数日 0 件継続) ため、硬直化リスクの発火確率も低い。実機データ (実エントリー数件) を見てから判断するのが妥当。想像で実装すると過剰設計になりがち。
- **§14.3 が設計書にあって未実装である事実**: 設計書と実装の乖離として、別途リファクタ計画で吸収するか、削除するかの判断が必要。

---

## 6. MOMENTUM × VWAP ±0.5% 帯の構造的トレードオフ

### 6.1 仕様確認 ([`src/core/entry_judge.py`](src/core/entry_judge.py))

```python
# LONG (line 24, 87-96)
_LONG_VWAP_MAX_DISTANCE_PCT = 0.5
_check_momentum_long:
    0 < vwap_distance_pct < 0.5   # VWAP 上 0.0〜0.5% の薄帯
    and is_not_overheated_long
    and momentum_5bar_pct > 0.3

# SHORT (line 41, 132-141)
_SHORT_VWAP_MIN_DISTANCE_PCT = -0.5
_check_momentum_short:
    -0.5 < vwap_distance_pct < 0   # VWAP 下 -0.5%〜0.0% の薄帯
    and is_not_overheated_short
    and momentum_5bar_pct < -0.3
```

### 6.2 帯の幅と実勢ボラの関係

- BTC の典型的な 5 分足変動: ~0.05〜0.15%。
- BTC ATR(1h, 14) / price ≈ 0.64% (5/15 実測)。
- ETH や ALT は BTC の 1.5〜2× ボラが多い → 5 分足変動 ~0.10〜0.30%、ATR/price ~1〜1.5%。

帯幅 0.5% は **BTC の 5 分足数本分**、**ETH や ALT では数十秒〜数分の小さなウィンドウ**。

### 6.3 5/15 ETH 急落時の MOMENTUM ✗ 構造 (調査済みの仕組み)

ETH SHORT MOMENTUM が ✗ になる支配的経路:

```
強い ETH 下落 → 価格が VWAP を -0.5% より深く突き抜ける
   ↓
vwap_distance_pct < -0.5  → MOMENTUM ✗ ("too far from VWAP")
   ↓
急落の本体に対しては SHORT エントリーできない
```

同じ向きで LONG も対称: 強い上昇 → +0.5% を超える → MOMENTUM ✗。

### 6.4 設計意図の再確認

このゲートは「**強いトレンドの本体には入らず、VWAP 付近の retest / 押し目に入る**」という mean-reversion 寄りの設計。auto-daytrade の「成行で trend を追って大幅 slippage」問題への対策として導入された経緯がある (§14.1)。

### 6.5 トレードオフの定量化

| シナリオ | 4 層通過 | 期待される動き |
|---|---|---|
| 強いトレンド継続中 (急騰・急落の本体) | ✗ (VWAP 帯外) | エントリーしない (機会損失) |
| 短期 retest / pullback | ○ (帯内 + momentum_5bar 反転) | エントリー候補 |
| 横ばい (VWAP 付近で振動) | ○ (帯内) だが momentum_5bar が薄い | momentum_5bar > 0.3 / < -0.3 で更にフィルタ |

実勢では「VWAP 帯内 + momentum_5bar 反転」が同時成立する瞬間は少なく、4 層通過頻度は極めて低い (mainnet 数日 0 件継続)。

### 6.6 未決の論点

- 帯幅 0.5% は妥当か。BTC では狭すぎ、ETH/ALT では更に狭すぎる可能性。動的 (ATR ベース) 帯幅も検討余地。
- momentum_5bar_pct 閾値 (±0.3%) が VWAP 帯と整合しているか。両方を同時に通せる "sweet spot" の存在性そのものを定量検証する余地。
- そもそも「mean-reversion 寄り戦略 + 4 層 AND」が望む姿か。Phase 1 の戦略選定時の議論を再訪する余地 (実機データを見てから)。

---

## 7. Watchlist 拡大の論点

### 7.1 現状

[`config/profile_phase2.yaml`](config/profile_phase2.yaml#L44-49):

```yaml
watchlist:
  fixed:
    - BTC
    - ETH
  directions:
    - LONG
    - SHORT
```

`max_position_count: 1` (L75) で同時保有 1 件、損失制限は標準より厳しめ (L78-80: daily 2.0% / weekly 5.0% / consecutive 2)。

### 7.2 候補 (SOL / XRP / BNB / HYPE)

ユーザー提案による拡張候補。中立的に並べる:

| 候補 | 流動性 (perp) | ボラ (1h) | HL 上場の安定性 | sentiment 取得性 |
|---|---|---|---|---|
| SOL | 高 (Tier 0 級) | ~1〜2% | 高 | funding_rate 標準 |
| XRP | 高 | ~1〜2% | 高 | funding_rate 標準 |
| BNB | 中〜高 | ~0.7〜1.5% | 高 | funding_rate 標準 |
| HYPE | 高 (native) | ~2〜4% | 最高 (HL 公式銘柄) | 取得可能だが ALT 特有のバイアスに留意 |

### 7.3 拡大時の構造的影響

- **API レート**: watchlist × directions の組み合わせで cycle あたり API 呼び出しが増える。BTC/ETH の 2 × 2 = 4 から 6 × 2 = 12 へ 3 倍。`meta_and_asset_ctxs` の cycle 間 cache (§20.4.D 設計予定だが未実装) が事実上の前提になる。
- **MOMENTUM 通過頻度**: ALT のほうがボラが高く VWAP 帯から外れやすい → 4 層通過は更に下がる方向。watchlist を増やしても通過件数の線形増加は見込めない。
- **共相関**: BTC が動くと ETH/SOL/XRP も同方向に動く。エントリー条件が一斉成立する場合があり、`max_position_count=1` の効果が「先着順 1 件」になる (どの銘柄を選ぶかの優先順位設計が必要)。
- **HL リスティング変更リスク**: ALT は突然 delisting や margin tier 変更の可能性あり。HYPE は native だが他の銘柄は HL 側仕様変更の影響を受ける。

### 7.4 段階的進め方の選択肢

| 段階 | 対象 | 前提作業 |
|---|---|---|
| Step 0 (現状) | BTC/ETH | (Phase A+B 完了済) |
| Step 1 | SOL を追加 | `meta_and_asset_ctxs` の cycle cache を先に実装 |
| Step 2 | SOL + XRP | tier 別の sentiment 取得安定性検証 |
| Step 3 | + HYPE | HL native 銘柄特有のバイアスを 1 週間観察 |
| Step 4 | + BNB | (任意。BNB 固有のメリットがあれば) |

優先順位の根拠: 流動性とボラの安定性。HYPE は流動性が最高だが native 特有のバイアス (HL チーム発信のニュースで動く等) があるため、観察期間を置く価値あり。

### 7.5 未決の論点

- **どの段階から始めるか**: 「4 層通過頻度が極めて低い」状況で watchlist を増やしても効果が出にくい可能性。先に §6 で議論した帯幅見直しが効くかもしれない。順序の判断は実機データ次第。
- **`meta_and_asset_ctxs` cycle cache**: §20.4.D に設計のみ存在。実装すれば API 負荷は線形でなく抑制できる。これが先決。
- **共相関時の優先銘柄選択**: 設計書 §4 には明示的なルールがない。FIFO / volatility 順 / sentiment 強度順 などの選択肢を後で議論する余地。

---

## 8. 4 層 AND の構造的トレードオフ (定量視点)

### 8.1 4 層の独立性

| 層 | 主な入力 | 通過率の主因 | 他層との独立性 |
|---|---|---|---|
| MOMENTUM | VWAP 距離 + momentum_5bar | 価格と VWAP の関係 | 価格に直結 |
| FLOW | buy/sell ratio + 大口 + volume surge | 板情報・約定情報 | 価格と中程度の相関 |
| SENTIMENT | funding_rate (contrarian) | funding 偏差 | 価格と弱相関 (遅行) |
| REGIME | BTC EMA + BTC ATR + funding + OI | マクロ条件 | 個別銘柄から独立 |

4 層 AND は独立性が高いほど効くが、MOMENTUM と FLOW は価格を介して相関する。

### 8.2 帯と倍率の積算で通過率が決まる

仮の独立性を仮定した粗推定 (各層通過率 p_i):

| 層 | 1 cycle あたり粗通過率 (仮) | 根拠 |
|---|---|---|
| MOMENTUM (LONG/SHORT 合算) | ~3〜5% | VWAP 帯 0.5% × momentum_5bar 0.3% を同時通過する瞬間が稀 |
| FLOW | ~5〜10% | volume surge 1.5x が常態でない |
| SENTIMENT | ~30〜50% | funding 偏差が方向性を持つ局面はそこそこある |
| REGIME | ~50〜70% | BTC ATR 5% 未満は常態 |

積算: 0.04 × 0.075 × 0.40 × 0.60 ≈ **0.072% / cycle**

cycle = 30s で 1 日 2880 cycle → 期待エントリー件数 ≈ 2 件/日。

**実観測**: mainnet Phase 2 で数日 0 件継続 → 実は 0.072% より低い (相関 + 過熱フィルタ + price_context 等の絞り込み)。

### 8.3 通過率が低いことのトレードオフ

| 視点 | 効果 (Pro) | 副作用 (Con) |
|---|---|---|
| 誤検知の少なさ | False positive が少なく無駄な loss を回避 | True positive も同時に逃す (機会損失) |
| 学習データ蓄積 | 高品質な signal だけが trade log に残る | サンプル数が少なく統計的検証が困難 |
| 心理的安全 | 大きく負ける確率が低い | 「BOT が動かない」体感ストレス |
| Phase 2 検証 | 1 件のエラーが重大な学びになる | 「動いてない」のと「動いて成功 / 失敗」の区別がつきにくい |

### 8.4 緩和の選択肢 (実装はしない・素材としてのみ)

| アプローチ | 対象 | 期待効果 | リスク |
|---|---|---|---|
| MOMENTUM 帯幅を 0.5% → 0.7% / 1.0% に拡張 | 1 層 | 通過率 ~1.5〜2× | mean-reversion 設計からの逸脱 |
| momentum_5bar 閾値を 0.3% → 0.2% に緩める | 1 層 | 通過率 ~1.5× | "薄い" 動きで入りやすくなる |
| REGIME の funding 過熱閾値を緩和 | 1 層 | 通過率 ~1.2× | 過熱の見逃し |
| 4 層 AND を 3 層 AND + 1 層 OR に変更 | 構造変更 | 通過率 ~3〜5× | 設計思想からの大幅逸脱 |
| 動的閾値 (ATR ベース) を導入 | 横断 | ボラ局面に応じて拡縮 | 実装規模大 |

### 8.5 未決の論点

- **緩和すべきか**: 「通過 0 件」は **設計通り** とも解釈できる。Phase 2 の目的が「壊さず観察」であれば、緩和は別 Phase に持ち越すべき。
- **動的閾値**: ATR ベースの帯幅は §6 の論点とも重なる。先に議論すべき。
- **観察期間**: 「数日 0 件」は 4 層 AND の問題か単に局面の問題か区別困難。最低 2〜4 週間の継続観察が必要。

---

## 9. 集約後の判断ポイント (記録のみ・判断は別途)

| トピック | 判断者が後で決めるべきこと | 関連節 |
|---|---|---|
| ALO 永続リスクへの対処 | 案 1 / 2 / 3 のどれか / 不要か | §5.4 |
| `meta_and_asset_ctxs` cycle cache | 実装するか / 後回しか | §7.5 |
| Watchlist 拡大 | どこから / どの順序 | §7.4 |
| MOMENTUM 帯幅 | 維持 / 緩和 / 動的化 | §6.6, §8.5 |
| 4 層 AND 構造変更 | 触らない / 緩和 / 構造変更 | §8.5 |
| §14.3 maker_first_router | 実装する / 設計書から削除 | §5.5 |
| incident response runbook | 整備するか | §1.4 |

すべての判断は **実機データを最低 2〜4 週間蓄積してから** を推奨。想像で実装すると過剰設計になりやすい (本ドキュメント自体がその回避のための記録)。

---

## 10. 参考リンク

### 10.1 コミット

- PR A1: `c0944fc` Enforce max_position_count gate at entry placement
- PR A2: `139cc37` Replace ATR placeholder with candle-based 14-period TR
- PR A3: `18d0645` Constrain reconciler fill matching by entry time and is_filled
- PR B1: `0b9f95e` Dedup fill processing by DB fill_time to prevent cross-trade contamination
- PR B2: `c2c4f04` Cancel known HL orders during CLOSE_FROM_FILL and MANUAL_REVIEW

### 10.2 関連設計書セクション

- §4 - 4 層 AND エントリー判定
- §6 - VWAP 計算と保有中追跡
- §9.3 - reconciliation のステップと cleanup
- §11 - TDD アーキテクチャ
- §14 - Maker-First 執行 (§14.3 は未実装)
- §20.4 - meta_and_asset_ctxs (cache 設計は未実装)
- §22.11 - HL の罠 (ALO 拒否)

### 10.3 外部参考

- [Hyperliquid Docs - Order types](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-types)
- [Hyperliquid Docs - Exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)
