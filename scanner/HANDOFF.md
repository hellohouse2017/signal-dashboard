# 工作進度與交接事項

最後更新: 2026-04-20

## 本次 Session 完成

### A. v2.1 主倉定案
- 5 種 whipsaw 防護方案實測（A-E），採 **E 漸進 reset30**
- ~~舊數據: 終值 22,635 萬 / CAGR 52.5% / Calmar 1.41 / 賣出 33 次~~
- **新數據 (raw+adjuster): 終值 26,446 萬 / CAGR 54.7% / MDD -37.2% / Calmar 1.47 / 賣出 31 次**
- 落檔: `h_v2_1.py`、`h_v3_whipsaw.py`、`STRATEGY_SUMMARY.md`
- 結論: 2018 Q4 whipsaw 在 consec 機制下本質無解，v2.1 為整體優化

### B. 資料庫自動更新模組（初版）
- 新增表 `corporate_actions`: 事件總表（PK ticker+ex_date+action）
- 新增表 `daily_prices_raw`: TWSE 原始 OHLCV
- 檔案:
  - `init_corporate_actions.py` — 事件表建立
  - `twse_fetcher.py` — TWSE OpenAPI 抓取（SSL 繞過 + 3.5s rate limit）
  - `adjuster.py` — 還原引擎（最新日 factor=1.0 往回推）
- 已驗證: 00631L.TW 2026-03-31 1:22 分割還原連續
- 已抓: 00631L raw 2014-10-31 ~ 2026-04-17（2787 筆）

## 已知錯誤紀錄（避免重犯）
- ❌ 誤宣稱 00631L 2022-10-24 有 1:5 反分割 → 已從 corporate_actions 刪除
- ✅ 0050 2025 拆分已確認: 1:4 分割, 2025-06-18 生效 (暫停交易 6/11~6/17)

## 待辦事項（優先順序）

### ~~1. 釐清 0050 2025 拆分~~ ✅ 已完成
- 1:4 分割, 除權生效日 2025-06-18, 已寫入 `corporate_actions` 表
- 來源: TWSE 公告、聯合新聞網、商周、今周刊等多方確認

### ~~2. 擴展原始資料覆蓋~~ ✅ 大部分完成
- ✅ 0050.TW: 4235 筆 (2009-01-02 ~ 2026-04-20), TWSE 2010+ / yfinance 2009
- ✅ 2330.TW: 6501 筆 (2000-01-04 ~ 2026-04-20), TWSE 2010+ / yfinance 2000-2009
- ⏳ 0050 配息歷史（TWSE 除權息表）→ `corporate_actions` cash_dividend (未做)
- ⚠️ TWSE API 限制: 民國 99 年 (2010) 以前不支援, 需靠 yfinance 補
- ⚠️ 2330 yfinance pre-2010 價格為已拆分調整價 (2000年 ~69 元), 與 TWSE 2010+ 接縫連續

### ~~3. MarketDataLoader 改造~~ ✅ 已完成
- `backtest_core.py` 已改從 `daily_prices_raw` + `adjuster.get_adjusted_prices()` 讀
- v2.1 重跑結果: 終值 26,446 萬 / CAGR 54.7% / MDD -37.2% / Calmar 1.47
- 差異原因: 舊 daily_prices 表有混雜還原問題, 新引擎更準確

### 4. 每日自動更新排程
- `data_updater.py`: 增量抓 TWSE（只抓最新一個月）+ yfinance VIX/SMH
- **事件表同步**: `corporate_actions.json` 為 source of truth，每次執行自動 sync 進 DB
  - Air 手動改 JSON → git push → Mini 06:30 自動 sync
- macOS launchd: 每日 15:30 執行

### 5. 加碼池實作（來自前段長期待辦）
- 觸發: 單日 ≤-8% 且 H 命中 4/4
- 獨立 50 萬池，與主倉 SELL 同步回收
- 歷史 8 次觸發 T+60 勝率 100%，alpha +37.5%

## 檔案狀態
| 檔案 | 狀態 |
|---|---|
| `h_v2_1.py` | ✅ 定稿 |
| `STRATEGY_SUMMARY.md` | ✅ 已更新 v2.1 |
| `init_corporate_actions.py` | ✅ 2 筆 (00631L 分割 + 0050 分割) |
| `twse_fetcher.py` | ✅ 可用 |
| `adjuster.py` | ✅ 已驗證 00631L |
| `backtest_core.py` | ✅ 已改走 adjuster 還原引擎 |
| `data_updater.py` | ⏳ 未建立 |

## 資料庫現況
- `daily_prices` — 舊表（混雜），已不再使用
- `daily_prices_raw` — 3 檔: 00631L(2787筆) + 0050(4235筆) + 2330(6501筆)
- `corporate_actions` — 2 筆（00631L split 22 + 0050 split 4）
- `etf_daily` — 重複表，待整併
