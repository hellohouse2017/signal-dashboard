#!/usr/bin/env python3
"""H 策略回測核心模組

提供:
  - BacktestConfig   — 回測參數
  - HStrategyInputs  — 策略輸入數據容器
  - MarketDataLoader — 從 DB/JSON 載入數據
  - HStrategyBacktester — v1 回測引擎
  - BacktestReporter — 績效分析報告
  - sma / expanding_max — 工具函式
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


# ── 工具函式 ──────────────────────────────────────────────────────────────────

def sma(data: dict[str, float], dates: list[str], window: int) -> dict[str, float]:
    """計算簡單移動平均"""
    result: dict[str, float] = {}
    vals: list[float] = []
    for d in dates:
        if d in data:
            vals.append(data[d])
        if len(vals) >= window:
            result[d] = sum(vals[-window:]) / window
    return result


def expanding_max(data: dict[str, float], dates: list[str]) -> dict[str, float]:
    """計算 expanding window 歷史最高點"""
    result: dict[str, float] = {}
    cur_max = float("-inf")
    for d in dates:
        if d in data:
            cur_max = max(cur_max, data[d])
            result[d] = cur_max
    return result


# ── 設定 ─────────────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    start_date: str = "2015-01-01"
    end_date: str = "2099-12-31"
    initial_capital: float = 1_000_000.0   # 100 萬
    annual_add: float = 500_000.0           # 每年加碼 50 萬
    trade_cost: float = 0.001425            # 0.1425% 單邊


# ── 輸入數據容器 ──────────────────────────────────────────────────────────────

@dataclass
class HStrategyInputs:
    dates: list[str]
    etf631l: dict[str, dict]              # {date: {open, close, ...}} — 主標的
    etf0050: dict[str, dict]              # {date: {close, ...}}
    vix: dict[str, float]
    vix9d: dict[str, float]
    vix3m: dict[str, float]
    smh: dict[str, float]
    ticker: str = "00631L.TW"             # 持有標的 ticker（用於顯示）


# ── 數據載入器 ────────────────────────────────────────────────────────────────

class MarketDataLoader:
    def __init__(self, script_dir: Path | None = None):
        self.script_dir = script_dir or Path(__file__).parent
        self.db_path = self.script_dir / "回測_0050還原數據.db"

    # ── 內部工具 ──────────────────────────────────────────────────────────────

    def _load_raw_ohlcv(self, ticker: str) -> dict[str, dict]:
        """從 daily_prices_raw 讀取完整 OHLCV"""
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume "
            "FROM daily_prices_raw WHERE ticker=? ORDER BY date",
            (ticker,),
        ).fetchall()
        conn.close()
        return {r[0]: {"open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
                for r in rows}

    def _load_adjusted_ohlcv(self, ticker: str) -> dict[str, dict]:
        """用 adjuster 取得還原後 OHLCV（處理 split + cash_dividend）"""
        from adjuster import get_adjusted_prices
        return get_adjusted_prices(ticker, str(self.db_path))

    def _load_json(self, name: str) -> dict[str, float]:
        p = self.script_dir / name
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    # ── 公開介面 ──────────────────────────────────────────────────────────────

    def load_h_strategy_inputs(
        self,
        config: BacktestConfig,
        main_ticker: str = "00631L.TW",
    ) -> HStrategyInputs:
        """
        載入 H 策略所需的全部數據。

        main_ticker: 持有標的 (e.g. "00631L.TW" / "00675L.TW")
        C3 條件的回撤計算永遠用 main_ticker 的 adj_close。
        C1 判斷永遠用 0050.TW。
        """
        # 主標的（還原後，用於 C3 計算 + 持倉損益）
        main_adj = self._load_adjusted_ohlcv(main_ticker)

        # 0050（還原後，用於 C1 MA 計算）
        etf0050_adj = self._load_adjusted_ohlcv("0050.TW")

        # VIX / SMH
        vix   = self._load_json("VIX歷史.json")
        vix9d = self._load_json("VIX9D歷史.json")
        vix3m = self._load_json("VIX3M歷史.json")
        smh   = self._load_json("SMH歷史.json")

        # 合併所有日期（取聯集，讓 sma 可以正確計算前置 MA）
        all_dates = sorted(set(
            list(main_adj.keys()) +
            list(etf0050_adj.keys()) +
            list(vix.keys()) +
            list(smh.keys())
        ))

        return HStrategyInputs(
            dates=all_dates,
            etf631l=main_adj,       # 欄位名沿用 etf631l 以保持 h_v2_1 相容
            etf0050=etf0050_adj,
            vix=vix,
            vix9d=vix9d,
            vix3m=vix3m,
            smh=smh,
            ticker=main_ticker,
        )


# ── v1 回測引擎 ───────────────────────────────────────────────────────────────

class HStrategyBacktester:
    """H 策略 v1 回測引擎（固定 consec >= 2 出場）"""

    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()

    def run(self, inputs: HStrategyInputs, t1: bool = True) -> dict:
        cfg = self.config
        dates = inputs.dates
        etf631l = inputs.etf631l
        etf0050 = inputs.etf0050

        p0050 = {d: etf0050[d]["close"] for d in dates if d in etf0050 and etf0050[d]["close"]}
        p631l = {d: etf631l[d]["close"] for d in dates if d in etf631l and etf631l[d]["close"]}
        ma60_0050  = sma(p0050, dates, 60)
        ma120_0050 = sma(p0050, dates, 120)
        exp_max = expanding_max(p631l, dates)
        smh_ma30 = sma(inputs.smh, dates, 30)
        smh_ma60 = sma(inputs.smh, dates, 60)

        cash = cfg.initial_capital
        shares = 0.0
        position = "out"
        invested = cfg.initial_capital
        last_year = None
        consec = 0
        pending = None
        out_low = None
        out_low_date = None
        prev_close_map: dict[str, float] = {}
        for i, d in enumerate(dates):
            if i > 0 and dates[i - 1] in etf631l:
                prev_close_map[d] = etf631l[dates[i - 1]]["close"]

        curve: list[dict] = []
        trades: list[dict] = []

        def buy(exe_date, price, reason):
            nonlocal cash, shares, position, out_low, out_low_date
            ep = price * (1 + cfg.trade_cost)
            shares = cash / ep
            cash = 0.0
            position = "holding"
            out_low = None
            out_low_date = None
            trades.append({"date": exe_date, "action": "BUY", "price": round(price, 2),
                           "exe_price": round(ep, 2), "shares": round(shares, 4),
                           "reason": reason})

        def sell(exe_date, price, reason):
            nonlocal cash, shares, position
            ep = price * (1 - cfg.trade_cost)
            cash = shares * ep
            shares = 0.0
            position = "out"
            trades.append({"date": exe_date, "action": "SELL", "price": round(price, 2),
                           "exe_price": round(ep, 2), "value": round(cash),
                           "reason": reason})

        for i, date in enumerate(dates):
            if date < cfg.start_date or date > cfg.end_date:
                continue
            if date not in etf631l:
                continue

            close = etf631l[date]["close"]
            open_ = etf631l[date]["open"]
            year = date[:4]

            if pending:
                action, _ = pending
                pending = None
                if action == "BUY":
                    buy(date, open_, "T+1回場")
                elif action == "SELL":
                    sell(date, open_, "T+1出場")

            if last_year and year != last_year and int(year) >= 2016:
                topup = cfg.annual_add
                cash += topup
                invested += topup
                if position == "holding" and close > 0:
                    ns = topup / close
                    shares += ns
                    cash -= topup
                    trades.append({"date": date, "action": "ADD", "price": round(close, 2),
                                   "shares": round(ns, 4), "reason": "年加碼"})
            last_year = year

            if position == "out" and not trades:
                pending = ("BUY", date)

            p50  = p0050.get(date)
            ma60 = ma60_0050.get(date)
            ma120 = ma120_0050.get(date)
            c1 = bool(p50 and ma60 and ma120 and p50 < ma60 and p50 < ma120)
            v = inputs.vix.get(date); v9 = inputs.vix9d.get(date); v3 = inputs.vix3m.get(date)
            c2 = bool(v and v9 and v3 and v > 28 and v9 > 28 and v3 > 28)
            mx = exp_max.get(date)
            c3 = (close / mx - 1) < -0.10 if mx else False
            smh = inputs.smh.get(date); s30 = smh_ma30.get(date); s60 = smh_ma60.get(date)
            c4 = bool(smh and s30 and s60 and smh < s30 and smh < s60)
            n = sum([bool(c1), bool(c2), bool(c3), bool(c4)])
            disaster = n >= 3
            consec = consec + 1 if disaster else 0

            if position == "holding" and consec >= 2:
                pending = ("SELL", date)
                consec = 0
            elif position == "out" and not pending:
                if out_low is None or close < out_low:
                    out_low = close
                    out_low_date = date
                reason = None
                if not disaster:
                    reason = f"原版回場({n}/4)"
                pc = prev_close_map.get(date)
                if not reason and pc and pc > 0 and close / pc - 1 >= 0.08:
                    reason = f"單日+{(close/pc-1)*100:.1f}%"
                if not reason and out_low and close / out_low - 1 >= 0.20:
                    reason = f"從低{out_low_date}反彈+{(close/out_low-1)*100:.1f}%"
                if reason:
                    pending = ("BUY", date)

            equity = cash + shares * close
            curve.append({"date": date, "equity": round(equity), "position": position,
                          "n_conds": n, "consec": consec})

        return {"curve": curve, "trades": trades, "invested": invested}

    def run_buy_and_hold(self, dates: list[str], etf631l: dict[str, dict]) -> dict:
        """純 B&H 對照組"""
        cfg = self.config
        cash = cfg.initial_capital
        shares = 0.0
        invested = cfg.initial_capital
        last_year = None
        bought = False
        curve = []
        trades = []

        for date in dates:
            if date < cfg.start_date or date > cfg.end_date:
                continue
            if date not in etf631l:
                continue
            close = etf631l[date]["close"]
            open_ = etf631l[date]["open"]
            year = date[:4]

            if not bought:
                ep = open_ * (1 + cfg.trade_cost)
                shares = cash / ep
                cash = 0.0
                bought = True
                trades.append({"date": date, "action": "BUY", "price": round(open_, 2),
                               "shares": round(shares, 4), "reason": "B&H初始買入"})

            if last_year and year != last_year and int(year) >= 2016:
                topup = cfg.annual_add
                cash += topup
                invested += topup
                if close > 0:
                    ns = topup / close
                    shares += ns
                    cash -= topup
                    trades.append({"date": date, "action": "ADD", "price": round(close, 2),
                                   "shares": round(ns, 4), "reason": "年加碼"})
            last_year = year

            equity = cash + shares * close
            curve.append({"date": date, "equity": round(equity), "position": "holding"})

        return {"curve": curve, "trades": trades, "invested": invested}


# ── 績效分析 ──────────────────────────────────────────────────────────────────

# 重大市場事件（用於壓力測試回撤分析）
EVENTS: dict[str, tuple[str, str]] = {
    "2015 中國熔斷": ("2015-06-01", "2016-02-29"),
    "2018 Q4 崩盤": ("2018-10-01", "2019-01-31"),
    "2020 COVID":   ("2020-02-15", "2020-04-30"),
    "2022 升息":    ("2022-01-01", "2022-10-31"),
    "2024 八月閃崩": ("2024-07-15", "2024-09-30"),
    "2025 關稅崩盤": ("2025-04-01", "2025-05-31"),
}


class BacktestReporter:

    @staticmethod
    def _xirr(cashflows: list[tuple[float, datetime]], guess: float = 0.1,
              tol: float = 1e-6, max_iter: int = 200) -> float | None:
        """用 Newton-Raphson 法計算 XIRR（擴展內部報酬率）。

        cashflows: [(amount, datetime), ...]  負值=投入, 正值=回收
        回傳年化報酬率（如 0.25 代表 25%），計算失敗回傳 None。
        """
        if not cashflows:
            return None
        d0 = cashflows[0][1]

        def _npv(rate: float) -> float:
            return sum(cf / (1 + rate) ** ((dt - d0).days / 365.25)
                       for cf, dt in cashflows)

        def _dnpv(rate: float) -> float:
            return sum(-cf * ((dt - d0).days / 365.25)
                       / (1 + rate) ** ((dt - d0).days / 365.25 + 1)
                       for cf, dt in cashflows)

        rate = guess
        for _ in range(max_iter):
            nv = _npv(rate)
            dnv = _dnpv(rate)
            if abs(dnv) < 1e-14:
                break
            new_rate = rate - nv / dnv
            if abs(new_rate - rate) < tol:
                return new_rate
            rate = new_rate
        # 收斂失敗，嘗試不同初始值
        for alt_guess in [0.0, 0.3, 0.5, 1.0, -0.3]:
            rate = alt_guess
            for _ in range(max_iter):
                nv = _npv(rate)
                dnv = _dnpv(rate)
                if abs(dnv) < 1e-14:
                    break
                new_rate = rate - nv / dnv
                if abs(new_rate - rate) < tol:
                    return new_rate
                rate = new_rate
        return None

    def analyze(self, result: dict, label: str = "") -> dict[str, Any]:
        curve = result["curve"]
        trades = result["trades"]
        invested = result.get("invested", 0)

        if not curve:
            return {}

        equities = [r["equity"] for r in curve]
        final = equities[-1]
        net = final - invested

        # CAGR (simple — 保留作為參考)
        first_date = curve[0]["date"]
        last_date  = curve[-1]["date"]
        years = (datetime.strptime(last_date, "%Y-%m-%d") -
                 datetime.strptime(first_date, "%Y-%m-%d")).days / 365.25
        cagr_simple = (final / invested) ** (1 / years) - 1 if years > 0 and invested > 0 else 0

        # XIRR (精確 — 考慮每筆現金流的投入時間)
        adds = [t for t in trades if t["action"] == "ADD"]
        cashflows: list[tuple[float, datetime]] = []
        # 初始投入
        from backtest_core import BacktestConfig
        cfg_initial = result.get("_config_initial", 1_000_000.0)
        cashflows.append((-cfg_initial, datetime.strptime(first_date, "%Y-%m-%d")))
        # 年度加碼
        for a in adds:
            add_amount = a["price"] * a["shares"]  # 近似加碼金額
            cashflows.append((-add_amount, datetime.strptime(a["date"], "%Y-%m-%d")))
        # 最終淨值
        cashflows.append((final, datetime.strptime(last_date, "%Y-%m-%d")))

        xirr = self._xirr(cashflows)
        # 使用 XIRR 作為主要 CAGR 指標；如計算失敗則 fallback 到 simple CAGR
        cagr = xirr if xirr is not None else cagr_simple

        # MDD
        peak = equities[0]
        mdd = 0.0
        mdd_peak_date = first_date
        mdd_bottom_date = first_date
        cur_peak_date = first_date
        for r in curve:
            e = r["equity"]
            if e > peak:
                peak = e
                cur_peak_date = r["date"]
            dd = (e - peak) / peak
            if dd < mdd:
                mdd = dd
                mdd_peak_date = cur_peak_date
                mdd_bottom_date = r["date"]

        calmar = cagr / abs(mdd) if mdd != 0 else 0
        sells = sum(1 for t in trades if t["action"] == "SELL")

        # 各事件期間最大回撤
        date_to_eq = {r["date"]: r["equity"] for r in curve}
        all_dates = [r["date"] for r in curve]
        event_dd: dict[str, str] = {}
        for name, (es, ee) in EVENTS.items():
            seg = [date_to_eq[d] for d in all_dates if es <= d <= ee and d in date_to_eq]
            if seg:
                pk = seg[0] or 1.0
                worst = 0.0
                for v in seg:
                    if v > pk:
                        pk = v
                    dd = (v - pk) / pk if pk else 0.0
                    if dd < worst:
                        worst = dd
                event_dd[name] = f"{worst*100:.1f}%"
            else:
                event_dd[name] = "N/A"

        return {
            "label": label,
            "invested": round(invested / 1e4),
            "final": round(final / 1e4),
            "net": round(net / 1e4),
            "cagr": f"{cagr*100:.1f}%",
            "cagr_simple": f"{cagr_simple*100:.1f}%",
            "cagr_xirr": f"{xirr*100:.1f}%" if xirr is not None else "N/A",
            "mdd": f"{mdd*100:.1f}%",
            "mdd_period": f"{mdd_peak_date} → {mdd_bottom_date}",
            "calmar": f"{calmar:.2f}",
            "sells": sells,
            "event_dd": event_dd,
        }
