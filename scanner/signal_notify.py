#!/usr/bin/env python3
"""每日進出場信號 TG 通知 — 本地版

取代壞掉的 Vercel API + GitHub Actions，直接讀本地 DB/JSON 計算信號。

用法:
  python3 signal_notify.py              # 計算信號 + 發 TG
  python3 signal_notify.py --dry-run    # 只顯示結果，不發 TG
  python3 signal_notify.py --no-alert   # 不發 TG

條件 (h_strategy canonical, v2.2):
  出場 4 取 3 + 漸進門檻 2+sell_streak（多頭 regime 再 +bonus）:
    c1: 0050 < MA60 且 < MA120
    c2: VIX > 26 且 VIX9D > 26 且 VIX3M > 26
    c3: 00631L 從歷史高點回撤 > 15%
    c4: SMH < MA30 且 < MA60
  多頭 regime (0050 > MA200):
    出場門檻 +1（避免多頭假跌破洗出）
    閃崩防守放寬為 單日 <= -9% 或 5 日 <= -22%
  閃崩防守（一般）:
    00631L 單日 <= -6% 或 5 日 <= -15%
  回場（三選一）:
    n_conds < 3 (不是災難)
    00631L 單日 >= +8%
    00631L 自出場後低點反彈 >= +20%

數據來源:
  - 0050, 00631L: daily_prices_raw (SQLite)
  - VIX, VIX9D, VIX3M, SMH: JSON 檔案

狀態追蹤:
  - scanner/.signal_state.json: 記錄連續天數、上次狀態
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# Canonical strategy definition — single source of truth (shared with backtester).
# Changing a threshold in h_strategy.py updates both backtest and this notifier.
from h_strategy import (
    V22,
    REGIME_WIN,
    RESET_QUIET_DAYS,
    disaster_exit_threshold,
    eval_conditions,
    flash_triggered,
    is_bull,
    reentry_reason,
)

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "回測_0050還原數據.db"
STATE_FILE = SCRIPT_DIR / ".signal_state.json"

# Live strategy parameters (v2.2). Imported preset keeps notifier == backtest.
PARAMS = V22

# JSON 資料檔
VIX_JSON = SCRIPT_DIR / "VIX歷史.json"
VIX9D_JSON = SCRIPT_DIR / "VIX9D歷史.json"
VIX3M_JSON = SCRIPT_DIR / "VIX3M歷史.json"
SMH_JSON = SCRIPT_DIR / "SMH歷史.json"


# ── 工具函式 ─────────────────────────────────────
def sma(data: dict[str, float], dates: list[str], window: int) -> dict[str, float]:
    """計算簡單移動平均"""
    result = {}
    vals = []
    for d in dates:
        if d in data:
            vals.append(data[d])
        if len(vals) >= window:
            result[d] = sum(vals[-window:]) / window
    return result


def expanding_max(data: dict[str, float], dates: list[str]) -> dict[str, float]:
    """計算 expanding window 最高值（歷史最高點）"""
    result = {}
    cur_max = float('-inf')
    for d in dates:
        if d in data:
            cur_max = max(cur_max, data[d])
            result[d] = cur_max
    return result


def load_json_prices(path: Path) -> dict[str, float]:
    """讀取 JSON 價格檔"""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_db_prices(conn: sqlite3.Connection, ticker: str,
                   limit: int = 200) -> dict[str, float]:
    """從 DB 讀取最近 N 天收盤價"""
    rows = conn.execute(
        "SELECT date, close FROM daily_prices_raw "
        "WHERE ticker=? ORDER BY date DESC LIMIT ?",
        (ticker, limit),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def load_adjusted_prices(ticker: str) -> dict[str, float]:
    """透過 adjuster 取得還原後收盤價（處理分割+配息）"""
    from adjuster import get_adjusted_prices
    adj = get_adjusted_prices(ticker, str(DB_PATH))
    return {d: v["close"] for d, v in adj.items() if v["close"] is not None}


def fetch_today_close_yf(yf_ticker: str, max_retries: int = 3,
                         retry_interval: int = 120) -> float | None:
    """用 yfinance 即時抓取今日收盤價（收盤後 ~5 分鐘可用）

    max_retries: 最多重試次數（含首次）
    retry_interval: 重試間隔秒數（預設 120 秒）
    """
    import time as _time
    for attempt in range(max_retries):
        try:
            import yfinance as yf
            ticker = yf.Ticker(yf_ticker)
            hist = ticker.history(period="1d")
            if not hist.empty:
                close_val = float(hist["Close"].iloc[-1])
                actual_date = hist.index[-1].strftime("%Y-%m-%d")
                if actual_date == date.today().isoformat():
                    return close_val
        except Exception as e:
            print(f"  ⚠️ yfinance {yf_ticker} 抓取失敗: {e}")

        if attempt < max_retries - 1:
            print(f"  ⏳ {yf_ticker} 今日資料尚未就緒，{retry_interval}s 後重試 ({attempt+2}/{max_retries})")
            _time.sleep(retry_interval)

    return None


def roc_date_to_iso(raw: str) -> str | None:
    """TWSE 民國日期轉 ISO 日期。"""
    try:
        year_s, month_s, day_s = raw.split("/")
        return f"{int(year_s) + 1911:04d}-{int(month_s):02d}-{int(day_s):02d}"
    except Exception:
        return None


def fetch_twse_latest_close(stock_no: str) -> tuple[str, float] | None:
    """從 TWSE 官方 STOCK_DAY 取得該標的目前可見的最新收盤價。"""
    for offset_days in (0, 7, 35):
        query_day = date.today() - timedelta(days=offset_days)
        params = urllib.parse.urlencode({
            "date": query_day.strftime("%Y%m%d"),
            "stockNo": stock_no,
            "response": "json",
        })
        req = urllib.request.Request(
            f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?{params}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                payload = json.loads(resp.read())
        except Exception as e:
            print(f"  ⚠️ TWSE {stock_no} 最新收盤抓取失敗: {e}")
            continue

        rows = payload.get("data") or []
        parsed: list[tuple[str, float]] = []
        for row in rows:
            if len(row) < 7:
                continue
            iso_date = roc_date_to_iso(row[0])
            if not iso_date:
                continue
            try:
                close_val = float(str(row[6]).replace(",", ""))
            except ValueError:
                continue
            parsed.append((iso_date, close_val))

        if parsed:
            return max(parsed, key=lambda item: item[0])

    return None


def supplement_twse_latest_closes(
    p0050_all: dict[str, float],
    p631l_raw: dict[str, float],
    p631l_adj: dict[str, float],
) -> tuple[dict[str, str], list[str]]:
    """用 TWSE 官方資料補齊 DB 尚未寫入的最新台股收盤。"""
    official_dates: dict[str, str] = {}
    warnings: list[str] = []

    for ticker, stock_no, target in [
        ("0050.TW", "0050", p0050_all),
        ("00631L.TW", "00631L", p631l_raw),
    ]:
        latest = fetch_twse_latest_close(stock_no)
        if not latest:
            warnings.append(f"TWSE 官方最新價無法驗證: {ticker}")
            continue

        latest_date, close_val = latest
        official_dates[ticker] = latest_date
        existing = target.get(latest_date)
        if existing is None:
            target[latest_date] = close_val
            print(f"  📡 TWSE 補齊 {ticker} {latest_date} 收盤: {close_val:.2f}")
        elif abs(existing - close_val) > 0.01:
            warnings.append(
                f"{ticker} DB 收盤 {existing:.2f} 與 TWSE {latest_date} {close_val:.2f} 不一致"
            )

        if ticker == "00631L.TW" and latest_date not in p631l_adj:
            p631l_adj[latest_date] = close_val

    return official_dates, warnings


def latest_date_on_or_before(data: dict[str, float], target_date: str) -> str | None:
    candidates = [d for d in data if d <= target_date]
    return max(candidates) if candidates else None


# ── TG ───────────────────────────────────────────
def _load_tg_credentials() -> tuple[str, str]:
    token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        env_path = SCRIPT_DIR / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "TG_BOT_TOKEN" and not token:
                    token = v
                elif k == "TG_CHAT_ID" and not chat_id:
                    chat_id = v
    return token, chat_id


def send_tg(message: str) -> bool:
    token, chat_id = _load_tg_credentials()
    if not token or not chat_id:
        print("  ⚠️ TG 未設定，跳過通知")
        return False
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print("  📱 TG 通知已送出")
                return True
            print(f"  ❌ TG 回應異常: {result}")
            return False
    except Exception as e:
        print(f"  ❌ TG 發送失敗: {e}")
        return False


# ── 狀態追蹤 ─────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "consec_days": 0,
        "sell_streak": 0,      # v2.1 漸進門檻
        "quiet_days": 0,       # 持倉安靜天數（重置 sell_streak 用）
        "position": "holding", # holding / out
        "out_low": None,       # 出場後最低價（回場條件 C）
        "last_reentry_reason": None,
        "last_status": "normal",
        "last_date": "",
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── 信號計算 ─────────────────────────────────────
def calc_signal(dry_run: bool = False) -> dict:
    """計算當日信號（h_v2_1 策略條件）"""

    # 讀資料（0050 需 400 筆：MA200 regime 需 200 天窗口 + 回溯補齊餘裕）
    conn = sqlite3.connect(str(DB_PATH))
    p0050_all = load_db_prices(conn, "0050.TW", 400)
    p631l_raw = load_db_prices(conn, "00631L.TW", 500)
    conn.close()

    # 00631L 用 adjuster 還原價（處理反分割+配息斷層）
    p631l_adj = load_adjusted_prices("00631L.TW")

    official_twse_dates, data_warnings = supplement_twse_latest_closes(
        p0050_all, p631l_raw, p631l_adj,
    )

    # ── yfinance 即時補抓（收盤後 DB 還沒更新時用）────
    today_str = date.today().isoformat()
    yf_supplemented = False
    if today_str not in p0050_all and datetime.now().hour >= 13:
        yf_0050 = fetch_today_close_yf("0050.TW")
        if yf_0050:
            p0050_all[today_str] = yf_0050
            print(f"  📡 0050 今日收盤 (yfinance): {yf_0050:.2f}")
            yf_supplemented = True
    if today_str not in p631l_raw and datetime.now().hour >= 13:
        yf_631l = fetch_today_close_yf("00631L.TW")
        if yf_631l:
            p631l_raw[today_str] = yf_631l
            # 今日 adj_factor = 1.0，所以 raw close = adj close
            p631l_adj[today_str] = yf_631l
            print(f"  📡 00631L 今日收盤 (yfinance): {yf_631l:.2f}")
            yf_supplemented = True

    vix_all = load_json_prices(VIX_JSON)
    vix9d_all = load_json_prices(VIX9D_JSON)
    vix3m_all = load_json_prices(VIX3M_JSON)
    smh_all = load_json_prices(SMH_JSON)

    if not p0050_all:
        return {"error": "0050 資料為空"}
    if not p631l_raw:
        return {"error": "00631L 資料為空"}

    # 按日期排序
    all_dates = sorted(set(
        list(p0050_all.keys()) + list(p631l_raw.keys()) + list(vix_all.keys()) + list(smh_all.keys())
    ))

    # 最新日期
    latest_0050_date = max(p0050_all.keys())
    latest_631l_date = max(p631l_raw.keys())
    latest_tw_date = max(set(p0050_all) & set(p631l_raw))
    latest_vix_date = max(vix_all.keys()) if vix_all else "N/A"
    latest_smh_date = max(smh_all.keys()) if smh_all else "N/A"

    if latest_0050_date != latest_631l_date:
        data_warnings.append(
            f"0050 最新 {latest_0050_date} / 00631L 最新 {latest_631l_date}，已用共同最新日 {latest_tw_date}"
        )

    official_common_dates = [
        d for d in [
            official_twse_dates.get("0050.TW"),
            official_twse_dates.get("00631L.TW"),
        ] if d
    ]
    if official_common_dates:
        official_latest_tw_date = min(official_common_dates)
        if latest_tw_date < official_latest_tw_date:
            data_warnings.append(
                f"本地台股資料只到 {latest_tw_date}，TWSE 官方已到 {official_latest_tw_date}"
            )

    # 計算 MA
    ma60_0050 = sma(p0050_all, all_dates, 60)
    ma120_0050 = sma(p0050_all, all_dates, 120)
    ma_regime_0050 = sma(p0050_all, all_dates, REGIME_WIN)  # v2.2 多頭 regime (MA200)
    ma30_smh = sma(smh_all, all_dates, 30)
    ma60_smh = sma(smh_all, all_dates, 60)
    # 00631L C3: 用 adjuster 還原價 + expanding_max（和回測引擎一致）
    adj_dates = sorted(p631l_adj.keys())
    exp_max_631l = expanding_max(p631l_adj, adj_dates)

    # 取最新值
    p50 = p0050_all.get(latest_tw_date)
    ma60 = ma60_0050.get(latest_tw_date)
    ma120 = ma120_0050.get(latest_tw_date)

    vix = vix_all.get(latest_vix_date)
    vix9d = vix9d_all.get(latest_vix_date) if vix9d_all else None
    vix3m = vix3m_all.get(latest_vix_date) if vix3m_all else None

    smh = smh_all.get(latest_smh_date)
    s30 = ma30_smh.get(latest_smh_date)
    s60 = ma60_smh.get(latest_smh_date)

    # 00631L 最新
    p631l = p631l_raw.get(latest_tw_date)  # 顯示用 raw 價
    p631l_adj_latest = p631l_adj.get(latest_tw_date)  # C3 用 adj 價
    mx631l_adj = exp_max_631l.get(latest_tw_date)  # expanding max (adj)

    # 00631L 前日收盤（回場 B 用）
    p631l_prev_adj = None
    if latest_tw_date in p631l_adj:
        prev_dates = [d for d in adj_dates if d < latest_tw_date]
        if prev_dates:
            p631l_prev_adj = p631l_adj[prev_dates[-1]]

    # 4 個出場條件 (h_v2_1)
    c1 = bool(p50 and ma60 and ma120 and p50 < ma60 and p50 < ma120)
    c2 = bool(vix and vix9d and vix3m and vix > 26 and vix9d > 26 and vix3m > 26)
    # C3: 用 adjuster 還原價的 expanding_max 判斷回撤（和回測一致）
    c3 = bool(p631l_adj_latest and mx631l_adj and mx631l_adj > 0
             and (p631l_adj_latest / mx631l_adj - 1) < -0.15)
    c4 = bool(smh and s30 and s60 and smh < s30 and smh < s60)

    n_conds = sum([c1, c2, c3, c4])
    disaster = n_conds >= 3

    # ── 狀態追蹤與回溯補齊 ─────────────────────────
    state = load_state()
    last_date = state.get("last_date", "")

    # 取得大於 last_date 且小於等於最新日期的所有台灣交易日
    missing_dates = sorted([d for d in p0050_all.keys() if d > last_date and d <= latest_tw_date])

    if missing_dates:
        print(f"  ⏳ 偵測到未處理交易日，開始自動回溯補齊狀態: {missing_dates}")
        for d in missing_dates:
            p50_d = p0050_all.get(d)
            ma60_d = ma60_0050.get(d)
            ma120_d = ma120_0050.get(d)
            ma_regime_d = ma_regime_0050.get(d)

            vix_d_dates = [date for date in vix_all if date <= d]
            vix_date = max(vix_d_dates) if vix_d_dates else None
            vix_d = vix_all.get(vix_date) if vix_date else None
            vix9d_d = vix9d_all.get(vix_date) if vix_date else None
            vix3m_d = vix3m_all.get(vix_date) if vix_date else None

            smh_d_dates = [date for date in smh_all if date <= d]
            smh_date = max(smh_d_dates) if smh_d_dates else None
            smh_d = smh_all.get(smh_date) if smh_date else None
            s30_d = ma30_smh.get(smh_date) if smh_date else None
            s60_d = ma60_smh.get(smh_date) if smh_date else None

            p631l_d = p631l_raw.get(d)
            p631l_adj_d = p631l_adj.get(d)
            mx631l_adj_d = exp_max_631l.get(d)

            p631l_prev_adj_d = None
            if d in p631l_adj:
                prev_dates_d = [dt for dt in adj_dates if dt < d]
                if prev_dates_d:
                    p631l_prev_adj_d = p631l_adj[prev_dates_d[-1]]

            # Canonical conditions (shared with backtest via h_strategy)
            c1_d, c2_d, c3_d, c4_d = eval_conditions(
                p50_d, ma60_d, ma120_d,
                vix_d, vix9d_d, vix3m_d,
                p631l_adj_d, mx631l_adj_d,
                smh_d, s30_d, s60_d,
            )
            n_conds_d = sum([c1_d, c2_d, c3_d, c4_d])
            disaster_d = n_conds_d >= 3

            # v2.2 多頭 regime + regime-aware 閃崩防守
            bull_d = is_bull(p50_d, ma_regime_d)
            recent_dates_d = [dt for dt in all_dates if dt <= d and dt in p631l_raw][-6:]
            recent_closes_d = [p631l_raw[dt] for dt in recent_dates_d]
            flash_exit_d, _ = flash_triggered(recent_closes_d, bull_d, PARAMS)

            if disaster_d:
                state["consec_days"] += 1
            else:
                state["consec_days"] = 0

            consec_d = state["consec_days"]
            sell_streak_d = state.get("sell_streak", 0)
            position_d = state.get("position", "holding")
            # v2.2 出場門檻: 2 + streak (+ bonus 多頭時)
            sell_threshold_d = disaster_exit_threshold(sell_streak_d, bull_d, PARAMS)
            exit_confirmed_d = (disaster_d and consec_d >= sell_threshold_d) or flash_exit_d

            if position_d == "holding":
                if exit_confirmed_d:
                    status_d = "exit_confirmed"
                    state["sell_streak"] = sell_streak_d + 1
                    state["quiet_days"] = 0
                    state["position"] = "out"
                    state["out_low"] = p631l_d
                    state["last_reentry_reason"] = None
                elif disaster_d:
                    status_d = "exit_warning"
                    state["quiet_days"] = 0
                elif n_conds_d >= 2:
                    status_d = "exit_caution"
                    state["quiet_days"] = 0
                else:
                    status_d = "normal"
                    if not flash_exit_d:
                        state["quiet_days"] = state.get("quiet_days", 0) + 1
                        if state["quiet_days"] >= RESET_QUIET_DAYS:
                            state["sell_streak"] = 0
            elif position_d == "out":
                out_low_d = state.get("out_low")
                if p631l_d and (out_low_d is None or p631l_d < out_low_d):
                    state["out_low"] = p631l_d
                    out_low_d = p631l_d

                reason_d = reentry_reason(
                    disaster_d, n_conds_d,
                    p631l_adj_d, p631l_prev_adj_d,
                    p631l_d, out_low_d,
                )
                if reason_d:
                    status_d = "entry_safe"
                    state["position"] = "holding"
                    state["out_low"] = None
                    state["last_reentry_reason"] = reason_d
                else:
                    status_d = "still_out"

            state["last_status"] = status_d
            state["last_date"] = d

        if dry_run:
            print(f"  🧪 DRY RUN: 狀態推演至 {latest_tw_date}，不寫入 {STATE_FILE.name}")
        else:
            save_state(state)
            print(f"  ✅ 狀態更新完成，最新日期已達: {latest_tw_date}")
    else:
        print(f"  ℹ️ 日期 {latest_tw_date} 已在先前處理完畢，本次僅進行指標評估不更新計數狀態")

    # 評估最新日期的指標數值（僅為回傳顯示，不重複更新計數狀態）
    # Canonical conditions — same helpers as backtest + backfill loop.
    c1, c2, c3, c4 = eval_conditions(
        p50, ma60, ma120,
        vix, vix9d, vix3m,
        p631l_adj_latest, mx631l_adj,
        smh, s30, s60,
    )

    # v2.2 多頭 regime（最新日）+ regime-aware 閃崩防守
    ma_regime = ma_regime_0050.get(latest_tw_date)
    bull = is_bull(p50, ma_regime)
    recent_dates_631l = [dt for dt in all_dates if dt <= latest_tw_date and dt in p631l_raw][-6:]
    recent_closes = [p631l_raw[dt] for dt in recent_dates_631l]
    flash_exit, flash_detail = flash_triggered(recent_closes, bull, PARAMS)

    n_conds = sum([c1, c2, c3, c4])
    status = state.get("last_status", "normal")
    consec = state.get("consec_days", 0)
    sell_threshold = disaster_exit_threshold(state.get("sell_streak", 0), bull, PARAMS)

    flash_label = (
        "閃崩防守 (多頭放寬 單日-9%/5日-22%)" if (bull and PARAMS.flash_mode == "bull_relax")
        else "閃崩防守 (多頭關閉)" if (bull and PARAMS.flash_mode == "bull_off")
        else "閃崩防守 (單日-6% 或 5日-15%)"
    )
    conditions = [
        {"name": "0050 < MA60 且 < MA120", "met": c1,
         "detail": f"0050={p50:.2f}, MA60={ma60:.2f}, MA120={ma120:.2f}" if p50 and ma60 and ma120 else "資料不足"},
        {"name": "VIX/9D/3M 全 > 26", "met": c2,
         "detail": f"VIX={vix:.1f}, 9D={vix9d:.1f}, 3M={vix3m:.1f}" if vix and vix9d and vix3m else "資料不足"},
        {"name": "00631L 回撤 > 15%", "met": c3,
         "detail": f"631L={p631l:.2f}(adj={p631l_adj_latest:.2f}), 高點={mx631l_adj:.2f}(adj), 跌幅={((p631l_adj_latest/mx631l_adj-1)*100):.1f}%" if p631l and p631l_adj_latest and mx631l_adj else "資料不足"},
        {"name": "SMH < MA30 且 < MA60", "met": c4,
         "detail": f"SMH={smh:.2f}, MA30={s30:.2f}, MA60={s60:.2f}" if smh and s30 and s60 else "資料不足"},
        {"name": flash_label, "met": flash_exit, "detail": flash_detail},
    ]

    return {
        "status": status,
        "n_conds": n_conds,
        "consec_days": consec,
        "sell_threshold": sell_threshold,
        "sell_streak": state.get("sell_streak", 0),
        "bull_regime": bull,
        "position": state.get("position", "holding"),
        "reentry_reason": state.get("last_reentry_reason"),
        "conditions": conditions,
        "prices": {
            "0050": round(p50, 2) if p50 else None,
            "00631L": round(p631l, 2) if p631l else None,
            "VIX": round(vix, 1) if vix else None,
            "SMH": round(smh, 2) if smh else None,
        },
        "dates": {
            "tw": latest_tw_date,
            "0050": latest_0050_date,
            "00631L": latest_631l_date,
            "VIX": latest_vix_date,
            "SMH": latest_smh_date,
        },
        "data_warnings": data_warnings,
    }


# ── 訊息格式 ─────────────────────────────────────
def format_message(sig: dict) -> str:
    """組合 TG 訊息"""
    today_str = date.today().isoformat()
    status = sig["status"]
    n = sig["n_conds"]
    consec = sig["consec_days"]
    p = sig["prices"]
    warnings = sig.get("data_warnings", [])
    reentry_reason = sig.get("reentry_reason")

    # 條件明細
    cond_lines = []
    for c in sig["conditions"]:
        icon = "🔴" if c["met"] else "⚪"
        cond_lines.append(f"  {icon} {c['name']}")

    conds_text = "\n".join(cond_lines)

    if status == "exit_confirmed":
        header = "‼️‼️‼️ 災難出場確認！立即賣出！"
        threshold = sig.get("sell_threshold", 2)
        
        flash_triggered = any(c.get("name", "").startswith("閃崩防守") and c.get("met") for c in sig["conditions"])
        
        if flash_triggered:
            trigger_reason = f"閃崩防守機制觸發"
        else:
            trigger_reason = f"{n}/4（連續 {consec} 天 ≥ 門檻{threshold}）"

        action = (
            f"🛡️ 出場條件：{trigger_reason}\n\n"
            f"👉 全部賣出 00631L，持有現金\n"
            f"👉 等待安全進場信號再買回"
        )
    elif status == "exit_warning":
        threshold = sig.get("sell_threshold", 2)
        remain = threshold - consec
        header = f"⚠️⚠️ 出場預警！第 {consec} 天亮燈"
        action = (
            f"🛡️ 出場條件：{n}/4（門檻 {threshold} 天，還差 {remain} 天）\n\n"
            f"👉 繼續持有，但明天務必再檢查\n"
            f"💡 連續 {threshold} 天 ≥3 條件才觸發出場"
        )
    elif status == "exit_caution":
        header = f"⚠️ 出場注意：{n}/4 條件亮燈"
        action = (
            f"👉 繼續持有，密切關注\n"
            f"💡 再多 1 個條件就進入預警"
        )
    elif status == "entry_safe":
        header = "✅ 安全進場！可買入"
        reason_text = reentry_reason or "回場條件已滿足"
        action = (
            f"🟢 {reason_text}\n\n"
            f"👉 市場已安全，回場持有 00631L"
        )
    elif status == "still_out":
        header = "💭 等待回場中…"
        action = (
            f"🔴 出場條件：{n}/4\n"
            f"👉 持有現金，等待回場信號\n"
            f"💡 回場條件：n<3 / 單日+8% / 低點反彈+20%"
        )
    else:
        header = "😎 每日信號：正常持有"
        action = f"✅ 出場 {n}/4 | 不需操作"

    price_line = f"📈 0050：{p.get('0050', 'N/A')} 元 | 00631L：{p.get('00631L', 'N/A')} 元"
    vix_line = f"📊 VIX：{p.get('VIX', 'N/A')} | SMH：{p.get('SMH', 'N/A')}"
    warning_text = ""
    if warnings:
        warning_text = "\n\n⚠️ 資料警告：\n" + "\n".join(f"  - {w}" for w in warnings)

    dates = sig["dates"]
    date_line = (
        f"📅 {today_str} "
        f"(台股至 {dates.get('tw', dates.get('0050'))}; "
        f"0050 {dates.get('0050')}; 00631L {dates.get('00631L')}; "
        f"VIX {dates.get('VIX')}; SMH {dates.get('SMH')})"
    )

    msg = f"{header}\n\n{price_line}\n{vix_line}{warning_text}\n{action}\n\n📋 條件明細：\n{conds_text}\n\n{date_line}"

    return msg


# ── 主程式 ────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="每日進出場信號 TG 通知")
    parser.add_argument("--dry-run", action="store_true", help="只顯示結果，不發 TG")
    parser.add_argument("--no-alert", action="store_true", help="不發 TG")
    args = parser.parse_args()

    print("=" * 50)
    print(f"  📡 信號檢查  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    sig = calc_signal(dry_run=args.dry_run)

    if "error" in sig:
        print(f"  ❌ 錯誤: {sig['error']}")
        return 1

    # 顯示結果
    status_icons = {
        "exit_confirmed": "🚨 出場確認",
        "exit_warning": "⚠️ 出場預警",
        "exit_caution": "⚠️ 出場注意",
        "entry_safe": "✅ 安全進場",
        "normal": "😎 正常",
    }
    print(f"\n  狀態: {status_icons.get(sig['status'], sig['status'])}")
    print(f"  條件: {sig['n_conds']}/4 | 連續: {sig['consec_days']} 天")
    if sig.get("status") == "entry_safe" and sig.get("reentry_reason"):
        print(f"  回場原因: {sig['reentry_reason']}")
    for warning in sig.get("data_warnings", []):
        print(f"  ⚠️ 資料警告: {warning}")
    for c in sig["conditions"]:
        icon = "🔴" if c["met"] else "⚪"
        print(f"    {icon} {c['name']} — {c['detail']}")

    msg = format_message(sig)
    print(f"\n{'─' * 50}")
    print(msg)
    print(f"{'─' * 50}")

    # 發送 TG
    if not args.dry_run and not args.no_alert:
        send_tg(msg)
    elif args.dry_run:
        print("\n  [DRY RUN] 未發送 TG")

    return 0


if __name__ == "__main__":
    sys.exit(main())
