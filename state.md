# 選股策略 — 即時狀態

最後更新: 2026-07-06

## 目前狀態
✅ 雙機資料同步架構運作中
✅ **H 策略 v2.2 定案並落地**（canonical single source of truth + TG 通知同步）
📦 8 個當沖策略 + 網格系統 + Dashboard 已歸檔（實測無 edge，見 `archive/README.md`）

## H 策略 v2.2（主倉，長期持有 00631L）

### 核心
- v2.1 的 alpha 來源是「災難避開」，不是選股訊號 → 保留。
- v2.2 只解一個實測弱點：**多頭中出場太頻繁**（假跌破洗出、隔天追高）。
- 兩個 regime 槓桿（都以 0050 收盤 vs MA200 判斷多頭）:
  - **多頭鈍化出場**：0050 > MA200 時，disaster 出場門檻 +1（`bull_exit_bonus=1`）。
  - **多頭放寬閃崩防守**：多頭時單日 -9% / 6 根 -22%（平時 -6% / -15%），`flash_mode=bull_relax`。

### 樣本外驗證（2015-2020 訓練挑參數 / 2021-2026 驗證，含證交稅）
| | v2.1 | v2.2 | 差 |
|---|---|---|---|
| TEST CAGR | 73.6% | 74.5% | +0.9pp |
| TEST 賣出次數 | 26 | 16 | **少 38%** |
| 全期 CAGR | 52.0% | 51.9% | 持平 |
| 全期 MDD | -30.7% | -30.7% | 持平 |

穩健結論：**報酬持平略升、交易次數大減**（省成本、更好執行）。
誠實標註：TEST 期 MDD 在不同算法下 v2.1/v2.2 互有領先（-28.2% vs -30.7%），差異不顯著；v2.2 的確定性優勢在「少交易」而非「更抗跌」。

### 檔案（canonical 架構，解決門檻漂移技術債）
- `scanner/h_strategy.py` — **唯一策略定義**（門檻、條件、決策 helper）
- `scanner/h_v2_2.py` — 回測引擎（import h_strategy）
- `scanner/signal_notify.py` — 每日 TG 通知（import h_strategy，已與回測同步）
- `scanner/optimize_h_v2_2.py` — OOS 網格研究腳本（產出上述驗證）
- `scanner/STRATEGY_SPEC_v2.2-live.md` — 規格書（取代 v2.1-live）
- 舊 `scanner/h_v2_1.py` / `backtest_core.py` 保留作 legacy 對照

### 重要修正
- 回測引擎補上 **0.1% 證交稅**（原 backtest_core 只算手續費，略高估所有 H 績效）。
- 移除主迴圈被複製 4 份、門檻不一致（VIX 26/28、C3 -10%/-15%）的技術債。

## 架構（不變）
- **Air (Mac)**: 開發 / 研究 / 手動更新事件
- **Mini**: 每日 06:30 cron 自動抓數據 + git push JSON
- **Source of Truth**: `corporate_actions.json` (33 筆) + `VIX/SMH歷史.json` + TWSE API
- **DB**: `回測_0050還原數據.db` — 本地重建，不走 git
- TG Bot: 選股王 @Bvcxza_bot（設定 `scanner/.env`）

## 歸檔（archive/，實測無 edge）
- `strategies/` 8 個當沖策略：全部負 CAGR。高勝率（gap_fill 70%）掩蓋不對稱爆虧；gap_fill 611% CAGR 是複利+前視假象。
- `grid_backtest.py` / `grid_variant_research.py`：20 組參數全部打不過 B&H（1/7 勝），且日內 OHLC 路徑用猜的。
- `dashboard.py`：上述策略的前端。
- 詳見 `archive/README.md`。

## 下次繼續
- **未實跑正式 launchd 通知的 v2.2 首次真發**（目前 dry-run 驗證通過，state.md 停在 2026-06-05，下次真跑會用 v2.2 邏輯回溯補齊）
- 加碼池實作（P1 長期待辦）
- 永豐證券 API 帳號進度（原待辦）

## 接手先做
```bash
cd ~/Documents/Antigravity/選股策略
git pull
cat state.md
python3 scanner/signal_notify.py --dry-run   # 看當前 v2.2 訊號
```
