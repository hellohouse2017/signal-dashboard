#!/usr/bin/env python3
"""H v2.1 主倉最終版

相對 v2 只動出場邏輯:
  出場門檻 consec >= (2 + sell_streak)
    sell_streak: 同段未重置的出場次數 (1st=2, 2nd=3, 3rd=4, ...)
  重置: 持倉期間連續 30 個交易日 n_conds < 3 → sell_streak = 0

回場邏輯 (與 v2 相同):
  A not disaster (n<3)
  B 單日 >= +8%
  C 從 out 期間最低反彈 >= 20%
"""
from __future__ import annotations
from pathlib import Path
from backtest_core import (
    BacktestConfig, MarketDataLoader, HStrategyBacktester, HStrategyInputs,
    BacktestReporter, sma, expanding_max,
)

SCRIPT_DIR = Path(__file__).parent

RESET_QUIET_DAYS = 30


class HStrategyV21Backtester:
    def __init__(self, config: BacktestConfig | None = None,
                 reset_quiet_days: int = RESET_QUIET_DAYS):
        self.config = config or BacktestConfig()
        self.reset_quiet_days = reset_quiet_days

    def run(self, inputs: HStrategyInputs) -> dict:
        cfg = self.config
        dates = inputs.dates
        etf631l = inputs.etf631l
        etf0050 = inputs.etf0050

        p0050 = {d: etf0050[d]["close"] for d in dates if d in etf0050 and etf0050[d]["close"]}
        p631l = {d: etf631l[d]["close"] for d in dates if d in etf631l and etf631l[d]["close"]}
        ma60_0050 = sma(p0050, dates, 60)
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
        sell_streak = 0
        quiet_days = 0
        recent_631l = []
        prev_close_map = {}
        for i, d in enumerate(dates):
            if i > 0 and dates[i - 1] in etf631l and etf631l[dates[i - 1]]["close"]:
                prev_close_map[d] = etf631l[dates[i - 1]]["close"]

        curve = []
        trades = []

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
            nonlocal cash, shares, position, out_low, out_low_date
            ep = price * (1 - cfg.trade_cost)
            cash = shares * ep
            shares = 0.0
            position = "out"
            out_low = None
            out_low_date = None
            trades.append({"date": exe_date, "action": "SELL", "price": round(price, 2),
                           "exe_price": round(ep, 2), "value": round(cash),
                           "reason": reason})

        for i, date in enumerate(dates):
            if date < cfg.start_date or date > cfg.end_date:
                continue
            if date not in etf631l:
                continue

            row = etf631l[date]
            close = row["close"]
            open_ = row["open"]
            if not close or not open_:
                continue
            year = date[:4]

            if pending:
                tup = pending
                pending = None
                if tup[0] == "BUY":
                    buy(date, open_, "T+1回場")
                elif tup[0] == "SELL":
                    reason = tup[2] if len(tup) > 2 else f"T+1出場(streak={sell_streak})"
                    sell(date, open_, reason)

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

            p50 = p0050.get(date); ma60 = ma60_0050.get(date); ma120 = ma120_0050.get(date)
            c1 = p50 is not None and ma60 and ma120 and p50 < ma60 and p50 < ma120
            v = inputs.vix.get(date); v9 = inputs.vix9d.get(date); v3 = inputs.vix3m.get(date)
            c2 = v and v9 and v3 and v > 26 and v9 > 26 and v3 > 26
            mx = exp_max.get(date)
            c3 = (close / mx - 1) < -0.15 if mx else False
            smh = inputs.smh.get(date); s30 = smh_ma30.get(date); s60 = smh_ma60.get(date)
            c4 = smh and s30 and s60 and smh < s30 and smh < s60
            n = sum([bool(c1), bool(c2), bool(c3), bool(c4)])
            disaster = n >= 3
            consec = consec + 1 if disaster else 0

            # === Flash crash defense ===
            flash_exit = False
            recent_631l.append(close)
            if len(recent_631l) > 6:
                recent_631l.pop(0)

            if position == "holding":
                pc = prev_close_map.get(date)
                if pc and pc > 0 and (close / pc - 1) <= -0.06:
                    flash_exit = True
                if not flash_exit and len(recent_631l) == 6:
                    if (close / recent_631l[0] - 1) <= -0.15:
                        flash_exit = True

            sell_threshold = 2 + sell_streak
            if position == "holding" and (consec >= sell_threshold or flash_exit):
                reason = f"T+1出場(閃崩防守)" if flash_exit else f"T+1出場(streak={sell_streak})"
                pending = ("SELL", date, reason)
                sell_streak += 1
                quiet_days = 0
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

            if position == "holding":
                if n < 3 and not flash_exit:
                    quiet_days += 1
                    if quiet_days >= self.reset_quiet_days:
                        sell_streak = 0
                else:
                    quiet_days = 0

            equity = cash + shares * close
            curve.append({"date": date, "equity": round(equity), "position": position,
                          "n_conds": n, "consec": consec, "sell_streak": sell_streak})

        return {"curve": curve, "trades": trades, "invested": invested}


def run_comparison(tickers: list[tuple[str, str]], cfg: BacktestConfig | None = None):
    """
    跑多標的比較回測。

    tickers: [(ticker_db, label), ...]
    C1/C2/C4 用 0050/VIX/SMH（固定）; C3 用各自標的的 adj_close expanding_max
    """
    cfg = cfg or BacktestConfig()
    loader = MarketDataLoader(SCRIPT_DIR)
    rep = BacktestReporter()

    results = {}
    for ticker, label in tickers:
        print(f"\n⏳ 載入 {label} ({ticker}) ...")
        try:
            inputs = loader.load_h_strategy_inputs(cfg, main_ticker=ticker)
        except Exception as e:
            print(f"  ❌ 載入失敗: {e}")
            continue

        data_start = min(inputs.etf631l.keys()) if inputs.etf631l else "N/A"
        data_end   = max(inputs.etf631l.keys()) if inputs.etf631l else "N/A"
        print(f"  資料範圍: {data_start} ~ {data_end}  ({len(inputs.etf631l)} 筆)")

        v21 = HStrategyV21Backtester(cfg).run(inputs)
        bh  = HStrategyBacktester(cfg).run_buy_and_hold(inputs.dates, inputs.etf631l)
        results[label] = {
            "v21": rep.analyze(v21, f"v2.1 {label}"),
            "bh":  rep.analyze(bh,  f"B&H {label}"),
            "trades": v21["trades"],
        }

    return results


if __name__ == "__main__":
    cfg = BacktestConfig()

    # ── 要比較的標的（00981A 2025-05 上市，資料不足，暫不納入）──
    TICKERS = [
        ("00631L.TW", "00631L"),
        ("00675L.TW", "00675L"),
    ]

    results = run_comparison(TICKERS, cfg)

    if not results:
        print("❌ 無結果，請確認資料已下載完畢")
        raise SystemExit(1)

    # ── 印出比較表 ────────────────────────────────────────
    labels = list(results.keys())
    col_w = 14

    print("\n" + "=" * (24 + col_w * len(labels) * 2))
    header = f"{'項目':22s}"
    for lbl in labels:
        header += f"  {'v2.1 ' + lbl:>{col_w}}  {'B&H ' + lbl:>{col_w}}"
    print(header)
    print("-" * (24 + col_w * len(labels) * 2))

    for lbl_r, key in [("投入 (萬)", "invested"), ("終值 (萬)", "final"),
                        ("淨賺 (萬)", "net"), ("CAGR %", "cagr"),
                        ("MDD %", "mdd"), ("Calmar", "calmar"), ("賣出次數", "sells")]:
        line = f"{lbl_r:22s}"
        for tl in labels:
            r = results[tl]
            line += f"  {str(r['v21'].get(key, '')):>{col_w}}  {str(r['bh'].get(key, '')):>{col_w}}"
        print(line)

    print("\n【事件期間最大回撤 %】")
    first_r = next(iter(results.values()))
    for name in first_r["v21"]["event_dd"]:
        line = f"  {name:22s}"
        for tl in labels:
            r = results[tl]
            line += (f"  v2.1={r['v21']['event_dd'].get(name, ''):>6}"
                     f"  B&H={r['bh']['event_dd'].get(name, ''):>6}")
        print(line)

    for tl in labels:
        print(f"\n【{tl} v2.1 交易紀錄】")
        for t in results[tl]["trades"]:
            if t["action"] == "ADD":
                continue
            p = t.get("exe_price", t.get("price"))
            print(f"  {t['date']} {t['action']:5} @{p:>7.2f}  {t.get('reason', '')[:40]}")
