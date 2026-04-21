#!/usr/bin/env python3
"""一次性抓取 1h 歷史資料存 SQLite (intraday.db)

標的:
  00631L.TW / 0050.TW  -- 台股 ETF
  ^VIX                 -- 恐慌指數
  SMH                  -- 半導體 ETF

yfinance 限制: interval=1h 最多可回溯 ~730 天
首跑預期每檔拿到 ~3600 根 bar (若 period="max" 仍會被截到 730d)

後續增量: 使用 incremental_fetch(ticker) 僅抓最後一筆之後的資料
"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

import yfinance as yf
import pandas as pd

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "intraday.db"

TICKERS = ["00631L.TW", "0050.TW", "^VIX", "SMH"]


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hourly_bars (
            ticker   TEXT    NOT NULL,
            datetime TEXT    NOT NULL,   -- ISO8601 UTC
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
        "CREATE INDEX IF NOT EXISTS idx_hourly_ticker_dt ON hourly_bars(ticker, datetime)"
    )
    conn.commit()


def to_utc_iso(ts) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_one(ticker: str, period: str = "730d") -> pd.DataFrame:
    df = yf.download(
        ticker,
        period=period,
        interval="1h",
        auto_adjust=False,
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


def upsert(conn: sqlite3.Connection, ticker: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    rows = [
        (ticker, r.datetime, float(r.open) if pd.notna(r.open) else None,
         float(r.high) if pd.notna(r.high) else None,
         float(r.low) if pd.notna(r.low) else None,
         float(r.close) if pd.notna(r.close) else None,
         int(r.volume) if pd.notna(r.volume) else None)
        for r in df.itertuples(index=False)
    ]
    cur = conn.executemany(
        """INSERT OR REPLACE INTO hourly_bars
           (ticker, datetime, open, high, low, close, volume)
           VALUES (?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    return cur.rowcount


def last_datetime(conn: sqlite3.Connection, ticker: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(datetime) FROM hourly_bars WHERE ticker=?", (ticker,)
    ).fetchone()
    return row[0] if row and row[0] else None


def report(conn: sqlite3.Connection) -> None:
    print("-" * 60)
    print(f"{'ticker':12s}  {'bars':>7s}  {'first':20s}  {'last':20s}")
    print("-" * 60)
    for t in TICKERS:
        row = conn.execute(
            "SELECT COUNT(*), MIN(datetime), MAX(datetime) FROM hourly_bars WHERE ticker=?",
            (t,),
        ).fetchone()
        n, first, last = row if row else (0, "", "")
        print(f"{t:12s}  {n:>7d}  {str(first or ''):20s}  {str(last or ''):20s}")


def main(tickers: list[str] | None = None, period: str = "730d") -> None:
    tickers = tickers or TICKERS
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    print(f"DB: {DB_PATH}")
    print(f"fetch interval=1h period={period}")
    for t in tickers:
        print(f"\n[{t}] downloading...")
        try:
            df = fetch_one(t, period=period)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue
        n = upsert(conn, t, df)
        print(f"  upsert {n} bars (downloaded {len(df)})")
    report(conn)
    conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args:
        main(tickers=args)
    else:
        main()
