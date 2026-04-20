# 選股策略 — 開發進度

最後更新: 2026-04-21

## 完成度總覽
- ✅ H 策略 v2.1 主倉定案 (終值 26,446 萬 / CAGR 54.7% / MDD -37.2% / Calmar 1.47)
- ✅ 資料庫還原引擎 (raw + adjuster + corporate_actions)
- ✅ 雙機自動同步架構 (Air + Mini via git + JSON)
- ⏳ 加碼池實作 (歷史長期待辦)

---

## 本 Sprint 完成（2026-04-20 ~ 04-21）

### 1. corporate_actions JSON Source of Truth
**問題**：Air 的 DB 手動維護了 33 筆事件 (2 split + 31 配息)，Mini clone 後是空的，adjuster 算出來還原價不一致。

**解法**：
- 匯出 `scanner/corporate_actions.json` (33 筆) 進 git
- `data_updater.py` 每次執行 [0/3] 步驟自動 sync JSON → DB (upsert + 刪除孤兒)
- `setup_mini.sh` 的 JSON_FILES 清單加入 corporate_actions.json
- Cron 指令加入 `git pull --ff-only` (確保拉到最新 JSON 才跑)

**檔案**：
- 新增 `scanner/corporate_actions.json`
- 改 `scanner/data_updater.py` (+sync_corporate_actions 函式)
- 改 `scanner/setup_mini.sh` (cron 加 git pull)

### 2. 補上遺漏 commit 的依賴
- `scanner/twse_fetcher.py` — 之前 untracked，Mini clone 後 data_updater import 失敗
- 修 `data_updater.py` 缺少 `import os` (TG credentials 讀取需要)

### 3. Mini 部署完成
- Repo clone 到 `~/Documents/Antigravity/選股策略/`
- `.env` 建好 (TG_BOT_TOKEN + TG_CHAT_ID)
- crontab 06:30 每日自動更新
- 首次全量抓完：0050/00631L/2330 + VIX 系列 + SMH (共 33,071 筆)

---

## 已完成項目清單

### 回測策略
- ✅ H 策略 v1.0 ~ v2.1 迭代
- ✅ Whipsaw 防護方案 A-E 實測，採 E (漸進 reset30)
- ✅ 災難出場規格書 v6
- ✅ 加碼池歷史驗證 (8 次觸發 T+60 勝率 100%)

### 資料基建
- ✅ `daily_prices_raw` — TWSE 原始 OHLCV
- ✅ `corporate_actions` — 事件表 (JSON as SoT)
- ✅ `adjuster.py` — 還原引擎 (最新日 factor=1.0 往回推)
- ✅ `twse_fetcher.py` — TWSE OpenAPI (3.5s rate limit)
- ✅ `data_updater.py` — 一鍵每日更新器
- ✅ 雙機 Air/Mini 自動同步

### 自動化
- ✅ Mini cron 06:30 每日更新 (git pull → sync → 抓數據 → git push)
- ✅ TG 警報 (@Bvcxza_bot)

---

## 待完成項目

### P1 — 加碼池實作
- 觸發: 單日 ≤-8% 且 H 命中 4/4
- 獨立 50 萬池，與主倉 SELL 同步回收
- 歷史勝率 100%, alpha +37.5%

### P2 — 資料完整性
- 0050 pre-2010 yfinance 補抓 (Air 有 4235 筆, Mini 3837 筆)
- 2330 pre-2010 同狀況 (目前影響小，回測從 2015 起)

### P3 — 長期
- 0050 新配息自動偵測 (目前手動維護 JSON)
- ETF 擴充 (0056、006208 等)

---

## 檔案狀態

| 檔案 | 狀態 |
|---|---|
| `scanner/h_v2_1.py` | ✅ 定稿 |
| `scanner/STRATEGY_SUMMARY.md` | ✅ 已更新 v2.1 |
| `scanner/corporate_actions.json` | ✅ 33 筆 (git SoT) |
| `scanner/twse_fetcher.py` | ✅ 已 commit |
| `scanner/adjuster.py` | ✅ 已驗證 |
| `scanner/backtest_core.py` | ✅ 走 adjuster 引擎 |
| `scanner/data_updater.py` | ✅ 含事件表 sync |
| `scanner/setup_mini.sh` | ✅ 含 git pull |

---

## 驗證指令

### 雙機資料一致性檢查（2015+）
```bash
python3 -c "
import sqlite3
c=sqlite3.connect('scanner/回測_0050還原數據.db')
for t in ('0050.TW','00631L.TW'):
    r=c.execute('SELECT COUNT(*), MIN(date), MAX(date), ROUND(SUM(close),2) FROM daily_prices_raw WHERE ticker=? AND date>=\"2015-01-01\"',(t,)).fetchone()
    print(t, r)
"
```
兩邊跑出完全相同 → 同步成功

### Mini 手動觸發
```bash
cd ~/Documents/Antigravity/選股策略/scanner
python3 data_updater.py --git-push
```

### TG 警報測試
```bash
python3 -c "from data_updater import send_tg_alert; send_tg_alert('test')"
```
