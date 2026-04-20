#!/usr/bin/env python3
"""加碼池交易策略回測
當 00631L 單日跌 ≤-8% 且 H 命中 4/4 時，注入額外資金。
測試不同加碼金額對報酬的影響。
"""
from __future__ import annotations
from pathlib import Path
from typing import Any
from backtest_core import (
    BacktestConfig, MarketDataLoader, HStrategyBacktester,
    HStrategyInputs, BacktestReporter,
    sma, expanding_max, forward_fill,
)


def run_with_bonus_pool(
    inputs: HStrategyInputs,
    config: BacktestConfig,
    bonus_amount: float = 500_000,
    t1: bool = True,
) -> dict[str, Any]:
    """H 策略 + 加碼池
    bonus_amount: 每次加碼池事件觸發時注入的額外資金 (0=不加碼=原版)
    """
    dates = inputs.dates
    etf631l, etf0050 = inputs.etf631l, inputs.etf0050

    p0050 = {d: etf0050[d]["close"] for d in dates if d in etf0050}
    p631l = {d: etf631l[d]["close"] for d in dates if d in etf631l}

    ma60_0050 = sma(p0050, dates, 60)
    ma120_0050 = sma(p0050, dates, 120)
    exp_max = expanding_max(p631l, dates)
    smh_ma30 = sma(inputs.smh, dates, 30)
    smh_ma60 = sma(inputs.smh, dates, 60)

    cash = config.initial_capital
    shares = 0.0
    position = "out"
    invested = config.initial_capital
    last_year = None
    consec = 0
    pending = None
    prev_date = None

    curve, trades = [], []
    bonus_events = []
    total_bonus_injected = 0.0

    for date in dates:
        if date < config.start_date or date > config.end_date:
            continue
        if date not in etf631l:
            continue

        close_price = etf631l[date]["close"]
        open_price = etf631l[date]["open"]
        year = date[:4]

        # T+1 執行
        if pending:
            action, trigger = pending
            pending = None
            if action == "BUY":
                eff = open_price * (1 + config.trade_cost)
                shares = cash / eff
                cash = 0.0
                position = "holding"
                trades.append({"date": date, "action": "BUY", "price": round(open_price, 2),
                               "reason": f"T+1回場 (signal={trigger})"})
            elif action == "SELL":
                eff = open_price * (1 - config.trade_cost)
                cash = shares * eff
                shares = 0.0
                position = "out"
                trades.append({"date": date, "action": "SELL", "price": round(open_price, 2),
                               "value": round(cash), "reason": f"T+1出場 (signal={trigger})"})

        # 年加碼
        if last_year and year != last_year and int(year) >= 2016:
            topup = config.annual_add
            cash += topup
            invested += topup
            if position == "holding" and close_price > 0:
                shares += topup / close_price
                cash -= topup
                trades.append({"date": date, "action": "ADD", "price": round(close_price, 2),
                               "reason": f"年加碼 {topup//10000:.0f}萬"})
        last_year = year

        # 初始建倉
        if position == "out" and not trades:
            if t1:
                pending = ("BUY", date)
            else:
                eff = close_price * (1 + config.trade_cost)
                shares = cash / eff
                cash = 0.0
                position = "holding"
                trades.append({"date": date, "action": "BUY", "price": round(close_price, 2), "reason": "初始建倉"})

        # ── H 條件 ──
        p50 = p0050.get(date)
        ma60 = ma60_0050.get(date)
        ma120 = ma120_0050.get(date)
        cond1 = bool(p50 is not None and ma60 is not None and ma120 is not None
                     and p50 < ma60 and p50 < ma120)

        vix = inputs.vix.get(date)
        vix9d = inputs.vix9d.get(date)
        vix3m = inputs.vix3m.get(date)
        cond2 = bool(vix is not None and vix9d is not None and vix3m is not None
                     and vix > 28 and vix9d > 28 and vix3m > 28)

        max_price = exp_max.get(date)
        drawdown = (close_price / max_price - 1) if max_price else 0.0
        cond3 = drawdown < -0.10

        smh = inputs.smh.get(date)
        smh30 = smh_ma30.get(date)
        smh60 = smh_ma60.get(date)
        cond4 = bool(smh is not None and smh30 is not None and smh60 is not None
                     and smh < smh30 and smh < smh60)

        num_conds = sum([cond1, cond2, cond3, cond4])
        disaster = num_conds >= 3
        consec = consec + 1 if disaster else 0

        # 加碼池偵測: 單日 ≤-8% 且 4/4（不限 position）
        if prev_date and prev_date in p631l and bonus_amount > 0:
            daily_ret = close_price / p631l[prev_date] - 1
            if daily_ret <= -0.08 and num_conds == 4:
                cash += bonus_amount
                invested += bonus_amount
                total_bonus_injected += bonus_amount
                if position == "holding" and close_price > 0:
                    # holding 中：直接加買
                    shares += bonus_amount / close_price
                    cash -= bonus_amount
                    tag = "加買"
                else:
                    tag = "注入"
                bonus_events.append({
                    "date": date,
                    "daily_return": round(daily_ret * 100, 2),
                    "close": round(close_price, 2),
                    "injected": bonus_amount,
                    "position": position,
                    "tag": tag,
                })
                trades.append({"date": date, "action": "BONUS", "price": round(close_price, 2),
                               "reason": f"加碼池{tag} {bonus_amount//10000:.0f}萬 (日跌{daily_ret*100:.1f}%)"})

        # 出場/回場
        if position == "holding" and consec >= 2:
            reason = f"災難出場({num_conds}/4連{consec}天)"
            if t1:
                pending = ("SELL", date)
            else:
                eff = close_price * (1 - config.trade_cost)
                cash = shares * eff
                shares = 0.0
                position = "out"
                trades.append({"date": date, "action": "SELL", "price": round(close_price, 2),
                               "value": round(cash), "reason": reason})
        elif position == "out" and not disaster and not pending:
            if t1:
                pending = ("BUY", date)
            else:
                eff = close_price * (1 + config.trade_cost)
                shares = cash / eff
                cash = 0.0
                position = "holding"
                trades.append({"date": date, "action": "BUY", "price": round(close_price, 2),
                               "reason": f"回場({num_conds}/4條件)"})

        equity = cash + shares * close_price
        curve.append({"date": date, "equity": round(equity), "position": position,
                      "n_conds": num_conds, "consec": consec})
        prev_date = date

    return {
        "curve": curve, "trades": trades, "invested": invested,
        "bonus_events": bonus_events,
        "total_bonus_injected": total_bonus_injected,
    }


if __name__ == "__main__":
    loader = MarketDataLoader(Path(__file__).parent)
    cfg = BacktestConfig(end_date="2026-04-20")
    inputs = loader.load_h_strategy_inputs(cfg)
    reporter = BacktestReporter()

    # 原版基準
    engine = HStrategyBacktester(cfg)
    bh = engine.run_buy_and_hold(inputs.dates, inputs.etf631l)
    h_orig = engine.run(inputs, t1=True)
    bh_s = reporter.analyze(bh, "B&H")
    h_s = reporter.analyze(h_orig, "H原版T+1")

    base_invested = h_s["invested"]

    print("=" * 90)
    print("  加碼池交易策略回測 — 極端崩跌時注入額外資金")
    print("  觸發條件：00631L 單日跌 ≤-8% 且 H 條件 4/4（不限持倉狀態）")
    print("=" * 90)
    print(f"  {'模式':16s}  {'終值(萬)':>10s}  {'投入(萬)':>10s}  {'淨賺(萬)':>10s}  {'CAGR':>7s}  {'MDD':>8s}  {'Calmar':>7s}  {'加碼次':>6s}")
    print("-" * 90)
    print(f"  {'B&H':16s}  {bh_s['final']:>10.1f}  {bh_s['invested']:>10.1f}  {bh_s['net']:>10.1f}"
          f"  {bh_s['cagr']:>6.1f}%  {bh_s['mdd']:>+7.1f}%  {bh_s['calmar']:>7.2f}  {'—':>6s}")
    print(f"  {'H原版(無加碼)':16s}  {h_s['final']:>10.1f}  {h_s['invested']:>10.1f}  {h_s['net']:>10.1f}"
          f"  {h_s['cagr']:>6.1f}%  {h_s['mdd']:>+7.1f}%  {h_s['calmar']:>7.2f}  {'0':>6s}")
    print("-" * 90)

    for bonus in [250_000, 500_000, 1_000_000, 2_000_000]:
        r = run_with_bonus_pool(inputs, cfg, bonus_amount=bonus, t1=True)
        s = reporter.analyze(r, f"+{bonus//10000:.0f}萬/次")
        n_bonus = len(r["bonus_events"])
        total_inj = r["total_bonus_injected"]
        label = f"加碼 {bonus//10000:.0f}萬/次"
        print(f"  {label:16s}  {s['final']:>10.1f}  {s['invested']:>10.1f}  {s['net']:>10.1f}"
              f"  {s['cagr']:>6.1f}%  {s['mdd']:>+7.1f}%  {s['calmar']:>7.2f}  {n_bonus:>6d}")

    print("=" * 90)

    # 詳細顯示 50萬/次 的加碼事件
    print("\n  加碼池事件明細 (50萬/次)")
    print("  " + "─" * 70)
    r50 = run_with_bonus_pool(inputs, cfg, bonus_amount=500_000, t1=True)
    for i, ev in enumerate(r50["bonus_events"], 1):
        print(f"  {i}. {ev['date']}  日跌{ev['daily_return']:+.1f}%  @{ev['close']:.2f}"
              f"  {ev['tag']} {ev['injected']//10000:.0f}萬  [{ev['position']}]")
    print(f"  → 共注入 {r50['total_bonus_injected']//10000:.0f}萬 (佔總投入 {r50['total_bonus_injected']/r50['invested']*100:.1f}%)")

    # ROI 比較: 每多投入 1 元加碼池資金，多賺多少
    print("\n  加碼效率分析")
    print("  " + "─" * 70)
    h_net = h_s["net"] * 10000  # 轉回元
    for bonus in [250_000, 500_000, 1_000_000, 2_000_000]:
        r = run_with_bonus_pool(inputs, cfg, bonus_amount=bonus, t1=True)
        s = reporter.analyze(r, "")
        extra_net = (s["net"] - h_s["net"]) * 10000
        total_inj = r["total_bonus_injected"]
        if total_inj > 0:
            roi = extra_net / total_inj
            print(f"  加碼 {bonus//10000:>3.0f}萬/次 × {len(r['bonus_events'])}次"
                  f" = 注入{total_inj//10000:.0f}萬"
                  f" → 多賺{extra_net/10000:.0f}萬"
                  f" → 每1元加碼賺{roi:.1f}元")
    print("=" * 90)
