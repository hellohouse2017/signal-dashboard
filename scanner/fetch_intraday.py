#!/usr/bin/env python3
"""統一 intraday 資料抓取器 (1h / 5m / 1m) → intraday.db

用法:
  python3 fetch_intraday.py init           # 首次全量 (各週期抓 max 可得範圍)
  python3 fetch_intraday.py daily          # 每日增量 (cron 用)
  python3 fetch_intraday.py init 00631L.TW # 指定 ticker 重抓
  python3 fetch_intraday.py daily 1h       # 指定 interval

yfinance 歷史窗口限制:
  interval=1h : ~730 天
  interval=5m : 60 天
  interval=1m : 7 天

表設計:
  hourly_bars (1h), min5_bars (5m), min1_bars (1m)
  schema 相同: ticker, datetime(UTC ISO), open, high, low, close, volume

增量策略 (daily):
  每天以各 interval 允許的 max window 全量抓取 + INSERT OR REPLACE
  避免維護 state；重抓 <10 秒即可完成
"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

import yfinance as yf
import pandas as pd

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "intraday.db"

TICKERS = ["00631L.TW", "0050.TW", "^VIX", "SMH"]

# (interval, table, init_period, daily_period)
INTERVAL_SPECS = [
    ("1h", "hourly_bars", "730d", "7d"),
    ("5m", "min5_bars",   "60d",  "60d"),
    ("1m", "min1_bars",   "7d",   "7d"),
]


def init_db(conn: sqlite3.Connection) -> None:
    for _, table, _, _ in INTERVAL_SPECS:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                ticker   TEXT    NOT NULL,
                datetime TEXT    NOT NULL,
                open     REAL,
                high     REAL,
                low      REAL,
                close    REAL,
                volume   INTEGER,
                PRIMARY KEY (ticker, datetime)
            )
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_ticker_dt ON {table}(ticker, datetime)"
        )
    conn.commit()


def to_utc_iso(ts) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_one(ticker: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(
        ticker,
        period=period,
        interval=interval,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    dt_col = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={
        dt_col: "datetime", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume",
    })
    df["datetime"] = df["datetime"].apply(to_utc_iso)
    return df[["datetime", "open", "high", "low", "close", "volume"]].dropna(subset=["close"])


def upsert(conn: sqlite3.Connection, table: str, ticker: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    rows = [
        (ticker, r.datetime,
         float(r.open) if pd.notna(r.open) else None,
         float(r.high) if pd.notna(r.high) else None,
         float(r.low) if pd.notna(r.low) else None,
         float(r.close) if pd.notna(r.close) else None,
         int(r.volume) if pd.notna(r.volume) else None)
        for r in df.itertuples(index=False)
    ]
    conn.executemany(
        f"""INSERT OR REPLACE INTO {table}
            (ticker, datetime, open, high, low, close, volume)
            VALUES (?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    return len(rows)


def report(conn: sqlite3.Connection, tickers: list[str]) -> None:
    print("\n" + "=" * 78)
    print(f"{'table':12s}  {'ticker':12s}  {'bars':>7s}  {'first':20s}  {'last':20s}")
    print("-" * 78)
    for _, table, _, _ in INTERVAL_SPECS:
        for t in tickers:
            row = conn.execute(
                f"SELECT COUNT(*), MIN(datetime), MAX(datetime) FROM {table} WHERE ticker=?",
                (t,),
            ).fetchone()
            n, first, last = row if row else (0, "", "")
            print(f"{table:12s}  {t:12s}  {n:>7d}  {str(first or ''):20s}  {str(last or ''):20s}")


def run(mode: str, tickers: list[str] | None = None, intervals: list[str] | None = None) -> None:
    assert mode in ("init", "daily")
    tickers = tickers or TICKERS
    specs = [s for s in INTERVAL_SPECS if (intervals is None or s[0] in intervals)]

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    print(f"DB: {DB_PATH}")
    print(f"mode: {mode}  tickers: {tickers}  intervals: {[s[0] for s in specs]}")

    for interval, table, init_p, daily_p in specs:
        period = init_p if mode == "init" else daily_p
        for t in tickers:
            try:
                df = fetch_one(t, interval, period)
            except Exception as e:
                print(f"  [{interval} {t}] ERROR: {e}")
                continue
            n = upsert(conn, table, t, df)
            print(f"  [{interval:<2s} {t:<10s}] period={period:<4s} upsert={n}")

    report(conn, tickers)
    conn.close()


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    mode = args[0]
    if mode not in ("init", "daily"):
        print(f"unknown mode: {mode}")
        sys.exit(2)

    rest = args[1:]
    intervals = [x for x in rest if x in {"1h", "5m", "1m"}]
    tickers = [x for x in rest if x not in {"1h", "5m", "1m"}]
    run(mode,
        tickers=tickers or None,
        intervals=intervals or None)


if __name__ == "__main__":
    main()
