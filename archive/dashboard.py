import sys
from pathlib import Path
from datetime import date, datetime
from typing import Any

import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
import dash
from dash import dcc, html, Input, Output, State, dash_table
import dash_bootstrap_components as dbc

sys.path.insert(0, str(Path(__file__).parent))

try:
    from strategies.gap_fill import gap_signal
except Exception:
    def gap_signal(ticker: str, dt: Any) -> dict: return {}

try:
    from strategies.climax_volume import climax_signal
except Exception:
    def climax_signal(ticker: str, dt: Any) -> dict: return {}

try:
    from strategies.open_fade import open_fade_signal
except Exception:
    def open_fade_signal(ticker: str, dt: Any) -> dict: return {}

try:
    from strategies.closing_trend import closing_trend_signal
except Exception:
    def closing_trend_signal(ticker: str, dt: Any) -> dict: return {}

try:
    from strategies.volume_squeeze import squeeze_signal
except Exception:
    def squeeze_signal(ticker: str, dt: Any) -> dict: return {}

try:
    from strategies.vix_reversal import vix_signal
except Exception:
    def vix_signal(dt: Any) -> dict: return {}

try:
    from strategies.sox_lead import sox_signal
except Exception:
    def sox_signal(dt: Any) -> dict: return {}

try:
    from strategies.margin_divergence import margin_signal
except Exception:
    def margin_signal(ticker: str, dt: Any) -> dict: return {}

TICKERS = ["0050.TW", "00631L.TW", "00632R.TW"]

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.DARKLY],
    title="台股 ETF 訊號 Dashboard",
)

app.layout = dbc.Container(
    fluid=True,
    children=[
        dbc.Row(
            dbc.Col(html.H2("台股 ETF 訊號 Dashboard", className="text-center my-3")),
        ),
        dbc.Row([
            dbc.Col([
                dbc.Label("標的"),
                dcc.Dropdown(
                    id="ticker-select",
                    options=[{"label": t, "value": t} for t in TICKERS],
                    value="0050.TW",
                    clearable=False,
                    style={"color": "#000"},
                ),
            ], width=3),
            dbc.Col([
                dbc.Label("日期"),
                dcc.DatePickerSingle(
                    id="date-select",
                    date=date.today().isoformat(),
                    display_format="YYYY-MM-DD",
                    style={"color": "#000"},
                ),
            ], width=3),
            dbc.Col([
                dbc.Label(" "),
                html.Div(
                    dbc.Button("刷新訊號", id="refresh-btn", color="primary", className="d-block"),
                ),
            ], width=2),
        ], className="mb-3"),

        dbc.Row([
            dbc.Col([
                html.H5("今日訊號總覽"),
                html.Div(id="signal-table"),
            ], width=12),
        ], className="mb-3"),

        dbc.Row([
            dbc.Col([
                html.H5("K 線圖（近 60 日）"),
                dcc.Graph(id="kline-chart", style={"height": "450px"}),
            ], width=12),
        ]),
    ],
)


def _direction_badge(direction: str) -> str:
    if direction in ("long", "buy", "多"):
        return "多"
    if direction in ("short", "sell", "空"):
        return "空"
    return "—"


def _collect_signals(ticker: str, dt: date) -> list[dict]:
    dt_str = dt.isoformat()
    rows = []

    def _row(name: str, sig: dict) -> dict:
        direction = _direction_badge(sig.get("direction", ""))
        return {
            "策略": name,
            "方向": direction,
            "進場價": sig.get("entry_price", "—"),
            "目標價": sig.get("target", "—"),
            "停損價": sig.get("stop_loss", "—"),
            "說明": sig.get("note", sig.get("grade", "")),
        }

    for name, fn in [
        ("跳空回補", lambda: gap_signal(ticker, dt_str)),
        ("爆量反轉", lambda: climax_signal(ticker, dt_str)),
        ("開盤假方向", lambda: open_fade_signal(ticker, dt_str)),
        ("尾盤順趨勢", lambda: closing_trend_signal(ticker, dt_str)),
        ("量縮突破", lambda: squeeze_signal(ticker, dt_str)),
    ]:
        try:
            sig = fn()
            rows.append(_row(name, sig if sig else {}))
        except Exception:
            rows.append({"策略": name, "方向": "—", "進場價": "—", "目標價": "—", "停損價": "—", "說明": "error"})

    for name, fn in [
        ("VIX 反向", lambda: vix_signal(dt_str)),
        ("SOX 領先", lambda: sox_signal(dt_str)),
    ]:
        try:
            sig = fn()
            rows.append(_row(name, sig if sig else {}))
        except Exception:
            rows.append({"策略": name, "方向": "—", "進場價": "—", "目標價": "—", "停損價": "—", "說明": "error"})

    try:
        sig = margin_signal(ticker, dt_str)
        rows.append(_row("融資背離", sig if sig else {}))
    except Exception:
        rows.append({"策略": "融資背離", "方向": "—", "進場價": "—", "目標價": "—", "停損價": "—", "說明": "error"})

    return rows


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v) if v is not None else "—"


@app.callback(
    Output("signal-table", "children"),
    Output("kline-chart", "figure"),
    Input("refresh-btn", "n_clicks"),
    State("ticker-select", "value"),
    State("date-select", "date"),
    prevent_initial_call=False,
)
def update_dashboard(n_clicks: int | None, ticker: str, date_str: str) -> tuple:
    dt = datetime.fromisoformat(date_str).date() if date_str else date.today()
    rows = _collect_signals(ticker, dt)

    for r in rows:
        r["進場價"] = _fmt(r["進場價"])
        r["目標價"] = _fmt(r["目標價"])
        r["停損價"] = _fmt(r["停損價"])

    def row_style(row: dict) -> dict:
        if row["方向"] == "多":
            return {"backgroundColor": "#1a3a1a", "color": "#7fff7f"}
        if row["方向"] == "空":
            return {"backgroundColor": "#3a1a1a", "color": "#ff9999"}
        return {}

    table = dash_table.DataTable(
        data=rows,
        columns=[{"name": c, "id": c} for c in ["策略", "方向", "進場價", "目標價", "停損價", "說明"]],
        style_header={"backgroundColor": "#303030", "color": "white", "fontWeight": "bold"},
        style_cell={"backgroundColor": "#222", "color": "#ccc", "textAlign": "center", "padding": "8px"},
        style_data_conditional=[
            {"if": {"filter_query": '{方向} = "多"'}, "backgroundColor": "#1a3a1a", "color": "#7fff7f"},
            {"if": {"filter_query": '{方向} = "空"'}, "backgroundColor": "#3a1a1a", "color": "#ff9999"},
        ],
    )

    df = yf.download(ticker, period="60d", auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    fig = go.Figure()
    if not df.empty:
        fig.add_trace(go.Candlestick(
            x=df.index,
            open=df["Open"],
            high=df["High"],
            low=df["Low"],
            close=df["Close"],
            name=ticker,
            increasing_line_color="#7fff7f",
            decreasing_line_color="#ff6666",
        ))
        for r in rows:
            if r["方向"] == "多" and r["進場價"] != "—":
                try:
                    fig.add_vline(x=dt.isoformat(), line_color="#7fff7f", line_dash="dash", opacity=0.5)
                except Exception:
                    pass
                break
            if r["方向"] == "空" and r["進場價"] != "—":
                try:
                    fig.add_vline(x=dt.isoformat(), line_color="#ff6666", line_dash="dash", opacity=0.5)
                except Exception:
                    pass
                break

    fig.update_layout(
        paper_bgcolor="#1e1e1e",
        plot_bgcolor="#1e1e1e",
        font_color="#cccccc",
        xaxis_rangeslider_visible=False,
        margin={"t": 20, "b": 20},
    )

    return table, fig


if __name__ == "__main__":
    app.run(debug=True, port=8050)
