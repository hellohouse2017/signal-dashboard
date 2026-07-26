# 選股策略 — 即時狀態

最後更新: 2026-07-27

## 目前狀態
✅ 雙機資料同步架構運作中
✅ **H 策略 v2.2 通知器已正式上線**（2026-07-27 首發 + launchd 每日 07:10 自動跑）
✅ Live-回測 parity 全面對齊（5 個不一致已修，見下）
📦 加碼池已實測否決（`scanner/bonus_pool_v2.py`）；當沖/網格/Dashboard 早前已歸檔

## H 策略 v2.2（主倉，長期持有 00631L）

### 誠實績效（2026-07-24 資料，parity 對齊後引擎）
| | v2.2 | v2.1 | B&H |
|---|---|---|---|
| 全期 CAGR | **47.0%** | 47.0% | 35.9% |
| 全期 MDD | -32.5% | -32.5% | -55.1% |
| 賣出次數 | **29** | 43 | 0 |

- 舊文件的 49.6%（更早的 51.9%）含「美國休市日 C2/C4 被強制 False」的評估漏洞，
  對齊 live 語意（美股 as-of forward-fill）後誠實數字是 47.0%。
- **v2.1 與 v2.2 終值已完全相同**；v2.2 的唯一穩健優勢是少 14 次交易。
- 2026-07 穩健性體檢結論：alpha 85% 集中在 4 個災難事件（2022、2025 最大）；
  2018 Q4 型陰跌是已知失敗模式；C3 門檻鄰域敏感（±2.5pp → CAGR ∓3-6pp）。
  引用數字時當區間看（45~48%），不要當點估計。

### 執行紀律（比調參重要）
- 回測假設 T+1 開盤成交。實測：拖到隔日收盤 CAGR -7.5pp、晚一天 -8.4pp。
- **06:30 後看訊號、09:00 開盤執行**，這個時序本身就是 edge 的一部分
  （美股同日資料配對貢獻 +4pp，靠的是 cron 在美股收盤後、台股開盤前跑）。
- 已測否決、勿再投入：T+0 盤中執行（更差）、加碼池留現金（大輸全投入）、
  參數再調優（曲面崎嶇 = overfit）、日內當沖疊加（archive 已否決）。

### 通知器（2026-07-27 上線）
- launchd: `com.signal.notify-local`（每日 07:10，資料更新 06:40 之後）
- 每天固定一則 TG = heartbeat；**超過 24h 沒訊息 = pipeline 掛了**
- 資料過期警報：台股落後 TWSE 官方、VIX/SMH 落後台股 >4 天都會在訊息內警告
- log: `scanner/signal-notify-local.log` / `.err`
- **史實更正**：體檢時說「7/17 訊號沒發出」是錯的——Mini 其實一直有一個
  13:40 cron 在跑舊版 signal_notify（美股資料滯後一天、含 parity bug、
  獨立狀態檔），7/17 閃崩當天有發出通知。該 cron 已於 2026-07-27 退役，
  收斂到本機 07:10 單一通知（美股同日配對、修 bug 後的 canonical 版）。

### Parity 修復（2026-07-27，回測與 live 現在走同一條路）
1. 回測美股資料改 as-of forward-fill（`h_strategy.asof_date_map`，兩邊共用）
2. 通知器閃崩防守 raw 價 → 還原價（分割/配息日不再誤觸發）
3. 通知器 out_low 改存日期、每次還原價重查（除權後回場條件 C 不會壞）
4. 通知器 quiet_days 對齊規格（n<3 即累計；原本 n=2 會錯誤歸零）
5. 刪掉 signal_notify 主路徑的 inline 條件複製（一律走 `eval_conditions`）
- 回測 prev_close 同步改為真實前一台股交易日（台股連假後回場條件 B 可評估）

## 待辦
1. **手續費折扣（使用者行動）**：向券商談電子下單折扣，0.1425%→0.04% 全期 +0.9pp CAGR，
   零風險。談成後不用改程式（回測維持保守全額費率）。
2. 部分出場研究（閃崩先砍半倉）：需先定義半倉回場規則 + train/test 協議才准跑。
3. 00675L 替代評估：需先補 raw 資料 + 事件表。
4. 永豐證券 API 帳號進度（原待辦）。

## 架構（2026-07-27 收斂為單機作業鏈）
- **Air（本機，唯一作業鏈）**:
  - 06:40 `data_updater --no-alert --git-push`（抓 TWSE+yfinance、sync 事件表、push JSON）
  - 07:10 `signal_notify`（算訊號 + 發 TG）
  - intraday fetcher（1分K 研究資料）
- **Mini：已退役**（2026-07-27 移除其 16:00 data_updater 與 13:40 signal_notify cron，
  備份在 Mini `~/crontab.backup.2026-07-27`；殭屍 agent `com.stockscanner.autoscan`
  已卸載至 `~/LaunchAgents.disabled`。民宿系統等其他排程不受影響）
- **Source of Truth**: `corporate_actions.json` + `VIX/SMH歷史.json` + TWSE API
- **DB**: `回測_0050還原數據.db` — 本地重建，不走 git
- TG Bot: 選股王 @Bvcxza_bot（設定 `scanner/.env`）
- 筆電風險：闔蓋出門則排程不跑；launchd 會在開蓋時補跑，09:00 前開機即可。
  可選強化：`sudo pmset repeat wakeorpoweron MTWRFSU 06:35:00`

## 接手先做
```bash
cd ~/Documents/Antigravity/選股策略
git pull
cat state.md
tail -20 scanner/signal-notify-local.log   # 通知器有沒有每天跑
python3 scanner/signal_notify.py --dry-run # 看當前 v2.2 訊號
```
