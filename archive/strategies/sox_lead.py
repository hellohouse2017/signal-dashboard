#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import yfinance as yf
from plotly.subplots import make_subplots

SOX_TICKER: str = "^SOX"
LONG_TICKERS: list[str] = ["0050.TW", "00631L.TW"]
SHORT_TICKER: str = "00632R.TW"
SOX_THRESHOLD: float = 0.02
GAP_FILTER_PCT: float = 0.01
HOLD_DAYS_OPTIONS: list[int] = [1, 2]
TRADE_COST: float = 0.001425


@dataclass
class SoxTrade:
    sox_date: str
    sox_return: float
    direction: str
    ticker: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    hold_days: int
    gap_filtered: bool
    pnl_pct: float


@dataclass
class BacktestResult:
    ticker: str
    hold_days: int
    equity_curve: pd.Series
    trades: list[SoxTrade] = field(default_factory=list)
    win_rate: float = 0.0
    cagr: float = 0.0
    avg_return: float = 0.0
    total_trades: int = 0


def fetch_price(ticker: str, years: int = 5) -> pd.DataFrame:
    end = date.today()
    start = end - timedelta(days=years * 365 + 60)
    df = yf.download(ticker, start=str(start), end=str(end), auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[cols].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.sort_index(inplace=True)
    return df


def _sox_daily_returns(sox_df: pd.DataFrame) -> pd.Series:
    return sox_df["Close"].pct_change()


def _gap_pct(target_df: pd.DataFrame, entry_date: pd.Timestamp) -> Optional[float]:
    idx = target_df.index.searchsorted(entry_date)
    if idx <= 0 or idx >= len(target_df):
        return None
    prev_close = float(target_df["Close"].iloc[idx - 1])
    open_price = float(target_df["Open"].iloc[idx])
    if prev_close == 0:
        return None
    return (open_price - prev_close) / prev_close


def run_backtest(
    sox_df: pd.DataFrame,
    target_df: pd.DataFrame,
    ticker: str,
    hold_days: int = 1,
    initial_capital: float = 1_000_000.0,
) -> BacktestResult:
    sox_ret = _sox_daily_returns(sox_df)

    capital = initial_capital
    equity_dates: list[str] = []
    equity_vals: list[float] = []
    trades: list[SoxTrade] = []

    target_dates = target_df.index

    for i, sox_date in enumerate(sox_df.index):
        ret = sox_ret.iloc[i]
        if np.isnan(ret) or abs(ret) <= SOX_THRESHOLD:
            continue

        direction = "long" if ret > 0 else "short"

        entry_idx = target_dates.searchsorted(sox_date + timedelta(days=1))
        if entry_idx >= len(target_dates):
            continue

        actual_entry_date = target_dates[entry_idx]
        if (actual_entry_date - sox_date).days > 4:
            continue

        gap = _gap_pct(target_df, actual_entry_date)
        gap_filtered = False
        if gap is not None and abs(gap) > GAP_FILTER_PCT:
            gap_filtered = True

        exit_idx = entry_idx + hold_days
        if exit_idx >= len(target_dates):
            continue

        entry_price_raw = float(target_df["Open"].iloc[entry_idx])
        exit_price_raw = float(target_df["Close"].iloc[exit_idx])

        if direction == "long":
            entry_price = entry_price_raw * (1 + TRADE_COST)
            exit_price = exit_price_raw * (1 - TRADE_COST)
            pnl_pct = (exit_price - entry_price) / entry_price
        else:
            entry_price = entry_price_raw * (1 - TRADE_COST)
            exit_price = exit_price_raw * (1 + TRADE_COST)
            pnl_pct = (entry_price - exit_price) / entry_price

        if gap_filtered:
            effective_pnl = pnl_pct * 0.5
        else:
            effective_pnl = pnl_pct

        capital += capital * effective_pnl
        equity_dates.append(actual_entry_date.strftime("%Y-%m-%d"))
        equity_vals.append(capital)

        trades.append(SoxTrade(
            sox_date=sox_date.strftime("%Y-%m-%d"),
            sox_return=round(float(ret), 6),
            direction=direction,
            ticker=ticker,
            entry_date=actual_entry_date.strftime("%Y-%m-%d"),
            entry_price=round(entry_price_raw, 4),
            exit_date=target_dates[exit_idx - 1].strftime("%Y-%m-%d"),
            exit_price=round(exit_price_raw, 4),
            hold_days=hold_days,
            gap_filtered=gap_filtered,
            pnl_pct=round(pnl_pct, 6),
        ))

    equity = pd.Series(equity_vals, index=pd.to_datetime(equity_dates), name=ticker)
    equity.sort_index(inplace=True)

    if not trades:
        return BacktestResult(ticker=ticker, hold_days=hold_days, equity_curve=equity)

    profitable = [t for t in trades if t.pnl_pct > 0]
    win_rate = len(profitable) / len(trades)
    avg_return = float(np.mean([t.pnl_pct for t in trades]))

    if len(equity) >= 2:
        years_held = (equity.index[-1] - equity.index[0]).days / 365.25
        cagr = (equity.iloc[-1] / initial_capital) ** (1 / years_held) - 1 if years_held > 0 else 0.0
    else:
        cagr = 0.0

    return BacktestResult(
        ticker=ticker,
        hold_days=hold_days,
        equity_curve=equity,
        trades=trades,
        win_rate=win_rate,
        cagr=cagr,
        avg_return=avg_return,
        total_trades=len(trades),
    )


def sox_signal(query_date: str) -> dict:
    sox_df = fetch_price(SOX_TICKER, years=1)
    sox_ret = _sox_daily_returns(sox_df)

    dt = pd.Timestamp(query_date)
    if dt not in sox_ret.index:
        idx = sox_ret.index.searchsorted(dt) - 1
        if idx < 0:
            return {}
        ret = float(sox_ret.iloc[idx])
        actual_date = sox_ret.index[idx].strftime("%Y-%m-%d")
    else:
        ret = float(sox_ret[dt])
        actual_date = query_date

    if ret > SOX_THRESHOLD:
        signal = "long"
        action = f"buy {', '.join(LONG_TICKERS)} next open"
        tickers = LONG_TICKERS
    elif ret < -SOX_THRESHOLD:
        signal = "short"
        action = f"buy {SHORT_TICKER} (or short 0050/00631L) next open"
        tickers = [SHORT_TICKER]
    else:
        signal = "none"
        action = "no action"
        tickers = []

    gap_alerts: dict[str, Optional[float]] = {}
    next_dt = pd.Timestamp(query_date) + timedelta(days=1)
    for tkr in LONG_TICKERS + [SHORT_TICKER]:
        try:
            tdf = fetch_price(tkr, years=1)
            gap = _gap_pct(tdf, next_dt)
            gap_alerts[tkr] = round(gap, 4) if gap is not None else None
        except Exception:
            gap_alerts[tkr] = None

    return {
        "sox_date": actual_date,
        "sox_return": round(ret, 4),
        "threshold": SOX_THRESHOLD,
        "signal": signal,
        "action": action,
        "tickers": tickers,
        "gap_filter_pct": GAP_FILTER_PCT,
        "next_day_gaps": gap_alerts,
    }


def build_performance_chart(
    results: list[BacktestResult],
    output_path: Optional[str] = None,
) -> go.Figure:
    ticker_groups: dict[str, list[BacktestResult]] = {}
    for r in results:
        ticker_groups.setdefault(r.ticker, []).append(r)

    n_rows = max(len(ticker_groups), 1)
    subplot_titles = [f"{tkr} Equity (hold={r.hold_days}d)" for tkr, rs in ticker_groups.items() for r in rs]
    fig = make_subplots(
        rows=n_rows,
        cols=1,
        subplot_titles=subplot_titles[:n_rows],
        vertical_spacing=0.12,
    )

    palette = ["#2196F3", "#FF9800", "#4CAF50", "#9C27B0"]
    color_idx = 0

    for row_i, (ticker, rs) in enumerate(ticker_groups.items(), start=1):
        for r in rs:
            eq = r.equity_curve
            if len(eq) == 0:
                continue
            color = palette[color_idx % len(palette)]
            color_idx += 1
            fig.add_trace(
                go.Scatter(
                    x=eq.index,
                    y=eq.values,
                    mode="lines",
                    name=f"{r.ticker} hold={r.hold_days}d",
                    line=dict(color=color),
                ),
                row=row_i,
                col=1,
            )
            stats_text = (
                f"Win: {r.win_rate:.1%}  CAGR: {r.cagr:.1%}  "
                f"Avg Ret: {r.avg_return:.2%}  Trades: {r.total_trades}"
            )
            if len(eq) > 0:
                fig.add_annotation(
                    xref=f"x{row_i}",
                    yref=f"y{row_i}",
                    x=eq.index[len(eq) // 2],
                    y=float(eq.max()),
                    text=stats_text,
                    showarrow=False,
                    font=dict(size=9),
                    bgcolor="rgba(255,255,255,0.8)",
                )

    fig.update_layout(
        title="SOX Lead Effect Strategy — Backtest Performance",
        height=400 * n_rows,
        template="plotly_white",
        font=dict(size=11),
    )

    if output_path:
        fig.write_html(output_path)

    return fig


def run_full_analysis(
    years: int = 5,
    output_html: Optional[str] = None,
) -> dict:
    sox_df = fetch_price(SOX_TICKER, years=years)

    all_results: list[BacktestResult] = []
    for ticker in LONG_TICKERS:
        target_df = fetch_price(ticker, years=years)
        for hold in HOLD_DAYS_OPTIONS:
            result = run_backtest(sox_df, target_df, ticker, hold_days=hold)
            all_results.append(result)

    fig = build_performance_chart(all_results, output_path=output_html)

    summary: dict[str, dict] = {}
    for r in all_results:
        key = f"{r.ticker}_hold{r.hold_days}d"
        summary[key] = {
            "total_trades": r.total_trades,
            "win_rate": round(r.win_rate, 4),
            "cagr": round(r.cagr, 4),
            "avg_return": round(r.avg_return, 4),
        }

    return {"summary": summary, "results": all_results, "fig": fig}


if __name__ == "__main__":
    out_dir = Path(__file__).parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    output = run_full_analysis(output_html=str(out_dir / "sox_lead_performance.html"))
    print("\n=== SOX Lead Effect Strategy Summary ===")
    for key, stats in output["summary"].items():
        print(f"\n  [{key}]")
        for k, v in stats.items():
            print(f"    {k}: {v}")
