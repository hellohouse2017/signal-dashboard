# 選股策略 — 專案總覽

## 狀態檔案讀取順序
1. `state.md` — 即時狀態（最新）
2. `PROGRESS.md` — 詳細進度記錄
3. `.claude/CLAUDE.md` — 本檔案（專案總覽）
4. `scanner/HANDOFF.md` — 上次 session 交接（歷史）

## 專案架構

### 核心目標
台股 ETF 策略（H 策略 v2.2，canonical 定義在 `scanner/h_strategy.py`），
以 0050 / 00631L 為主，用於實盤輔助決策。

### 資料庫
- **Primary DB**: `scanner/回測_0050還原數據.db`
- **Tables**:
  - `daily_prices_raw` — TWSE 原始 OHLCV (不還原)
  - `corporate_actions` — 拆分/配息事件 (Source: `corporate_actions.json`)
- **還原引擎**: `scanner/adjuster.py` (動態計算，最新日 factor=1.0 往回推)

### 架構（2026-07-27 定案：作業鏈在 Mini 24/7，Air 純開發）
| Mini cron | 職責 |
|---|---|
| 06:40 | `git pull --rebase` + `data_updater --no-alert --git-push`：抓數據、sync 事件表、push JSON |
| 07:10 | `signal_notify`：算 v2.2 訊號 + 發 TG（每日一則 = heartbeat，>24h 沒訊息=pipeline 掛了） |

- Air 的資料/通知 launchd 已停用（`~/Library/LaunchAgents.disabled/`），只留 intraday fetcher（研究用）
- **JSON 進 git**（Mini 每日 push）：`corporate_actions.json`（事件表 SoT）+ `VIX/9D/3M/SMH歷史.json`
- **DB 不走 git**：各機本地由 raw + adjuster 重建；Air 要新資料就 `git pull` + 跑 `data_updater.py`（不加 --git-push）
- ⚠️ **Air 上 signal_notify 一律 `--dry-run`**（真跑會雙發 TG + 狀態檔分岔，正式狀態在 Mini）

## 重要檔案

### 策略
- `scanner/h_strategy.py` — **唯一策略定義**（門檻/條件/決策 helper/asof 對齊）
- `scanner/h_v2_2.py` — 回測引擎（import h_strategy）
- `scanner/signal_notify.py` — 每日 TG 通知（import h_strategy）
- `scanner/STRATEGY_SPEC_v2.2-live.md` — 規格書
- `scanner/h_v2_1.py` / `h_v3_whipsaw.py` — legacy 對照

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
1. 在 Air 改 `scanner/corporate_actions.json` → `git push`
2. Mini 隔天 06:40 自動 pull + sync 進 DB（急用就 `ssh mini` 手動跑 data_updater）

### 絕不要做
- ❌ 手動改 DB 事件表（會被下次 sync 覆蓋回 JSON 版本）
- ❌ commit `.env` 或任何 `.db` 檔（已 gitignored）
- ❌ 不經測試改 `adjuster.py` 的還原邏輯

### 2026-04 已確認事件
- 00631L.TW 2026-03-31 1:22 分割 (ratio=22)
- 0050.TW 2025-06-18 1:4 分割 (ratio=4)
- 0050.TW 31 筆歷年配息 (2005-2026)

## 下次繼續

**優先順序：** 見 `state.md` 待辦（加碼池已於 2026-07 實測否決，勿再列入）

**接手 session 時先做：**
```bash
cd ~/Documents/Antigravity/選股策略
git pull
cat state.md                                # 看即時狀態
tail -5 scanner/signal-notify-local.log     # 通知器有沒有每天跑
```
