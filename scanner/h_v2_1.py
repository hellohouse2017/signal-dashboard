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

        p0050 = {d: etf0050[d]["close"] for d in dates if d in etf0050}
        p631l = {d: etf631l[d]["close"] for d in dates if d in etf631l}
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
        prev_close_map = {}
        for i, d in enumerate(dates):
            if i > 0:
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

            close = etf631l[date]["close"]
            open_ = etf631l[date]["open"]
            year = date[:4]

            if pending:
                action, _ = pending
                pending = None
                if action == "BUY":
                    buy(date, open_, "T+1回場")
                elif action == "SELL":
                    sell(date, open_, f"T+1出場(streak={sell_streak})")

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
            c2 = v and v9 and v3 and v > 28 and v9 > 28 and v3 > 28
            mx = exp_max.get(date)
            c3 = (close / mx - 1) < -0.10 if mx else False
            smh = inputs.smh.get(date); s30 = smh_ma30.get(date); s60 = smh_ma60.get(date)
            c4 = smh and s30 and s60 and smh < s30 and smh < s60
            n = sum([bool(c1), bool(c2), bool(c3), bool(c4)])
            disaster = n >= 3
            consec = consec + 1 if disaster else 0

            sell_threshold = 2 + sell_streak
            if position == "holding" and consec >= sell_threshold:
                pending = ("SELL", date)
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
                if n < 3:
                    quiet_days += 1
                    if quiet_days >= self.reset_quiet_days:
                        sell_streak = 0
                else:
                    quiet_days = 0

            equity = cash + shares * close
            curve.append({"date": date, "equity": round(equity), "position": position,
                          "n_conds": n, "consec": consec, "sell_streak": sell_streak})

        return {"curve": curve, "trades": trades, "invested": invested}


if __name__ == "__main__":
    loader = MarketDataLoader(SCRIPT_DIR)
    cfg = BacktestConfig()
    inputs = loader.load_h_strategy_inputs(cfg)

    v1 = HStrategyBacktester(cfg).run(inputs, t1=True)
    v21 = HStrategyV21Backtester(cfg).run(inputs)
    bh = HStrategyBacktester(cfg).run_buy_and_hold(inputs.dates, inputs.etf631l)

    rep = BacktestReporter()
    s1 = rep.analyze(v1, "H v1")
    s21 = rep.analyze(v21, "H v2.1")
    sb = rep.analyze(bh, "B&H")

    print("=" * 90)
    print(f"{'項目':22s}  {'v1':>12s}  {'v2.1':>12s}  {'B&H':>12s}")
    print("-" * 90)
    for lbl, key in [("投入 (萬)", "invested"), ("終值 (萬)", "final"),
                     ("淨賺 (萬)", "net"), ("CAGR %", "cagr"),
                     ("MDD %", "mdd"), ("Calmar", "calmar"), ("賣出次數", "sells")]:
        print(f"{lbl:22s}  {s1.get(key,''):>12}  {s21.get(key,''):>12}  {sb.get(key,''):>12}")

    print("\n【事件期間最大回撤 %】")
    for name in s1["event_dd"]:
        print(f"  {name:20s}  v1={s1['event_dd'].get(name,''):>6}  "
              f"v2.1={s21['event_dd'].get(name,''):>6}  B&H={sb['event_dd'].get(name,''):>6}")

    print("\n【v2.1 交易紀錄】")
    for t in v21["trades"]:
        if t["action"] == "ADD":
            continue
        p = t.get("exe_price", t.get("price"))
        print(f"  {t['date']} {t['action']:5} @{p:>6.2f}  {t.get('reason','')[:40]}")
