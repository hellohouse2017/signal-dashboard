#!/usr/bin/env python3
"""H strategy v2.2 backtest engine — canonical.

All decision logic comes from `h_strategy` (the single source of truth), so
this engine cannot drift from the live notifier. Execution model is unchanged
from v2.1: signal on close, execute T+1 at next open, fee + sell tax applied.

Run directly to compare v2.2 vs v2.1 vs Buy & Hold on 00631L.
"""
from __future__ import annotations

from pathlib import Path

from backtest_core import (
    BacktestConfig, MarketDataLoader, HStrategyBacktester,
    HStrategyInputs, BacktestReporter, sma, expanding_max,
)
from h_strategy import (
    StrategyParams, V21, V22, SELL_TAX, FLASH_WINDOW,
    asof_date_map, eval_conditions, is_bull, flash_triggered,
    disaster_exit_threshold, reentry_reason, RESET_QUIET_DAYS,
)

SCRIPT_DIR = Path(__file__).parent


class HStrategyBacktesterV22:
    """Parameterized H backtester. params=V22 (default) is live; params=V21 is legacy."""

    def __init__(self, config: BacktestConfig | None = None,
                 params: StrategyParams | None = None):
        self.config = config or BacktestConfig()
        self.params = params or V22

    def run(self, inputs: HStrategyInputs) -> dict:
        cfg = self.config
        p = self.params
        dates = inputs.dates
        etf631l = inputs.etf631l
        etf0050 = inputs.etf0050

        p0050 = {d: etf0050[d]["close"] for d in dates if d in etf0050 and etf0050[d]["close"]}
        p631l = {d: etf631l[d]["close"] for d in dates if d in etf631l and etf631l[d]["close"]}
        ma60 = sma(p0050, dates, 60)
        ma120 = sma(p0050, dates, 120)
        ma_regime = sma(p0050, dates, p.regime_win)
        exp_max = expanding_max(p631l, dates)
        smh_ma30 = sma(inputs.smh, dates, 30)
        smh_ma60 = sma(inputs.smh, dates, 60)

        # Live parity: US series are read as-of the latest US close on or before
        # the TW date (the notifier forward-fills the same way). Same-date lookup
        # would force C2/C4 false on US holidays and diverge from live.
        vix_asof = asof_date_map(inputs.vix, dates)
        smh_asof = asof_date_map(inputs.smh, dates)

        # Live parity: previous close = previous 00631L trading day, not the
        # previous union date (which can be a US-only day after a TW holiday).
        prev_close_map: dict[str, float] = {}
        prev = None
        for d in dates:
            if d in etf631l and etf631l[d]["close"]:
                if prev is not None:
                    prev_close_map[d] = prev
                prev = etf631l[d]["close"]

        cash = cfg.initial_capital
        shares = 0.0
        position = "out"
        invested = cfg.initial_capital
        last_year = None
        consec = 0
        pending: tuple[str, str] | None = None
        out_low = None
        out_low_date = None
        sell_streak = 0
        quiet_days = 0
        recent_631l: list[float] = []

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
                           "exe_price": round(ep, 4), "shares": round(shares, 4),
                           "reason": reason})

        def sell(exe_date, price, reason):
            nonlocal cash, shares, position, out_low, out_low_date
            ep = price * (1 - cfg.trade_cost - SELL_TAX)
            cash = shares * ep
            shares = 0.0
            position = "out"
            out_low = None
            out_low_date = None
            trades.append({"date": exe_date, "action": "SELL", "price": round(price, 2),
                           "exe_price": round(ep, 4), "value": round(cash),
                           "reason": reason})

        for date in dates:
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
                action, reason = pending
                pending = None
                if action == "BUY":
                    buy(date, open_, reason)
                elif action == "SELL":
                    sell(date, open_, reason)

            if last_year and year != last_year and int(year) >= 2016 and cfg.annual_add > 0:
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
                pending = ("BUY", "初始")

            vd = vix_asof.get(date)
            sd = smh_asof.get(date)
            c1, c2, c3, c4 = eval_conditions(
                p0050.get(date), ma60.get(date), ma120.get(date),
                inputs.vix.get(vd), inputs.vix9d.get(vd), inputs.vix3m.get(vd),
                close, exp_max.get(date),
                inputs.smh.get(sd), smh_ma30.get(sd), smh_ma60.get(sd),
            )
            n = sum([c1, c2, c3, c4])
            disaster = n >= 3
            consec = consec + 1 if disaster else 0

            bull = is_bull(p0050.get(date), ma_regime.get(date))

            recent_631l.append(close)
            if len(recent_631l) > FLASH_WINDOW:
                recent_631l.pop(0)

            flash_exit = False
            if position == "holding":
                flash_exit, _ = flash_triggered(recent_631l, bull, p)

            threshold = disaster_exit_threshold(sell_streak, bull, p)
            if position == "holding" and (consec >= threshold or flash_exit):
                reason = "閃崩防守" if flash_exit else f"disaster(streak={sell_streak})"
                pending = ("SELL", reason)
                sell_streak += 1
                quiet_days = 0
            elif position == "out" and not pending:
                if out_low is None or close < out_low:
                    out_low = close
                    out_low_date = date
                pc = prev_close_map.get(date)
                reason = reentry_reason(disaster, n, close, pc, close, out_low)
                if reason:
                    pending = ("BUY", reason)

            if position == "holding":
                if n < 3 and not flash_exit:
                    quiet_days += 1
                    if quiet_days >= RESET_QUIET_DAYS:
                        sell_streak = 0
                else:
                    quiet_days = 0

            equity = cash + shares * close
            curve.append({"date": date, "equity": round(equity), "position": position,
                          "n_conds": n, "consec": consec, "sell_streak": sell_streak,
                          "bull": bull})

        return {"curve": curve, "trades": trades, "invested": invested,
                "_config_initial": cfg.initial_capital}


def _fmt(m: dict) -> str:
    return (f"終值={m['final']:>6}萬  CAGR={m['cagr']:>7}  MDD={m['mdd']:>7}  "
            f"Calmar={m['calmar']:>5}  賣出={m['sells']:>2}")


if __name__ == "__main__":
    cfg = BacktestConfig(initial_capital=1_000_000.0, annual_add=0.0)
    loader = MarketDataLoader(SCRIPT_DIR)
    inputs = loader.load_h_strategy_inputs(cfg, main_ticker="00631L.TW")
    rep = BacktestReporter()

    v22 = HStrategyBacktesterV22(cfg, V22).run(inputs)
    v21 = HStrategyBacktesterV22(cfg, V21).run(inputs)
    bh = HStrategyBacktester(cfg).run_buy_and_hold(inputs.dates, inputs.etf631l)
    for r in (v22, v21, bh):
        r["_config_initial"] = cfg.initial_capital

    print("=" * 90)
    print("H 策略 v2.2 (canonical) — 00631L 全期 2015-2026  [100萬, annual_add=0, 含證交稅]")
    print("-" * 90)
    print(f"  v2.2 (live)   {_fmt(rep.analyze(v22, 'v2.2'))}")
    print(f"  v2.1 (legacy) {_fmt(rep.analyze(v21, 'v2.1'))}")
    print(f"  Buy & Hold    {_fmt(rep.analyze(bh, 'B&H'))}")
