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

TICKERS: list[str] = ["0050.TW", "00631L.TW"]
SQUEEZE_WINDOW: int = 5
VOL_MA_WINDOW: int = 20
SQUEEZE_RATIO: float = 0.7
BREAKOUT_VOL_RATIO: float = 1.5
TARGET_PCT: float = 0.04
STOP_PCT: float = 0.02
MAX_HOLD_DAYS: int = 8
TRADE_COST: float = 0.001425


@dataclass
class SqueezeTrade:
    ticker: str
    direction: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    exit_reason: str
    hold_days: int
    pnl_pct: float


@dataclass
class BacktestResult:
    ticker: str
    equity_curve: pd.Series
    trades: list[SqueezeTrade] = field(default_factory=list)
    win_rate: float = 0.0
    cagr: float = 0.0
    mdd: float = 0.0
    total_trades: int = 0
    avg_hold_days: float = 0.0


def fetch_price(ticker: str, years: int = 5) -> pd.DataFrame:
    end = date.today()
    start = end - timedelta(days=years * 365 + 60)
    df = yf.download(ticker, start=str(start), end=str(end), auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.sort_index(inplace=True)
    return df


def _build_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    vol_ma = df["Volume"].rolling(VOL_MA_WINDOW).mean()
    close_5h = df["Close"].shift(1).rolling(SQUEEZE_WINDOW).max()

    squeeze_days = (df["Volume"] < vol_ma * SQUEEZE_RATIO).astype(int)
    squeeze_streak = (
        squeeze_days.groupby((squeeze_days != squeeze_days.shift()).cumsum())
        .cumsum()
    )

    squeeze_ended = (squeeze_streak.shift(1) >= SQUEEZE_WINDOW) & (squeeze_days == 0)
    high_vol = df["Volume"] > vol_ma * BREAKOUT_VOL_RATIO

    df["breakout_long"] = squeeze_ended & high_vol & (df["Close"] > close_5h)
    df["breakout_short"] = squeeze_ended & high_vol & (df["Close"] < df["Close"].shift(1).rolling(SQUEEZE_WINDOW).min())
    df["vol_ma"] = vol_ma
    return df


def run_backtest(
    df: pd.DataFrame,
    ticker: str,
    initial_capital: float = 1_000_000.0,
) -> BacktestResult:
    df = _build_signals(df)

    capital = initial_capital
    equity_dates: list[str] = []
    equity_vals: list[float] = []
    trades: list[SqueezeTrade] = []

    in_position = False
    direction = ""
    entry_price = 0.0
    entry_idx = 0
    entry_date_str = ""

    closes = df["Close"].values
    opens = df["Open"].values
    highs = df["High"].values
    lows = df["Low"].values
    dates = df.index

    for i in range(len(df)):
        dt = dates[i]

        if in_position:
            days_held = i - entry_idx
            exit_price_raw: Optional[float] = None
            exit_reason = ""

            if direction == "long":
                target = entry_price * (1 + TARGET_PCT)
                stop = entry_price * (1 - STOP_PCT)
                if float(lows[i]) <= stop:
                    exit_price_raw = stop
                    exit_reason = "stop"
                elif float(highs[i]) >= target:
                    exit_price_raw = target
                    exit_reason = "target"
                elif days_held >= MAX_HOLD_DAYS:
                    exit_price_raw = float(closes[i])
                    exit_reason = "max_hold"
            else:
                target = entry_price * (1 - TARGET_PCT)
                stop = entry_price * (1 + STOP_PCT)
                if float(highs[i]) >= stop:
                    exit_price_raw = stop
                    exit_reason = "stop"
                elif float(lows[i]) <= target:
                    exit_price_raw = target
                    exit_reason = "target"
                elif days_held >= MAX_HOLD_DAYS:
                    exit_price_raw = float(closes[i])
                    exit_reason = "max_hold"

            if exit_price_raw is not None:
                ep = exit_price_raw * (1 - TRADE_COST)
                if direction == "long":
                    pnl_pct = (ep - entry_price) / entry_price
                else:
                    pnl_pct = (entry_price - ep) / entry_price
                capital += capital * pnl_pct
                equity_dates.append(dt.strftime("%Y-%m-%d"))
                equity_vals.append(capital)
                trades.append(SqueezeTrade(
                    ticker=ticker,
                    direction=direction,
                    entry_date=entry_date_str,
                    entry_price=round(entry_price, 4),
                    exit_date=dt.strftime("%Y-%m-%d"),
                    exit_price=round(exit_price_raw, 4),
                    exit_reason=exit_reason,
                    hold_days=days_held,
                    pnl_pct=round(pnl_pct, 6),
                ))
                in_position = False

        if not in_position:
            row = df.iloc[i]
            if row["breakout_long"]:
                entry_price = float(opens[min(i + 1, len(df) - 1)]) * (1 + TRADE_COST)
                direction = "long"
                entry_idx = i + 1
                entry_date_str = dates[min(i + 1, len(dates) - 1)].strftime("%Y-%m-%d")
                in_position = True
            elif row["breakout_short"]:
                entry_price = float(opens[min(i + 1, len(df) - 1)]) * (1 - TRADE_COST)
                direction = "short"
                entry_idx = i + 1
                entry_date_str = dates[min(i + 1, len(dates) - 1)].strftime("%Y-%m-%d")
                in_position = True

    equity = pd.Series(equity_vals, index=pd.to_datetime(equity_dates), name=ticker)
    equity.sort_index(inplace=True)

    if not trades:
        return BacktestResult(ticker=ticker, equity_curve=equity)

    profitable = [t for t in trades if t.pnl_pct > 0]
    win_rate = len(profitable) / len(trades)

    if len(equity) >= 2:
        years_held = (equity.index[-1] - equity.index[0]).days / 365.25
        cagr = (equity.iloc[-1] / initial_capital) ** (1 / years_held) - 1 if years_held > 0 else 0.0
    else:
        cagr = 0.0

    rolling_max = equity.cummax()
    drawdown = (equity - rolling_max) / rolling_max
    mdd = float(drawdown.min()) if len(drawdown) > 0 else 0.0

    avg_hold = sum(t.hold_days for t in trades) / len(trades)

    return BacktestResult(
        ticker=ticker,
        equity_curve=equity,
        trades=trades,
        win_rate=win_rate,
        cagr=cagr,
        mdd=mdd,
        total_trades=len(trades),
        avg_hold_days=round(avg_hold, 2),
    )


def squeeze_signal(ticker: str, query_date: str) -> dict:
    df = fetch_price(ticker, years=1)
    df = _build_signals(df)

    dt = pd.Timestamp(query_date)
    if dt not in df.index:
        idx = df.index.searchsorted(dt) - 1
        if idx < 0:
            return {}
        row = df.iloc[idx]
        actual_date = df.index[idx].strftime("%Y-%m-%d")
    else:
        row = df.loc[dt]
        actual_date = query_date

    if row["breakout_long"]:
        signal = "long"
        action = f"buy {ticker} next open"
    elif row["breakout_short"]:
        signal = "short"
        action = f"short {ticker} next open"
    else:
        signal = "none"
        action = "no action"

    return {
        "date": actual_date,
        "ticker": ticker,
        "signal": signal,
        "action": action,
        "close": round(float(row["Close"]), 4),
        "volume": int(row["Volume"]),
        "vol_ma": round(float(row["vol_ma"]), 0) if not np.isnan(row["vol_ma"]) else None,
        "target_pct": TARGET_PCT,
        "stop_pct": STOP_PCT,
        "max_hold_days": MAX_HOLD_DAYS,
    }


def build_performance_chart(
    results: list[BacktestResult],
    output_path: Optional[str] = None,
) -> go.Figure:
    fig = make_subplots(
        rows=2,
        cols=1,
        subplot_titles=["Equity Curves", "Exit Reason Distribution"],
        vertical_spacing=0.15,
        row_heights=[0.65, 0.35],
    )

    colors = {"0050.TW": "#2196F3", "00631L.TW": "#FF9800"}

    for result in results:
        eq = result.equity_curve
        if len(eq) == 0:
            continue
        color = colors.get(result.ticker, "#9E9E9E")
        fig.add_trace(
            go.Scatter(
                x=eq.index,
                y=eq.values,
                mode="lines",
                name=result.ticker,
                line=dict(color=color),
            ),
            row=1,
            col=1,
        )
        stats_text = (
            f"{result.ticker} | Win: {result.win_rate:.1%}  CAGR: {result.cagr:.1%}  "
            f"MDD: {result.mdd:.1%}  Trades: {result.total_trades}  Avg Hold: {result.avg_hold_days:.1f}d"
        )
        fig.add_annotation(
            xref="x",
            yref="paper",
            x=eq.index[len(eq) // 2],
            y=0.95 - results.index(result) * 0.08,
            text=stats_text,
            showarrow=False,
            font=dict(size=9),
            bgcolor="rgba(255,255,255,0.8)",
        )

    all_trades: list[SqueezeTrade] = []
    for r in results:
        all_trades.extend(r.trades)

    if all_trades:
        reason_counts: dict[str, int] = {}
        for t in all_trades:
            reason_counts[t.exit_reason] = reason_counts.get(t.exit_reason, 0) + 1
        reasons = list(reason_counts.keys())
        counts = [reason_counts[r] for r in reasons]
        bar_colors_map = {"stop": "#F44336", "target": "#4CAF50", "max_hold": "#FF9800"}
        bar_colors = [bar_colors_map.get(r, "#9E9E9E") for r in reasons]
        fig.add_trace(
            go.Bar(x=reasons, y=counts, marker_color=bar_colors, showlegend=False, name="exit reasons"),
            row=2,
            col=1,
        )

    fig.update_layout(
        title="Volume Squeeze Breakout Strategy — Backtest Performance",
        height=800,
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
    results = []
    for ticker in TICKERS:
        df = fetch_price(ticker, years=years)
        result = run_backtest(df, ticker)
        results.append(result)

    fig = build_performance_chart(results, output_path=output_html)

    summary = {}
    for r in results:
        summary[r.ticker] = {
            "total_trades": r.total_trades,
            "win_rate": round(r.win_rate, 4),
            "cagr": round(r.cagr, 4),
            "mdd": round(r.mdd, 4),
            "avg_hold_days": r.avg_hold_days,
        }

    return {"summary": summary, "results": results, "fig": fig}


if __name__ == "__main__":
    out_dir = Path(__file__).parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    output = run_full_analysis(output_html=str(out_dir / "volume_squeeze_performance.html"))
    print("\n=== Volume Squeeze Breakout Strategy Summary ===")
    for ticker, stats in output["summary"].items():
        print(f"\n  [{ticker}]")
        for k, v in stats.items():
            print(f"    {k}: {v}")
