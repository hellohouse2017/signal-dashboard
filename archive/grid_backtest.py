#!/usr/bin/env python3
"""Simple fixed-grid backtest for TW ETFs using local daily bars.

This script is intentionally narrow:
  - data source: scanner/回測_0050還原數據.db
  - execution granularity: daily OHLC
  - execution heuristic: infer intraday path from candle shape
  - position model: fixed grid with one target unit per crossed level

It is meant for quick research, not production execution modeling.
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from adjuster import get_adjusted_prices

LOT_SIZE = 1000
BUY_FEE_RATE = 0.001425
SELL_FEE_RATE = 0.001425
SELL_TAX_RATE = 0.001


@dataclass
class DayBar:
    date: str
    open: float
    high: float
    low: float
    close: float


@dataclass
class Trade:
    date: str
    side: str
    price: float
    shares: int


@dataclass
class BacktestResult:
    mode: str
    grid_step_pct: float
    base_position_ratio: float
    max_position_ratio: float
    initial_capital: float
    initial_shares: int
    max_shares: int
    trades: int
    buys: int
    sells: int
    total_fees_tax: float
    shares_end: int
    cash_end: float
    mtm_end: float
    liquidated_end: float
    total_return: float
    max_drawdown: float


def load_raw_bars(db_path: Path, ticker: str, start: str, end: str) -> list[DayBar]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        """
        SELECT date, open, high, low, close
        FROM daily_prices_raw
        WHERE ticker = ? AND date BETWEEN ? AND ?
        ORDER BY date
        """,
        (ticker, start, end),
    ).fetchall()
    conn.close()
    return [DayBar(*row) for row in rows]


def load_adjusted_bars(db_path: Path, ticker: str, start: str, end: str) -> list[DayBar]:
    prices = get_adjusted_prices(ticker, str(db_path))
    bars: list[DayBar] = []
    for date in sorted(prices):
        if start <= date <= end:
            row = prices[date]
            bars.append(DayBar(date, row["open"], row["high"], row["low"], row["close"]))
    return bars


def build_levels(center_price: float, grid_step_pct: float, max_steps: int = 500) -> list[float]:
    levels: list[float] = []
    for step in range(1, max_steps + 1):
        lower = center_price * (1 - grid_step_pct * step)
        upper = center_price * (1 + grid_step_pct * step)
        if lower > 0:
            levels.append(lower)
        levels.append(upper)
    return sorted(levels)


def mark_to_market(cash: float, shares: int, close_price: float) -> float:
    return cash + shares * close_price


def liquidation_value(cash: float, shares: int, close_price: float) -> float:
    if shares <= 0:
        return cash
    return cash + shares * close_price * (1 - SELL_FEE_RATE - SELL_TAX_RATE)


def max_drawdown(curve: list[float]) -> float:
    peak = curve[0]
    worst = 0.0
    for equity in curve:
        if equity > peak:
            peak = equity
        drawdown = equity / peak - 1
        if drawdown < worst:
            worst = drawdown
    return worst


def simulate_grid(
    bars: list[DayBar],
    initial_capital: float,
    grid_step_pct: float,
    base_position_ratio: float,
    max_position_ratio: float,
    unit_lots: int,
) -> BacktestResult:
    if not bars:
        raise ValueError("No bars loaded.")

    start_price = bars[0].close
    unit_shares = unit_lots * LOT_SIZE
    cost_per_initial_lot = start_price * (1 + BUY_FEE_RATE) * LOT_SIZE

    initial_lots = int((initial_capital * base_position_ratio) // cost_per_initial_lot)
    max_lots = int((initial_capital * max_position_ratio) // cost_per_initial_lot)
    shares = initial_lots * LOT_SIZE
    max_shares = max_lots * LOT_SIZE
    cash = initial_capital - shares * start_price * (1 + BUY_FEE_RATE)
    buy_fees = shares * start_price * BUY_FEE_RATE
    sell_fees_tax = 0.0

    levels = build_levels(start_price, grid_step_pct)
    trades: list[Trade] = []
    curve: list[float] = []

    def process_segment(p1: float, p2: float, date: str) -> None:
        nonlocal cash, shares, buy_fees, sell_fees_tax
        if p2 == p1:
            return

        if p2 > p1:
            crossed = [level for level in levels if p1 < level <= p2]
            for level in crossed:
                if shares < unit_shares:
                    break
                gross = level * unit_shares
                fee_tax = gross * (SELL_FEE_RATE + SELL_TAX_RATE)
                cash += gross - fee_tax
                shares -= unit_shares
                sell_fees_tax += fee_tax
                trades.append(Trade(date, "SELL", level, unit_shares))
            return

        crossed = [level for level in levels if p2 <= level < p1]
        crossed.sort(reverse=True)
        for level in crossed:
            if shares + unit_shares > max_shares:
                break
            gross = level * unit_shares
            fee = gross * BUY_FEE_RATE
            total_cost = gross + fee
            if cash < total_cost:
                break
            cash -= total_cost
            shares += unit_shares
            buy_fees += fee
            trades.append(Trade(date, "BUY", level, unit_shares))

    for bar in bars:
        path = [bar.open, bar.low, bar.high, bar.close] if bar.close >= bar.open else [bar.open, bar.high, bar.low, bar.close]
        for p1, p2 in zip(path, path[1:]):
            process_segment(p1, p2, bar.date)
        curve.append(mark_to_market(cash, shares, bar.close))

    mtm_end = mark_to_market(cash, shares, bars[-1].close)
    liquidated_end = liquidation_value(cash, shares, bars[-1].close)
    return BacktestResult(
        mode="grid",
        grid_step_pct=grid_step_pct,
        base_position_ratio=base_position_ratio,
        max_position_ratio=max_position_ratio,
        initial_capital=initial_capital,
        initial_shares=initial_lots * LOT_SIZE,
        max_shares=max_shares,
        trades=len(trades),
        buys=sum(1 for trade in trades if trade.side == "BUY"),
        sells=sum(1 for trade in trades if trade.side == "SELL"),
        total_fees_tax=buy_fees + sell_fees_tax,
        shares_end=shares,
        cash_end=cash,
        mtm_end=mtm_end,
        liquidated_end=liquidated_end,
        total_return=liquidated_end / initial_capital - 1,
        max_drawdown=max_drawdown(curve),
    )


def simulate_buy_and_hold(
    bars: list[DayBar],
    initial_capital: float,
) -> BacktestResult:
    if not bars:
        raise ValueError("No bars loaded.")

    start_price = bars[0].close
    cost_per_lot = start_price * (1 + BUY_FEE_RATE) * LOT_SIZE
    lots = int(initial_capital // cost_per_lot)
    shares = lots * LOT_SIZE
    cash = initial_capital - shares * start_price * (1 + BUY_FEE_RATE)
    buy_fees = shares * start_price * BUY_FEE_RATE
    curve = [mark_to_market(cash, shares, bar.close) for bar in bars]
    liquidated_end = liquidation_value(cash, shares, bars[-1].close)

    return BacktestResult(
        mode="buy_hold",
        grid_step_pct=0.0,
        base_position_ratio=1.0,
        max_position_ratio=1.0,
        initial_capital=initial_capital,
        initial_shares=shares,
        max_shares=shares,
        trades=1,
        buys=1,
        sells=0,
        total_fees_tax=buy_fees,
        shares_end=shares,
        cash_end=cash,
        mtm_end=curve[-1],
        liquidated_end=liquidated_end,
        total_return=liquidated_end / initial_capital - 1,
        max_drawdown=max_drawdown(curve),
    )


def pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def money(value: float) -> str:
    return f"{value:,.0f}"


def print_result(result: BacktestResult) -> None:
    print(
        f"{result.mode:>9}  step={result.grid_step_pct * 100:>4.1f}%"
        f"  base={result.base_position_ratio * 100:>5.1f}%"
        f"  max={result.max_position_ratio * 100:>5.1f}%"
        f"  init={result.initial_shares // LOT_SIZE:>3} lots"
        f"  cap={result.max_shares // LOT_SIZE:>3} lots"
        f"  trades={result.trades:>4}"
        f"  return={pct(result.total_return):>8}"
        f"  mdd={pct(result.max_drawdown):>8}"
        f"  end={money(result.liquidated_end):>10}"
        f"  fees+tax={money(result.total_fees_tax):>9}"
        f"  end_shares={result.shares_end:>6}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fixed-grid backtest on local daily bars.")
    parser.add_argument("--ticker", default="00631L.TW")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2019-12-31")
    parser.add_argument("--db-path", default=str(Path(__file__).with_name("回測_0050還原數據.db")))
    parser.add_argument("--initial-capital", type=float, default=1_000_000)
    parser.add_argument("--base-position-ratio", type=float, default=0.5)
    parser.add_argument("--max-position-ratio", type=float, default=1.0)
    parser.add_argument("--grid-step", type=float, default=0.04, help="Decimal, e.g. 0.04 = 4%%")
    parser.add_argument("--unit-lots", type=int, default=1, help="Trade unit in 1000-share lots")
    parser.add_argument("--adjusted", action="store_true", help="Use adjusted prices from adjuster.py")
    parser.add_argument(
        "--sweep",
        default="0.02,0.03,0.04,0.05,0.06,0.08",
        help="Comma-separated grid steps for batch run.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db_path)
    loader = load_adjusted_bars if args.adjusted else load_raw_bars
    bars = loader(db_path, args.ticker, args.start, args.end)
    if not bars:
        raise SystemExit("No data loaded.")

    print(f"Ticker: {args.ticker}")
    print(f"Range:  {bars[0].date} -> {bars[-1].date} ({len(bars)} bars)")
    print(f"Mode:   {'adjusted' if args.adjusted else 'raw'}")
    initial_lot_cost = bars[0].close * (1 + BUY_FEE_RATE) * LOT_SIZE
    print(f"LotCost:{money(initial_lot_cost)} per 1 lot at start")
    print(
        "Model:  fixed grid, 1-way crossing execution, "
        "OHLC path inferred as open->low->high->close on up days, else open->high->low->close"
    )
    print()

    buy_hold = simulate_buy_and_hold(bars, args.initial_capital)
    print_result(buy_hold)

    steps = [float(token) for token in args.sweep.split(",") if token.strip()]
    for step in steps:
        result = simulate_grid(
            bars,
            initial_capital=args.initial_capital,
            grid_step_pct=step,
            base_position_ratio=args.base_position_ratio,
            max_position_ratio=args.max_position_ratio,
            unit_lots=args.unit_lots,
        )
        print_result(result)


if __name__ == "__main__":
    main()
