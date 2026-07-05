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
GAP_THRESHOLDS: dict[str, tuple[float, float]] = {
    "small":  (0.003, 0.01),
    "medium": (0.01,  0.02),
    "large":  (0.02,  float("inf")),
}
MAX_FILL_DAYS = 20
STOP_LOSS_MULT = 0.5


@dataclass
class GapEvent:
    ticker: str
    gap_date: str
    direction: str
    gap_pct: float
    grade: str
    open_price: float
    prev_close: float
    filled: bool
    fill_days: Optional[int]
    fill_return: Optional[float]


@dataclass
class GapStats:
    ticker: str
    grade: str
    direction: str
    total: int
    filled: int
    fill_rate: float
    avg_fill_days: Optional[float]
    avg_return: Optional[float]


def _classify_gap(gap_pct: float) -> str:
    abs_gap = abs(gap_pct)
    for grade, (lo, hi) in GAP_THRESHOLDS.items():
        if lo <= abs_gap < hi:
            return grade
    return "large"


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


def detect_gaps(df: pd.DataFrame, ticker: str) -> list[GapEvent]:
    events: list[GapEvent] = []
    closes = df["Close"].values
    opens = df["Open"].values
    dates = df.index.strftime("%Y-%m-%d").tolist()

    for i in range(1, len(df)):
        prev_close = float(closes[i - 1])
        open_price = float(opens[i])
        if prev_close <= 0:
            continue
        gap_pct = (open_price - prev_close) / prev_close
        if abs(gap_pct) < GAP_THRESHOLDS["small"][0]:
            continue

        direction = "up" if gap_pct > 0 else "down"
        grade = _classify_gap(gap_pct)

        filled = False
        fill_days: Optional[int] = None
        fill_return: Optional[float] = None

        for j in range(i, min(i + MAX_FILL_DAYS + 1, len(df))):
            day_close = float(closes[j])
            if direction == "up" and day_close <= prev_close:
                filled = True
                fill_days = j - i
                fill_return = (day_close - open_price) / open_price
                break
            elif direction == "down" and day_close >= prev_close:
                filled = True
                fill_days = j - i
                fill_return = (day_close - open_price) / open_price
                break

        events.append(GapEvent(
            ticker=ticker,
            gap_date=dates[i],
            direction=direction,
            gap_pct=gap_pct,
            grade=grade,
            open_price=open_price,
            prev_close=prev_close,
            filled=filled,
            fill_days=fill_days,
            fill_return=fill_return,
        ))

    return events


def compute_stats(events: list[GapEvent]) -> list[GapStats]:
    from itertools import groupby

    stats: list[GapStats] = []
    key_fn = lambda e: (e.ticker, e.grade, e.direction)
    sorted_events = sorted(events, key=key_fn)

    for (ticker, grade, direction), group in groupby(sorted_events, key=key_fn):
        grp = list(group)
        total = len(grp)
        filled_grp = [e for e in grp if e.filled]
        filled = len(filled_grp)
        fill_rate = filled / total if total > 0 else 0.0
        avg_fill_days = (
            sum(e.fill_days for e in filled_grp if e.fill_days is not None) / len(filled_grp)
            if filled_grp else None
        )
        avg_return = (
            sum(e.fill_return for e in filled_grp if e.fill_return is not None) / len(filled_grp)
            if filled_grp else None
        )
        stats.append(GapStats(
            ticker=ticker,
            grade=grade,
            direction=direction,
            total=total,
            filled=filled,
            fill_rate=fill_rate,
            avg_fill_days=avg_fill_days,
            avg_return=avg_return,
        ))

    return stats


def gap_signal(ticker: str, query_date: str) -> dict:
    df = fetch_data(ticker, years=1)
    date_idx = df.index.strftime("%Y-%m-%d").tolist()
    if query_date not in date_idx:
        return {}
    i = date_idx.index(query_date)
    if i == 0:
        return {}

    prev_close = float(df["Close"].iloc[i - 1])
    open_price = float(df["Open"].iloc[i])
    if prev_close <= 0:
        return {}

    gap_pct = (open_price - prev_close) / prev_close
    if abs(gap_pct) < GAP_THRESHOLDS["small"][0]:
        return {}

    direction = "up" if gap_pct > 0 else "down"
    grade = _classify_gap(gap_pct)

    if direction == "down":
        target = prev_close
        stop_loss = open_price * (1 - abs(gap_pct) * STOP_LOSS_MULT)
    else:
        target = prev_close
        stop_loss = open_price * (1 + abs(gap_pct) * STOP_LOSS_MULT)

    return {
        "ticker": ticker,
        "date": query_date,
        "direction": direction,
        "grade": grade,
        "gap_pct": round(gap_pct, 6),
        "entry_price": round(open_price, 4),
        "target": round(target, 4),
        "stop_loss": round(stop_loss, 4),
    }


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


def _run_backtest(
    df: pd.DataFrame,
    events: list[GapEvent],
    ticker: str,
    initial_capital: float = 1_000_000.0,
    trade_cost: float = 0.001425,
) -> BacktestResult:
    date_to_idx: dict[str, int] = {d: i for i, d in enumerate(df.index.strftime("%Y-%m-%d").tolist())}
    closes = df["Close"].values

    capital = initial_capital
    equity_dates: list[str] = []
    equity_vals: list[float] = []
    trades: list[dict] = []

    active_events = [e for e in events if e.ticker == ticker]

    for event in active_events:
        i = date_to_idx.get(event.gap_date)
        if i is None:
            continue

        position_size = capital * 0.10
        entry = event.open_price * (1 + trade_cost)
        shares = position_size / entry

        if event.filled and event.fill_days is not None:
            exit_idx = min(i + event.fill_days, len(closes) - 1)
        else:
            exit_idx = min(i + MAX_FILL_DAYS, len(closes) - 1)

        exit_price = float(closes[exit_idx]) * (1 - trade_cost)
        if event.direction == "down":
            pnl = shares * (exit_price - entry)
        else:
            pnl = shares * (entry - exit_price)
        capital += pnl

        equity_dates.append(event.gap_date)
        equity_vals.append(capital)

        trades.append({
            "date": event.gap_date,
            "direction": event.direction,
            "grade": event.grade,
            "gap_pct": event.gap_pct,
            "entry": round(entry, 4),
            "exit": round(exit_price, 4),
            "pnl": round(pnl, 2),
            "filled": event.filled,
        })

    equity = pd.Series(equity_vals, index=pd.to_datetime(equity_dates), name=ticker)
    equity.sort_index(inplace=True)

    if len(trades) == 0:
        return BacktestResult(ticker=ticker, equity_curve=equity)

    profitable = [t for t in trades if t["pnl"] > 0]
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


def build_performance_chart(
    results: list[BacktestResult],
    stats: list[GapStats],
    output_path: Optional[str] = None,
) -> go.Figure:
    n_tickers = len(results)
    fig = make_subplots(
        rows=3,
        cols=n_tickers,
        subplot_titles=[
            f"{r.ticker} Equity" for r in results
        ] + [
            f"{r.ticker} Fill Rate" for r in results
        ] + [
            f"{r.ticker} Avg Return" for r in results
        ],
        vertical_spacing=0.12,
        horizontal_spacing=0.08,
    )

    grade_colors = {"small": "#4CAF50", "medium": "#FF9800", "large": "#F44336"}

    for col_idx, result in enumerate(results, start=1):
        eq = result.equity_curve
        if len(eq) > 0:
            fig.add_trace(
                go.Scatter(
                    x=eq.index,
                    y=eq.values,
                    mode="lines",
                    name=f"{result.ticker} Equity",
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

        ticker_stats = [s for s in stats if s.ticker == result.ticker]
        grades = ["small", "medium", "large"]
        for direction in ["up", "down"]:
            fill_rates = []
            labels = []
            for grade in grades:
                matching = [s for s in ticker_stats if s.grade == grade and s.direction == direction]
                fill_rates.append(matching[0].fill_rate if matching else 0.0)
                labels.append(f"{grade[0].upper()} {direction}")

            fig.add_trace(
                go.Bar(
                    x=grades,
                    y=fill_rates,
                    name=f"{direction} fill rate",
                    marker_color=[grade_colors[g] for g in grades],
                    opacity=0.8 if direction == "up" else 0.5,
                    showlegend=False,
                ),
                row=2,
                col=col_idx,
            )

        avg_returns: list[float] = []
        avg_labels: list[str] = []
        for grade in grades:
            for direction in ["up", "down"]:
                matching = [s for s in ticker_stats if s.grade == grade and s.direction == direction]
                if matching and matching[0].avg_return is not None:
                    avg_returns.append(matching[0].avg_return * 100)
                    avg_labels.append(f"{grade[0].upper()}-{direction[0].upper()}")

        if avg_returns:
            bar_colors = ["#4CAF50" if v >= 0 else "#F44336" for v in avg_returns]
            fig.add_trace(
                go.Bar(
                    x=avg_labels,
                    y=avg_returns,
                    marker_color=bar_colors,
                    showlegend=False,
                    name="avg return %",
                ),
                row=3,
                col=col_idx,
            )

    fig.update_layout(
        title="Gap Fill Strategy — Backtest Performance",
        height=900,
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
    all_events: list[GapEvent] = []
    all_stats: list[GapStats] = []
    results: list[BacktestResult] = []
    dfs: dict[str, pd.DataFrame] = {}

    for ticker in tickers:
        df = fetch_data(ticker, years=years)
        dfs[ticker] = df
        events = detect_gaps(df, ticker)
        all_events.extend(events)
        stats = compute_stats(events)
        all_stats.extend(stats)
        result = _run_backtest(df, events, ticker)
        results.append(result)

    fig = build_performance_chart(results, all_stats, output_path=output_html)

    summary: dict[str, dict] = {}
    for result in results:
        summary[result.ticker] = {
            "total_trades": result.total_trades,
            "win_rate": round(result.win_rate, 4),
            "cagr": round(result.cagr, 4),
            "mdd": round(result.mdd, 4),
        }

    stats_df = pd.DataFrame([
        {
            "ticker": s.ticker,
            "grade": s.grade,
            "direction": s.direction,
            "total": s.total,
            "filled": s.filled,
            "fill_rate": round(s.fill_rate, 4),
            "avg_fill_days": round(s.avg_fill_days, 2) if s.avg_fill_days else None,
            "avg_return_pct": round(s.avg_return * 100, 4) if s.avg_return else None,
        }
        for s in all_stats
    ])

    return {
        "summary": summary,
        "stats_df": stats_df,
        "events": all_events,
        "results": results,
        "fig": fig,
    }


if __name__ == "__main__":
    out_dir = Path(__file__).parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    output = run_full_analysis(output_html=str(out_dir / "gap_fill_performance.html"))
    print("\n=== Gap Fill Strategy Summary ===")
    for ticker, metrics in output["summary"].items():
        print(f"\n{ticker}:")
        for k, v in metrics.items():
            print(f"  {k}: {v}")
    print("\n=== Fill Rate Stats ===")
    print(output["stats_df"].to_string(index=False))
