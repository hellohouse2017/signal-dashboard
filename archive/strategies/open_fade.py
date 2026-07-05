#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import yfinance as yf
from plotly.subplots import make_subplots

TICKERS: list[str] = ["0050.TW", "00631L.TW", "00632R.TW"]
GAP_UP_THRESHOLD: float = 1.003
GAP_DOWN_THRESHOLD: float = 0.997
PREV_DAY_FILTER: float = 0.005
TARGET_PCT: float = 0.015
STOP_LOSS_PCT: float = 0.01
TRADE_COST: float = 0.001425


@dataclass
class FadeTrade:
    ticker: str
    trade_date: str
    gap_direction: str
    prev_close: float
    open_price: float
    gap_pct: float
    entry_price: float
    exit_price: float
    exit_reason: str
    pnl_pct: float


@dataclass
class BacktestResult:
    ticker: str
    equity_curve: pd.Series
    trades: list[FadeTrade] = field(default_factory=list)
    win_rate: float = 0.0
    cagr: float = 0.0
    mdd: float = 0.0
    total_trades: int = 0
    profitable_trades: int = 0


def fetch_data(ticker: str, years: int = 5) -> pd.DataFrame:
    end = date.today()
    start = end - timedelta(days=years * 365 + 30)
    df = yf.download(ticker, start=str(start), end=str(end), auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.sort_index(inplace=True)
    return df


def _simulate_intraday(
    open_price: float,
    high: float,
    low: float,
    close: float,
    gap_direction: str,
) -> tuple[float, str]:
    if gap_direction == "up":
        entry = open_price
        target = entry * (1 - TARGET_PCT)
        stop = entry * (1 + STOP_LOSS_PCT)
        if low <= target and high >= stop:
            exit_price = target if low <= target else stop
            exit_reason = "target" if low <= target else "stop"
        elif low <= target:
            exit_price = target
            exit_reason = "target"
        elif high >= stop:
            exit_price = stop
            exit_reason = "stop"
        else:
            exit_price = close
            exit_reason = "eod"
    else:
        entry = open_price
        target = entry * (1 + TARGET_PCT)
        stop = entry * (1 - STOP_LOSS_PCT)
        if high >= target and low <= stop:
            exit_price = target if high >= target else stop
            exit_reason = "target" if high >= target else "stop"
        elif high >= target:
            exit_price = target
            exit_reason = "target"
        elif low <= stop:
            exit_price = stop
            exit_reason = "stop"
        else:
            exit_price = close
            exit_reason = "eod"

    return exit_price, exit_reason


def run_backtest(
    df: pd.DataFrame,
    ticker: str,
    initial_capital: float = 1_000_000.0,
) -> BacktestResult:
    closes = df["Close"].values
    opens = df["Open"].values
    highs = df["High"].values
    lows = df["Low"].values
    dates = df.index.strftime("%Y-%m-%d").tolist()

    capital = initial_capital
    equity_dates: list[str] = []
    equity_vals: list[float] = []
    trades: list[FadeTrade] = []

    for i in range(1, len(df)):
        prev_close = float(closes[i - 1])
        open_price = float(opens[i])
        high = float(highs[i])
        low = float(lows[i])
        close = float(closes[i])

        if prev_close <= 0:
            continue

        prev_day_change = abs((prev_close - float(closes[i - 2])) / float(closes[i - 2])) if i >= 2 else 0.0
        if prev_day_change < PREV_DAY_FILTER:
            continue

        gap_pct = (open_price - prev_close) / prev_close

        if open_price > prev_close * GAP_UP_THRESHOLD:
            gap_direction = "up"
        elif open_price < prev_close * GAP_DOWN_THRESHOLD:
            gap_direction = "down"
        else:
            continue

        # 開盤跳空假方向：開盤反向進場，隔日開盤出場
        # gap_up → 做空 → 進場用開盤賣，出場用隔日開盤買回
        # gap_down → 做多 → 進場用開盤買，出場用隔日開盤賣
        if i + 1 >= len(df):
            continue
        next_open = float(opens[i + 1])
        if gap_direction == "down":
            entry = open_price * (1 + TRADE_COST)
            exit_price = next_open * (1 - TRADE_COST)
            pnl_pct = (exit_price - entry) / entry
        else:
            entry = open_price * (1 - TRADE_COST)
            exit_price = next_open * (1 + TRADE_COST)
            pnl_pct = (entry - exit_price) / entry
        exit_reason = "next_open"
        pnl = capital * pnl_pct * 0.10
        capital += pnl

        equity_dates.append(dates[i])
        equity_vals.append(capital)

        trades.append(FadeTrade(
            ticker=ticker,
            trade_date=dates[i],
            gap_direction=gap_direction,
            prev_close=prev_close,
            open_price=open_price,
            gap_pct=round(gap_pct, 6),
            entry_price=round(entry, 4),
            exit_price=round(exit_price, 4),
            exit_reason=exit_reason,
            pnl_pct=round(pnl_pct, 6),
        ))

    equity = pd.Series(equity_vals, index=pd.to_datetime(equity_dates), name=ticker)
    equity.sort_index(inplace=True)

    if len(trades) == 0:
        return BacktestResult(ticker=ticker, equity_curve=equity)

    profitable = [t for t in trades if t.pnl_pct > 0]
    win_rate = len(profitable) / len(trades)

    if len(equity) >= 2:
        years = (equity.index[-1] - equity.index[0]).days / 365.25
        cagr = (equity.iloc[-1] / initial_capital) ** (1 / years) - 1 if years > 0 else 0.0
    else:
        cagr = 0.0

    rolling_max = equity.cummax()
    drawdown = (equity - rolling_max) / rolling_max
    mdd = float(drawdown.min())

    return BacktestResult(
        ticker=ticker,
        equity_curve=equity,
        trades=trades,
        win_rate=win_rate,
        cagr=cagr,
        mdd=mdd,
        total_trades=len(trades),
        profitable_trades=len(profitable),
    )


def open_fade_signal(ticker: str, query_date: str) -> dict:
    df = fetch_data(ticker, years=1)
    date_list = df.index.strftime("%Y-%m-%d").tolist()
    if query_date not in date_list:
        return {}
    i = date_list.index(query_date)
    if i < 2:
        return {}

    prev_close = float(df["Close"].iloc[i - 1])
    prev2_close = float(df["Close"].iloc[i - 2])
    open_price = float(df["Open"].iloc[i])

    if prev_close <= 0 or prev2_close <= 0:
        return {}

    prev_day_change = abs((prev_close - prev2_close) / prev2_close)
    if prev_day_change < PREV_DAY_FILTER:
        return {}

    gap_pct = (open_price - prev_close) / prev_close

    if open_price > prev_close * GAP_UP_THRESHOLD:
        gap_direction = "up"
        action = "short (buy 00632R or short 00631L)"
        target = round(open_price * (1 - TARGET_PCT), 4)
        stop = round(open_price * (1 + STOP_LOSS_PCT), 4)
    elif open_price < prev_close * GAP_DOWN_THRESHOLD:
        gap_direction = "down"
        action = "long (buy 00631L or 0050)"
        target = round(open_price * (1 + TARGET_PCT), 4)
        stop = round(open_price * (1 - STOP_LOSS_PCT), 4)
    else:
        return {}

    return {
        "ticker": ticker,
        "date": query_date,
        "gap_direction": gap_direction,
        "gap_pct": round(gap_pct, 6),
        "action": action,
        "entry_price": round(open_price, 4),
        "target": target,
        "stop_loss": stop,
        "prev_day_change_pct": round(prev_day_change, 6),
    }


def build_performance_chart(
    results: list[BacktestResult],
    output_path: Optional[str] = None,
) -> go.Figure:
    n = len(results)
    fig = make_subplots(
        rows=2,
        cols=n,
        subplot_titles=[f"{r.ticker} Equity" for r in results]
        + [f"{r.ticker} Exit Reasons" for r in results],
        vertical_spacing=0.15,
        horizontal_spacing=0.08,
    )

    for col_idx, result in enumerate(results, start=1):
        eq = result.equity_curve
        if len(eq) > 0:
            fig.add_trace(
                go.Scatter(
                    x=eq.index,
                    y=eq.values,
                    mode="lines",
                    name=f"{result.ticker}",
                    line=dict(color="#2196F3"),
                    showlegend=(col_idx == 1),
                ),
                row=1,
                col=col_idx,
            )
            annotation_text = (
                f"Win: {result.win_rate:.1%}<br>"
                f"CAGR: {result.cagr:.1%}<br>"
                f"MDD: {result.mdd:.1%}<br>"
                f"Trades: {result.total_trades}"
            )
            fig.add_annotation(
                xref=f"x{col_idx}",
                yref=f"y{col_idx}",
                x=eq.index[len(eq) // 2],
                y=float(eq.max()),
                text=annotation_text,
                showarrow=False,
                font=dict(size=10),
                bgcolor="rgba(255,255,255,0.7)",
                row=1,
                col=col_idx,
            )

        if result.trades:
            reason_counts: dict[str, int] = {}
            for t in result.trades:
                reason_counts[t.exit_reason] = reason_counts.get(t.exit_reason, 0) + 1
            reasons = list(reason_counts.keys())
            counts = [reason_counts[r] for r in reasons]
            colors = {"target": "#4CAF50", "stop": "#F44336", "eod": "#FF9800"}
            bar_colors = [colors.get(r, "#9E9E9E") for r in reasons]
            fig.add_trace(
                go.Bar(
                    x=reasons,
                    y=counts,
                    marker_color=bar_colors,
                    showlegend=False,
                    name="exit reasons",
                ),
                row=2,
                col=col_idx,
            )

    fig.update_layout(
        title="Open Fade Strategy — Backtest Performance",
        height=700,
        template="plotly_white",
        font=dict(size=11),
    )

    if output_path:
        fig.write_html(output_path)

    return fig


def run_full_analysis(
    tickers: list[str] = TICKERS,
    years: int = 5,
    output_html: Optional[str] = None,
) -> dict:
    results: list[BacktestResult] = []

    for ticker in tickers:
        df = fetch_data(ticker, years=years)
        result = run_backtest(df, ticker)
        results.append(result)

    fig = build_performance_chart(results, output_path=output_html)

    summary: dict[str, dict] = {}
    for result in results:
        summary[result.ticker] = {
            "total_trades": result.total_trades,
            "win_rate": round(result.win_rate, 4),
            "cagr": round(result.cagr, 4),
            "mdd": round(result.mdd, 4),
        }

    return {
        "summary": summary,
        "results": results,
        "fig": fig,
    }


if __name__ == "__main__":
    out_dir = Path(__file__).parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    output = run_full_analysis(output_html=str(out_dir / "open_fade_performance.html"))
    print("\n=== Open Fade Strategy Summary ===")
    for ticker, metrics in output["summary"].items():
        print(f"\n{ticker}:")
        for k, v in metrics.items():
            print(f"  {k}: {v}")
