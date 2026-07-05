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
TREND_THRESHOLD: float = 0.005
STOP_LOSS_PCT: float = 0.015
VOLUME_MULT: float = 0.8
VOLUME_WINDOW: int = 20


@dataclass
class TradeRecord:
    entry_date: str
    exit_date: str
    ticker: str
    direction: str
    day_return_pct: float
    entry_price: float
    exit_price: float
    trade_return: float
    stopped_out: bool


@dataclass
class BacktestResult:
    ticker: str
    equity_curve: pd.Series
    trades: list[TradeRecord] = field(default_factory=list)
    win_rate: float = 0.0
    cagr: float = 0.0
    mdd: float = 0.0
    total_trades: int = 0
    profitable_trades: int = 0
    avg_next_day_return: float = 0.0


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


def _volume_filter(df: pd.DataFrame, i: int) -> bool:
    if i < VOLUME_WINDOW:
        return False
    avg_vol = df["Volume"].iloc[i - VOLUME_WINDOW:i].mean()
    return float(df["Volume"].iloc[i]) >= avg_vol * VOLUME_MULT


def _run_backtest(
    df_0050: pd.DataFrame,
    df_631l: pd.DataFrame,
    df_632r: pd.DataFrame,
    initial_capital: float = 1_000_000.0,
    trade_cost: float = 0.001425,
) -> BacktestResult:
    closes_0050 = df_0050["Close"].values
    opens_0050 = df_0050["Open"].values
    dates_0050 = df_0050.index.strftime("%Y-%m-%d").tolist()

    date_to_idx_631l: dict[str, int] = {d: i for i, d in enumerate(df_631l.index.strftime("%Y-%m-%d").tolist())}
    date_to_idx_632r: dict[str, int] = {d: i for i, d in enumerate(df_632r.index.strftime("%Y-%m-%d").tolist())}

    capital = initial_capital
    equity_dates: list[str] = []
    equity_vals: list[float] = []
    trades: list[TradeRecord] = []

    for i in range(VOLUME_WINDOW, len(df_0050) - 1):
        prev_close = float(closes_0050[i - 1])
        today_close = float(closes_0050[i])
        if prev_close <= 0:
            continue

        day_ret = (today_close - prev_close) / prev_close

        if abs(day_ret) <= TREND_THRESHOLD:
            continue

        if not _volume_filter(df_0050, i):
            continue

        entry_date = dates_0050[i]
        next_date = dates_0050[i + 1]

        if day_ret > 0:
            direction = "up"
            target_df = df_631l
            target_idx = date_to_idx_631l.get(entry_date)
            next_idx = date_to_idx_631l.get(next_date)
        else:
            direction = "down"
            target_df = df_632r
            target_idx = date_to_idx_632r.get(entry_date)
            next_idx = date_to_idx_632r.get(next_date)

        if target_idx is None or next_idx is None:
            continue

        entry_price = float(target_df["Close"].iloc[target_idx]) * (1 + trade_cost)
        next_open = float(target_df["Open"].iloc[next_idx])

        stop_price = entry_price * (1 - STOP_LOSS_PCT)
        stopped_out = next_open <= stop_price

        if stopped_out:
            exit_price = stop_price * (1 - trade_cost)
        else:
            exit_price = next_open * (1 - trade_cost)

        trade_ret = (exit_price - entry_price) / entry_price
        shares = capital / entry_price
        capital += shares * (exit_price - entry_price)

        equity_dates.append(next_date)
        equity_vals.append(capital)

        trades.append(TradeRecord(
            entry_date=entry_date,
            exit_date=next_date,
            ticker="00631L.TW" if direction == "up" else "00632R.TW",
            direction=direction,
            day_return_pct=day_ret,
            entry_price=round(entry_price, 4),
            exit_price=round(exit_price, 4),
            trade_return=trade_ret,
            stopped_out=stopped_out,
        ))

    equity = pd.Series(equity_vals, index=pd.to_datetime(equity_dates), name="ClosingTrend")
    equity.sort_index(inplace=True)

    if not trades:
        return BacktestResult(ticker="0050.TW", equity_curve=equity)

    profitable = [t for t in trades if t.trade_return > 0]
    win_rate = len(profitable) / len(trades)
    avg_next_day = sum(t.trade_return for t in trades) / len(trades)

    if len(equity) >= 2:
        years = (equity.index[-1] - equity.index[0]).days / 365.25
        cagr = (equity.iloc[-1] / initial_capital) ** (1 / years) - 1 if years > 0 else 0.0
    else:
        cagr = 0.0

    rolling_max = equity.cummax()
    drawdown = (equity - rolling_max) / rolling_max
    mdd = float(drawdown.min())

    return BacktestResult(
        ticker="0050.TW",
        equity_curve=equity,
        trades=trades,
        win_rate=win_rate,
        cagr=cagr,
        mdd=mdd,
        total_trades=len(trades),
        profitable_trades=len(profitable),
        avg_next_day_return=avg_next_day,
    )


def closing_trend_signal(ticker: str, query_date: str) -> dict:
    df = fetch_data(ticker, years=1)
    dates = df.index.strftime("%Y-%m-%d").tolist()
    if query_date not in dates:
        return {}
    i = dates.index(query_date)
    if i == 0:
        return {}

    prev_close = float(df["Close"].iloc[i - 1])
    today_close = float(df["Close"].iloc[i])
    if prev_close <= 0:
        return {}

    day_ret = (today_close - prev_close) / prev_close

    if abs(day_ret) <= TREND_THRESHOLD:
        return {}

    if not _volume_filter(df, i):
        return {}

    direction = "up" if day_ret > 0 else "down"
    trade_ticker = "00631L.TW" if direction == "up" else "00632R.TW"
    stop_loss = today_close * (1 - STOP_LOSS_PCT)

    return {
        "signal_ticker": ticker,
        "date": query_date,
        "direction": direction,
        "day_return_pct": round(day_ret, 6),
        "trade_ticker": trade_ticker,
        "entry_price": round(today_close, 4),
        "stop_loss": round(stop_loss, 4),
        "exit_rule": "next day open (30min after open)",
    }


def build_performance_chart(
    result: BacktestResult,
    output_path: Optional[str] = None,
) -> go.Figure:
    fig = make_subplots(
        rows=2,
        cols=2,
        subplot_titles=[
            "Equity Curve",
            "Trade Return Distribution",
            "Win Rate by Direction",
            "Monthly PnL",
        ],
        vertical_spacing=0.14,
        horizontal_spacing=0.1,
    )

    eq = result.equity_curve
    if len(eq) > 0:
        annotation_text = (
            f"Win: {result.win_rate:.1%}  |  "
            f"CAGR: {result.cagr:.1%}  |  "
            f"MDD: {result.mdd:.1%}  |  "
            f"Trades: {result.total_trades}  |  "
            f"Avg Next-Day: {result.avg_next_day_return:.2%}"
        )
        fig.add_trace(
            go.Scatter(
                x=eq.index,
                y=eq.values,
                mode="lines",
                name="Equity",
                line=dict(color="#2196F3", width=2),
            ),
            row=1, col=1,
        )
        fig.add_annotation(
            xref="paper", yref="paper",
            x=0.0, y=1.08,
            text=annotation_text,
            showarrow=False,
            font=dict(size=11),
            bgcolor="rgba(255,255,255,0.8)",
        )

    if result.trades:
        returns = [t.trade_return * 100 for t in result.trades]
        colors = ["#4CAF50" if r >= 0 else "#F44336" for r in returns]
        fig.add_trace(
            go.Histogram(
                x=returns,
                nbinsx=30,
                marker_color="#2196F3",
                name="Trade Returns %",
                showlegend=False,
            ),
            row=1, col=2,
        )

        up_trades = [t for t in result.trades if t.direction == "up"]
        down_trades = [t for t in result.trades if t.direction == "down"]
        directions = []
        win_rates = []
        counts = []
        for label, grp in [("up→00631L", up_trades), ("down→00632R", down_trades)]:
            if grp:
                directions.append(label)
                win_rates.append(len([t for t in grp if t.trade_return > 0]) / len(grp) * 100)
                counts.append(len(grp))

        fig.add_trace(
            go.Bar(
                x=directions,
                y=win_rates,
                text=[f"{w:.1f}%<br>n={c}" for w, c in zip(win_rates, counts)],
                textposition="auto",
                marker_color=["#4CAF50", "#FF9800"],
                showlegend=False,
            ),
            row=2, col=1,
        )

        if len(eq) > 0:
            monthly = eq.resample("ME").last().pct_change().dropna() * 100
            bar_colors = ["#4CAF50" if v >= 0 else "#F44336" for v in monthly.values]
            fig.add_trace(
                go.Bar(
                    x=monthly.index,
                    y=monthly.values,
                    marker_color=bar_colors,
                    showlegend=False,
                    name="Monthly PnL %",
                ),
                row=2, col=2,
            )

    fig.update_layout(
        title="Closing Trend Strategy — Backtest Performance",
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
    df_0050 = fetch_data("0050.TW", years=years)
    df_631l = fetch_data("00631L.TW", years=years)
    df_632r = fetch_data("00632R.TW", years=years)

    result = _run_backtest(df_0050, df_631l, df_632r)
    fig = build_performance_chart(result, output_path=output_html)

    summary = {
        "total_trades": result.total_trades,
        "win_rate": round(result.win_rate, 4),
        "cagr": round(result.cagr, 4),
        "mdd": round(result.mdd, 4),
        "avg_next_day_return": round(result.avg_next_day_return, 4),
    }

    trades_df = pd.DataFrame([
        {
            "entry_date": t.entry_date,
            "exit_date": t.exit_date,
            "ticker": t.ticker,
            "direction": t.direction,
            "day_return_pct": round(t.day_return_pct * 100, 3),
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "trade_return_pct": round(t.trade_return * 100, 3),
            "stopped_out": t.stopped_out,
        }
        for t in result.trades
    ])

    return {
        "summary": summary,
        "trades_df": trades_df,
        "result": result,
        "fig": fig,
    }


if __name__ == "__main__":
    out_dir = Path(__file__).parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    output = run_full_analysis(output_html=str(out_dir / "closing_trend_performance.html"))
    print("\n=== Closing Trend Strategy Summary ===")
    for k, v in output["summary"].items():
        print(f"  {k}: {v}")
    print(f"\n=== Recent Trades (last 10) ===")
    df = output["trades_df"]
    if len(df) > 0:
        print(df.tail(10).to_string(index=False))
