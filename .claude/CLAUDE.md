# 選股策略 — 專案總覽

## 狀態檔案讀取順序
1. `state.md` — 即時狀態（最新）
2. `PROGRESS.md` — 詳細進度記錄
3. `.claude/CLAUDE.md` — 本檔案（專案總覽）
4. `scanner/HANDOFF.md` — 上次 session 交接（歷史）

## 專案架構

### 核心目標
台股 ETF 策略（H 策略 v2.1），以 0050 / 00631L 為主，用於實盤輔助決策。

### 資料庫
- **Primary DB**: `scanner/回測_0050還原數據.db`
- **Tables**:
  - `daily_prices_raw` — TWSE 原始 OHLCV (不還原)
  - `corporate_actions` — 拆分/配息事件 (Source: `corporate_actions.json`)
- **還原引擎**: `scanner/adjuster.py` (動態計算，最新日 factor=1.0 往回推)

### 雙機架構
| 角色 | 職責 |
|---|---|
| **Air** (Mac) | 開發、回測、手動維護 `corporate_actions.json` |
| **Mini** (Mac mini) | 每日 06:30 cron 自動抓數據 + git push |

### 同步機制
- **JSON as Source of Truth**：所有需要兩邊對齊的資料存 JSON 進 git
  - `corporate_actions.json` — 事件表
  - `VIX歷史.json` / `VIX9D歷史.json` / `VIX3M歷史.json` / `SMH歷史.json` — 市場指標
- **DB 不走 git**：每台機器本地重建，透過 JSON + TWSE API 同步

## 重要檔案

### 策略
- `scanner/h_v2_1.py` — H 策略 v2.1 主檔
- `scanner/h_v3_whipsaw.py` — Whipsaw 防護方案 A-E 測試
- `scanner/STRATEGY_SUMMARY.md` — 策略總結

### 資料
- `scanner/data_updater.py` — 每日更新器（TWSE + yfinance + 事件表 sync）
- `scanner/twse_fetcher.py` — TWSE OpenAPI 抓取
- `scanner/adjuster.py` — 還原引擎
- `scanner/corporate_actions.json` — 事件表 SoT

### 部署
- `scanner/setup_mini.sh` — Mini 一鍵安裝（crontab + dry-run 驗證）
- `scanner/.env.example` — TG 警報設定範本
- `scanner/.env` — TG 實際憑證（gitignored）

## 重要提醒

### 改事件時必走流程
1. 改 `scanner/corporate_actions.json`
2. `git push`
3. Mini 隔天 06:30 自動同步（或手動 `python3 data_updater.py`）
4. 兩邊 DB 自動對齊

### 絕不要做
- ❌ 手動改 DB 事件表（會被下次 sync 覆蓋回 JSON 版本）
- ❌ commit `.env` 或任何 `.db` 檔（已 gitignored）
- ❌ 不經測試改 `adjuster.py` 的還原邏輯

### 2026-04 已確認事件
- 00631L.TW 2026-03-31 1:22 分割 (ratio=22)
- 0050.TW 2025-06-18 1:4 分割 (ratio=4)
- 0050.TW 31 筆歷年配息 (2005-2026)

## 下次繼續

**優先順序：**
1. 等待明確回測需求（如策略調整、新標的加入）
2. 加碼池實作（P1 長期待辦）
3. 資料完整性補強（P2, 影響小）

**接手 session 時先做：**
```bash
cd ~/Documents/Antigravity/選股策略
git pull  # 拿 Mini 最新數據
cat state.md  # 看即時狀態
```
