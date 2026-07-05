#!/usr/bin/env python3
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import yfinance as yf
from plotly.subplots import make_subplots

TICKER: str = "0050.TW"
VIX_JSON: Path = Path(__file__).parent.parent / "scanner" / "VIX歷史.json"
VIX_BUY_THRESHOLD: float = 25.0
VIX_SELL_THRESHOLD: float = 18.0
VIX_LOOKBACK_DAYS: int = 3
MAX_HOLD_DAYS: int = 20
STOP_LOSS_PCT: float = 0.05
TRADE_COST: float = 0.001425


@dataclass
class VixTrade:
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
    trades: list[VixTrade] = field(default_factory=list)
    win_rate: float = 0.0
    cagr: float = 0.0
    mdd: float = 0.0
    total_trades: int = 0
    profitable_trades: int = 0
    avg_hold_days: float = 0.0


def load_vix(path: Path) -> pd.Series:
    with open(path, "r") as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        series = pd.Series(raw, dtype=float)
        series.index = pd.to_datetime(series.index)
    else:
        df = pd.DataFrame(raw)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        series = df["close"].astype(float)
    series.sort_index(inplace=True)
    return series


def fetch_price(ticker: str, years: int = 6) -> pd.DataFrame:
    end = date.today()
    start = end - timedelta(days=years * 365 + 30)
    df = yf.download(ticker, start=str(start), end=str(end), auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.sort_index(inplace=True)
    return df


def run_backtest(
    price_df: pd.DataFrame,
    vix: pd.Series,
    initial_capital: float = 1_000_000.0,
) -> BacktestResult:
    price_df = price_df.copy()
    vix = vix.copy()

    common_start = max(price_df.index[0], vix.index[0])
    price_df = price_df[price_df.index >= common_start]
    vix = vix[vix.index >= common_start]

    price_dates = price_df.index
    closes = price_df["Close"].values
    opens = price_df["Open"].values
    lows = price_df["Low"].values

    capital = initial_capital
    equity_dates: list[str] = []
    equity_vals: list[float] = []
    trades: list[VixTrade] = []

    in_position = False
    entry_price = 0.0
    entry_idx = 0
    entry_date_str = ""

    for i, dt in enumerate(price_dates):
        vix_today = vix.get(dt)
        if vix_today is None:
            closest_idx = vix.index.searchsorted(dt) - 1
            if closest_idx < 0:
                continue
            vix_today = float(vix.iloc[closest_idx])
        else:
            vix_today = float(vix_today)

        lookback_dt = dt - timedelta(days=VIX_LOOKBACK_DAYS * 2)
        vix_past_idx = vix.index.searchsorted(lookback_dt)
        vix_past_idx = max(0, min(vix_past_idx, len(vix) - 1))
        vix_3d_ago = float(vix.iloc[vix_past_idx])

        if in_position:
            current_low = float(lows[i])
            current_close = float(closes[i])
            days_held = i - entry_idx
            exit_price_raw: Optional[float] = None
            exit_reason = ""

            stop_price = entry_price * (1 - STOP_LOSS_PCT)
            if current_low <= stop_price:
                exit_price_raw = stop_price
                exit_reason = "stop"
            elif vix_today < VIX_SELL_THRESHOLD and vix_today < vix_3d_ago:
                exit_price_raw = current_close
                exit_reason = "vix_exit"
            elif days_held >= MAX_HOLD_DAYS:
                exit_price_raw = current_close
                exit_reason = "max_hold"

            if exit_price_raw is not None:
                exit_price = exit_price_raw * (1 - TRADE_COST)
                pnl_pct = (exit_price - entry_price) / entry_price
                pnl = capital * pnl_pct
                capital += pnl

                equity_dates.append(dt.strftime("%Y-%m-%d"))
                equity_vals.append(capital)

                trades.append(VixTrade(
                    entry_date=entry_date_str,
                    entry_price=round(entry_price, 4),
                    exit_date=dt.strftime("%Y-%m-%d"),
                    exit_price=round(exit_price, 4),
                    exit_reason=exit_reason,
                    hold_days=days_held,
                    pnl_pct=round(pnl_pct, 6),
                ))
                in_position = False

        if not in_position:
            if vix_today > VIX_BUY_THRESHOLD and vix_today > vix_3d_ago:
                entry_price = float(opens[i]) * (1 + TRADE_COST)
                entry_idx = i
                entry_date_str = dt.strftime("%Y-%m-%d")
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


def vix_signal(query_date: str) -> dict:
    vix = load_vix(VIX_JSON)
    price_df = fetch_price(TICKER, years=1)

    dt = pd.Timestamp(query_date)
    if dt not in vix.index:
        idx = vix.index.searchsorted(dt) - 1
        if idx < 0:
            return {}
        vix_today = float(vix.iloc[idx])
    else:
        vix_today = float(vix[dt])

    lookback_dt = dt - timedelta(days=VIX_LOOKBACK_DAYS * 2)
    vix_past_idx = vix.index.searchsorted(lookback_dt)
    vix_past_idx = max(0, min(vix_past_idx, len(vix) - 1))
    vix_3d_ago = float(vix.iloc[vix_past_idx])

    price_dates = price_df.index.strftime("%Y-%m-%d").tolist()

    if vix_today > VIX_BUY_THRESHOLD and vix_today > vix_3d_ago:
        signal = "buy"
        action = "buy 0050.TW"
        if query_date in price_dates:
            i = price_dates.index(query_date)
            entry = round(float(price_df["Open"].iloc[i]) * (1 + TRADE_COST), 4)
        else:
            entry = None
        stop_loss = round(entry * (1 - STOP_LOSS_PCT), 4) if entry else None
    elif vix_today < VIX_SELL_THRESHOLD and vix_today < vix_3d_ago:
        signal = "sell"
        action = "exit 0050.TW"
        entry = None
        stop_loss = None
    else:
        signal = "hold"
        action = "no action"
        entry = None
        stop_loss = None

    return {
        "date": query_date,
        "vix": round(vix_today, 2),
        "vix_3d_ago": round(vix_3d_ago, 2),
        "signal": signal,
        "action": action,
        "entry_price": entry,
        "stop_loss": stop_loss,
    }


def build_performance_chart(
    result: BacktestResult,
    vix: pd.Series,
    output_path: Optional[str] = None,
) -> go.Figure:
    fig = make_subplots(
        rows=3,
        cols=1,
        subplot_titles=["VIX History", "0050 Equity Curve", "Exit Reason Distribution"],
        vertical_spacing=0.12,
        row_heights=[0.3, 0.4, 0.3],
    )

    vix_plot = vix[vix.index >= result.equity_curve.index[0]] if len(result.equity_curve) > 0 else vix
    fig.add_trace(
        go.Scatter(
            x=vix_plot.index,
            y=vix_plot.values,
            mode="lines",
            name="VIX",
            line=dict(color="#9C27B0"),
        ),
        row=1,
        col=1,
    )
    fig.add_hline(y=VIX_BUY_THRESHOLD, line_dash="dash", line_color="#F44336", annotation_text="Buy >25", row=1, col=1)
    fig.add_hline(y=VIX_SELL_THRESHOLD, line_dash="dash", line_color="#4CAF50", annotation_text="Sell <18", row=1, col=1)

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
        colors = {"stop": "#F44336", "vix_exit": "#4CAF50", "max_hold": "#FF9800"}
        bar_colors = [colors.get(r, "#9E9E9E") for r in reasons]
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
        title="VIX Reversal Strategy — Backtest Performance",
        height=900,
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
    vix = load_vix(VIX_JSON)
    price_df = fetch_price(TICKER, years=years + 1)
    result = run_backtest(price_df, vix)
    fig = build_performance_chart(result, vix, output_path=output_html)

    summary = {
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
    output = run_full_analysis(output_html=str(out_dir / "vix_reversal_performance.html"))
    print("\n=== VIX Reversal Strategy Summary ===")
    for k, v in output["summary"].items():
        print(f"  {k}: {v}")
