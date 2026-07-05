#!/usr/bin/env python3
# NOTE: Uses proxy indicators (volume+price divergence) instead of real margin data.
# Replace with Shioaji API margin balance once available.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import yfinance as yf
from plotly.subplots import make_subplots

TICKER: str = "0050.TW"
VOLUME_STREAK: int = 3
PRICE_FLAT_PCT: float = 0.003
TARGET_PCT: float = 0.03
STOP_PCT: float = 0.02
MAX_HOLD_DAYS: int = 10
TRADE_COST: float = 0.001425


@dataclass
class MarginTrade:
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    exit_reason: str
    hold_days: int
    signal_type: str
    pnl_pct: float


@dataclass
class BacktestResult:
    ticker: str
    equity_curve: pd.Series
    trades: list[MarginTrade] = field(default_factory=list)
    win_rate: float = 0.0
    cagr: float = 0.0
    mdd: float = 0.0
    total_trades: int = 0
    profitable_trades: int = 0
    avg_hold_days: float = 0.0


def fetch_price(ticker: str, years: int = 6) -> pd.DataFrame:
    end = date.today()
    start = end - timedelta(days=years * 365 + 30)
    df = yf.download(ticker, start=str(start), end=str(end), auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.sort_index(inplace=True)
    return df


def detect_signals(df: pd.DataFrame) -> pd.Series:
    closes = df["Close"]
    volumes = df["Volume"]

    price_chg = closes.pct_change().abs()
    vol_up = volumes > volumes.shift(1)
    vol_dn = volumes < volumes.shift(1)
    flat = price_chg < PRICE_FLAT_PCT

    vol_up_streak = pd.Series(0, index=df.index)
    vol_dn_streak = pd.Series(0, index=df.index)
    for i in range(1, len(df)):
        vol_up_streak.iloc[i] = (vol_up_streak.iloc[i - 1] + 1) if vol_up.iloc[i] else 0
        vol_dn_streak.iloc[i] = (vol_dn_streak.iloc[i - 1] + 1) if vol_dn.iloc[i] else 0

    signals = pd.Series("none", index=df.index, dtype=object)
    short_cond = (vol_up_streak >= VOLUME_STREAK) & flat
    long_cond = (vol_dn_streak >= VOLUME_STREAK) & flat
    signals[short_cond] = "short"
    signals[long_cond] = "long"
    return signals


def run_backtest(
    price_df: pd.DataFrame,
    initial_capital: float = 1_000_000.0,
) -> BacktestResult:
    df = price_df.copy()
    signals = detect_signals(df)

    closes = df["Close"].values
    opens = df["Open"].values
    highs = df["High"].values
    lows = df["Low"].values
    dates = df.index

    capital = initial_capital
    equity_dates: list[str] = []
    equity_vals: list[float] = []
    trades: list[MarginTrade] = []

    in_position = False
    entry_price = 0.0
    entry_idx = 0
    entry_date_str = ""
    signal_type = ""

    for i, dt in enumerate(dates):
        if in_position:
            days_held = i - entry_idx
            current_high = float(highs[i])
            current_low = float(lows[i])
            current_close = float(closes[i])
            exit_price_raw: Optional[float] = None
            exit_reason = ""

            if signal_type == "short":
                target_price = entry_price * (1 - TARGET_PCT)
                stop_price = entry_price * (1 + STOP_PCT)
                if current_low <= target_price:
                    exit_price_raw = target_price
                    exit_reason = "target"
                elif current_high >= stop_price:
                    exit_price_raw = stop_price
                    exit_reason = "stop"
                elif days_held >= MAX_HOLD_DAYS:
                    exit_price_raw = current_close
                    exit_reason = "max_hold"
            else:
                target_price = entry_price * (1 + TARGET_PCT)
                stop_price = entry_price * (1 - STOP_PCT)
                if current_high >= target_price:
                    exit_price_raw = target_price
                    exit_reason = "target"
                elif current_low <= stop_price:
                    exit_price_raw = stop_price
                    exit_reason = "stop"
                elif days_held >= MAX_HOLD_DAYS:
                    exit_price_raw = current_close
                    exit_reason = "max_hold"

            if exit_price_raw is not None:
                if signal_type == "short":
                    pnl_pct = (entry_price - exit_price_raw) / entry_price - TRADE_COST * 2
                else:
                    pnl_pct = (exit_price_raw - entry_price) / entry_price - TRADE_COST * 2

                pnl = capital * pnl_pct
                capital += pnl

                equity_dates.append(dt.strftime("%Y-%m-%d"))
                equity_vals.append(capital)

                trades.append(MarginTrade(
                    entry_date=entry_date_str,
                    entry_price=round(entry_price, 4),
                    exit_date=dt.strftime("%Y-%m-%d"),
                    exit_price=round(exit_price_raw, 4),
                    exit_reason=exit_reason,
                    hold_days=days_held,
                    signal_type=signal_type,
                    pnl_pct=round(pnl_pct, 6),
                ))
                in_position = False

        if not in_position and i + 1 < len(dates):
            sig = signals.iloc[i]
            if sig in ("short", "long"):
                entry_price = float(opens[i + 1]) * (1 + TRADE_COST)
                entry_idx = i + 1
                entry_date_str = dates[i + 1].strftime("%Y-%m-%d")
                signal_type = sig
                in_position = True

    equity = pd.Series(equity_vals, index=pd.to_datetime(equity_dates), name=TICKER)
    equity.sort_index(inplace=True)

    if len(trades) == 0:
        return BacktestResult(ticker=TICKER, equity_curve=equity)

    profitable = [t for t in trades if t.pnl_pct > 0]
    win_rate = len(profitable) / len(trades)

    if len(equity) >= 2:
        years_held = (equity.index[-1] - equity.index[0]).days / 365.25
        cagr = (equity.iloc[-1] / initial_capital) ** (1 / years_held) - 1 if years_held > 0 else 0.0
    else:
        cagr = 0.0

    rolling_max = equity.cummax()
    drawdown = (equity - rolling_max) / rolling_max
    mdd = float(drawdown.min())
    avg_hold = sum(t.hold_days for t in trades) / len(trades)

    return BacktestResult(
        ticker=TICKER,
        equity_curve=equity,
        trades=trades,
        win_rate=win_rate,
        cagr=cagr,
        mdd=mdd,
        total_trades=len(trades),
        profitable_trades=len(profitable),
        avg_hold_days=round(avg_hold, 2),
    )


def margin_signal(ticker: str, query_date: str) -> dict:
    price_df = fetch_price(ticker, years=1)
    signals = detect_signals(price_df)

    dt = pd.Timestamp(query_date)
    if dt not in price_df.index:
        idx = price_df.index.searchsorted(dt) - 1
        if idx < 0:
            return {}
        dt = price_df.index[idx]

    sig = signals.get(dt, "none")
    pos = price_df.index.get_loc(dt)
    next_open: Optional[float] = None
    if pos + 1 < len(price_df):
        next_open = round(float(price_df["Open"].iloc[pos + 1]) * (1 + TRADE_COST), 4)

    if sig == "short":
        action = f"short {ticker}"
        target = round(next_open * (1 - TARGET_PCT), 4) if next_open else None
        stop = round(next_open * (1 + STOP_PCT), 4) if next_open else None
    elif sig == "long":
        action = f"buy {ticker}"
        target = round(next_open * (1 + TARGET_PCT), 4) if next_open else None
        stop = round(next_open * (1 - STOP_PCT), 4) if next_open else None
    else:
        action = "no action"
        target = None
        stop = None

    vol = price_df["Volume"]
    vol_chg_3d = [
        round(float(vol.iloc[pos - k] / vol.iloc[pos - k - 1] - 1), 4)
        for k in range(3)
        if pos - k - 1 >= 0
    ]
    price_chg = round(float(price_df["Close"].pct_change().iloc[pos]), 4)

    return {
        "date": dt.strftime("%Y-%m-%d"),
        "ticker": ticker,
        "signal": sig,
        "action": action,
        "entry_price": next_open,
        "target_price": target,
        "stop_loss": stop,
        "price_chg_pct": price_chg,
        "vol_chg_3d": vol_chg_3d,
    }


def build_performance_chart(
    result: BacktestResult,
    price_df: pd.DataFrame,
    output_path: Optional[str] = None,
) -> go.Figure:
    fig = make_subplots(
        rows=3,
        cols=1,
        subplot_titles=[
            "Price & Signals",
            "Equity Curve",
            "Exit Reason Distribution",
        ],
        vertical_spacing=0.12,
        row_heights=[0.35, 0.40, 0.25],
    )

    plot_df = price_df.tail(500)
    fig.add_trace(
        go.Scatter(
            x=plot_df.index,
            y=plot_df["Close"],
            mode="lines",
            name="Close",
            line=dict(color="#607D8B"),
        ),
        row=1,
        col=1,
    )

    if result.trades:
        long_entries = [t for t in result.trades if t.signal_type == "long"]
        short_entries = [t for t in result.trades if t.signal_type == "short"]

        if long_entries:
            fig.add_trace(
                go.Scatter(
                    x=[pd.Timestamp(t.entry_date) for t in long_entries],
                    y=[t.entry_price for t in long_entries],
                    mode="markers",
                    name="Long Entry",
                    marker=dict(symbol="triangle-up", color="#4CAF50", size=10),
                ),
                row=1,
                col=1,
            )

        if short_entries:
            fig.add_trace(
                go.Scatter(
                    x=[pd.Timestamp(t.entry_date) for t in short_entries],
                    y=[t.entry_price for t in short_entries],
                    mode="markers",
                    name="Short Entry",
                    marker=dict(symbol="triangle-down", color="#F44336", size=10),
                ),
                row=1,
                col=1,
            )

    eq = result.equity_curve
    if len(eq) > 0:
        fig.add_trace(
            go.Scatter(
                x=eq.index,
                y=eq.values,
                mode="lines",
                name="Equity",
                line=dict(color="#2196F3"),
            ),
            row=2,
            col=1,
        )
        annotation_text = (
            f"Win: {result.win_rate:.1%}  CAGR: {result.cagr:.1%}  "
            f"MDD: {result.mdd:.1%}  Trades: {result.total_trades}  "
            f"Avg Hold: {result.avg_hold_days:.1f}d"
        )
        fig.add_annotation(
            xref="x2",
            yref="y2",
            x=eq.index[len(eq) // 2],
            y=float(eq.max()),
            text=annotation_text,
            showarrow=False,
            font=dict(size=10),
            bgcolor="rgba(255,255,255,0.8)",
        )

    if result.trades:
        reason_counts: dict[str, int] = {}
        for t in result.trades:
            reason_counts[t.exit_reason] = reason_counts.get(t.exit_reason, 0) + 1
        reasons = list(reason_counts.keys())
        counts = [reason_counts[r] for r in reasons]
        colors_map = {"target": "#4CAF50", "stop": "#F44336", "max_hold": "#FF9800"}
        bar_colors = [colors_map.get(r, "#9E9E9E") for r in reasons]
        fig.add_trace(
            go.Bar(
                x=reasons,
                y=counts,
                marker_color=bar_colors,
                showlegend=False,
                name="exit reasons",
            ),
            row=3,
            col=1,
        )

    fig.update_layout(
        title="Margin Divergence Strategy (Proxy Indicators) — Backtest Performance",
        height=950,
        template="plotly_white",
        font=dict(size=11),
    )

    if output_path:
        fig.write_html(output_path)

    return fig


def run_full_analysis(
    ticker: str = TICKER,
    years: int = 5,
    output_html: Optional[str] = None,
) -> dict:
    price_df = fetch_price(ticker, years=years + 1)
    result = run_backtest(price_df)
    fig = build_performance_chart(result, price_df, output_path=output_html)

    summary = {
        "ticker": ticker,
        "total_trades": result.total_trades,
        "win_rate": round(result.win_rate, 4),
        "cagr": round(result.cagr, 4),
        "mdd": round(result.mdd, 4),
        "avg_hold_days": result.avg_hold_days,
    }

    return {
        "summary": summary,
        "result": result,
        "fig": fig,
    }


if __name__ == "__main__":
    out_dir = Path(__file__).parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    output = run_full_analysis(output_html=str(out_dir / "margin_divergence_performance.html"))
    print("\n=== Margin Divergence Strategy Summary ===")
    for k, v in output["summary"].items():
        print(f"  {k}: {v}")
