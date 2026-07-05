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
VOLUME_MULT: float = 2.0
VOL_MA_WINDOW: int = 20
PROFIT_TARGET: float = 0.02
STOP_LOSS: float = 0.015
MAX_HOLD_DAYS: int = 5
INITIAL_CAPITAL: float = 1_000_000.0
TRADE_COST: float = 0.001425


@dataclass
class ClimaxEvent:
    ticker: str
    signal_date: str
    direction: str
    volume_ratio: float
    signal_close: float
    signal_open: float
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
    trades: list[dict] = field(default_factory=list)
    win_rate: float = 0.0
    cagr: float = 0.0
    mdd: float = 0.0
    total_trades: int = 0
    profitable_trades: int = 0
    avg_hold_days: float = 0.0
    profit_factor: float = 0.0


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


def detect_climax_events(df: pd.DataFrame, ticker: str) -> list[ClimaxEvent]:
    vol_ma = df["Volume"].rolling(VOL_MA_WINDOW).mean()
    events: list[ClimaxEvent] = []
    dates = df.index.strftime("%Y-%m-%d").tolist()
    closes = df["Close"].values
    opens = df["Open"].values
    volumes = df["Volume"].values

    for i in range(VOL_MA_WINDOW, len(df) - 1):
        avg_vol = float(vol_ma.iloc[i])
        if avg_vol <= 0:
            continue
        vol_ratio = float(volumes[i]) / avg_vol
        if vol_ratio < VOLUME_MULT:
            continue

        c = float(closes[i])
        o = float(opens[i])
        if c == o:
            continue

        direction = "long" if c < o else "short"

        entry_idx = i + 1
        entry_price = float(opens[entry_idx]) * (1 + TRADE_COST if direction == "long" else 1 - TRADE_COST)

        exit_price = entry_price
        exit_reason = "max_hold"
        exit_date = dates[min(entry_idx + MAX_HOLD_DAYS, len(df) - 1)]
        hold_days = 0

        for j in range(entry_idx, min(entry_idx + MAX_HOLD_DAYS + 1, len(df))):
            day_close = float(closes[j])
            hold_days = j - entry_idx
            raw_exit = day_close * (1 - TRADE_COST if direction == "long" else 1 + TRADE_COST)
            pnl_pct_now = (
                (raw_exit - entry_price) / entry_price
                if direction == "long"
                else (entry_price - raw_exit) / entry_price
            )
            if direction == "long":
                if pnl_pct_now >= PROFIT_TARGET:
                    exit_price = raw_exit
                    exit_reason = "profit_target"
                    exit_date = dates[j]
                    break
                if pnl_pct_now <= -STOP_LOSS:
                    exit_price = raw_exit
                    exit_reason = "stop_loss"
                    exit_date = dates[j]
                    break
            else:
                if pnl_pct_now >= PROFIT_TARGET:
                    exit_price = raw_exit
                    exit_reason = "profit_target"
                    exit_date = dates[j]
                    break
                if pnl_pct_now <= -STOP_LOSS:
                    exit_price = raw_exit
                    exit_reason = "stop_loss"
                    exit_date = dates[j]
                    break
            if j == min(entry_idx + MAX_HOLD_DAYS, len(df) - 1):
                exit_price = raw_exit
                exit_date = dates[j]
                hold_days = j - entry_idx

        pnl_pct = (
            (exit_price - entry_price) / entry_price
            if direction == "long"
            else (entry_price - exit_price) / entry_price
        )

        events.append(ClimaxEvent(
            ticker=ticker,
            signal_date=dates[i],
            direction=direction,
            volume_ratio=round(vol_ratio, 4),
            signal_close=round(c, 4),
            signal_open=round(o, 4),
            entry_date=dates[entry_idx],
            entry_price=round(entry_price, 4),
            exit_date=exit_date,
            exit_price=round(exit_price, 4),
            exit_reason=exit_reason,
            hold_days=hold_days,
            pnl_pct=round(pnl_pct, 6),
        ))

    return events


def _run_backtest(
    events: list[ClimaxEvent],
    ticker: str,
) -> BacktestResult:
    ticker_events = [e for e in events if e.ticker == ticker]

    capital = INITIAL_CAPITAL
    equity_dates: list[str] = []
    equity_vals: list[float] = []
    trades: list[dict] = []

    for event in ticker_events:
        shares = capital / event.entry_price
        pnl = shares * (
            event.exit_price - event.entry_price
            if event.direction == "long"
            else event.entry_price - event.exit_price
        )
        capital += pnl

        equity_dates.append(event.entry_date)
        equity_vals.append(capital)
        trades.append({
            "signal_date": event.signal_date,
            "entry_date": event.entry_date,
            "exit_date": event.exit_date,
            "direction": event.direction,
            "volume_ratio": event.volume_ratio,
            "entry": event.entry_price,
            "exit": event.exit_price,
            "exit_reason": event.exit_reason,
            "hold_days": event.hold_days,
            "pnl": round(pnl, 2),
            "pnl_pct": event.pnl_pct,
        })

    equity = pd.Series(equity_vals, index=pd.to_datetime(equity_dates), name=ticker)
    equity.sort_index(inplace=True)

    if not trades:
        return BacktestResult(ticker=ticker, equity_curve=equity)

    profitable = [t for t in trades if t["pnl"] > 0]
    win_rate = len(profitable) / len(trades)

    if len(equity) >= 2:
        years = (equity.index[-1] - equity.index[0]).days / 365.25
        cagr = (equity.iloc[-1] / INITIAL_CAPITAL) ** (1 / years) - 1 if years > 0 else 0.0
    else:
        cagr = 0.0

    rolling_max = equity.cummax()
    drawdown = (equity - rolling_max) / rolling_max
    mdd = float(drawdown.min()) if len(equity) > 0 else 0.0

    avg_hold_days = sum(t["hold_days"] for t in trades) / len(trades)

    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return BacktestResult(
        ticker=ticker,
        equity_curve=equity,
        trades=trades,
        win_rate=win_rate,
        cagr=cagr,
        mdd=mdd,
        total_trades=len(trades),
        profitable_trades=len(profitable),
        avg_hold_days=round(avg_hold_days, 2),
        profit_factor=round(profit_factor, 4),
    )


def climax_signal(ticker: str, query_date: str) -> dict:
    df = fetch_data(ticker, years=1)
    dates = df.index.strftime("%Y-%m-%d").tolist()
    if query_date not in dates:
        return {}
    i = dates.index(query_date)
    if i < VOL_MA_WINDOW or i >= len(df) - 1:
        return {}

    avg_vol = float(df["Volume"].iloc[i - VOL_MA_WINDOW:i].mean())
    if avg_vol <= 0:
        return {}

    vol_ratio = float(df["Volume"].iloc[i]) / avg_vol
    if vol_ratio < VOLUME_MULT:
        return {}

    c = float(df["Close"].iloc[i])
    o = float(df["Open"].iloc[i])
    if c == o:
        return {}

    direction = "long" if c < o else "short"
    entry_price = float(df["Open"].iloc[i + 1])
    target = entry_price * (1 + PROFIT_TARGET) if direction == "long" else entry_price * (1 - PROFIT_TARGET)
    stop = entry_price * (1 - STOP_LOSS) if direction == "long" else entry_price * (1 + STOP_LOSS)

    return {
        "ticker": ticker,
        "signal_date": query_date,
        "entry_date": dates[i + 1],
        "direction": direction,
        "volume_ratio": round(vol_ratio, 4),
        "entry_price": round(entry_price, 4),
        "profit_target": round(target, 4),
        "stop_loss": round(stop, 4),
        "max_hold_days": MAX_HOLD_DAYS,
    }


def build_performance_chart(
    results: list[BacktestResult],
    output_path: Optional[str] = None,
) -> go.Figure:
    n = len(results)
    fig = make_subplots(
        rows=2,
        cols=n,
        subplot_titles=(
            [f"{r.ticker} Equity Curve" for r in results]
            + [f"{r.ticker} Trade PnL%" for r in results]
        ),
        vertical_spacing=0.14,
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
                    showlegend=True,
                ),
                row=1,
                col=col_idx,
            )
            annotation_text = (
                f"Win: {result.win_rate:.1%}<br>"
                f"CAGR: {result.cagr:.1%}<br>"
                f"MDD: {result.mdd:.1%}<br>"
                f"Trades: {result.total_trades}<br>"
                f"AvgHold: {result.avg_hold_days}d<br>"
                f"PF: {result.profit_factor}"
            )
            fig.add_annotation(
                xref=f"x{col_idx}",
                yref=f"y{col_idx}",
                x=eq.index[len(eq) // 2],
                y=float(eq.max()),
                text=annotation_text,
                showarrow=False,
                font=dict(size=10),
                bgcolor="rgba(255,255,255,0.8)",
                row=1,
                col=col_idx,
            )

        if result.trades:
            pnl_pcts = [t["pnl_pct"] * 100 for t in result.trades]
            bar_colors = ["#4CAF50" if v >= 0 else "#F44336" for v in pnl_pcts]
            trade_labels = [t["entry_date"] for t in result.trades]
            fig.add_trace(
                go.Bar(
                    x=trade_labels,
                    y=pnl_pcts,
                    marker_color=bar_colors,
                    showlegend=False,
                    name="pnl%",
                ),
                row=2,
                col=col_idx,
            )

    fig.update_layout(
        title="Climax Volume Strategy — Backtest Performance",
        height=800,
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
    all_events: list[ClimaxEvent] = []
    results: list[BacktestResult] = []

    for ticker in tickers:
        df = fetch_data(ticker, years=years)
        events = detect_climax_events(df, ticker)
        all_events.extend(events)
        result = _run_backtest(events, ticker)
        results.append(result)

    fig = build_performance_chart(results, output_path=output_html)

    summary: dict[str, dict] = {}
    for result in results:
        summary[result.ticker] = {
            "total_trades": result.total_trades,
            "win_rate": round(result.win_rate, 4),
            "cagr": round(result.cagr, 4),
            "mdd": round(result.mdd, 4),
            "avg_hold_days": result.avg_hold_days,
            "profit_factor": result.profit_factor,
        }

    events_df = pd.DataFrame([
        {
            "ticker": e.ticker,
            "signal_date": e.signal_date,
            "direction": e.direction,
            "volume_ratio": e.volume_ratio,
            "entry_date": e.entry_date,
            "exit_date": e.exit_date,
            "exit_reason": e.exit_reason,
            "hold_days": e.hold_days,
            "pnl_pct": e.pnl_pct,
        }
        for e in all_events
    ])

    return {
        "summary": summary,
        "events_df": events_df,
        "events": all_events,
        "results": results,
        "fig": fig,
    }


if __name__ == "__main__":
    out_dir = Path(__file__).parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    output = run_full_analysis(output_html=str(out_dir / "climax_volume_performance.html"))
    print("\n=== Climax Volume Strategy Summary ===")
    for ticker, metrics in output["summary"].items():
        print(f"\n{ticker}:")
        for k, v in metrics.items():
            print(f"  {k}: {v}")
    print("\n=== Event Log ===")
    if not output["events_df"].empty:
        print(output["events_df"].to_string(index=False))
