# 選股策略 — 即時狀態

最後更新: 2026-04-21

## 目前狀態
✅ 雙機資料同步架構完成並運作中

## 架構
- **Air (Mac)**: 開發 / 研究 / 手動更新事件
- **Mini**: 每日 06:30 cron 自動抓數據 + git push JSON
- **Source of Truth**:
  - `scanner/corporate_actions.json` (事件表, 33 筆)
  - `scanner/VIX歷史.json` 等 (yfinance 指標)
  - TWSE API (每日增量)
- **DB**: `scanner/回測_0050還原數據.db` — 本地重建，不走 git

## 同步鏈
```
Air 改 corporate_actions.json → git push
  ↓
Mini 06:30 → git pull → sync JSON 進 DB → 抓 TWSE + yfinance → git push
  ↓
Air 下次工作前 → git pull 拿回 Mini 抓的最新 JSON
```

## 當前資料覆蓋（Mini, 2026-04-21 首跑後）
| Ticker | 筆數 | 起迄 |
|---|---|---|
| 0050.TW | 3837 | 2010-2026 (TWSE) |
| 00631L.TW | 2663 | 2014-10 ~ 2026 |
| 2330.TW | 6536 | 2000-2026 (yfinance) |
| VIX/VIX9D/VIX3M/SMH | 3800~5600 | 2004-2026 |

注: Air 端 0050 有 4235 筆 (多了 2009 年 yfinance 補抓段)。回測從 2015 起，不影響。

## 警報
- TG Bot: 選股王 @Bvcxza_bot
- 觸發: 更新失敗 / git push 失敗 / 每週一正常確認
- 設定檔: `scanner/.env` (gitignored)

## 下次繼續
- 等待實際回測需求
- 加碼池實作（歷史長期待辦）
- 0050 配息資料自動補 (目前手動維護 JSON)
