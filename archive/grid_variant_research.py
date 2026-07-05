#!/usr/bin/env python3
"""Research multiple long-only grid variants for 00631L.

This script compares several grid styles under the same execution model:
  - fixed grid
  - reset grid (recenter after large drift)
  - moving grid (20-day MA as center)
  - moving grid + regime filter (suppress new buys under 60-day MA)

It evaluates:
  - yearly restart results for 2019-2025
  - a continuous run for 2019-2025

The goal is not to find the prettiest backtest, but the least fragile grid
style for a leveraged ETF like 00631L.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median

from grid_backtest import (
    BUY_FEE_RATE,
    LOT_SIZE,
    SELL_FEE_RATE,
    SELL_TAX_RATE,
    BacktestResult,
    DayBar,
    build_levels,
    load_raw_bars,
    mark_to_market,
    max_drawdown,
    money,
    pct,
    simulate_buy_and_hold,
)

DB_PATH = Path(__file__).with_name("回測_0050還原數據.db")
TICKER = "00631L.TW"
YEARS = list(range(2019, 2026))
INITIAL_CAPITAL = 1_000_000.0
BASE_POSITION_RATIO = 0.5
MAX_POSITION_RATIO = 1.0
UNIT_LOTS = 1
GRID_STEPS = [0.03, 0.04, 0.05, 0.06, 0.08]


@dataclass(frozen=True)
class Variant:
    name: str
    center_mode: str
    reset_mult: float | None = None
    trend_window: int | None = None
    ma_window: int | None = None
    suppress_buys_below_trend: bool = False


@dataclass
class Summary:
    variant: str
    step: float
    avg_return: float
    med_return: float
    avg_mdd: float
    worst_return: float
    best_return: float
    avg_excess_vs_bh: float
    beat_bh_years: int
    continuous_return: float
    continuous_mdd: float


VARIANTS = [
    Variant(name="fixed", center_mode="fixed"),
    Variant(name="reset_3x", center_mode="reset", reset_mult=3.0),
    Variant(name="ma20", center_mode="ma", ma_window=20),
    Variant(
        name="ma20_regime60",
        center_mode="ma",
        ma_window=20,
        trend_window=60,
        suppress_buys_below_trend=True,
    ),
]


def rolling_mean_prev(closes: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    prefix: list[float] = []
    for close in closes:
        if not prefix:
            out.append(None)
        else:
            sample = prefix[-window:]
            out.append(sum(sample) / len(sample))
        prefix.append(close)
    return out


def liquidation_value(cash: float, shares: int, close_price: float) -> float:
    if shares <= 0:
        return cash
    return cash + shares * close_price * (1 - SELL_FEE_RATE - SELL_TAX_RATE)


def simulate_variant(
    bars: list[DayBar],
    variant: Variant,
    grid_step_pct: float,
    initial_capital: float = INITIAL_CAPITAL,
    base_position_ratio: float = BASE_POSITION_RATIO,
    max_position_ratio: float = MAX_POSITION_RATIO,
    unit_lots: int = UNIT_LOTS,
) -> BacktestResult:
    if not bars:
        raise ValueError("No bars loaded.")

    closes = [bar.close for bar in bars]
    ma_values = rolling_mean_prev(closes, variant.ma_window) if variant.ma_window else [None] * len(bars)
    trend_values = rolling_mean_prev(closes, variant.trend_window) if variant.trend_window else [None] * len(bars)

    start_price = bars[0].close
    unit_shares = unit_lots * LOT_SIZE
    cost_per_lot = start_price * (1 + BUY_FEE_RATE) * LOT_SIZE
    initial_lots = int((initial_capital * base_position_ratio) // cost_per_lot)
    max_lots = int((initial_capital * max_position_ratio) // cost_per_lot)
    shares = initial_lots * LOT_SIZE
    max_shares = max_lots * LOT_SIZE
    cash = initial_capital - shares * start_price * (1 + BUY_FEE_RATE)
    buy_fees = shares * start_price * BUY_FEE_RATE
    sell_fees_tax = 0.0
    curve: list[float] = []
    trades = 0
    buys = 0
    sells = 0
    center = start_price

    for idx, bar in enumerate(bars):
        if variant.center_mode == "ma" and ma_values[idx]:
            center = ma_values[idx] or center

        levels = build_levels(center, grid_step_pct)
        allow_buys = True
        if variant.suppress_buys_below_trend and trend_values[idx] is not None and idx > 0:
            allow_buys = closes[idx - 1] >= float(trend_values[idx])

        path = [bar.open, bar.low, bar.high, bar.close] if bar.close >= bar.open else [bar.open, bar.high, bar.low, bar.close]
        for p1, p2 in zip(path, path[1:]):
            if p2 == p1:
                continue
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
                    trades += 1
                    sells += 1
                continue

            crossed = [level for level in levels if p2 <= level < p1]
            crossed.sort(reverse=True)
            for level in crossed:
                if not allow_buys:
                    break
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
                trades += 1
                buys += 1

        if variant.center_mode == "reset" and variant.reset_mult is not None:
            threshold = grid_step_pct * variant.reset_mult
            if abs(bar.close / center - 1) >= threshold:
                center = bar.close

        curve.append(mark_to_market(cash, shares, bar.close))

    liquidated_end = liquidation_value(cash, shares, bars[-1].close)
    return BacktestResult(
        mode=variant.name,
        grid_step_pct=grid_step_pct,
        base_position_ratio=base_position_ratio,
        max_position_ratio=max_position_ratio,
        initial_capital=initial_capital,
        initial_shares=initial_lots * LOT_SIZE,
        max_shares=max_shares,
        trades=trades,
        buys=buys,
        sells=sells,
        total_fees_tax=buy_fees + sell_fees_tax,
        shares_end=shares,
        cash_end=cash,
        mtm_end=curve[-1],
        liquidated_end=liquidated_end,
        total_return=liquidated_end / initial_capital - 1,
        max_drawdown=max_drawdown(curve),
    )


def load_year(year: int) -> list[DayBar]:
    return load_raw_bars(DB_PATH, TICKER, f"{year}-01-01", f"{year}-12-31")


def summarize_combo(variant: Variant, step: float) -> Summary:
    returns: list[float] = []
    mdds: list[float] = []
    excesses: list[float] = []
    beat_bh = 0
    for year in YEARS:
        bars = load_year(year)
        bh = simulate_buy_and_hold(bars, INITIAL_CAPITAL)
        grid = simulate_variant(bars, variant, step)
        returns.append(grid.total_return)
        mdds.append(grid.max_drawdown)
        excess = grid.total_return - bh.total_return
        excesses.append(excess)
        if excess > 0:
            beat_bh += 1

    all_bars = load_raw_bars(DB_PATH, TICKER, f"{YEARS[0]}-01-01", f"{YEARS[-1]}-12-31")
    continuous = simulate_variant(all_bars, variant, step)
    return Summary(
        variant=variant.name,
        step=step,
        avg_return=mean(returns),
        med_return=median(returns),
        avg_mdd=mean(mdds),
        worst_return=min(returns),
        best_return=max(returns),
        avg_excess_vs_bh=mean(excesses),
        beat_bh_years=beat_bh,
        continuous_return=continuous.total_return,
        continuous_mdd=continuous.max_drawdown,
    )


def print_yearly_reference() -> None:
    print("Yearly buy-and-hold reference")
    for year in YEARS:
        bars = load_year(year)
        bh = simulate_buy_and_hold(bars, INITIAL_CAPITAL)
        print(
            f"  {year}: return={pct(bh.total_return):>8}  mdd={pct(bh.max_drawdown):>8}"
            f"  end={money(bh.liquidated_end):>10}"
        )
    print()


def print_summary_table(summaries: list[Summary]) -> None:
    print("Grid variant comparison (2019-2025 yearly restarts + continuous run)")
    print(
        "variant           step   avg_ret   med_ret   avg_mdd  worst_yr  beat_bh"
        "  avg_excess  cont_ret  cont_mdd"
    )
    for row in summaries:
        print(
            f"{row.variant:16s} {row.step * 100:>4.0f}%"
            f" {pct(row.avg_return):>9}"
            f" {pct(row.med_return):>9}"
            f" {pct(row.avg_mdd):>9}"
            f" {pct(row.worst_return):>9}"
            f" {row.beat_bh_years:>7d}/{len(YEARS)}"
            f" {pct(row.avg_excess_vs_bh):>11}"
            f" {pct(row.continuous_return):>9}"
            f" {pct(row.continuous_mdd):>9}"
        )


def main() -> None:
    print_yearly_reference()
    summaries = [summarize_combo(variant, step) for variant in VARIANTS for step in GRID_STEPS]
    summaries.sort(
        key=lambda row: (
            row.avg_excess_vs_bh,
            row.continuous_return,
            -abs(row.avg_mdd),
        ),
        reverse=True,
    )
    print_summary_table(summaries)
    print()
    print("Top 5 by average excess vs buy-and-hold:")
    for row in summaries[:5]:
        print(
            f"  {row.variant} step={row.step * 100:.0f}%"
            f"  avg_excess={pct(row.avg_excess_vs_bh)}"
            f"  beat_bh={row.beat_bh_years}/{len(YEARS)}"
            f"  cont={pct(row.continuous_return)}"
            f"  cont_mdd={pct(row.continuous_mdd)}"
        )


if __name__ == "__main__":
    main()
